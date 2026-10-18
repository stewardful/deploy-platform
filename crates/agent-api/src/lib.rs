//! Agent v1 DTOs and service ports, with optional Unix HTTP client/server features.
mod maintenance;
use app_model::{AppKey, AppSpec};
use async_trait::async_trait;
pub use maintenance::{WriteGate, WritePermit};
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const API_VERSION: &str = "v1";
pub const MAX_REQUEST_BYTES: usize = 256 * 1024;
pub const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
pub type Result<T> = std::result::Result<T, ApiError>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    BadRequest,
    NotFound,
    Conflict,
    QueueFull,
    Unavailable,
    Internal,
}
#[derive(Debug, Clone, Serialize, Deserialize, Error)]
#[error("{message}")]
pub struct ApiError {
    pub code: ErrorCode,
    pub message: String,
    pub retryable: bool,
}
impl ApiError {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            retryable: matches!(code, ErrorCode::QueueFull | ErrorCode::Unavailable),
        }
    }
    pub fn internal() -> Self {
        Self::new(ErrorCode::Internal, "agent state unavailable")
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Health {
    pub api_version: String,
    pub service_version: String,
    /// Worker availability, including draining accepted jobs. Admission is in Maintenance.
    pub accepting: bool,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Intent {
    Running,
    Stopped,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Recovery {
    pub attempts: u32,
    pub next_attempt_ms: u64,
    pub running_since_ms: u64,
    pub error_code: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Application {
    pub app: AppKey,
    pub fingerprint: String,
    pub phase: String,
    pub instance_id: Option<String>,
    pub desired: Intent,
    pub recovery: Recovery,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Operation {
    pub id: i64,
    pub app: AppKey,
    pub phase: String,
    pub error_code: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Apply {
        spec: Box<AppSpec>,
        startup_wait_ms: u64,
    },
    Start {
        app: AppKey,
    },
    Stop {
        app: AppKey,
    },
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Submission {
    pub request_id: String,
    pub command: Command,
}
impl Submission {
    pub fn validate(&self) -> Result<()> {
        if self.request_id.is_empty()
            || self.request_id.len() > 128
            || !self
                .request_id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        {
            return Err(ApiError::new(ErrorCode::BadRequest, "invalid request_id"));
        }
        if let Command::Apply {
            spec,
            startup_wait_ms,
        } = &self.command
        {
            spec.validate().map_err(|_| {
                ApiError::new(ErrorCode::BadRequest, "invalid application configuration")
            })?;
            if !(100..=30000).contains(startup_wait_ms) {
                return Err(ApiError::new(
                    ErrorCode::BadRequest,
                    "startup_wait_ms must be 100..30000",
                ));
            }
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JobState {
    Queued,
    Running,
    Succeeded,
    Failed,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum JobResult {
    Applied {
        operation_id: i64,
        created: bool,
        instance_id: String,
        state: String,
    },
    Intent {
        app: AppKey,
        desired: Intent,
        outcome: String,
        next_attempt_ms: u64,
    },
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Job {
    pub id: i64,
    pub request_id: String,
    pub state: JobState,
    pub result: Option<JobResult>,
    pub error: Option<ApiError>,
}
impl Job {
    pub fn terminal(&self) -> bool {
        matches!(self.state, JobState::Succeeded | JobState::Failed)
    }
}
/// Write-only secret upload: never put this object into the durable job queue.
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SecretInput {
    pub project: app_model::Identifier,
    pub name: app_model::Identifier,
    pub value: String,
}
impl std::fmt::Debug for SecretInput {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SecretInput")
            .field("project", &self.project)
            .field("name", &self.name)
            .finish_non_exhaustive()
    }
}
impl SecretInput {
    pub fn validate(&self) -> Result<()> {
        if self.value.is_empty() || self.value.len() > 65536 || self.value.contains('\0') {
            return Err(ApiError::new(
                ErrorCode::BadRequest,
                "secret must contain 1..65536 UTF-8 bytes without NUL",
            ));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SecretInfo {
    pub project: app_model::Identifier,
    pub name: app_model::Identifier,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VolumeInfo {
    pub name: app_model::Identifier,
    pub target: String,
    pub uid: u32,
    pub gid: u32,
}

/// Private durable queue item. Never serialize command bodies into public job responses.
pub struct QueuedJob {
    pub id: i64,
    pub command: Command,
}

/// Durable-job port; a backend must deduplicate identical requests and reject key reuse
/// with different commands. An accepted submission must survive process restart.
pub trait JobRepository {
    fn enqueue(&mut self, submission: &Submission, capacity: u32) -> Result<Job>;
    fn job(&self, id: i64) -> Result<Job>;
    fn claim_job(&mut self) -> Result<Option<QueuedJob>>;
    fn finish_job(&mut self, id: i64, result: Result<JobResult>) -> Result<()>;
    fn recover_jobs(&mut self) -> Result<()>;
    fn pending_jobs(&self) -> Result<u32>;
}

#[async_trait]
pub trait AgentApi: Send + Sync {
    /// Composition-only admission port. Move this permit into any detached write.
    fn acquire_write(&self) -> Result<WritePermit> {
        Ok(WritePermit::untracked())
    }
    /// None queries status; true drains; false resumes. Not exposed by the Go API.
    async fn maintenance(&self, _draining: Option<bool>) -> Result<Maintenance> {
        Err(ApiError::new(
            ErrorCode::Unavailable,
            "maintenance is not supported",
        ))
    }
    async fn routes(&self) -> Result<ingress_model::IngressSnapshot> {
        Err(ApiError::new(
            ErrorCode::Unavailable,
            "ingress not configured",
        ))
    }
    async fn change_route(
        &self,
        _change: ingress_model::RouteChange,
    ) -> Result<ingress_model::RouteReceipt> {
        Err(ApiError::new(
            ErrorCode::Unavailable,
            "ingress not configured",
        ))
    }
    async fn delete_route(
        &self,
        _domain: ingress_model::DomainName,
        _revision: i64,
    ) -> Result<ingress_model::RouteReceipt> {
        Err(ApiError::new(
            ErrorCode::Unavailable,
            "ingress not configured",
        ))
    }
    /// Internal composition port, deliberately not exposed as an HTTP endpoint.
    async fn publication_target(&self, _app: AppKey) -> Result<Option<std::net::Ipv4Addr>> {
        Err(ApiError::new(
            ErrorCode::Unavailable,
            "publication target unavailable",
        ))
    }

    async fn diagnostics(&self, _app: AppKey) -> Result<Diagnostics> {
        Err(ApiError::new(
            ErrorCode::Unavailable,
            "diagnostics not configured",
        ))
    }
    async fn logs(&self, _app: AppKey, _tail: u16) -> Result<LogSnapshot> {
        Err(ApiError::new(ErrorCode::Unavailable, "logs not configured"))
    }
    async fn create_secret(&self, _input: SecretInput) -> Result<SecretInfo> {
        Err(ApiError::new(
            ErrorCode::Unavailable,
            "resource service is not configured",
        ))
    }
    async fn secrets(&self, _project: app_model::Identifier) -> Result<Vec<SecretInfo>> {
        Err(ApiError::new(
            ErrorCode::Unavailable,
            "resource service is not configured",
        ))
    }
    async fn volumes(&self, _app: AppKey) -> Result<Vec<VolumeInfo>> {
        Err(ApiError::new(
            ErrorCode::Unavailable,
            "resource service is not configured",
        ))
    }
    async fn health(&self) -> Result<Health>;
    async fn applications(&self) -> Result<Vec<Application>>;
    async fn operations(&self, limit: u16) -> Result<Vec<Operation>>;
    async fn submit(&self, submission: Submission) -> Result<Job>;
    async fn job(&self, id: i64) -> Result<Job>;
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Maintenance {
    pub draining: bool,
    pub pending_jobs: u32,
    pub active_writers: u32,
    pub quiescent: bool,
}

pub use runtime_api::{Diagnostics, LogSnapshot};

#[cfg(all(unix, feature = "client"))]
pub mod client;
#[cfg(all(unix, feature = "server"))]
pub mod server;
