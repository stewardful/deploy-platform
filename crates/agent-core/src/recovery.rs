//! Single-pass recovery. Scheduling, process lifetime and database backend belong to the host.
use crate::{
    AppRecord, ApplyError, ApplyOptions, DeploymentStore, DesiredState, Phase, RecoveryState,
    StoreError,
};
use app_model::AppKey;
use runtime_api::{ContainerRuntime, ContainerState, ContainerStatus, RuntimeError};
use serde::Serialize;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use thiserror::Error;

pub trait Clock {
    fn now_ms(&self) -> u64;
}
pub struct SystemClock;
impl Clock for SystemClock {
    fn now_ms(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .min(i64::MAX as u128) as u64
    }
}

#[derive(Debug, Clone, Copy)]
pub struct RecoveryOptions {
    pub retry_base: Duration,
    pub retry_max: Duration,
    pub stable_window: Duration,
    pub apply: ApplyOptions,
}
impl Default for RecoveryOptions {
    fn default() -> Self {
        Self {
            retry_base: Duration::from_secs(5),
            retry_max: Duration::from_secs(300),
            stable_window: Duration::from_secs(60),
            apply: ApplyOptions::default(),
        }
    }
}
impl RecoveryOptions {
    pub fn validate(&self) -> Result<(), RecoveryError> {
        if self.retry_base < Duration::from_millis(100)
            || self.retry_max < self.retry_base
            || self.retry_max > Duration::from_secs(86400)
            || self.stable_window.is_zero()
            || !(Duration::from_millis(100)..=Duration::from_secs(30))
                .contains(&self.apply.startup_observation)
        {
            return Err(RecoveryError::Options);
        }
        Ok(())
    }
    fn delay_ms(&self, attempts: u32) -> u64 {
        let multiplier = 1_u128 << attempts.saturating_sub(1).min(31);
        (self.retry_base.as_millis() * multiplier).min(self.retry_max.as_millis()) as u64
    }
}

#[derive(Debug, Error)]
pub enum RecoveryError {
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error(transparent)]
    Runtime(#[from] RuntimeError),
    #[error(transparent)]
    Apply(#[from] ApplyError),
    #[error("invalid recovery options")]
    Options,
    #[error("persisted configuration or runtime ownership does not match")]
    Conflict,
    #[error("application is not in the required runtime state")]
    State,
}
impl RecoveryError {
    fn code(&self) -> &'static str {
        match self {
            Self::Conflict | Self::Runtime(RuntimeError::Ownership) => "recovery_conflict",
            Self::Runtime(_) => "runtime_unavailable",
            Self::Apply(_) => "restore_failed",
            Self::Store(_) => "state_unavailable",
            Self::Options => "invalid_options",
            Self::State => "unexpected_state",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Healthy,
    Restored,
    Stopped,
    Deferred,
    Failed,
}
#[derive(Debug, Serialize)]
pub struct RecoveryEvent {
    pub app: AppKey,
    pub outcome: Outcome,
    pub error_code: Option<String>,
    pub next_attempt_ms: u64,
}

pub async fn reconcile_once<
    R: ContainerRuntime + ?Sized,
    S: DeploymentStore + ?Sized,
    C: Clock + ?Sized,
>(
    runtime: &R,
    store: &mut S,
    clock: &C,
    options: RecoveryOptions,
) -> Result<Vec<RecoveryEvent>, RecoveryError> {
    options.validate()?;
    let mut events = Vec::new();
    for app in store.applications()? {
        events.push(reconcile_app(runtime, store, &app, clock, options).await?);
    }
    Ok(events)
}

/// Caller retains the exclusive store lease. Failure of one application does not block others.
pub async fn reconcile_app<
    R: ContainerRuntime + ?Sized,
    S: DeploymentStore + ?Sized,
    C: Clock + ?Sized,
>(
    runtime: &R,
    store: &mut S,
    app: &AppRecord,
    clock: &C,
    options: RecoveryOptions,
) -> Result<RecoveryEvent, RecoveryError> {
    options.validate()?;
    let now = clock.now_ms();
    // Bound stale deadlines after a backwards wall-clock adjustment.
    let remaining = app.recovery.next_attempt_ms.saturating_sub(now);
    if remaining > 0 && remaining <= options.retry_max.as_millis() as u64 {
        return Ok(event(
            app,
            Outcome::Deferred,
            app.recovery.error_code.clone(),
            app.recovery.next_attempt_ms,
        ));
    }
    let result = converge(runtime, store, app, options).await;
    let now = clock.now_ms();
    match result {
        Ok((outcome, status)) => {
            let instance = status.as_ref().map(|s| s.id.as_str());
            if app.desired == DesiredState::Stopped {
                store.observe(&app.app, Phase::Stopped, instance)?;
                if app.recovery != RecoveryState::default() {
                    store.set_recovery(&app.app, &RecoveryState::default())?;
                }
                return Ok(event(app, outcome, None, 0));
            }
            store.observe(&app.app, Phase::Succeeded, instance)?;
            let mut recovery = app.recovery.clone();
            if outcome == Outcome::Restored {
                recovery.attempts = recovery.attempts.saturating_add(1);
                recovery.next_attempt_ms = now.saturating_add(options.delay_ms(recovery.attempts));
                recovery.running_since_ms = now;
            } else {
                if recovery.running_since_ms == 0 || recovery.running_since_ms > now {
                    recovery.running_since_ms = now;
                }
                if now.saturating_sub(recovery.running_since_ms)
                    >= options.stable_window.as_millis().min(u64::MAX as u128) as u64
                {
                    recovery.attempts = 0;
                    recovery.next_attempt_ms = 0;
                }
            }
            recovery.error_code = None;
            if recovery != app.recovery {
                store.set_recovery(&app.app, &recovery)?;
            }
            Ok(event(app, outcome, None, recovery.next_attempt_ms))
        }
        Err(error) => {
            let attempts = app.recovery.attempts.saturating_add(1);
            let next = now.saturating_add(options.delay_ms(attempts));
            let code = error.code().to_owned();
            store.set_recovery(
                &app.app,
                &RecoveryState {
                    attempts,
                    next_attempt_ms: next,
                    running_since_ms: 0,
                    error_code: Some(code.clone()),
                },
            )?;
            store.observe(&app.app, Phase::Failed, app.instance_id.as_deref())?;
            Ok(event(app, Outcome::Failed, Some(code), next))
        }
    }
}

fn event(
    app: &AppRecord,
    outcome: Outcome,
    error_code: Option<String>,
    next_attempt_ms: u64,
) -> RecoveryEvent {
    RecoveryEvent {
        app: app.app.clone(),
        outcome,
        error_code,
        next_attempt_ms,
    }
}
fn verify(status: &ContainerStatus, app: &AppRecord) -> Result<(), RecoveryError> {
    if status.app != app.app || status.spec_fingerprint.as_deref() != Some(&app.fingerprint) {
        return Err(RecoveryError::Conflict);
    }
    Ok(())
}
async fn converge<R: ContainerRuntime + ?Sized, S: DeploymentStore + ?Sized>(
    runtime: &R,
    store: &mut S,
    app: &AppRecord,
    options: RecoveryOptions,
) -> Result<(Outcome, Option<ContainerStatus>), RecoveryError> {
    let spec = store.spec(&app.app)?.ok_or(RecoveryError::Conflict)?;
    if spec.fingerprint().map_err(|_| RecoveryError::Conflict)? != app.fingerprint {
        return Err(RecoveryError::Conflict);
    }
    let actual = runtime.find(&app.app).await?;
    if let Some(ref actual) = actual {
        verify(actual, app)?;
    }
    match app.desired {
        DesiredState::Stopped => {
            if let Some(status) = actual {
                match status.state {
                    ContainerState::Created | ContainerState::Exited => {
                        Ok((Outcome::Healthy, Some(status)))
                    }
                    ContainerState::Running => {
                        store.observe(&app.app, Phase::Stopping, Some(&status.id))?;
                        runtime.stop(&app.app).await?;
                        let stopped = runtime.inspect(&app.app).await?;
                        verify(&stopped, app)?;
                        if stopped.id != status.id
                            || !matches!(
                                stopped.state,
                                ContainerState::Exited | ContainerState::Created
                            )
                        {
                            return Err(RecoveryError::State);
                        }
                        Ok((Outcome::Stopped, Some(stopped)))
                    }
                    _ => Err(RecoveryError::State),
                }
            } else {
                Ok((Outcome::Healthy, None))
            }
        }
        DesiredState::Running => {
            if let Some(ref status) = actual {
                if status.state == ContainerState::Running {
                    return Ok((Outcome::Healthy, actual));
                }
                if !matches!(
                    status.state,
                    ContainerState::Created | ContainerState::Exited
                ) {
                    return Err(RecoveryError::State);
                }
            }
            let report = crate::apply(runtime, store, &spec, options.apply).await?;
            Ok((Outcome::Restored, Some(report.container)))
        }
    }
}
