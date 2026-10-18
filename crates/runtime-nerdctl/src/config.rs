use runtime_api::{Result, RuntimeError};
use std::{path::PathBuf, time::Duration};

/// Trusted host configuration, never filled directly from an application's request.
#[derive(Debug, Clone)]
pub struct NerdctlConfig {
    pub binary: PathBuf,
    pub address: PathBuf,
    pub namespace: String,
    pub timeout: Duration,
    pub max_output_bytes: usize,
}
impl Default for NerdctlConfig {
    fn default() -> Self {
        Self {
            binary: "/usr/local/bin/nerdctl".into(),
            address: "/run/containerd/containerd.sock".into(),
            namespace: "deploy-platform".into(),
            timeout: Duration::from_secs(120),
            max_output_bytes: 1024 * 1024,
        }
    }
}
impl NerdctlConfig {
    pub(crate) fn validate(&self) -> Result<()> {
        if !self.binary.is_absolute() || !self.address.is_absolute() {
            return Err(RuntimeError::Configuration(
                "binary and socket paths must be absolute",
            ));
        }
        if self.namespace.is_empty()
            || self.namespace.len() > 64
            || !self
                .namespace
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        {
            return Err(RuntimeError::Configuration("invalid namespace"));
        }
        if self.timeout.is_zero() || self.max_output_bytes == 0 {
            return Err(RuntimeError::Configuration(
                "timeout and output limit must be positive",
            ));
        }
        Ok(())
    }
}
