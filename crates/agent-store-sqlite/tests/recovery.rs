use agent_core::recovery::{Clock, Outcome, RecoveryOptions, reconcile_once};
use agent_core::{ApplyOptions, DeploymentStore, DesiredState, Phase};
use agent_store_sqlite::SqliteStore;
use app_model::{AppKey, AppSpec, Identifier, ImageRef};
use async_trait::async_trait;
use runtime_api::{ContainerRuntime, ContainerState, ContainerStatus, RuntimeError};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

struct ManualClock(AtomicU64);
impl ManualClock {
    fn new(now: u64) -> Self {
        Self(AtomicU64::new(now))
    }
    fn set(&self, now: u64) {
        self.0.store(now, Ordering::SeqCst);
    }
}
impl Clock for ManualClock {
    fn now_ms(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }
}
#[derive(Default)]
struct State {
    containers: BTreeMap<String, ContainerStatus>,
    calls: Vec<(&'static str, String)>,
    unavailable: BTreeSet<String>,
    crash: BTreeSet<String>,
}
#[derive(Default)]
struct Runtime(Mutex<State>);
impl Runtime {
    fn seed(&self, spec: &AppSpec, state: ContainerState) {
        self.0.lock().unwrap().containers.insert(
            spec.app.instance_name(),
            ContainerStatus {
                id: "a".repeat(64),
                app: spec.app.clone(),
                state,
                exit_code: None,
                spec_fingerprint: Some(spec.fingerprint().unwrap()),
            },
        );
    }
    fn count(&self, operation: &str) -> usize {
        self.0
            .lock()
            .unwrap()
            .calls
            .iter()
            .filter(|(op, _)| *op == operation)
            .count()
    }
}
#[async_trait]
impl ContainerRuntime for Runtime {
    async fn version(&self) -> runtime_api::Result<String> {
        Ok("fake".into())
    }
    async fn pull(&self, _: &ImageRef) -> runtime_api::Result<()> {
        self.0.lock().unwrap().calls.push(("pull", String::new()));
        Ok(())
    }
    async fn create(&self, spec: &AppSpec) -> runtime_api::Result<String> {
        assert!(
            !self
                .0
                .lock()
                .unwrap()
                .containers
                .contains_key(&spec.app.instance_name())
        );
        self.seed(spec, ContainerState::Created);
        self.0
            .lock()
            .unwrap()
            .calls
            .push(("create", spec.app.instance_name()));
        Ok("a".repeat(64))
    }
    async fn inspect(&self, app: &AppKey) -> runtime_api::Result<ContainerStatus> {
        self.0
            .lock()
            .unwrap()
            .containers
            .get(&app.instance_name())
            .cloned()
            .ok_or(RuntimeError::Protocol("inspect"))
    }
    async fn find(&self, app: &AppKey) -> runtime_api::Result<Option<ContainerStatus>> {
        let mut state = self.0.lock().unwrap();
        let name = app.instance_name();
        state.calls.push(("find", name.clone()));
        if state.unavailable.contains(&name) {
            return Err(RuntimeError::Protocol("inventory"));
        }
        Ok(state.containers.get(&name).cloned())
    }
    async fn start(&self, app: &AppKey) -> runtime_api::Result<()> {
        let mut state = self.0.lock().unwrap();
        let name = app.instance_name();
        state.calls.push(("start", name.clone()));
        let crash = state.crash.contains(&name);
        state.containers.get_mut(&name).unwrap().state = if crash {
            ContainerState::Exited
        } else {
            ContainerState::Running
        };
        Ok(())
    }
    async fn stop(&self, app: &AppKey) -> runtime_api::Result<()> {
        let mut state = self.0.lock().unwrap();
        let name = app.instance_name();
        state.calls.push(("stop", name.clone()));
        state.containers.get_mut(&name).unwrap().state = ContainerState::Exited;
        Ok(())
    }
    async fn remove(&self, _: &AppKey) -> runtime_api::Result<()> {
        panic!("recovery must not delete containers")
    }
}
fn spec(name: &str) -> AppSpec {
    let mut spec: AppSpec =
        toml::from_str(include_str!("../../../examples/http-service.toml")).unwrap();
    spec.app.name = Identifier::new(name).unwrap();
    spec
}
fn register(store: &mut SqliteStore, spec: &AppSpec) {
    let id = store.begin(spec, &spec.fingerprint().unwrap()).unwrap();
    store.record(id, Phase::Succeeded, None, None).unwrap();
}
fn options() -> RecoveryOptions {
    RecoveryOptions {
        retry_base: Duration::from_secs(1),
        retry_max: Duration::from_secs(8),
        stable_window: Duration::from_secs(10),
        apply: ApplyOptions {
            startup_observation: Duration::from_millis(100),
        },
    }
}

#[tokio::test]
async fn restart_loads_persisted_configuration_and_restores_exited_container() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state");
    let spec = spec("web");
    let runtime = Runtime::default();
    {
        let mut store = SqliteStore::open(&path, "target").unwrap();
        register(&mut store, &spec);
    }
    runtime.seed(&spec, ContainerState::Exited); // A reboot leaves desired state, but no running task.
    let mut reopened = SqliteStore::open(&path, "target").unwrap();
    let result = reconcile_once(&runtime, &mut reopened, &ManualClock::new(1000), options())
        .await
        .unwrap();
    assert_eq!(result[0].outcome, Outcome::Restored);
    assert_eq!(runtime.count("start"), 1);
    assert_eq!(runtime.count("create"), 0);
    assert_eq!(
        reopened.applications().unwrap()[0].desired,
        DesiredState::Running
    );
}

#[tokio::test]
async fn missing_container_is_recreated_but_healthy_polls_do_not_add_history() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state");
    let spec = spec("web");
    let runtime = Runtime::default();
    let mut store = SqliteStore::open(&path, "target").unwrap();
    register(&mut store, &spec);
    let clock = ManualClock::new(1000);
    assert_eq!(
        reconcile_once(&runtime, &mut store, &clock, options())
            .await
            .unwrap()[0]
            .outcome,
        Outcome::Restored
    );
    let history = store.operations(100).unwrap().len();
    clock.set(2000);
    for _ in 0..5 {
        assert_eq!(
            reconcile_once(&runtime, &mut store, &clock, options())
                .await
                .unwrap()[0]
                .outcome,
            Outcome::Healthy
        );
    }
    assert_eq!(runtime.count("create"), 1);
    assert_eq!(store.operations(100).unwrap().len(), history);
}

#[tokio::test]
async fn stopped_intent_survives_restart_and_missing_container_is_not_recreated() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state");
    let spec = spec("web");
    let runtime = Runtime::default();
    runtime.seed(&spec, ContainerState::Running);
    {
        let mut store = SqliteStore::open(&path, "target").unwrap();
        register(&mut store, &spec);
        store.set_desired(&spec.app, DesiredState::Stopped).unwrap();
        assert_eq!(
            reconcile_once(&runtime, &mut store, &ManualClock::new(1000), options())
                .await
                .unwrap()[0]
                .outcome,
            Outcome::Stopped
        );
    }
    runtime.0.lock().unwrap().containers.clear();
    let mut store = SqliteStore::open(&path, "target").unwrap();
    reconcile_once(&runtime, &mut store, &ManualClock::new(2000), options())
        .await
        .unwrap();
    assert_eq!(runtime.count("stop"), 1);
    assert_eq!(runtime.count("start"), 0);
    assert_eq!(runtime.count("create"), 0);
    assert_eq!(store.applications().unwrap()[0].phase, Phase::Stopped);
    store.set_desired(&spec.app, DesiredState::Running).unwrap();
    reconcile_once(&runtime, &mut store, &ManualClock::new(2000), options())
        .await
        .unwrap();
    assert_eq!(runtime.count("create"), 1);
}

#[tokio::test]
async fn crash_loop_backoff_is_persistent_exponential_and_capped() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state");
    let spec = spec("web");
    let runtime = Runtime::default();
    runtime.seed(&spec, ContainerState::Exited);
    runtime
        .0
        .lock()
        .unwrap()
        .crash
        .insert(spec.app.instance_name());
    {
        let mut store = SqliteStore::open(&path, "target").unwrap();
        register(&mut store, &spec);
    }
    for (attempt, now, next) in [
        (1, 1000, 2000),
        (2, 2000, 4000),
        (3, 4000, 8000),
        (4, 8000, 16000),
        (5, 16000, 24000),
    ] {
        let mut store = SqliteStore::open(&path, "target").unwrap();
        let clock = ManualClock::new(now);
        let event = reconcile_once(&runtime, &mut store, &clock, options())
            .await
            .unwrap()
            .remove(0);
        assert_eq!(event.outcome, Outcome::Failed);
        assert_eq!(event.next_attempt_ms, next);
        assert_eq!(store.applications().unwrap()[0].recovery.attempts, attempt);
        let calls = runtime.count("find");
        clock.set(next - 1);
        assert_eq!(
            reconcile_once(&runtime, &mut store, &clock, options())
                .await
                .unwrap()[0]
                .outcome,
            Outcome::Deferred
        );
        assert_eq!(runtime.count("find"), calls);
    }
}

#[tokio::test]
async fn brief_success_does_not_reset_crash_loop_but_stable_running_does() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state");
    let spec = spec("web");
    let runtime = Runtime::default();
    let mut store = SqliteStore::open(&path, "target").unwrap();
    register(&mut store, &spec);
    let clock = ManualClock::new(1000);
    reconcile_once(&runtime, &mut store, &clock, options())
        .await
        .unwrap();
    runtime.seed(&spec, ContainerState::Exited);
    clock.set(2000);
    reconcile_once(&runtime, &mut store, &clock, options())
        .await
        .unwrap();
    assert_eq!(store.applications().unwrap()[0].recovery.attempts, 2);
    clock.set(12000);
    reconcile_once(&runtime, &mut store, &clock, options())
        .await
        .unwrap();
    let recovery = store.applications().unwrap()[0].recovery.clone();
    assert_eq!(recovery.attempts, 0);
    assert_eq!(recovery.next_attempt_ms, 0);
}

#[tokio::test]
async fn failed_application_does_not_prevent_other_applications_from_recovering() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state");
    let runtime = Runtime::default();
    let mut store = SqliteStore::open(&path, "target").unwrap();
    let bad = spec("a-bad");
    let good = spec("b-good");
    register(&mut store, &bad);
    register(&mut store, &good);
    runtime
        .0
        .lock()
        .unwrap()
        .unavailable
        .insert(bad.app.instance_name());
    let events = reconcile_once(&runtime, &mut store, &ManualClock::new(1000), options())
        .await
        .unwrap();
    assert_eq!(events[0].outcome, Outcome::Failed);
    assert_eq!(events[1].outcome, Outcome::Restored);
    assert_eq!(runtime.count("create"), 1);
}

#[tokio::test]
async fn mismatched_configuration_is_not_started_stopped_or_replaced() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state");
    let runtime = Runtime::default();
    let spec = spec("web");
    let mut store = SqliteStore::open(&path, "target").unwrap();
    register(&mut store, &spec);
    runtime.seed(&spec, ContainerState::Running);
    runtime
        .0
        .lock()
        .unwrap()
        .containers
        .get_mut(&spec.app.instance_name())
        .unwrap()
        .spec_fingerprint = None;
    for desired in [DesiredState::Running, DesiredState::Stopped] {
        store.set_desired(&spec.app, desired).unwrap();
        let event = reconcile_once(&runtime, &mut store, &ManualClock::new(1000), options())
            .await
            .unwrap()
            .remove(0);
        assert_eq!(event.error_code.as_deref(), Some("recovery_conflict"));
    }
    assert_eq!(runtime.count("start"), 0);
    assert_eq!(runtime.count("stop"), 0);
    assert_eq!(runtime.count("create"), 0);
}

#[tokio::test]
async fn stop_intent_is_retained_when_runtime_is_temporarily_unavailable() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state");
    let spec = spec("web");
    let runtime = Runtime::default();
    runtime.seed(&spec, ContainerState::Running);
    {
        let mut store = SqliteStore::open(&path, "target").unwrap();
        register(&mut store, &spec);
        store.set_desired(&spec.app, DesiredState::Stopped).unwrap();
        runtime
            .0
            .lock()
            .unwrap()
            .unavailable
            .insert(spec.app.instance_name());
        reconcile_once(&runtime, &mut store, &ManualClock::new(1000), options())
            .await
            .unwrap();
    }
    runtime.0.lock().unwrap().unavailable.clear();
    let mut store = SqliteStore::open(&path, "target").unwrap();
    reconcile_once(&runtime, &mut store, &ManualClock::new(2000), options())
        .await
        .unwrap();
    assert_eq!(runtime.count("stop"), 1);
    assert_eq!(runtime.count("start"), 0);
    assert_eq!(
        store.applications().unwrap()[0].desired,
        DesiredState::Stopped
    );
}

#[tokio::test]
async fn cancelled_restore_is_reconciled_after_reopen_without_double_start() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state");
    let spec = spec("web");
    let runtime = Runtime::default();
    {
        let mut store = SqliteStore::open(&path, "target").unwrap();
        register(&mut store, &spec);
        let clock = ManualClock::new(1000);
        let result = tokio::time::timeout(
            Duration::from_millis(20),
            reconcile_once(&runtime, &mut store, &clock, options()),
        )
        .await;
        assert!(result.is_err());
        assert_eq!(runtime.count("start"), 1);
    }
    let mut reopened = SqliteStore::open(&path, "target").unwrap();
    assert_eq!(reopened.operations(1).unwrap()[0].phase, Phase::Interrupted);
    assert_eq!(
        reconcile_once(&runtime, &mut reopened, &ManualClock::new(2000), options())
            .await
            .unwrap()[0]
            .outcome,
        Outcome::Healthy
    );
    assert_eq!(runtime.count("start"), 1);
}

#[tokio::test]
async fn invalid_options_do_not_touch_runtime_and_explicit_start_clears_backoff() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state");
    let spec = spec("web");
    let runtime = Runtime::default();
    let mut store = SqliteStore::open(&path, "target").unwrap();
    register(&mut store, &spec);
    let bad = RecoveryOptions {
        retry_max: Duration::ZERO,
        ..options()
    };
    assert!(
        reconcile_once(&runtime, &mut store, &ManualClock::new(1000), bad)
            .await
            .is_err()
    );
    assert_eq!(runtime.count("find"), 0);
    runtime
        .0
        .lock()
        .unwrap()
        .unavailable
        .insert(spec.app.instance_name());
    reconcile_once(&runtime, &mut store, &ManualClock::new(1000), options())
        .await
        .unwrap();
    assert!(store.applications().unwrap()[0].recovery.next_attempt_ms > 1000);
    store.set_desired(&spec.app, DesiredState::Running).unwrap();
    assert_eq!(store.applications().unwrap()[0].recovery.attempts, 0);
    assert_eq!(store.applications().unwrap()[0].recovery.next_attempt_ms, 0);
}
