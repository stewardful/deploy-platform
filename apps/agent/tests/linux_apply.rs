//! Explicitly enabled host integration test. Uses a real container runtime and network.
#![cfg(unix)]
use app_model::{AppSpec, Identifier};
use std::{
    io::{Read, Write},
    net::{Ipv4Addr, TcpListener, TcpStream},
    path::PathBuf,
    process::Command,
    time::Duration,
};

struct Fixture {
    binary: PathBuf,
    socket: String,
    namespace: String,
    name: String,
}
impl Fixture {
    fn nerdctl(&self) -> Command {
        let mut command = Command::new(&self.binary);
        command.args(["--address", &self.socket, "--namespace", &self.namespace]);
        command
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        // Only remove the fixture's own labeled container, addressed by immutable ID.
        if let Ok(output) = self.nerdctl().args(["inspect", "--", &self.name]).output() {
            if let Ok(value) = serde_json::from_slice::<serde_json::Value>(&output.stdout) {
                if value[0]["Config"]["Labels"]["io.deploy-platform.managed-by"] == "rust-agent" {
                    if let Some(id) = value[0]["Id"].as_str() {
                        if id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit()) {
                            let _ = self.nerdctl().args(["rm", "--force", "--", id]).output();
                        }
                    }
                }
            }
        }
        // Namespace removal fails safely if an unexpected resource still exists.
        let _ = self
            .nerdctl()
            .args(["namespace", "remove", "--", &self.namespace])
            .output();
    }
}

struct Daemon(std::process::Child);
impl Daemon {
    fn start(fixture: &Fixture, state: &std::path::Path, socket: &std::path::Path) -> Self {
        let child = Command::new(env!("CARGO_BIN_EXE_agentd"))
            .arg("--nerdctl")
            .arg(&fixture.binary)
            .args([
                "--address",
                &fixture.socket,
                "--namespace",
                &fixture.namespace,
            ])
            .arg("--state-dir")
            .arg(state)
            .arg("--socket")
            .arg(socket)
            // This fixture owns a separate namespace/state; never reconcile
            // an existing node's proxy while testing ordinary app recovery.
            .arg("--proxy-socket")
            .arg(state.join("test-proxy/admin.sock"))
            .args(["--poll-seconds", "1"])
            .stdout(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let mut daemon = Self(child);
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        loop {
            let output = cli(socket, &["doctor"]);
            if output.status.success() {
                return daemon;
            }
            assert!(
                daemon.0.try_wait().unwrap().is_none(),
                "agentd exited during startup"
            );
            assert!(
                std::time::Instant::now() < deadline,
                "agentd socket did not become ready"
            );
            std::thread::sleep(Duration::from_millis(100));
        }
    }
    fn stop(mut self) {
        assert!(
            Command::new("kill")
                .args(["-TERM", &self.0.id().to_string()])
                .status()
                .unwrap()
                .success()
        );
        assert!(self.0.wait().unwrap().success());
    }
}
impl Drop for Daemon {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}
fn cli(socket: &std::path::Path, args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_agentctl"))
        .arg("--socket")
        .arg(socket)
        .args(args)
        .output()
        .unwrap()
}
fn success(output: std::process::Output) -> serde_json::Value {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
#[ignore = "requires an explicitly prepared Linux/cgroup-v2 host with nerdctl, containerd, CNI, registry access"]
fn deploy_via_socket_and_recover_after_daemon_restart() {
    assert_eq!(
        std::env::consts::OS,
        "linux",
        "this integration test requires Linux"
    );
    let binary = PathBuf::from(
        std::env::var("AGENT_TEST_NERDCTL").unwrap_or("/usr/local/bin/nerdctl".into()),
    );
    assert!(binary.is_absolute());
    let socket =
        std::env::var("AGENT_TEST_SOCKET").unwrap_or("/run/containerd/containerd.sock".into());
    let unique = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let mut spec: AppSpec =
        toml::from_str(include_str!("../../../examples/http-service.toml")).unwrap();
    spec.app.project = Identifier::new(format!("test-{unique}")).unwrap();
    let reservation = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let port = reservation.local_addr().unwrap().port();
    spec.ports[0].host = port;
    let fixture = Fixture {
        binary,
        socket,
        namespace: format!("dp-test-{unique}"),
        name: spec.app.instance_name(),
    };
    assert!(
        fixture
            .nerdctl()
            .args(["namespace", "create", "--", &fixture.namespace])
            .status()
            .unwrap()
            .success()
    );
    let dir = tempfile::tempdir_in("/tmp").unwrap();
    let file = dir.path().join("app.toml");
    std::fs::write(&file, toml::to_string(&spec).unwrap()).unwrap();
    let state = dir.path().join("state");
    let api = dir.path().join("run/api.sock");
    drop(reservation);
    let mut first_id = String::new();
    for expected_created in [true, false] {
        // Real process restart; both mutations go through HTTP while the daemon owns SQLite.
        let daemon = Daemon::start(&fixture, &state, &api);
        let job = success(cli(
            &api,
            &[
                "apply",
                "-f",
                file.to_str().unwrap(),
                "--startup-wait-ms",
                "1000",
            ],
        ));
        assert_eq!(job["state"], "succeeded");
        assert_eq!(job["result"]["created"], expected_created);
        let id = job["result"]["instance_id"].as_str().unwrap();
        if expected_created {
            first_id = id.into();
        } else {
            assert_eq!(id, first_id);
        }
        daemon.stop();
    }
    // Running process state alone is not HTTP readiness; verify the business endpoint separately.
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    loop {
        let response = (|| -> std::io::Result<String> {
            let mut stream = TcpStream::connect_timeout(
                &(Ipv4Addr::LOCALHOST, port).into(),
                Duration::from_secs(1),
            )?;
            stream.set_read_timeout(Some(Duration::from_secs(1)))?;
            stream.write_all(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")?;
            let mut result = String::new();
            stream.take(65536).read_to_string(&mut result)?;
            Ok(result)
        })();
        if response
            .is_ok_and(|text| text.starts_with("HTTP/1.1 200") || text.starts_with("HTTP/1.0 200"))
        {
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "HTTP response did not become ready"
        );
        std::thread::sleep(Duration::from_millis(200));
    }
    assert!(
        fixture
            .nerdctl()
            .args(["stop", "--", &first_id])
            .status()
            .unwrap()
            .success()
    );
    let daemon = Daemon::start(&fixture, &state, &api);
    let deadline = std::time::Instant::now() + Duration::from_secs(15);
    loop {
        let inspected = fixture
            .nerdctl()
            .args(["inspect", "--", &fixture.name])
            .output()
            .unwrap();
        let restored: serde_json::Value = serde_json::from_slice(&inspected.stdout).unwrap();
        if restored[0]["State"]["Status"] == "running" {
            assert_eq!(restored[0]["Id"], first_id);
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "daemon did not restore running intent"
        );
        std::thread::sleep(Duration::from_millis(200));
    }
    success(cli(
        &api,
        &[
            "stop",
            "--project",
            spec.app.project.as_str(),
            "--name",
            spec.app.name.as_str(),
        ],
    ));
    daemon.stop();
    assert!(
        fixture
            .nerdctl()
            .args(["rm", "--", &first_id])
            .status()
            .unwrap()
            .success()
    );
    let daemon = Daemon::start(&fixture, &state, &api);
    std::thread::sleep(Duration::from_secs(2));
    assert!(
        !fixture
            .nerdctl()
            .args(["inspect", "--", &fixture.name])
            .output()
            .unwrap()
            .status
            .success()
    );
    daemon.stop();
}
