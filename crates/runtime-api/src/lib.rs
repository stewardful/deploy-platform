//! Runtime contracts, without CLI, filesystem or database implementation details.
use app_model::{AppKey, AppSpec, ImageRef, ValidationError};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub type Result<T> = std::result::Result<T, RuntimeError>;

#[derive(Debug, Error)]
pub enum RuntimeError {
    #[error(transparent)]
    Validation(#[from] ValidationError),
    #[error("invalid runtime configuration: {0}")]
    Configuration(&'static str),
    #[error("runtime operation {operation} failed: {kind}")]
    Execution {
        operation: &'static str,
        kind: ExecutionFailure,
    },
    #[error("runtime returned an invalid response for {0}")]
    Protocol(&'static str),
    #[error("resource is not owned by this application")]
    Ownership,
}

#[derive(Debug, Clone, Copy, Error, PartialEq, Eq)]
pub enum ExecutionFailure {
    #[error("could not execute command")]
    Io,
    #[error("deadline exceeded; reconcile actual state before retrying")]
    Timeout,
    #[error("output limit exceeded; reconcile actual state before retrying")]
    OutputLimit,
    #[error("command exited unsuccessfully")]
    Exit,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ContainerState {
    Created,
    Running,
    Paused,
    Restarting,
    Exited,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ContainerStatus {
    pub id: String,
    pub app: AppKey,
    pub state: ContainerState,
    pub exit_code: Option<i32>,
    /// Configuration label assigned at create time. None when the runtime does not expose the label.
    pub spec_fingerprint: Option<String>,
}

/// One instance per AppKey in v1. Mutations require node-level serialization by the caller.
/// No implicit retries: timeouts may leave successful or partially completed operations.
#[async_trait]
pub trait ContainerRuntime: Send + Sync {
    async fn publication_target(&self, _spec: &AppSpec) -> Result<Option<std::net::Ipv4Addr>> {
        Err(RuntimeError::Configuration("publication not supported"))
    }

    async fn prepare_image(&self, spec: &AppSpec) -> Result<()> {
        if spec.pull_policy != app_model::PullPolicy::Always {
            return Err(RuntimeError::Configuration("pull policy not supported"));
        }
        self.pull(&spec.image).await
    }
    async fn diagnostics(&self, _spec: &AppSpec) -> Result<Diagnostics> {
        Err(RuntimeError::Configuration("diagnostics not supported"))
    }
    async fn logs(&self, _app: &AppKey, _tail: u16) -> Result<LogSnapshot> {
        Err(RuntimeError::Configuration("logs not supported"))
    }
    async fn version(&self) -> Result<String>;
    async fn pull(&self, image: &ImageRef) -> Result<()>;
    async fn create(&self, spec: &AppSpec) -> Result<String>;
    async fn inspect(&self, app: &AppKey) -> Result<ContainerStatus>;
    /// Only returns None after a successful inventory query confirms absence.
    async fn find(&self, app: &AppKey) -> Result<Option<ContainerStatus>>;
    async fn start(&self, app: &AppKey) -> Result<()>;
    async fn stop(&self, app: &AppKey) -> Result<()>;
    /// Never removes volumes; never forces removal of a running container.
    async fn remove(&self, app: &AppKey) -> Result<()>;
}

/// Prepared node resources. Values never contain secret contents.
#[derive(Debug)]
pub struct RuntimeMount {
    pub source: std::path::PathBuf,
    pub target: String,
    pub read_only: bool,
}
/// Optional host adapter; no filesystem dependency in the runtime contract.
pub trait ResourceProvider: Send + Sync {
    fn prepare(&self, spec: &AppSpec) -> Result<Vec<RuntimeMount>>;
}

/// A point-in-time probe, distinct from process state and deployment task success.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Diagnostics {
    pub state: ContainerState,
    pub network: Option<String>,
    pub service_name: Option<String>,
    pub health: String,
    pub http_status: Option<u16>,
    pub checked_at_ms: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogSnapshot {
    pub text: String,
    pub tail: u16,
}

#[async_trait]
impl<T: ContainerRuntime + ?Sized> ContainerRuntime for std::sync::Arc<T> {
    async fn publication_target(&self, spec: &AppSpec) -> Result<Option<std::net::Ipv4Addr>> {
        (**self).publication_target(spec).await
    }
    async fn prepare_image(&self, spec: &AppSpec) -> Result<()> {
        (**self).prepare_image(spec).await
    }
    async fn version(&self) -> Result<String> {
        (**self).version().await
    }
    async fn pull(&self, image: &ImageRef) -> Result<()> {
        (**self).pull(image).await
    }
    async fn create(&self, spec: &AppSpec) -> Result<String> {
        (**self).create(spec).await
    }
    async fn inspect(&self, app: &AppKey) -> Result<ContainerStatus> {
        (**self).inspect(app).await
    }
    async fn find(&self, app: &AppKey) -> Result<Option<ContainerStatus>> {
        (**self).find(app).await
    }
    async fn start(&self, app: &AppKey) -> Result<()> {
        (**self).start(app).await
    }
    async fn stop(&self, app: &AppKey) -> Result<()> {
        (**self).stop(app).await
    }
    async fn remove(&self, app: &AppKey) -> Result<()> {
        (**self).remove(app).await
    }
    async fn diagnostics(&self, spec: &AppSpec) -> Result<Diagnostics> {
        (**self).diagnostics(spec).await
    }
    async fn logs(&self, app: &AppKey, tail: u16) -> Result<LogSnapshot> {
        (**self).logs(app, tail).await
    }
}
