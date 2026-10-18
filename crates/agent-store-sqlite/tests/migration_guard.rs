use agent_store_sqlite::SqliteStore;
use rusqlite::Connection;

#[test]
fn failed_later_migration_rolls_back_all_earlier_steps() {
    let temp = tempfile::tempdir().unwrap();
    let state = temp.path().join("private");
    std::fs::create_dir(&state).unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&state, std::fs::Permissions::from_mode(0o700)).unwrap();
    let path = state.join("agent.sqlite");
    {
        let db = Connection::open(&path).unwrap();
        db.execute_batch(include_str!("../migrations/001_initial.sql"))
            .unwrap();
        // Force the second ALTER to fail after the first would have added desired.
        db.execute_batch("ALTER TABLE applications ADD COLUMN recovery TEXT;")
            .unwrap();
    }
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    assert!(SqliteStore::open(&state, "test").is_err());
    let db = Connection::open(&path).unwrap();
    assert_eq!(
        db.query_row("PRAGMA user_version", [], |r| r.get::<_, u32>(0))
            .unwrap(),
        1
    );
    let count: u32 = db
        .query_row(
            "SELECT count(*) FROM pragma_table_info('applications') WHERE name='desired'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 0);
}
