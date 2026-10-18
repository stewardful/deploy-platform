use crate::{
    AppRecord, DeploymentStore, DesiredState, OperationRecord, Phase, RecoveryState, StoreError,
};
use app_model::{AppKey, AppSpec};
use std::sync::{Arc, Mutex};
/// Every lock ends at the metadata method boundary, never across a runtime await.
pub(crate) struct StoreProxy<S>(pub Arc<Mutex<S>>);
impl<S: DeploymentStore> DeploymentStore for StoreProxy<S> {
    fn begin(&mut self, spec: &AppSpec, hash: &str) -> Result<i64, StoreError> {
        self.0
            .lock()
            .map_err(|_| StoreError::Unavailable)?
            .begin(spec, hash)
    }
    fn record(
        &mut self,
        id: i64,
        phase: Phase,
        instance: Option<&str>,
        error: Option<&str>,
    ) -> Result<(), StoreError> {
        self.0
            .lock()
            .map_err(|_| StoreError::Unavailable)?
            .record(id, phase, instance, error)
    }
    fn applications(&self) -> Result<Vec<AppRecord>, StoreError> {
        self.0
            .lock()
            .map_err(|_| StoreError::Unavailable)?
            .applications()
    }
    fn operations(&self, limit: u16) -> Result<Vec<OperationRecord>, StoreError> {
        self.0
            .lock()
            .map_err(|_| StoreError::Unavailable)?
            .operations(limit)
    }
    fn spec(&self, app: &AppKey) -> Result<Option<AppSpec>, StoreError> {
        self.0
            .lock()
            .map_err(|_| StoreError::Unavailable)?
            .spec(app)
    }
    fn set_desired(&mut self, app: &AppKey, desired: DesiredState) -> Result<(), StoreError> {
        self.0
            .lock()
            .map_err(|_| StoreError::Unavailable)?
            .set_desired(app, desired)
    }
    fn set_recovery(&mut self, app: &AppKey, state: &RecoveryState) -> Result<(), StoreError> {
        self.0
            .lock()
            .map_err(|_| StoreError::Unavailable)?
            .set_recovery(app, state)
    }
    fn observe(&mut self, app: &AppKey, phase: Phase, id: Option<&str>) -> Result<(), StoreError> {
        self.0
            .lock()
            .map_err(|_| StoreError::Unavailable)?
            .observe(app, phase, id)
    }
}
