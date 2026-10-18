//! Shared host wiring. Deployment and recovery libraries remain framework-independent.
use clap::Args;
use runtime_nerdctl::{NerdctlConfig, NerdctlRuntime, TokioExecutor};
use std::path::PathBuf;

#[derive(Debug, Clone, Args)]
pub struct HostOptions {
    #[arg(long, global = true, default_value = "/usr/local/bin/nerdctl")]
    pub nerdctl: PathBuf,
    #[arg(long, global = true, default_value = "/run/containerd/containerd.sock")]
    pub address: PathBuf,
    #[arg(long, global = true, default_value = "deploy-platform")]
    pub namespace: String,
    #[arg(long, global = true, default_value = "/var/lib/deploy-platform")]
    pub state_dir: PathBuf,
}
impl HostOptions {
    pub fn config(&self) -> NerdctlConfig {
        NerdctlConfig {
            binary: self.nerdctl.clone(),
            address: self.address.clone(),
            namespace: self.namespace.clone(),
            ..Default::default()
        }
    }
    pub fn target(&self) -> Result<String, String> {
        serde_json::to_string(&(self.address.to_string_lossy(), &self.namespace))
            .map_err(|_| "invalid runtime target".into())
    }
    pub fn runtime(&self) -> Result<NerdctlRuntime<TokioExecutor>, String> {
        NerdctlRuntime::new(self.config(), TokioExecutor).map_err(|e| e.to_string())
    }
}
