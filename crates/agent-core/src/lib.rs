//! Persist intent before mutations, reconcile before retries. No specific storage/runtime backend.
mod apply;
mod store;
pub use apply::{ApplyError, ApplyOptions, ApplyReport, apply};
pub use store::{
    AppRecord, DeploymentStore, DesiredState, OperationRecord, Phase, RecoveryState, StoreError,
};

pub mod recovery;
pub mod service;
