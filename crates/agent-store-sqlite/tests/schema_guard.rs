#![cfg(unix)]
use agent_store_sqlite::SqliteStore;
use rusqlite::Connection;
use std::os::unix::fs::PermissionsExt;

#[test]
fn unsupported_schemas_are_rejected_without_rewriting_their_contents() {
    for version in [0, 1, 2, 4] {
        let temp = tempfile::tempdir().unwrap();
        std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let path = temp.path().join("agent.sqlite");
        {
            let db = Connection::open(&path).unwrap();
            db.execute_batch(
                "CREATE TABLE retained(value TEXT); INSERT INTO retained VALUES('keep');",
            )
            .unwrap();
            db.pragma_update(None, "user_version", version).unwrap();
        }
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(SqliteStore::open(temp.path(), "test").is_err());
        let db = Connection::open(&path).unwrap();
        assert_eq!(
            db.query_row("PRAGMA user_version", [], |r| r.get::<_, u32>(0))
                .unwrap(),
            version
        );
        assert_eq!(
            db.query_row("SELECT value FROM retained", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "keep"
        );
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM sqlite_schema WHERE name='applications'",
                [],
                |r| r.get::<_, u32>(0)
            )
            .unwrap(),
            0
        );
    }
}
