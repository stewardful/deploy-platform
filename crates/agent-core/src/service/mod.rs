//! Owns control ordering; HTTP, CLI and concrete database/runtime backends are outside this crate.
mod store_proxy;
use crate::recovery::{Outcome, RecoveryOptions, SystemClock, reconcile_app, reconcile_once};
use crate::{AppRecord, ApplyOptions, DeploymentStore, DesiredState, Phase, StoreError};
use agent_api::{
    AgentApi, ApiError, Application, Command, ErrorCode, Health, Intent, Job, JobRepository,
    JobResult, Operation, Recovery, Submission,
};
use async_trait::async_trait;
use runtime_api::ContainerRuntime;
use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use store_proxy::StoreProxy;
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

#[derive(Clone, Copy)]
pub struct ServiceOptions {
    pub poll_interval: Duration,
    pub queue_capacity: u32,
    pub recovery: RecoveryOptions,
}
impl Default for ServiceOptions {
    fn default() -> Self {
        Self {
            poll_interval: Duration::from_secs(10),
            queue_capacity: 32,
            recovery: RecoveryOptions::default(),
        }
    }
}
struct Backend<S> {
    runtime: Arc<dyn ContainerRuntime>,
    diagnostics_slots: tokio::sync::Semaphore,
    store: Arc<Mutex<S>>,
    wake: Arc<Notify>,
    shutdown: CancellationToken,
    finished: CancellationToken,
    capacity: u32,
}
pub struct Service<S> {
    backend: Arc<Backend<S>>,
    worker: Option<std::thread::JoinHandle<agent_api::Result<()>>>,
}
impl<S> Drop for Service<S> {
    fn drop(&mut self) {
        self.backend.shutdown.cancel();
    }
}
struct Finish(CancellationToken);
impl Drop for Finish {
    fn drop(&mut self) {
        self.0.cancel();
    }
}

impl<S: DeploymentStore + JobRepository + Send + 'static> Service<S> {
    pub fn start<R: ContainerRuntime + 'static>(
        runtime: R,
        mut store: S,
        options: ServiceOptions,
    ) -> agent_api::Result<Self> {
        options
            .recovery
            .validate()
            .map_err(|_| ApiError::new(ErrorCode::BadRequest, "invalid recovery options"))?;
        if options.poll_interval.is_zero() || !(1..=128).contains(&options.queue_capacity) {
            return Err(ApiError::new(
                ErrorCode::BadRequest,
                "invalid service options",
            ));
        }
        store.recover_jobs()?;
        let runtime: Arc<dyn ContainerRuntime> = Arc::new(runtime);
        let backend = Arc::new(Backend {
            runtime: runtime.clone(),
            diagnostics_slots: tokio::sync::Semaphore::new(2),
            store: Arc::new(Mutex::new(store)),
            wake: Arc::new(Notify::new()),
            shutdown: CancellationToken::new(),
            finished: CancellationToken::new(),
            capacity: options.queue_capacity,
        });
        let worker_backend = backend.clone();
        let worker = std::thread::Builder::new()
            .name("agent-worker".into())
            .spawn(move || {
                let _finish = Finish(worker_backend.finished.clone());
                let executor = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .max_blocking_threads(1)
                    .build()
                    .map_err(|_| ApiError::internal())?;
                executor.block_on(worker_loop(runtime, worker_backend, options))
            })
            .map_err(|_| ApiError::internal())?;
        Ok(Self {
            backend,
            worker: Some(worker),
        })
    }
    pub fn api(&self) -> Arc<dyn AgentApi> {
        self.backend.clone()
    }
    pub async fn finished(&self) {
        self.backend.finished.cancelled().await;
    }
    pub async fn shutdown(mut self) -> agent_api::Result<()> {
        self.backend.shutdown.cancel();
        let worker = self.worker.take().ok_or_else(ApiError::internal)?;
        tokio::task::spawn_blocking(move || worker.join().map_err(|_| ApiError::internal())?)
            .await
            .map_err(|_| ApiError::internal())?
    }
}

impl<S: Send + 'static> Backend<S> {
    async fn access<T: Send + 'static>(
        &self,
        action: impl FnOnce(&mut S) -> agent_api::Result<T> + Send + 'static,
    ) -> agent_api::Result<T> {
        let store = self.store.clone();
        tokio::task::spawn_blocking(move || {
            action(&mut *store.lock().map_err(|_| ApiError::internal())?)
        })
        .await
        .map_err(|_| ApiError::internal())?
    }
    fn accepting(&self) -> bool {
        !self.shutdown.is_cancelled() && !self.finished.is_cancelled()
    }
}

#[async_trait]
impl<S: DeploymentStore + JobRepository + Send + 'static> AgentApi for Backend<S> {
    async fn publication_target(
        &self,
        app: app_model::AppKey,
    ) -> agent_api::Result<Option<std::net::Ipv4Addr>> {
        let spec = self
            .access(move |s| {
                s.spec(&app)
                    .map_err(map_store)?
                    .ok_or_else(|| ApiError::new(ErrorCode::NotFound, "application not found"))
            })
            .await?;
        self.runtime.publication_target(&spec).await.map_err(|_| {
            ApiError::new(
                ErrorCode::Unavailable,
                "owned running project-network target required",
            )
        })
    }

    async fn diagnostics(
        &self,
        app: app_model::AppKey,
    ) -> agent_api::Result<agent_api::Diagnostics> {
        let _slot = self
            .diagnostics_slots
            .try_acquire()
            .map_err(|_| ApiError::new(ErrorCode::QueueFull, "diagnostics busy"))?;
        let spec = self
            .access(move |s| {
                s.spec(&app)
                    .map_err(map_store)?
                    .ok_or_else(|| ApiError::new(ErrorCode::NotFound, "application not found"))
            })
            .await?;
        self.runtime
            .diagnostics(&spec)
            .await
            .map_err(|_| ApiError::new(ErrorCode::Unavailable, "cannot inspect application health"))
    }
    async fn logs(
        &self,
        app: app_model::AppKey,
        tail: u16,
    ) -> agent_api::Result<agent_api::LogSnapshot> {
        if !(1..=500).contains(&tail) {
            return Err(ApiError::new(ErrorCode::BadRequest, "tail must be 1..500"));
        }
        let _slot = self
            .diagnostics_slots
            .try_acquire()
            .map_err(|_| ApiError::new(ErrorCode::QueueFull, "diagnostics busy"))?;
        let key = app.clone();
        self.access(move |s| {
            s.spec(&key)
                .map_err(map_store)?
                .ok_or_else(|| ApiError::new(ErrorCode::NotFound, "application not found"))
        })
        .await?;
        self.runtime.logs(&app, tail).await.map_err(|_| {
            ApiError::new(
                ErrorCode::Unavailable,
                "cannot read logs; try fewer lines or check runtime state",
            )
        })
    }
    async fn health(&self) -> agent_api::Result<Health> {
        Ok(Health {
            api_version: agent_api::API_VERSION.into(),
            service_version: env!("CARGO_PKG_VERSION").into(),
            accepting: self.accepting(),
        })
    }
    async fn applications(&self) -> agent_api::Result<Vec<Application>> {
        self.access(|s| {
            s.applications()
                .map(|apps| apps.into_iter().map(application).collect())
                .map_err(map_store)
        })
        .await
    }
    async fn operations(&self, limit: u16) -> agent_api::Result<Vec<Operation>> {
        if !(1..=1000).contains(&limit) {
            return Err(ApiError::new(
                ErrorCode::BadRequest,
                "limit must be 1..1000",
            ));
        }
        self.access(move |s| {
            s.operations(limit)
                .map(|items| {
                    items
                        .into_iter()
                        .map(|o| Operation {
                            id: o.id,
                            app: o.app,
                            phase: phase(o.phase).into(),
                            error_code: o.error_code,
                        })
                        .collect()
                })
                .map_err(map_store)
        })
        .await
    }
    async fn submit(&self, submission: Submission) -> agent_api::Result<Job> {
        if !self.accepting() {
            return Err(ApiError::new(
                ErrorCode::Unavailable,
                "agent is stopping or worker is unavailable",
            ));
        }
        submission.validate()?;
        let capacity = self.capacity;
        let job = self
            .access(move |s| s.enqueue(&submission, capacity))
            .await?;
        self.wake.notify_one();
        Ok(job)
    }
    async fn job(&self, id: i64) -> agent_api::Result<Job> {
        if id < 1 {
            return Err(ApiError::new(ErrorCode::BadRequest, "invalid job id"));
        }
        self.access(move |s| s.job(id)).await
    }
}

async fn worker_loop<R: ContainerRuntime, S: DeploymentStore + JobRepository + Send + 'static>(
    runtime: R,
    backend: Arc<Backend<S>>,
    options: ServiceOptions,
) -> agent_api::Result<()> {
    let mut store = StoreProxy(backend.store.clone());
    let mut next_recovery = Instant::now();
    // Snapshot the durable backlog, including jobs accepted under an older, larger
    // capacity. Drain it before boot recovery so a queued Stop is not bypassed.
    // New arrivals cannot indefinitely postpone recovery.
    let mut startup_budget = backend
        .store
        .lock()
        .map_err(|_| ApiError::internal())?
        .pending_jobs()?;
    loop {
        let step = async {
            if startup_budget == 0 && Instant::now() >= next_recovery {
                let events = reconcile_once(&runtime, &mut store, &SystemClock, options.recovery)
                    .await
                    .map_err(|_| ApiError::internal())?;
                for event in events {
                    if !matches!(event.outcome, Outcome::Healthy | Outcome::Deferred) {
                        if let Ok(encoded) = serde_json::to_string(&event) {
                            eprintln!("{encoded}");
                        }
                    }
                }
                next_recovery = Instant::now() + options.poll_interval;
            }
            let job = backend
                .store
                .lock()
                .map_err(|_| ApiError::internal())?
                .claim_job()?;
            if let Some(job) = job {
                startup_budget = startup_budget.saturating_sub(1);
                let result = execute(&runtime, &mut store, job.command, options.recovery).await;
                backend
                    .store
                    .lock()
                    .map_err(|_| ApiError::internal())?
                    .finish_job(job.id, result)?;
                return Ok::<_, ApiError>(());
            }
            startup_budget = 0;
            if Instant::now() < next_recovery {
                tokio::select! { _ = backend.wake.notified() => {}, _ = tokio::time::sleep_until(next_recovery.into()) => {} }
            }
            Ok(())
        };
        tokio::select! {
            biased;
            _ = backend.shutdown.cancelled() => return Ok(()),
            result = step => result?,
        }
    }
}

async fn execute<R: ContainerRuntime, S: DeploymentStore>(
    runtime: &R,
    store: &mut S,
    command: Command,
    recovery: RecoveryOptions,
) -> agent_api::Result<JobResult> {
    match command {
        Command::Apply {
            spec,
            startup_wait_ms,
        } => {
            let result = crate::apply(
                runtime,
                store,
                &spec,
                ApplyOptions {
                    startup_observation: Duration::from_millis(startup_wait_ms),
                },
            )
            .await
            .map_err(|error| {
                use crate::ApplyError;
                let code = match &error {
                    ApplyError::Store(StoreError::ConfigurationChange)
                    | ApplyError::ContainerConflict
                    | ApplyError::Runtime(runtime_api::RuntimeError::Ownership) => {
                        ErrorCode::Conflict
                    }
                    ApplyError::Validation(_) | ApplyError::Options => ErrorCode::BadRequest,
                    _ => ErrorCode::Unavailable,
                };
                ApiError::new(code, error.to_string())
            })?;
            Ok(JobResult::Applied {
                operation_id: result.operation_id,
                created: result.created,
                instance_id: result.container.id,
                state: state_name(result.container.state).into(),
            })
        }
        command => {
            let (app, desired, intent) = match command {
                Command::Start { app } => (app, DesiredState::Running, Intent::Running),
                Command::Stop { app } => (app, DesiredState::Stopped, Intent::Stopped),
                _ => unreachable!(),
            };
            if store.spec(&app).map_err(map_store)?.is_none() {
                return Err(ApiError::new(ErrorCode::NotFound, "application not found"));
            }
            store.set_desired(&app, desired).map_err(map_store)?;
            let record = store
                .applications()
                .map_err(map_store)?
                .into_iter()
                .find(|a| a.app == app)
                .ok_or_else(ApiError::internal)?;
            let event = reconcile_app(runtime, store, &record, &SystemClock, recovery)
                .await
                .map_err(|_| ApiError::internal())?;
            if event.outcome == Outcome::Failed {
                return Err(ApiError::new(
                    ErrorCode::Unavailable,
                    "intent saved; runtime action failed and will be retried",
                ));
            }
            Ok(JobResult::Intent {
                app,
                desired: intent,
                outcome: format!("{:?}", event.outcome).to_ascii_lowercase(),
                next_attempt_ms: event.next_attempt_ms,
            })
        }
    }
}
fn map_store(error: StoreError) -> ApiError {
    match error {
        StoreError::ConfigurationChange => ApiError::new(ErrorCode::Conflict, error.to_string()),
        _ => ApiError::internal(),
    }
}
fn phase(phase: Phase) -> &'static str {
    match phase {
        Phase::Pending => "pending",
        Phase::Pulling => "pulling",
        Phase::Creating => "creating",
        Phase::Starting => "starting",
        Phase::Verifying => "verifying",
        Phase::Succeeded => "succeeded",
        Phase::Failed => "failed",
        Phase::Interrupted => "interrupted",
        Phase::Stopping => "stopping",
        Phase::Stopped => "stopped",
    }
}
fn state_name(state: runtime_api::ContainerState) -> &'static str {
    use runtime_api::ContainerState::*;
    match state {
        Created => "created",
        Running => "running",
        Paused => "paused",
        Restarting => "restarting",
        Exited => "exited",
        Unknown => "unknown",
    }
}
fn application(app: AppRecord) -> Application {
    Application {
        app: app.app,
        fingerprint: app.fingerprint,
        phase: phase(app.phase).into(),
        instance_id: app.instance_id,
        desired: match app.desired {
            DesiredState::Running => Intent::Running,
            DesiredState::Stopped => Intent::Stopped,
        },
        recovery: Recovery {
            attempts: app.recovery.attempts,
            next_attempt_ms: app.recovery.next_attempt_ms,
            running_since_ms: app.recovery.running_since_ms,
            error_code: app.recovery.error_code,
        },
    }
}
