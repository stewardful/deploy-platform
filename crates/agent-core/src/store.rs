use app_model::{AppKey, AppSpec};
use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("state store unavailable or invalid; inspect host permissions and database integrity")]
    Unavailable,
    #[error("another agentctl or recovery pass is using this state directory")]
    Busy,
    #[error("this state directory belongs to a different runtime target")]
    TargetMismatch,
    #[error("configuration changes are not supported yet; existing application was not modified")]
    ConfigurationChange,
    #[error("invalid persisted state")]
    InvalidState,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Pending,
    Pulling,
    Creating,
    Starting,
    Verifying,
    Succeeded,
    Failed,
    Interrupted,
    Stopping,
    Stopped,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DesiredState {
    Running,
    Stopped,
}

#[derive(Debug, Clone, Serialize)]
pub struct OperationRecord {
    pub id: i64,
    pub app: AppKey,
    pub phase: Phase,
    pub error_code: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AppRecord {
    pub app: AppKey,
    pub fingerprint: String,
    pub phase: Phase,
    pub instance_id: Option<String>,
    pub desired: DesiredState,
    pub recovery: RecoveryState,
}

#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecoveryState {
    pub attempts: u32,
    pub next_attempt_ms: u64,
    pub running_since_ms: u64,
    pub error_code: Option<String>,
}

/// Blocking, short local metadata calls. The owner must retain an exclusive node lease
/// for the complete apply. A long-lived async server should offload SQL to a DB worker.
pub trait DeploymentStore {
    /// Atomically persist normalized spec and a new Pending operation. Reject changed specs.
    fn begin(&mut self, spec: &AppSpec, fingerprint: &str) -> Result<i64, StoreError>;
    fn record(
        &mut self,
        operation: i64,
        phase: Phase,
        instance_id: Option<&str>,
        error_code: Option<&str>,
    ) -> Result<(), StoreError>;
    fn applications(&self) -> Result<Vec<AppRecord>, StoreError>;
    fn operations(&self, limit: u16) -> Result<Vec<OperationRecord>, StoreError>;
    fn spec(&self, app: &AppKey) -> Result<Option<AppSpec>, StoreError>;
    /// Persist intent BEFORE stopping/starting. Explicit intent changes reset retry backoff.
    fn set_desired(&mut self, app: &AppKey, desired: DesiredState) -> Result<(), StoreError>;
    fn set_recovery(&mut self, app: &AppKey, recovery: &RecoveryState) -> Result<(), StoreError>;
    /// Record a monitoring observation without creating a deployment attempt/history row.
    fn observe(
        &mut self,
        app: &AppKey,
        phase: Phase,
        instance_id: Option<&str>,
    ) -> Result<(), StoreError>;
}
