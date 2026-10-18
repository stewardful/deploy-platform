#![cfg(unix)]
mod support;
use agent_api::{Command, ErrorCode, JobRepository, JobState, Submission};
use agent_api::{
    client::{Client, ClientError},
    server::BoundServer,
};
use agent_core::service::{Service, ServiceOptions};
use agent_store_sqlite::SqliteStore;
use std::{
    path::{Path, PathBuf},
    sync::atomic::Ordering,
    time::Duration,
};
use tokio_util::sync::CancellationToken;

struct Harness {
    service: Option<Service<SqliteStore>>,
    server: Option<tokio::task::JoinHandle<std::io::Result<()>>>,
    shutdown: CancellationToken,
    client: Client,
}
impl Harness {
    async fn start(state: &Path, socket: &Path, runtime: support::Runtime, capacity: u32) -> Self {
        let store = SqliteStore::open(state, "api-test").unwrap();
        let service = Service::start(
            runtime,
            store,
            ServiceOptions {
                queue_capacity: capacity,
                poll_interval: Duration::from_secs(3600),
                ..Default::default()
            },
        )
        .unwrap();
        let bound = BoundServer::bind(socket).await.unwrap();
        let shutdown = CancellationToken::new();
        let server = tokio::spawn(bound.serve(service.api(), shutdown.clone()));
        Self {
            service: Some(service),
            server: Some(server),
            shutdown,
            client: Client::new(socket, Duration::from_secs(3)).unwrap(),
        }
    }
    async fn stop(mut self) {
        self.shutdown.cancel();
        self.server.take().unwrap().await.unwrap().unwrap();
        self.service.take().unwrap().shutdown().await.unwrap();
    }
}
impl Drop for Harness {
    fn drop(&mut self) {
        self.shutdown.cancel();
        if let Some(server) = &self.server {
            server.abort();
        }
    }
}
fn paths() -> (tempfile::TempDir, PathBuf, PathBuf) {
    let root = tempfile::Builder::new()
        .prefix("dp-api-")
        .tempdir_in("/private/tmp")
        .or_else(|_| {
            tempfile::Builder::new()
                .prefix("dp-api-")
                .tempdir_in("/tmp")
        })
        .unwrap();
    let state = root.path().join("state");
    let socket = root.path().join("run/api.sock");
    (root, state, socket)
}
fn submission(key: &str) -> Submission {
    Submission {
        request_id: key.into(),
        command: Command::Apply {
            spec: Box::new(support::spec()),
            startup_wait_ms: 100,
        },
    }
}
async fn wait_pull(runtime: &support::Runtime) {
    tokio::time::timeout(Duration::from_secs(3), async {
        while !runtime.pull_started.load(Ordering::SeqCst) {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn cli_manages_through_real_socket_while_daemon_owns_state_lease() {
    let (_root, state, socket) = paths();
    let runtime = support::Runtime::default();
    let harness = Harness::start(&state, &socket, runtime.clone(), 32).await;
    assert!(SqliteStore::open(&state, "api-test").is_err());
    let file = state.parent().unwrap().join("app.toml");
    std::fs::write(&file, toml::to_string(&support::spec()).unwrap()).unwrap();
    for arguments in [
        vec![
            "apply",
            "-f",
            file.to_str().unwrap(),
            "--startup-wait-ms",
            "100",
            "--request-id",
            "cli-apply",
        ],
        vec!["apps"],
        vec!["operations"],
        vec!["doctor"],
        vec!["stop", "--project", "demo", "--name", "web"],
        vec!["start", "--project", "demo", "--name", "web"],
    ] {
        let output = tokio::process::Command::new(env!("CARGO_BIN_EXE_agentctl"))
            .arg("--socket")
            .arg(&socket)
            .args(arguments)
            .output()
            .await
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let _: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    }
    assert_eq!(runtime.creates(), 1);
    harness.stop().await;
    assert!(!socket.exists());
}

#[tokio::test]
async fn reads_remain_responsive_during_pull_and_queue_is_bounded_and_deduplicated() {
    let (_root, state, socket) = paths();
    let runtime = support::Runtime::default();
    runtime.blocked.store(true, Ordering::SeqCst);
    let harness = Harness::start(&state, &socket, runtime.clone(), 1).await;
    let first = harness.client.submit(&submission("slow")).await.unwrap();
    wait_pull(&runtime).await;
    let health = tokio::time::timeout(Duration::from_secs(1), harness.client.health())
        .await
        .unwrap()
        .unwrap();
    assert!(health.accepting);
    assert_eq!(
        harness.client.operations(20).await.unwrap()[0].phase,
        "pulling"
    );
    assert_eq!(
        harness.client.applications().await.unwrap()[0].phase,
        "pulling"
    );
    assert_eq!(
        harness.client.submit(&submission("slow")).await.unwrap().id,
        first.id
    );
    assert!(
        matches!(harness.client.submit(&submission("full")).await,Err(ClientError::Api(error)) if error.code==ErrorCode::QueueFull)
    );
    let mut changed = submission("slow");
    if let Command::Apply { ref mut spec, .. } = changed.command {
        spec.resources.memory_mib += 1;
    }
    assert!(
        matches!(harness.client.submit(&changed).await,Err(ClientError::Api(error)) if error.code==ErrorCode::Conflict)
    );
    assert!(
        matches!(harness.client.wait(first.id, Duration::from_millis(20)).await, Err(ClientError::WaitTimeout(id)) if id == first.id)
    );
    assert_eq!(
        harness.client.job(first.id).await.unwrap().state,
        JobState::Running
    );
    runtime.blocked.store(false, Ordering::SeqCst);
    runtime.gate.add_permits(1);
    assert_eq!(
        harness
            .client
            .wait(first.id, Duration::from_secs(3))
            .await
            .unwrap()
            .state,
        JobState::Succeeded
    );
    harness.stop().await;
}

#[tokio::test]
async fn interrupted_job_survives_restart_and_reuses_id_without_duplicate_creation() {
    let (_root, state, socket) = paths();
    let runtime = support::Runtime::default();
    runtime.blocked.store(true, Ordering::SeqCst);
    let harness = Harness::start(&state, &socket, runtime.clone(), 32).await;
    let job = harness
        .client
        .submit(&submission("restart-key"))
        .await
        .unwrap();
    wait_pull(&runtime).await;
    harness.stop().await;
    runtime.blocked.store(false, Ordering::SeqCst);
    let harness = Harness::start(&state, &socket, runtime.clone(), 32).await;
    let finished = harness
        .client
        .wait(job.id, Duration::from_secs(3))
        .await
        .unwrap();
    assert_eq!(finished.state, JobState::Succeeded);
    let duplicate = harness
        .client
        .submit(&submission("restart-key"))
        .await
        .unwrap();
    assert_eq!(duplicate.id, job.id);
    assert_eq!(runtime.creates(), 1);
    harness.stop().await;
}

#[tokio::test]
async fn unknown_application_is_a_failed_job_not_a_missing_intent_row() {
    let (_root, state, socket) = paths();
    let harness = Harness::start(&state, &socket, support::Runtime::default(), 32).await;
    let job = harness
        .client
        .submit(&Submission {
            request_id: "unknown".into(),
            command: Command::Stop {
                app: support::spec().app,
            },
        })
        .await
        .unwrap();
    let result = harness
        .client
        .wait(job.id, Duration::from_secs(3))
        .await
        .unwrap();
    assert_eq!(result.state, JobState::Failed);
    assert_eq!(result.error.unwrap().code, ErrorCode::NotFound);
    assert!(harness.client.applications().await.unwrap().is_empty());
    harness.stop().await;
}

#[tokio::test]
async fn malformed_json_size_limits_and_not_found_return_json_without_echoing_input() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let (_root, state, socket) = paths();
    let harness = Harness::start(&state, &socket, support::Runtime::default(), 32).await;
    for (request,expected) in [
        ("POST /v1/jobs HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 16\r\n\r\nsecret-not-json!".to_owned(),"400"),
        (format!("POST /v1/jobs HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",agent_api::MAX_REQUEST_BYTES+1),"413"),
        ("GET /v9/missing HTTP/1.1\r\nHost: localhost\r\n\r\n".to_owned(),"404"),
    ] {
        let mut stream=tokio::net::UnixStream::connect(&socket).await.unwrap(); stream.write_all(request.as_bytes()).await.unwrap();
        let mut output=Vec::new(); tokio::time::timeout(Duration::from_secs(2),stream.read_to_end(&mut output)).await.unwrap().unwrap();
        let text=String::from_utf8(output).unwrap(); assert!(text.starts_with(&format!("HTTP/1.1 {expected}")),"{text}");
        assert!(!text.contains("secret-not-json")); let body=text.split("\r\n\r\n").nth(1).unwrap(); let _:agent_api::ApiError=serde_json::from_str(body).unwrap();
    }
    harness.stop().await;
}

#[tokio::test]
async fn socket_bind_preserves_files_and_cleans_stale_sockets_safely() {
    use std::os::unix::fs::PermissionsExt;
    let (_root, _state, socket) = paths();
    std::fs::create_dir_all(socket.parent().unwrap()).unwrap();
    std::fs::set_permissions(
        socket.parent().unwrap(),
        std::fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    std::fs::write(&socket, "keep-me").unwrap();
    assert!(BoundServer::bind(&socket).await.is_err());
    assert_eq!(std::fs::read_to_string(&socket).unwrap(), "keep-me");
    std::fs::remove_file(&socket).unwrap();
    let old = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    drop(old);
    let bound = BoundServer::bind(&socket).await.unwrap();
    assert_eq!(
        std::fs::metadata(&socket).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert!(BoundServer::bind(&socket).await.is_err());
    drop(bound);
    assert!(!socket.exists());
}

#[tokio::test]
async fn boot_drains_old_backlog_before_recovery_even_when_capacity_is_reduced() {
    use agent_core::DeploymentStore;
    let (_root, state, socket) = paths();
    let runtime = support::Runtime::default();
    let spec = support::spec();
    let mut store = SqliteStore::open(&state, "api-test").unwrap();
    store.begin(&spec, &spec.fingerprint().unwrap()).unwrap();
    let mut unknown = spec.app.clone();
    unknown.name = app_model::Identifier::new("unknown").unwrap();
    store
        .enqueue(
            &Submission {
                request_id: "first".into(),
                command: Command::Stop { app: unknown },
            },
            32,
        )
        .unwrap();
    let stop = store
        .enqueue(
            &Submission {
                request_id: "stop-before-restore".into(),
                command: Command::Stop { app: spec.app },
            },
            32,
        )
        .unwrap();
    drop(store);
    let harness = Harness::start(&state, &socket, runtime.clone(), 1).await;
    assert_eq!(
        harness
            .client
            .wait(stop.id, Duration::from_secs(3))
            .await
            .unwrap()
            .state,
        JobState::Succeeded
    );
    assert_eq!(
        runtime.creates(),
        0,
        "queued stop must precede boot restoration"
    );
    assert!(matches!(
        harness.client.applications().await.unwrap()[0].desired,
        agent_api::Intent::Stopped
    ));
    harness.stop().await;
}
