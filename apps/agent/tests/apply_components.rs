use agent_core::{ApplyOptions, DeploymentStore, Phase, apply};
use agent_store_sqlite::SqliteStore;
use app_model::AppSpec;
use async_trait::async_trait;
use runtime_nerdctl::{
    CommandExecutor, CommandOutput, CommandRequest, NerdctlConfig, NerdctlRuntime,
};
use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
    time::Duration,
};

#[derive(Clone)]
struct ScriptedExecutor {
    replies: Arc<Mutex<VecDeque<Vec<u8>>>>,
}
#[async_trait]
impl CommandExecutor for ScriptedExecutor {
    async fn execute(&self, _: CommandRequest) -> runtime_api::Result<CommandOutput> {
        Ok(CommandOutput {
            stdout: self
                .replies
                .lock()
                .unwrap()
                .pop_front()
                .expect("unexpected runtime call"),
            stderr: vec![],
        })
    }
}

#[tokio::test]
async fn real_engine_store_and_nerdctl_adapter_interoperate_across_store_reopen() {
    let spec: AppSpec =
        toml::from_str(include_str!("../../../examples/http-service.toml")).unwrap();
    let container = |status: &str| {
        serde_json::to_vec(&serde_json::json!([{
        "Id": "b".repeat(64), "Config": { "Labels": {
            "io.deploy-platform.managed-by": "rust-agent", "io.deploy-platform.project": spec.app.project.as_str(),
            "io.deploy-platform.app": spec.app.name.as_str(), "io.deploy-platform.spec-sha256": spec.fingerprint().unwrap()
        }}, "State": { "Status": status, "ExitCode": 0 }
    }])).unwrap()
    };
    let replies = VecDeque::from([
        b"[]".to_vec(),
        vec![],
        "b".repeat(64).into_bytes(),
        container("created"),
        container("created"),
        vec![],
        container("running"),
        serde_json::to_vec(&serde_json::json!({"Names": spec.app.instance_name()})).unwrap(),
        container("running"),
        container("running"),
    ]);
    let executor = ScriptedExecutor {
        replies: Arc::new(Mutex::new(replies)),
    };
    let runtime = NerdctlRuntime::new(NerdctlConfig::default(), executor.clone()).unwrap();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("private-state");
    for expected_created in [true, false] {
        let mut store = SqliteStore::open(&path, "component-test").unwrap();
        let report = apply(
            &runtime,
            &mut store,
            &spec,
            ApplyOptions {
                startup_observation: Duration::from_millis(100),
            },
        )
        .await
        .unwrap();
        assert_eq!(report.created, expected_created);
        assert_eq!(store.applications().unwrap()[0].phase, Phase::Succeeded);
    }
    assert!(executor.replies.lock().unwrap().is_empty());
}
