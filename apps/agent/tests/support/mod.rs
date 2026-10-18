#![allow(dead_code)]
use app_model::{AppKey, AppSpec, ImageRef};
use async_trait::async_trait;
use runtime_api::{ContainerRuntime, ContainerState, ContainerStatus, RuntimeError};
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio::sync::Semaphore;

#[derive(Default)]
struct State {
    containers: BTreeMap<String, ContainerStatus>,
    creates: u64,
}
#[derive(Clone)]
pub struct Runtime {
    state: Arc<Mutex<State>>,
    pub blocked: Arc<AtomicBool>,
    pub pull_started: Arc<AtomicBool>,
    pub gate: Arc<Semaphore>,
}
impl Default for Runtime {
    fn default() -> Self {
        Self {
            state: Arc::new(Mutex::new(State::default())),
            blocked: Arc::new(AtomicBool::new(false)),
            pull_started: Arc::new(AtomicBool::new(false)),
            gate: Arc::new(Semaphore::new(0)),
        }
    }
}
impl Runtime {
    pub fn creates(&self) -> u64 {
        self.state.lock().unwrap().creates
    }
}
#[async_trait]
impl ContainerRuntime for Runtime {
    async fn publication_target(
        &self,
        spec: &AppSpec,
    ) -> runtime_api::Result<Option<std::net::Ipv4Addr>> {
        if spec.network != app_model::NetworkMode::Project {
            return Err(RuntimeError::Configuration("project network required"));
        }
        Ok(
            (self.inspect(&spec.app).await?.state == ContainerState::Running)
                .then(|| "10.4.0.2".parse().unwrap()),
        )
    }

    async fn diagnostics(&self, spec: &AppSpec) -> runtime_api::Result<runtime_api::Diagnostics> {
        let status = self.inspect(&spec.app).await?;
        Ok(runtime_api::Diagnostics {
            state: status.state,
            network: Some("dp-project-demo".into()),
            service_name: Some(spec.app.name.as_str().into()),
            health: "healthy".into(),
            http_status: Some(200),
            checked_at_ms: 1,
        })
    }
    async fn logs(&self, app: &AppKey, tail: u16) -> runtime_api::Result<runtime_api::LogSnapshot> {
        self.inspect(app).await?;
        Ok(runtime_api::LogSnapshot {
            text: "fixture: request completed <script>text only</script>".into(),
            tail,
        })
    }
    async fn version(&self) -> runtime_api::Result<String> {
        Ok("test".into())
    }
    async fn pull(&self, _: &ImageRef) -> runtime_api::Result<()> {
        if self.blocked.load(Ordering::SeqCst) {
            self.pull_started.store(true, Ordering::SeqCst);
            let _permit = self
                .gate
                .acquire()
                .await
                .map_err(|_| RuntimeError::Protocol("gate"))?;
        }
        Ok(())
    }
    async fn create(&self, spec: &AppSpec) -> runtime_api::Result<String> {
        let mut state = self.state.lock().unwrap();
        let name = spec.app.instance_name();
        assert!(!state.containers.contains_key(&name));
        state.creates += 1;
        let id = format!("{:064x}", state.creates);
        state.containers.insert(
            name,
            ContainerStatus {
                id: id.clone(),
                app: spec.app.clone(),
                state: ContainerState::Created,
                exit_code: None,
                spec_fingerprint: Some(spec.fingerprint().unwrap()),
            },
        );
        Ok(id)
    }
    async fn find(&self, app: &AppKey) -> runtime_api::Result<Option<ContainerStatus>> {
        Ok(self
            .state
            .lock()
            .unwrap()
            .containers
            .get(&app.instance_name())
            .cloned())
    }
    async fn inspect(&self, app: &AppKey) -> runtime_api::Result<ContainerStatus> {
        self.find(app)
            .await?
            .ok_or(RuntimeError::Protocol("inspect"))
    }
    async fn start(&self, app: &AppKey) -> runtime_api::Result<()> {
        self.state
            .lock()
            .unwrap()
            .containers
            .get_mut(&app.instance_name())
            .unwrap()
            .state = ContainerState::Running;
        Ok(())
    }
    async fn stop(&self, app: &AppKey) -> runtime_api::Result<()> {
        self.state
            .lock()
            .unwrap()
            .containers
            .get_mut(&app.instance_name())
            .unwrap()
            .state = ContainerState::Exited;
        Ok(())
    }
    async fn remove(&self, _: &AppKey) -> runtime_api::Result<()> {
        panic!("unexpected delete")
    }
}
pub fn spec() -> AppSpec {
    toml::from_str(include_str!("../../../../examples/http-service.toml")).unwrap()
}

#[derive(Default)]
pub struct Proxy;
#[async_trait]
impl ingress_model::Proxy for Proxy {
    async fn reconcile(
        &self,
        _: &[ingress_model::ResolvedRoute],
    ) -> std::result::Result<(), ingress_model::ProxyError> {
        Ok(())
    }
}
