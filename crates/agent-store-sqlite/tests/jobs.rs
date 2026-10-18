use agent_api::{Command, ErrorCode, JobRepository, JobState, Submission};
use agent_store_sqlite::SqliteStore;
use app_model::{AppKey, Identifier};

fn request(key: &str, stop: bool) -> Submission {
    let app = AppKey {
        project: Identifier::new("demo").unwrap(),
        name: Identifier::new("web").unwrap(),
    };
    Submission {
        request_id: key.into(),
        command: if stop {
            Command::Stop { app }
        } else {
            Command::Start { app }
        },
    }
}
#[test]
fn deduplication_precedes_capacity_check_and_conflicting_reuse_is_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = SqliteStore::open(&dir.path().join("state"), "target").unwrap();
    let first = store.enqueue(&request("key", true), 1).unwrap();
    assert_eq!(
        store.enqueue(&request("key", true), 1).unwrap().id,
        first.id
    );
    assert_eq!(
        store.enqueue(&request("key", false), 1).unwrap_err().code,
        ErrorCode::Conflict
    );
    assert_eq!(
        store.enqueue(&request("other", true), 1).unwrap_err().code,
        ErrorCode::QueueFull
    );
}
#[test]
fn running_job_is_replayed_with_same_id_after_store_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state");
    let id = {
        let mut store = SqliteStore::open(&path, "target").unwrap();
        let id = store.enqueue(&request("stop-1", true), 32).unwrap().id;
        assert_eq!(store.claim_job().unwrap().unwrap().id, id);
        assert_eq!(store.job(id).unwrap().state, JobState::Running);
        id
    };
    let mut store = SqliteStore::open(&path, "target").unwrap();
    store.recover_jobs().unwrap();
    assert_eq!(store.job(id).unwrap().state, JobState::Queued);
    assert_eq!(store.claim_job().unwrap().unwrap().id, id);
    store
        .finish_job(
            id,
            Err(agent_api::ApiError::new(
                ErrorCode::NotFound,
                "application not found",
            )),
        )
        .unwrap();
    assert_eq!(store.job(id).unwrap().state, JobState::Failed);
    assert_eq!(
        store.enqueue(&request("stop-1", true), 1).unwrap().state,
        JobState::Failed
    );
    assert!(store.claim_job().unwrap().is_none());
    assert!(
        store
            .finish_job(id, Err(agent_api::ApiError::internal()))
            .is_err()
    );
}
