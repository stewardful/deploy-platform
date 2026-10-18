//! Project network ownership stays in the runtime adapter. CNI owns IPAM/firewall.
use crate::{CommandExecutor, NerdctlRuntime};
use app_model::Identifier;
use runtime_api::{Result, RuntimeError};
use serde_json::Value;

pub(crate) fn network_name(project: &Identifier) -> String {
    format!("dp-project-{}", project.as_str())
}
impl<E: CommandExecutor> NerdctlRuntime<E> {
    pub(crate) async fn ensure_project_network(&self, project: &Identifier) -> Result<()> {
        let name = network_name(project);
        let output = self
            .execute(
                "network-list",
                vec!["network".into(), "ls".into(), "--format=json".into()],
            )
            .await?;
        let mut found = false;
        for item in serde_json::Deserializer::from_slice(&output.stdout).into_iter::<Value>() {
            let item = item.map_err(|_| RuntimeError::Protocol("network-list"))?;
            let entries = match item {
                Value::Array(v) => v,
                v => vec![v],
            };
            for entry in entries {
                let actual = entry
                    .get("Name")
                    .and_then(Value::as_str)
                    .ok_or(RuntimeError::Protocol("network-list"))?;
                found |= actual == name;
            }
        }
        if !found {
            self.execute(
                "network-create",
                vec![
                    "network".into(),
                    "create".into(),
                    "--driver=bridge".into(),
                    "--label=io.deploy-platform.managed-by=rust-agent".into(),
                    format!("--label=io.deploy-platform.project={}", project.as_str()).into(),
                    "--".into(),
                    name.clone().into(),
                ],
            )
            .await?;
        }
        self.verify_project_network(project).await
    }
    pub(crate) async fn verify_project_network(&self, project: &Identifier) -> Result<()> {
        let name = network_name(project);
        let output = self
            .execute(
                "network-inspect",
                vec![
                    "network".into(),
                    "inspect".into(),
                    "--mode=native".into(),
                    "--".into(),
                    name.clone().into(),
                ],
            )
            .await?;
        let entries: Vec<Value> = serde_json::from_slice(&output.stdout)
            .map_err(|_| RuntimeError::Protocol("network-inspect"))?;
        let entry = entries
            .first()
            .filter(|_| entries.len() == 1)
            .ok_or(RuntimeError::Protocol("network-inspect"))?;
        // Native inspect retains the CNI policy, unlike Docker-compatible inspect.
        let labels = &entry["NerdctlLabels"];
        let cni = &entry["CNI"];
        if labels["io.deploy-platform.managed-by"] != "rust-agent"
            || labels["io.deploy-platform.project"] != project.as_str()
            || cni["name"] != name
        {
            return Err(RuntimeError::Ownership);
        }
        let plugins = cni["plugins"]
            .as_array()
            .ok_or(RuntimeError::Protocol("network plugins"))?;
        if !plugins.iter().any(|p| p["type"] == "bridge")
            || !plugins
                .iter()
                .any(|p| p["type"] == "firewall" && p["ingressPolicy"] == "same-bridge")
        {
            return Err(RuntimeError::Configuration(
                "project bridge isolation is required",
            ));
        }
        Ok(())
    }
}
