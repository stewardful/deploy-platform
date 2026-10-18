use crate::{CommandExecutor, CommandOutput, CommandRequest, NerdctlConfig};
use app_model::{AppKey, AppSpec, ImageRef, LocalPort, NetworkMode};
use async_trait::async_trait;
use runtime_api::{ContainerRuntime, ContainerState, ContainerStatus, Result, RuntimeError};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, ffi::OsString};

const OWNER_KEY: &str = "io.deploy-platform.managed-by";
const OWNER: &str = "rust-agent";
const PROJECT_KEY: &str = "io.deploy-platform.project";
const APP_KEY: &str = "io.deploy-platform.app";
const SPEC_KEY: &str = "io.deploy-platform.spec-sha256";

pub struct NerdctlRuntime<E> {
    pub(crate) config: NerdctlConfig,
    pub(crate) executor: E,
    resources: Option<std::sync::Arc<dyn runtime_api::ResourceProvider>>,
}

/// A safe summary, not executable argv. No environment values or application arguments.
#[derive(Debug, Serialize)]
pub struct CreatePreview {
    pub instance_name: String,
    pub image: String,
    pub namespace: String,
    pub network: &'static str,
    pub ports: Vec<String>,
    pub memory_mib: u32,
    pub cpu_millis: u32,
    pub pids: u32,
    pub environment_keys: Vec<String>,
    pub argument_count: usize,
    pub user: Option<app_model::RunAs>,
    pub volumes: Vec<app_model::VolumeMount>,
    pub secrets: Vec<app_model::SecretMount>,
}

impl<E: CommandExecutor> NerdctlRuntime<E> {
    pub fn new(config: NerdctlConfig, executor: E) -> Result<Self> {
        config.validate()?;
        Ok(Self {
            config,
            executor,
            resources: None,
        })
    }
    pub fn with_resources(
        mut self,
        resources: std::sync::Arc<dyn runtime_api::ResourceProvider>,
    ) -> Self {
        self.resources = Some(resources);
        self
    }
    pub fn preview(&self, spec: &AppSpec) -> Result<CreatePreview> {
        spec.validate()?;
        Ok(CreatePreview {
            instance_name: spec.app.instance_name(),
            image: spec.image.as_str().into(),
            namespace: self.config.namespace.clone(),
            network: match spec.network {
                NetworkMode::None => "none",
                NetworkMode::Bridge => "bridge",
                NetworkMode::Project => "project",
            },
            ports: spec
                .ports
                .iter()
                .map(|p| format!("127.0.0.1:{}:{}", p.host, p.container))
                .collect(),
            memory_mib: spec.resources.memory_mib,
            cpu_millis: spec.resources.cpu_millis,
            pids: spec.resources.pids,
            environment_keys: spec.environment.0.keys().cloned().collect(),
            argument_count: spec.args.len(),
            user: spec.user.clone(),
            volumes: spec.volumes.clone(),
            secrets: spec.secrets.clone(),
        })
    }
    /// Read-only check of the daemon connection; does not create namespaces or containers.
    pub async fn check_connection(&self) -> Result<()> {
        let output = self
            .execute("info", vec!["info".into(), "--format=json".into()])
            .await?;
        let value: serde_json::Value =
            serde_json::from_slice(&output.stdout).map_err(|_| RuntimeError::Protocol("info"))?;
        if !value.is_object() {
            return Err(RuntimeError::Protocol("info"));
        }
        Ok(())
    }
    pub(crate) fn request(&self, operation: &'static str, args: Vec<OsString>) -> CommandRequest {
        let mut all = vec![
            OsString::from("--address"),
            self.config.address.as_os_str().into(),
            OsString::from("--namespace"),
            self.config.namespace.clone().into(),
        ];
        all.extend(args);
        CommandRequest {
            operation,
            program: self.config.binary.clone(),
            args: all,
            timeout: self.config.timeout,
            max_output_bytes: self.config.max_output_bytes,
        }
    }
    pub(crate) async fn execute(
        &self,
        operation: &'static str,
        args: Vec<OsString>,
    ) -> Result<CommandOutput> {
        self.executor.execute(self.request(operation, args)).await
    }
    fn create_args(&self, spec: &AppSpec) -> Result<Vec<OsString>> {
        spec.validate()?;
        let mut args: Vec<OsString> = ["create", "--name"].into_iter().map(Into::into).collect();
        args.push(spec.app.instance_name().into());
        args.extend(
            [
                "--restart=no",
                "--log-driver=json-file",
                "--log-opt=max-size=10m",
                "--log-opt=max-file=3",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
            ]
            .into_iter()
            .map(OsString::from),
        );
        args.push(
            match spec.network {
                NetworkMode::None => "--net=none".to_owned(),
                NetworkMode::Bridge => "--net=bridge".to_owned(),
                NetworkMode::Project => {
                    format!("--net={}", crate::network::network_name(&spec.app.project))
                }
            }
            .into(),
        );
        if spec.network == NetworkMode::Project {
            args.extend(["--hostname".into(), spec.app.name.as_str().into()]);
        }
        for LocalPort { host, container } in &spec.ports {
            args.extend([
                "--publish".into(),
                format!("127.0.0.1:{host}:{container}/tcp").into(),
            ]);
        }
        args.extend([
            "--label".into(),
            format!("{SPEC_KEY}={}", spec.fingerprint()?).into(),
        ]);
        for (key, value) in [
            (OWNER_KEY, OWNER),
            (PROJECT_KEY, spec.app.project.as_str()),
            (APP_KEY, spec.app.name.as_str()),
        ] {
            args.extend(["--label".into(), format!("{key}={value}").into()]);
        }
        args.extend([
            "--memory".into(),
            (u64::from(spec.resources.memory_mib) * 1024 * 1024)
                .to_string()
                .into(),
            "--cpus".into(),
            format!(
                "{}.{:03}",
                spec.resources.cpu_millis / 1000,
                spec.resources.cpu_millis % 1000
            )
            .into(),
            "--pids-limit".into(),
            spec.resources.pids.to_string().into(),
        ]);
        for (key, value) in &spec.environment.0 {
            args.extend(["--env".into(), format!("{key}={value}").into()]);
        }
        if let Some(user) = &spec.user {
            args.extend(["--user".into(), format!("{}:{}", user.uid, user.gid).into()]);
        }
        if !spec.volumes.is_empty() || !spec.secrets.is_empty() {
            let provider = self.resources.as_ref().ok_or(RuntimeError::Configuration(
                "resource provider is required for mounts",
            ))?;
            for mount in provider.prepare(spec)? {
                let source = mount
                    .source
                    .to_str()
                    .filter(|s| s.starts_with('/') && !s.contains([',', '\0']))
                    .ok_or(RuntimeError::Configuration("invalid prepared mount"))?;
                args.extend([
                    "--mount".into(),
                    format!(
                        "type=bind,src={source},dst={}{}",
                        mount.target,
                        if mount.read_only { ",ro" } else { "" }
                    )
                    .into(),
                ]);
            }
        }
        args.extend(["--".into(), spec.image.as_str().into()]);
        args.extend(spec.args.iter().map(OsString::from));
        Ok(args)
    }
    async fn mutate_owned(&self, operation: &'static str, app: &AppKey) -> Result<()> {
        // Use the inspected immutable ID, not the reusable container name.
        let status = self.inspect(app).await?;
        self.execute(
            operation,
            vec![operation.into(), "--".into(), status.id.into()],
        )
        .await?;
        Ok(())
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct RawContainer {
    id: String,
    config: RawConfig,
    state: RawState,
}
#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct RawConfig {
    labels: Option<BTreeMap<String, String>>,
}
#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct RawState {
    status: String,
    exit_code: Option<i32>,
    running: Option<bool>,
    paused: Option<bool>,
    restarting: Option<bool>,
    pid: Option<u32>,
}

#[async_trait]
impl<E: CommandExecutor> ContainerRuntime for NerdctlRuntime<E> {
    async fn publication_target(&self, spec: &AppSpec) -> Result<Option<std::net::Ipv4Addr>> {
        self.owned_address(spec).await
    }

    async fn prepare_image(&self, spec: &AppSpec) -> Result<()> {
        use app_model::PullPolicy;
        if spec.pull_policy != PullPolicy::Always {
            let cached = self
                .execute(
                    "image-inspect",
                    vec![
                        "image".into(),
                        "inspect".into(),
                        "--".into(),
                        spec.image.as_str().into(),
                    ],
                )
                .await;
            match cached {
                Ok(output) => {
                    let entries: Vec<serde_json::Value> = serde_json::from_slice(&output.stdout)
                        .map_err(|_| RuntimeError::Protocol("image-inspect"))?;
                    if entries.len() != 1 || !entries[0].is_object() {
                        return Err(RuntimeError::Protocol("image-inspect"));
                    }
                    return Ok(());
                }
                Err(e) if spec.pull_policy == PullPolicy::Never => return Err(e),
                Err(_) => {} // Pull remains bounded and fails normally if the daemon is unavailable.
            }
        }
        self.pull(&spec.image).await
    }
    async fn diagnostics(&self, spec: &AppSpec) -> Result<runtime_api::Diagnostics> {
        self.read_diagnostics(spec).await
    }
    async fn logs(&self, app: &AppKey, tail: u16) -> Result<runtime_api::LogSnapshot> {
        if !(1..=500).contains(&tail) {
            return Err(RuntimeError::Configuration("tail must be 1..500"));
        }
        let status = self.inspect(app).await?;
        let mut request = self.request(
            "logs",
            vec![
                "logs".into(),
                format!("--tail={tail}").into(),
                "--timestamps".into(),
                "--".into(),
                status.id.into(),
            ],
        );
        request.timeout = std::time::Duration::from_secs(2);
        request.max_output_bytes = 64 * 1024;
        let output = self.executor.execute(request).await?;
        // No terminal escape/control sequences and no HTML interpretation by clients.
        let bytes = [output.stdout, output.stderr].concat();
        if bytes.len() > 64 * 1024 {
            return Err(RuntimeError::Protocol("logs too large; reduce tail"));
        }
        let text = String::from_utf8_lossy(&bytes)
            .chars()
            .filter(|c| !c.is_control() || *c == '\n' || *c == '\t')
            .collect();
        Ok(runtime_api::LogSnapshot { text, tail })
    }
    async fn version(&self) -> Result<String> {
        let output = self.execute("version", vec!["--version".into()]).await?;
        String::from_utf8(output.stdout)
            .map(|s| s.trim().to_owned())
            .map_err(|_| RuntimeError::Protocol("version"))
    }
    async fn pull(&self, image: &ImageRef) -> Result<()> {
        self.execute(
            "pull",
            vec!["pull".into(), "--".into(), image.as_str().into()],
        )
        .await?;
        Ok(())
    }
    async fn create(&self, spec: &AppSpec) -> Result<String> {
        spec.validate()?;
        if spec.network == NetworkMode::Project {
            self.ensure_project_network(&spec.app.project).await?;
        }
        let output = self.execute("create", self.create_args(spec)?).await?;
        let id = String::from_utf8(output.stdout).map_err(|_| RuntimeError::Protocol("create"))?;
        let id = id.trim();
        if !valid_id(id) {
            return Err(RuntimeError::Protocol("create"));
        }
        Ok(id.into())
    }
    async fn inspect(&self, app: &AppKey) -> Result<ContainerStatus> {
        let output = self
            .execute(
                "inspect",
                vec!["inspect".into(), "--".into(), app.instance_name().into()],
            )
            .await?;
        let mut containers: Vec<RawContainer> = serde_json::from_slice(&output.stdout)
            .map_err(|_| RuntimeError::Protocol("inspect"))?;
        if containers.len() != 1 {
            return Err(RuntimeError::Protocol("inspect"));
        }
        let raw = containers.remove(0);
        let labels = raw.config.labels.unwrap_or_default();
        if labels.get(OWNER_KEY).map(String::as_str) != Some(OWNER)
            || labels.get(PROJECT_KEY).map(String::as_str) != Some(app.project.as_str())
            || labels.get(APP_KEY).map(String::as_str) != Some(app.name.as_str())
        {
            return Err(RuntimeError::Ownership);
        }
        if !valid_id(&raw.id) {
            return Err(RuntimeError::Protocol("inspect"));
        }
        let state = match raw.state.status.as_str() {
            "created" => ContainerState::Created,
            // nerdctl 2.4.1 leaves Status empty when no containerd task exists.
            // Require its complete inactive shape; missing fields stay unknown.
            "" if raw.state.running == Some(false)
                && raw.state.paused == Some(false)
                && raw.state.restarting == Some(false)
                && raw.state.pid == Some(0) =>
            {
                ContainerState::Created
            }
            "running" => ContainerState::Running,
            "paused" => ContainerState::Paused,
            "restarting" => ContainerState::Restarting,
            "exited" => ContainerState::Exited,
            _ => ContainerState::Unknown,
        };
        Ok(ContainerStatus {
            id: raw.id,
            app: app.clone(),
            state,
            exit_code: raw.state.exit_code,
            spec_fingerprint: labels.get(SPEC_KEY).cloned(),
        })
    }
    async fn find(&self, app: &AppKey) -> Result<Option<ContainerStatus>> {
        let output = self
            .execute(
                "inventory",
                vec![
                    "ps".into(),
                    "--all".into(),
                    "--no-trunc".into(),
                    "--format=json".into(),
                ],
            )
            .await?;
        let mut found = false;
        // nerdctl versions may return JSON lines or a JSON array. Empty output is empty inventory.
        for item in
            serde_json::Deserializer::from_slice(&output.stdout).into_iter::<serde_json::Value>()
        {
            let item = item.map_err(|_| RuntimeError::Protocol("inventory"))?;
            let entries = match item {
                serde_json::Value::Array(items) => items,
                other => vec![other],
            };
            for entry in entries {
                let names = entry
                    .get("Names")
                    .ok_or(RuntimeError::Protocol("inventory"))?;
                let expected = app.instance_name();
                let matches_name = |name: &str| name.trim_start_matches('/') == expected;
                match names {
                    serde_json::Value::String(name) => found |= matches_name(name),
                    serde_json::Value::Array(names) => {
                        for name in names {
                            found |= matches_name(
                                name.as_str().ok_or(RuntimeError::Protocol("inventory"))?,
                            );
                        }
                    }
                    _ => return Err(RuntimeError::Protocol("inventory")),
                }
            }
        }
        if found {
            self.inspect(app).await.map(Some)
        } else {
            Ok(None)
        }
    }
    async fn start(&self, app: &AppKey) -> Result<()> {
        self.mutate_owned("start", app).await
    }
    async fn stop(&self, app: &AppKey) -> Result<()> {
        self.mutate_owned("stop", app).await
    }
    async fn remove(&self, app: &AppKey) -> Result<()> {
        self.mutate_owned("rm", app).await
    }
}

fn valid_id(id: &str) -> bool {
    id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit())
}

#[cfg(test)]
mod tests {
    use super::*;
    use app_model::{Environment, Identifier, Resources};
    use std::sync::{Arc, Mutex};
    #[derive(Default, Clone)]
    struct Fake {
        requests: Arc<Mutex<Vec<CommandRequest>>>,
        replies: Arc<Mutex<Vec<Vec<u8>>>>,
    }
    #[async_trait]
    impl CommandExecutor for Fake {
        async fn execute(&self, request: CommandRequest) -> Result<CommandOutput> {
            self.requests.lock().unwrap().push(request);
            let stdout = self.replies.lock().unwrap().remove(0);
            Ok(CommandOutput {
                stdout,
                stderr: vec![],
            })
        }
    }
    fn spec() -> AppSpec {
        AppSpec {
            schema_version: 1,
            app: AppKey {
                project: Identifier::new("demo").unwrap(),
                name: Identifier::new("api").unwrap(),
            },
            image: ImageRef::new("example/api:v1").unwrap(),
            resources: Resources {
                memory_mib: 128,
                cpu_millis: 500,
                pids: 64,
            },
            environment: Environment::default(),
            args: vec![],
            network: NetworkMode::None,
            ports: vec![],
            user: None,
            volumes: vec![],
            secrets: vec![],
            health_check: None,
            pull_policy: app_model::PullPolicy::Always,
        }
    }
    fn raw(owned: bool) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!([{"Id": "a".repeat(64), "Config": {"Labels": {
            OWNER_KEY: if owned { OWNER } else { "someone-else" }, PROJECT_KEY: "demo", APP_KEY: "api"
        }}, "State": {"Status": "running", "ExitCode": 0}}])).unwrap()
    }
    #[tokio::test]
    async fn creates_bounded_isolated_container_without_shell() {
        let fake = Fake::default();
        fake.replies
            .lock()
            .unwrap()
            .push("a".repeat(64).into_bytes());
        let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake.clone()).unwrap();
        let mut spec = spec();
        spec.environment
            .0
            .insert("SETTING".into(), "$(touch /tmp/no); literal".into());
        runtime.create(&spec).await.unwrap();
        let requests = fake.requests.lock().unwrap();
        let args: Vec<_> = requests[0]
            .args
            .iter()
            .map(|a| a.to_str().unwrap())
            .collect();
        assert_eq!(
            &args[..4],
            &[
                "--address",
                "/run/containerd/containerd.sock",
                "--namespace",
                "deploy-platform"
            ]
        );
        assert!(args.contains(&"--net=none"));
        assert!(args.contains(&"--log-opt=max-size=10m"));
        assert!(args.contains(&"--log-opt=max-file=3"));
        assert!(args.contains(&"134217728"));
        assert!(args.contains(&"0.500"));
        assert!(args.contains(&"SETTING=$(touch /tmp/no); literal"));
        assert!(!format!("{:?}", requests[0]).contains("touch"));
        assert!(
            !serde_json::to_string(&runtime.preview(&spec).unwrap())
                .unwrap()
                .contains("touch")
        );
    }
    #[tokio::test]
    async fn refuses_foreign_container_before_mutation() {
        let fake = Fake::default();
        fake.replies.lock().unwrap().push(raw(false));
        let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake.clone()).unwrap();
        assert!(matches!(
            runtime.remove(&spec().app).await,
            Err(RuntimeError::Ownership)
        ));
        assert_eq!(fake.requests.lock().unwrap().len(), 1);
    }
    #[tokio::test]
    async fn mutates_by_inspected_id_without_force() {
        let fake = Fake::default();
        fake.replies.lock().unwrap().extend([raw(true), vec![]]);
        let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake.clone()).unwrap();
        runtime.remove(&spec().app).await.unwrap();
        let requests = fake.requests.lock().unwrap();
        assert_eq!(
            requests[1].args.last().unwrap(),
            &OsString::from("a".repeat(64))
        );
        assert!(!requests[1].args.contains(&OsString::from("--force")));
    }
    #[tokio::test]
    async fn invalid_configuration_never_calls_executor() {
        let fake = Fake::default();
        let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake.clone()).unwrap();
        let mut spec = spec();
        spec.resources.pids = 0;
        assert!(runtime.create(&spec).await.is_err());
        assert!(fake.requests.lock().unwrap().is_empty());
        assert!(
            NerdctlRuntime::new(
                NerdctlConfig {
                    binary: "nerdctl".into(),
                    ..Default::default()
                },
                fake
            )
            .is_err()
        );
    }
    #[tokio::test]
    async fn malformed_runtime_response_fails_closed() {
        let fake = Fake::default();
        fake.replies.lock().unwrap().push(b"not json".to_vec());
        let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake).unwrap();
        assert!(matches!(
            runtime.inspect(&spec().app).await,
            Err(RuntimeError::Protocol(_))
        ));
    }
    #[tokio::test]
    async fn taskless_nerdctl_container_is_created_only_with_complete_inactive_state() {
        for (state, expected) in [
            (
                serde_json::json!({"Status":"", "ExitCode":0, "Running":false, "Paused":false, "Restarting":false, "Pid":0}),
                ContainerState::Created,
            ),
            (
                serde_json::json!({"Status":"", "ExitCode":0}),
                ContainerState::Unknown,
            ),
            (
                serde_json::json!({"Status":"", "ExitCode":0, "Running":true, "Paused":false, "Restarting":false, "Pid":0}),
                ContainerState::Unknown,
            ),
        ] {
            let fake = Fake::default();
            let mut value: serde_json::Value = serde_json::from_slice(&raw(true)).unwrap();
            value[0]["State"] = state;
            fake.replies
                .lock()
                .unwrap()
                .push(serde_json::to_vec(&value).unwrap());
            let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake.clone()).unwrap();
            assert_eq!(runtime.inspect(&spec().app).await.unwrap().state, expected);
            assert_eq!(fake.requests.lock().unwrap().len(), 1);
        }
    }
    #[tokio::test]
    async fn inventory_matches_exact_names_and_supports_json_lines() {
        let fake = Fake::default();
        let name = spec().app.instance_name();
        fake.replies.lock().unwrap().extend([
            format!("{{\"Names\":\"{name}-other\"}}\n{{\"Names\":\"{name}\"}}\n").into_bytes(),
            raw(true),
        ]);
        let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake).unwrap();
        assert!(runtime.find(&spec().app).await.unwrap().is_some());
    }
    #[tokio::test]
    async fn malformed_inventory_never_means_not_found() {
        for data in [b"not-json".to_vec(), b"[{}]".to_vec(), b"null".to_vec()] {
            let fake = Fake::default();
            fake.replies.lock().unwrap().push(data);
            let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake).unwrap();
            assert!(matches!(
                runtime.find(&spec().app).await,
                Err(RuntimeError::Protocol("inventory"))
            ));
        }
        let fake = Fake::default();
        fake.replies.lock().unwrap().push(b"[]".to_vec());
        let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake).unwrap();
        assert!(runtime.find(&spec().app).await.unwrap().is_none());
    }
    #[tokio::test]
    async fn bridge_ports_are_explicit_loopback_only_and_spec_is_labeled() {
        let fake = Fake::default();
        fake.replies
            .lock()
            .unwrap()
            .push("a".repeat(64).into_bytes());
        let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake.clone()).unwrap();
        let mut spec = spec();
        spec.network = NetworkMode::Bridge;
        spec.ports.push(LocalPort {
            host: 18080,
            container: 8080,
        });
        runtime.create(&spec).await.unwrap();
        let requests = fake.requests.lock().unwrap();
        assert!(requests[0].args.contains(&OsString::from("--net=bridge")));
        assert!(
            requests[0]
                .args
                .contains(&OsString::from("127.0.0.1:18080:8080/tcp"))
        );
        assert!(requests[0].args.contains(&OsString::from(format!(
            "{SPEC_KEY}={}",
            spec.fingerprint().unwrap()
        ))));
    }
    fn network(owned: bool, isolated: bool) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!([{"NerdctlLabels":{
            OWNER_KEY:if owned {OWNER}else{"foreign"},PROJECT_KEY:"demo"},
            "CNI":{"name":"dp-project-demo","plugins":[{"type":"bridge"},{"type":"firewall","ingressPolicy":if isolated {"same-bridge"} else {"open"}}]}}])).unwrap()
    }
    #[tokio::test]
    async fn project_network_is_owned_and_requires_firewall_before_container_creation() {
        for (owned, isolated) in [(false, true), (true, false)] {
            let fake = Fake::default();
            fake.replies.lock().unwrap().extend([
                br#"[{"Name":"dp-project-demo"}]"#.to_vec(),
                network(owned, isolated),
            ]);
            let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake.clone()).unwrap();
            let mut spec = spec();
            spec.network = NetworkMode::Project;
            assert!(runtime.create(&spec).await.is_err());
            assert_eq!(fake.requests.lock().unwrap().len(), 2);
        }
        let fake = Fake::default();
        fake.replies.lock().unwrap().extend([
            b"[]".to_vec(),
            b"created".to_vec(),
            network(true, true),
            "a".repeat(64).into_bytes(),
        ]);
        let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake.clone()).unwrap();
        let mut spec = spec();
        spec.network = NetworkMode::Project;
        runtime.create(&spec).await.unwrap();
        let requests = fake.requests.lock().unwrap();
        let args = &requests[3].args;
        assert!(args.contains(&"--net=dp-project-demo".into()));
        assert!(
            args.windows(2)
                .any(|v| v == [OsString::from("--hostname"), OsString::from("api")])
        );
    }
    #[tokio::test]
    async fn bounded_logs_use_owned_immutable_id_and_strip_controls() {
        let fake = Fake::default();
        fake.replies
            .lock()
            .unwrap()
            .extend([raw(true), b"ok\x1b[31m\n".to_vec()]);
        let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake.clone()).unwrap();
        assert!(runtime.logs(&spec().app, 501).await.is_err());
        let output = runtime.logs(&spec().app, 20).await.unwrap();
        assert!(!output.text.contains('\x1b'));
        {
            let requests = fake.requests.lock().unwrap();
            assert_eq!(requests[1].timeout, std::time::Duration::from_secs(2));
            assert_eq!(requests[1].max_output_bytes, 65536);
            assert_eq!(
                requests[1].args.last().unwrap(),
                &OsString::from("a".repeat(64))
            );
        }
        let foreign = Fake::default();
        foreign.replies.lock().unwrap().push(raw(false));
        let runtime = NerdctlRuntime::new(NerdctlConfig::default(), foreign.clone()).unwrap();
        assert!(matches!(
            runtime.logs(&spec().app, 20).await,
            Err(RuntimeError::Ownership)
        ));
        assert_eq!(foreign.requests.lock().unwrap().len(), 1);
    }
    #[tokio::test]
    async fn offline_image_policy_never_pulls_and_invalid_inspection_fails_closed() {
        for data in [b"[{}]".to_vec(), b"[]".to_vec()] {
            let valid = data == b"[{}]";
            let fake = Fake::default();
            fake.replies.lock().unwrap().push(data);
            let runtime = NerdctlRuntime::new(NerdctlConfig::default(), fake.clone()).unwrap();
            let mut spec = spec();
            spec.pull_policy = app_model::PullPolicy::Never;
            assert_eq!(runtime.prepare_image(&spec).await.is_ok(), valid);
            assert_eq!(fake.requests.lock().unwrap().len(), 1);
        }
    }
}
