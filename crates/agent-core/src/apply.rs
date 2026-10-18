use crate::{DeploymentStore, Phase, StoreError};
use app_model::{AppSpec, ValidationError};
use runtime_api::{ContainerRuntime, ContainerState, ContainerStatus, RuntimeError};
use serde::Serialize;
use std::time::Duration;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ApplyError {
    #[error(transparent)]
    Validation(#[from] ValidationError),
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error(transparent)]
    Runtime(#[from] RuntimeError),
    #[error("existing container configuration does not match; no replacement was attempted")]
    ContainerConflict,
    #[error("container is not running; inspect application logs before retrying")]
    NotRunning,
    #[error("startup observation must be between 100 ms and 30 seconds")]
    Options,
}
impl ApplyError {
    fn code(&self) -> &'static str {
        match self {
            Self::ContainerConflict => "container_conflict",
            Self::NotRunning => "not_running",
            Self::Runtime(_) => "runtime_failure",
            Self::Store(_) => "store_failure",
            Self::Validation(_) => "invalid_configuration",
            Self::Options => "invalid_options",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct ApplyOptions {
    pub startup_observation: Duration,
}
impl Default for ApplyOptions {
    fn default() -> Self {
        Self {
            startup_observation: Duration::from_secs(1),
        }
    }
}
#[derive(Debug, Serialize)]
pub struct ApplyReport {
    pub operation_id: i64,
    pub created: bool,
    pub container: ContainerStatus,
}

pub async fn apply<R: ContainerRuntime + ?Sized, S: DeploymentStore + ?Sized>(
    runtime: &R,
    store: &mut S,
    spec: &AppSpec,
    options: ApplyOptions,
) -> Result<ApplyReport, ApplyError> {
    let fingerprint = spec.fingerprint()?;
    if !(Duration::from_millis(100)..=Duration::from_secs(30))
        .contains(&options.startup_observation)
    {
        return Err(ApplyError::Options);
    }
    let operation = store.begin(spec, &fingerprint)?;
    let result = run(runtime, store, spec, &fingerprint, operation, options).await;
    match result {
        Ok(report) => {
            store.record(
                operation,
                Phase::Succeeded,
                Some(&report.container.id),
                None,
            )?;
            Ok(report)
        }
        Err(error) => {
            store.record(operation, Phase::Failed, None, Some(error.code()))?;
            Err(error)
        }
    }
}

fn check(status: &ContainerStatus, fingerprint: &str) -> Result<(), ApplyError> {
    if status.spec_fingerprint.as_deref() != Some(fingerprint) {
        return Err(ApplyError::ContainerConflict);
    }
    Ok(())
}

async fn run<R: ContainerRuntime + ?Sized, S: DeploymentStore + ?Sized>(
    runtime: &R,
    store: &mut S,
    spec: &AppSpec,
    fingerprint: &str,
    operation: i64,
    options: ApplyOptions,
) -> Result<ApplyReport, ApplyError> {
    let existing = runtime.find(&spec.app).await?;
    let created = existing.is_none();
    let status = match existing {
        Some(status) => status,
        None => {
            store.record(operation, Phase::Pulling, None, None)?;
            runtime.prepare_image(spec).await?;
            store.record(operation, Phase::Creating, None, None)?;
            let id = runtime.create(spec).await?;
            store.record(operation, Phase::Creating, Some(&id), None)?;
            let status = runtime.inspect(&spec.app).await?;
            if status.id != id {
                return Err(ApplyError::ContainerConflict);
            }
            status
        }
    };
    check(&status, fingerprint)?;
    match status.state {
        ContainerState::Created | ContainerState::Exited => {
            store.record(operation, Phase::Starting, Some(&status.id), None)?;
            runtime.start(&spec.app).await?;
        }
        ContainerState::Running => {}
        _ => return Err(ApplyError::NotRunning),
    }
    store.record(operation, Phase::Verifying, Some(&status.id), None)?;
    tokio::time::sleep(options.startup_observation).await;
    let observed = runtime.inspect(&spec.app).await?;
    check(&observed, fingerprint)?;
    if observed.id != status.id {
        return Err(ApplyError::ContainerConflict);
    }
    if observed.state != ContainerState::Running {
        return Err(ApplyError::NotRunning);
    }
    Ok(ApplyReport {
        operation_id: operation,
        created,
        container: observed,
    })
}
