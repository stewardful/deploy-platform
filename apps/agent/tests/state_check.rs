#![cfg(unix)]
use rusqlite::Connection;
use std::{os::unix::fs::PermissionsExt, process::Command};

#[test]
fn candidate_migrates_only_isolated_copy_without_a_runtime_binary() {
    let root = tempfile::tempdir().unwrap();
    let original = root.path().join("original.sqlite");
    {
        let db = Connection::open(&original).unwrap();
        db.execute_batch(include_str!(
            "../../../crates/agent-store-sqlite/migrations/001_initial.sql"
        ))
        .unwrap();
    }
    let copy = root.path().join("copy");
    std::fs::create_dir(&copy).unwrap();
    std::fs::set_permissions(&copy, std::fs::Permissions::from_mode(0o700)).unwrap();
    std::fs::copy(&original, copy.join("agent.sqlite")).unwrap();
    std::fs::set_permissions(
        copy.join("agent.sqlite"),
        std::fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_agentd"))
        .args([
            "--check-state",
            "--nerdctl",
            "/no-runtime-exists",
            "--state-dir",
        ])
        .arg(&copy)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let version = |path| {
        Connection::open(path)
            .unwrap()
            .query_row("PRAGMA user_version", [], |r| r.get::<_, u32>(0))
            .unwrap()
    };
    assert_eq!(version(original), 1);
    assert_eq!(version(copy.join("agent.sqlite")), 3);
}
