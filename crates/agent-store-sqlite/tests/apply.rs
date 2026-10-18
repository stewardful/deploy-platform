use agent_core::{
    ApplyError, ApplyOptions, DeploymentStore, DesiredState, Phase, StoreError, apply,
};
use agent_store_sqlite::{DaemonLease, SqliteStore};
use app_model::{AppKey, AppSpec, Environment, Identifier, ImageRef, NetworkMode, Resources};
use async_trait::async_trait;
use runtime_api::{
    ContainerRuntime, ContainerState, ContainerStatus, ExecutionFailure, RuntimeError,
};
use std::{sync::Mutex, time::Duration};

#[derive(Default)]
struct FakeState {
    container: Option<ContainerStatus>,
    calls: Vec<&'static str>,
    fail_pull: bool,
    fail_find: bool,
    timeout_after_create: bool,
    crash_on_start: bool,
}
#[derive(Default)]
struct FakeRuntime(Mutex<FakeState>);
fn failure(operation: &'static str) -> RuntimeError {
    RuntimeError::Execution {
        operation,
        kind: ExecutionFailure::Timeout,
    }
}
#[async_trait]
impl ContainerRuntime for FakeRuntime {
    async fn version(&self) -> runtime_api::Result<String> {
        Ok("fake".into())
    }
    async fn pull(&self, _: &ImageRef) -> runtime_api::Result<()> {
        let mut state = self.0.lock().unwrap();
        state.calls.push("pull");
        if state.fail_pull {
            Err(failure("pull"))
        } else {
            Ok(())
        }
    }
    async fn create(&self, spec: &AppSpec) -> runtime_api::Result<String> {
        let mut state = self.0.lock().unwrap();
        state.calls.push("create");
        assert!(
            state.container.is_none(),
            "must not duplicate existing container"
        );
        state.container = Some(ContainerStatus {
            id: "a".repeat(64),
            app: spec.app.clone(),
            state: ContainerState::Created,
            exit_code: None,
            spec_fingerprint: Some(spec.fingerprint().unwrap()),
        });
        if state.timeout_after_create {
            Err(failure("create"))
        } else {
            Ok("a".repeat(64))
        }
    }
    async fn inspect(&self, _: &AppKey) -> runtime_api::Result<ContainerStatus> {
        let mut state = self.0.lock().unwrap();
        state.calls.push("inspect");
        state
            .container
            .clone()
            .ok_or(RuntimeError::Protocol("inspect"))
    }
    async fn find(&self, _: &AppKey) -> runtime_api::Result<Option<ContainerStatus>> {
        let mut state = self.0.lock().unwrap();
        state.calls.push("find");
        if state.fail_find {
            Err(failure("inventory"))
        } else {
            Ok(state.container.clone())
        }
    }
    async fn start(&self, _: &AppKey) -> runtime_api::Result<()> {
        let mut state = self.0.lock().unwrap();
        state.calls.push("start");
        let crash = state.crash_on_start;
        state.container.as_mut().unwrap().state = if crash {
            ContainerState::Exited
        } else {
            ContainerState::Running
        };
        Ok(())
    }
    async fn stop(&self, _: &AppKey) -> runtime_api::Result<()> {
        panic!("apply must not stop existing containers")
    }
    async fn remove(&self, _: &AppKey) -> runtime_api::Result<()> {
        panic!("apply must not delete existing containers")
    }
}
fn spec() -> AppSpec {
    AppSpec {
        schema_version: 1,
        app: AppKey {
            project: Identifier::new("demo").unwrap(),
            name: Identifier::new("web").unwrap(),
        },
        image: ImageRef::new("example/web:v1").unwrap(),
        resources: Resources {
            memory_mib: 128,
            cpu_millis: 500,
            pids: 64,
        },
        args: vec![],
        environment: Environment::default(),
        network: NetworkMode::None,
        ports: vec![],
        user: None,
        volumes: vec![],
        secrets: vec![],
        health_check: None,
        pull_policy: app_model::PullPolicy::Always,
    }
}
fn options() -> ApplyOptions {
    ApplyOptions {
        startup_observation: Duration::from_millis(100),
    }
}

#[tokio::test]
async fn first_apply_persists_configuration_and_repeated_apply_is_non_destructive() {
    let dir = private_directory();
    let runtime = FakeRuntime::default();
    let spec = spec();
    {
        let mut store = SqliteStore::open(dir.path(), "target").unwrap();
        let report = apply(&runtime, &mut store, &spec, options()).await.unwrap();
        assert!(report.created);
        assert_eq!(report.container.state, ContainerState::Running);
        assert_eq!(store.applications().unwrap()[0].phase, Phase::Succeeded);
    }
    let mut reopened = SqliteStore::open(dir.path(), "target").unwrap();
    assert_eq!(
        reopened
            .spec(&spec.app)
            .unwrap()
            .unwrap()
            .fingerprint()
            .unwrap(),
        spec.fingerprint().unwrap()
    );
    let second = apply(&runtime, &mut reopened, &spec, options())
        .await
        .unwrap();
    assert!(!second.created);
    assert_eq!(
        runtime
            .0
            .lock()
            .unwrap()
            .calls
            .iter()
            .filter(|&&c| c == "create")
            .count(),
        1
    );
    assert_eq!(reopened.operations(20).unwrap().len(), 2);
}

#[tokio::test]
async fn timeout_after_create_recovers_on_next_process_without_duplicate_create() {
    let dir = private_directory();
    let runtime = FakeRuntime::default();
    let spec = spec();
    runtime.0.lock().unwrap().timeout_after_create = true;
    {
        let mut store = SqliteStore::open(dir.path(), "target").unwrap();
        assert!(apply(&runtime, &mut store, &spec, options()).await.is_err());
        assert_eq!(store.operations(1).unwrap()[0].phase, Phase::Failed);
    }
    let mut reopened = SqliteStore::open(dir.path(), "target").unwrap();
    let report = apply(&runtime, &mut reopened, &spec, options())
        .await
        .unwrap();
    assert!(!report.created);
    assert_eq!(
        runtime
            .0
            .lock()
            .unwrap()
            .calls
            .iter()
            .filter(|&&c| c == "create")
            .count(),
        1
    );
}

#[tokio::test]
async fn failed_inventory_is_not_mistaken_for_absence() {
    let dir = private_directory();
    let runtime = FakeRuntime::default();
    runtime.0.lock().unwrap().fail_find = true;
    let mut store = SqliteStore::open(dir.path(), "target").unwrap();
    assert!(
        apply(&runtime, &mut store, &spec(), options())
            .await
            .is_err()
    );
    assert_eq!(runtime.0.lock().unwrap().calls, ["find"]);
    assert_eq!(store.operations(1).unwrap()[0].phase, Phase::Failed);
}

#[tokio::test]
async fn pull_failure_is_recorded_and_does_not_create() {
    let dir = private_directory();
    let runtime = FakeRuntime::default();
    runtime.0.lock().unwrap().fail_pull = true;
    let mut store = SqliteStore::open(dir.path(), "target").unwrap();
    assert!(
        apply(&runtime, &mut store, &spec(), options())
            .await
            .is_err()
    );
    assert_eq!(runtime.0.lock().unwrap().calls, ["find", "pull"]);
    assert_eq!(
        store.operations(1).unwrap()[0].error_code.as_deref(),
        Some("runtime_failure")
    );
    assert!(store.spec(&spec().app).unwrap().is_some());
}

#[tokio::test]
async fn changed_spec_is_rejected_before_touching_the_runtime_or_overwriting_config() {
    let dir = private_directory();
    let runtime = FakeRuntime::default();
    let mut store = SqliteStore::open(dir.path(), "target").unwrap();
    let original = spec();
    apply(&runtime, &mut store, &original, options())
        .await
        .unwrap();
    let before = runtime.0.lock().unwrap().calls.len();
    let mut changed = original.clone();
    changed.resources.memory_mib = 256;
    assert!(matches!(
        apply(&runtime, &mut store, &changed, options()).await,
        Err(ApplyError::Store(StoreError::ConfigurationChange))
    ));
    assert_eq!(runtime.0.lock().unwrap().calls.len(), before);
    assert_eq!(
        store
            .spec(&original.app)
            .unwrap()
            .unwrap()
            .resources
            .memory_mib,
        128
    );
}

#[tokio::test]
async fn mismatched_existing_container_is_never_started_or_replaced() {
    let dir = private_directory();
    let runtime = FakeRuntime::default();
    let spec = spec();
    runtime.create(&spec).await.unwrap();
    runtime
        .0
        .lock()
        .unwrap()
        .container
        .as_mut()
        .unwrap()
        .spec_fingerprint = None;
    let mut store = SqliteStore::open(dir.path(), "target").unwrap();
    assert!(matches!(
        apply(&runtime, &mut store, &spec, options()).await,
        Err(ApplyError::ContainerConflict)
    ));
    assert!(!runtime.0.lock().unwrap().calls.contains(&"start"));
}

#[tokio::test]
async fn immediate_exit_is_not_reported_as_success() {
    let dir = private_directory();
    let runtime = FakeRuntime::default();
    runtime.0.lock().unwrap().crash_on_start = true;
    let mut store = SqliteStore::open(dir.path(), "target").unwrap();
    assert!(matches!(
        apply(&runtime, &mut store, &spec(), options()).await,
        Err(ApplyError::NotRunning)
    ));
    assert_eq!(store.applications().unwrap()[0].phase, Phase::Failed);
}

#[test]
fn state_lease_prevents_concurrent_writers_and_releases_on_drop() {
    let dir = private_directory();
    let first = SqliteStore::open(dir.path(), "target").unwrap();
    assert!(matches!(
        SqliteStore::open(dir.path(), "target"),
        Err(StoreError::Busy)
    ));
    drop(first);
    assert!(SqliteStore::open(dir.path(), "target").is_ok());
}

#[test]
fn interrupted_operation_survives_reopen_and_target_cannot_change() {
    let dir = private_directory();
    let spec = spec();
    {
        let mut store = SqliteStore::open(dir.path(), "target").unwrap();
        let id = store.begin(&spec, &spec.fingerprint().unwrap()).unwrap();
        store.record(id, Phase::Creating, None, None).unwrap();
    }
    {
        let reopened = SqliteStore::open(dir.path(), "target").unwrap();
        assert_eq!(reopened.operations(1).unwrap()[0].phase, Phase::Interrupted);
        assert_eq!(
            reopened.applications().unwrap()[0].phase,
            Phase::Interrupted
        );
    }
    assert!(matches!(
        SqliteStore::open(dir.path(), "different"),
        Err(StoreError::TargetMismatch)
    ));
}

#[cfg(unix)]
#[test]
fn refuses_public_state_directory_and_symlinked_database() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let dir = private_directory();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(SqliteStore::open(dir.path(), "target").is_err());
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let other = tempfile::NamedTempFile::new().unwrap();
    symlink(other.path(), dir.path().join("agent.sqlite")).unwrap();
    assert!(SqliteStore::open(dir.path(), "target").is_err());
}

fn private_directory() -> tempfile::TempDir {
    use std::os::unix::fs::PermissionsExt;
    tempfile::Builder::new()
        .permissions(std::fs::Permissions::from_mode(0o700))
        .tempdir()
        .unwrap()
}

#[test]
fn daemon_lease_excludes_second_daemon_but_not_short_lived_cli_store() {
    let dir = private_directory();
    let lease = DaemonLease::acquire(dir.path()).unwrap();
    assert!(matches!(
        DaemonLease::acquire(dir.path()),
        Err(StoreError::Busy)
    ));
    let store = SqliteStore::open(dir.path(), "target").unwrap();
    drop(store);
    drop(lease);
    assert!(DaemonLease::acquire(dir.path()).is_ok());
}

#[test]
fn version_one_database_migrates_without_losing_configuration_or_history() {
    let dir = private_directory();
    let spec = spec();
    {
        let connection = rusqlite::Connection::open(dir.path().join("agent.sqlite")).unwrap();
        connection
            .execute_batch(include_str!("../migrations/001_initial.sql"))
            .unwrap();
        connection
            .execute(
                "INSERT INTO metadata(key,value) VALUES('runtime_target','target')",
                [],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO applications(name,spec,fingerprint,phase) VALUES(?1,?2,?3,?4)",
                rusqlite::params![
                    spec.app.instance_name(),
                    serde_json::to_string(&spec).unwrap(),
                    spec.fingerprint().unwrap(),
                    "\"succeeded\""
                ],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO operations(name,phase) VALUES(?1,?2)",
                rusqlite::params![spec.app.instance_name(), "\"succeeded\""],
            )
            .unwrap();
    }
    let store = SqliteStore::open(dir.path(), "target").unwrap();
    let app = &store.applications().unwrap()[0];
    assert_eq!(app.desired, DesiredState::Running);
    assert_eq!(app.recovery.attempts, 0);
    assert_eq!(app.fingerprint, spec.fingerprint().unwrap());
    assert_eq!(store.operations(20).unwrap().len(), 1);
}
