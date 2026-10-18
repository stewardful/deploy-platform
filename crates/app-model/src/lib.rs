//! Portable application definitions. No filesystem, process, database or runtime dependency.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, fmt};
use thiserror::Error;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ValidationError {
    #[error("invalid {0}")]
    Invalid(&'static str),
    #[error("unsupported schema version; expected 1")]
    SchemaVersion,
}

/// Lowercase ASCII identifier, 1..=48 bytes. Safe in generated resource names.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct Identifier(String);

impl Identifier {
    pub fn new(value: impl Into<String>) -> Result<Self, ValidationError> {
        let value = value.into();
        let valid = !value.is_empty()
            && value.len() <= 48
            && value
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
            && value.as_bytes()[0].is_ascii_alphanumeric()
            && value.as_bytes()[value.len() - 1].is_ascii_alphanumeric();
        if !valid {
            return Err(ValidationError::Invalid("identifier"));
        }
        Ok(Self(value))
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl TryFrom<String> for Identifier {
    type Error = ValidationError;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        Self::new(value)
    }
}
impl From<Identifier> for String {
    fn from(value: Identifier) -> Self {
        value.0
    }
}

/// Registry reference only; local archives, URLs and option-like values are not accepted.
/// This is boundary validation, not a complete OCI distribution grammar parser.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct ImageRef(String);

impl ImageRef {
    pub fn new(value: impl Into<String>) -> Result<Self, ValidationError> {
        let value = value.into();
        if value.is_empty()
            || value.len() > 512
            || !value.as_bytes()[0].is_ascii_alphanumeric()
            || value.contains("://")
            || !value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"/._-:@".contains(&b))
        {
            return Err(ValidationError::Invalid("image reference"));
        }
        if let Some((name, digest)) = value.split_once('@') {
            let hash = digest.strip_prefix("sha256:").unwrap_or("");
            if name.is_empty() || hash.len() != 64 || !hash.bytes().all(|b| b.is_ascii_hexdigit()) {
                return Err(ValidationError::Invalid("image digest"));
            }
        }
        Ok(Self(value))
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}
impl TryFrom<String> for ImageRef {
    type Error = ValidationError;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        Self::new(value)
    }
}
impl From<ImageRef> for String {
    fn from(value: ImageRef) -> Self {
        value.0
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AppKey {
    pub project: Identifier,
    pub name: Identifier,
}
impl AppKey {
    /// Length-prefixing prevents project/name delimiter collisions.
    pub fn instance_name(&self) -> String {
        format!(
            "dp-{}-{}-{}",
            self.project.as_str().len(),
            self.project.as_str(),
            self.name.as_str()
        )
    }
}

/// Non-secret configuration only. Debug omits values; serialization is not encryption.
#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Environment(pub BTreeMap<String, String>);
impl fmt::Debug for Environment {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Environment")
            .field("keys", &self.0.keys().collect::<Vec<_>>())
            .finish()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Resources {
    pub memory_mib: u32,
    pub cpu_millis: u32,
    pub pids: u32,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NetworkMode {
    #[default]
    None,
    /// Shared nerdctl bridge. This is not project-level network isolation.
    Bridge,
    /// Private project bridge; the application name is its service hostname.
    Project,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LocalPort {
    /// Always bound to IPv4 loopback, never all host interfaces.
    pub host: u16,
    pub container: u16,
}

/// Numeric image identity; no host user lookup or privilege escalation.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunAs {
    pub uid: u32,
    pub gid: u32,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VolumeMount {
    pub name: Identifier,
    pub target: String,
    #[serde(default)]
    pub read_only: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SecretMount {
    pub name: Identifier,
}

/// Container paths only. Host paths are never accepted from app requests.
pub fn valid_volume_target(value: &str) -> bool {
    value.len() <= 256
        && !value.ends_with('/')
        && !value.contains(',')
        && value.split('/').skip(1).all(|s| {
            !s.is_empty()
                && s != "."
                && s != ".."
                && s.bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
        })
        && ["/data", "/var/lib", "/srv", "/opt", "/app", "/workspace"]
            .iter()
            .any(|p| value == *p || value.starts_with(&format!("{p}/")))
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PullPolicy {
    #[default]
    Always,
    IfNotPresent,
    Never,
}
impl PullPolicy {
    pub fn is_default(&self) -> bool {
        *self == Self::Always
    }
}

/// HTTP readiness only, never an arbitrary URL or command.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HttpHealthCheck {
    pub port: u16,
    pub path: String,
}
impl HttpHealthCheck {
    pub fn validate(&self) -> Result<(), ValidationError> {
        if self.port == 0
            || self.path.len() > 256
            || !self.path.starts_with('/')
            || self.path.starts_with("//")
            || !self
                .path
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"/._~-".contains(&b))
        {
            return Err(ValidationError::Invalid("HTTP health check"));
        }
        Ok(())
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AppSpec {
    pub schema_version: u32,
    pub app: AppKey,
    pub image: ImageRef,
    pub resources: Resources,
    #[serde(default)]
    pub environment: Environment,
    /// Arguments passed to the image's entrypoint, not shell source.
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub network: NetworkMode,
    #[serde(default)]
    pub ports: Vec<LocalPort>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub user: Option<RunAs>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub volumes: Vec<VolumeMount>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub secrets: Vec<SecretMount>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub health_check: Option<HttpHealthCheck>,
    #[serde(default, skip_serializing_if = "PullPolicy::is_default")]
    pub pull_policy: PullPolicy,
}

impl fmt::Debug for AppSpec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("AppSpec")
            .field("schema_version", &self.schema_version)
            .field("app", &self.app)
            .field("image", &self.image)
            .field("resources", &self.resources)
            .field("environment", &self.environment)
            .field("argument_count", &self.args.len())
            .field("network", &self.network)
            .field("ports", &self.ports)
            .finish()
    }
}

impl AppSpec {
    /// Stable hash of normalized, validated configuration; not a secret-safe token.
    pub fn fingerprint(&self) -> Result<String, ValidationError> {
        self.validate()?;
        let bytes = serde_json::to_vec(self)
            .map_err(|_| ValidationError::Invalid("configuration encoding"))?;
        Ok(format!("{:x}", Sha256::digest(bytes)))
    }

    pub fn validate(&self) -> Result<(), ValidationError> {
        if let Some(check) = &self.health_check {
            check.validate()?;
            if self.network != NetworkMode::Project {
                return Err(ValidationError::Invalid(
                    "HTTP checks require project network",
                ));
            }
        }
        if self.schema_version != 1 {
            return Err(ValidationError::SchemaVersion);
        }
        if self.resources.memory_mib < 16 {
            return Err(ValidationError::Invalid("memory_mib (minimum 16)"));
        }
        if self.resources.cpu_millis == 0 {
            return Err(ValidationError::Invalid("cpu_millis"));
        }
        if self.resources.pids == 0 {
            return Err(ValidationError::Invalid("pids"));
        }
        if self.volumes.len() > 8 || self.secrets.len() > 16 {
            return Err(ValidationError::Invalid("mount count"));
        }
        let mut names = std::collections::BTreeSet::new();
        let mut targets: Vec<&str> = Vec::new();
        for mount in &self.volumes {
            if !names.insert(mount.name.as_str())
                || !valid_volume_target(&mount.target)
                || targets.iter().any(|t| {
                    mount.target == *t
                        || mount.target.starts_with(&format!("{t}/"))
                        || t.starts_with(&format!("{}/", mount.target))
                })
            {
                return Err(ValidationError::Invalid("volume mount"));
            }
            targets.push(&mount.target);
        }
        names.clear();
        for mount in &self.secrets {
            if !names.insert(mount.name.as_str()) {
                return Err(ValidationError::Invalid("secret mount"));
            }
        }
        if self.args.len() > 128 || self.args.iter().any(|a| a.len() > 8192 || a.contains('\0')) {
            return Err(ValidationError::Invalid("arguments"));
        }
        if self.ports.len() > 16 || (!self.ports.is_empty() && self.network == NetworkMode::None) {
            return Err(ValidationError::Invalid(
                "ports require bridge or project network; maximum 16",
            ));
        }
        let mut used = std::collections::BTreeSet::new();
        for port in &self.ports {
            if port.host < 1024 || port.container == 0 || !used.insert(port.host) {
                return Err(ValidationError::Invalid(
                    "ports (host >= 1024, container > 0, unique host)",
                ));
            }
        }
        if self.environment.0.len() > 128 {
            return Err(ValidationError::Invalid("environment size"));
        }
        for (key, value) in &self.environment.0 {
            if key.is_empty()
                || key.len() > 128
                || !(key.as_bytes()[0].is_ascii_alphabetic() || key.starts_with('_'))
                || !key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
                || value.len() > 8192
                || value.contains('\0')
            {
                return Err(ValidationError::Invalid("environment entry"));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identifiers_reject_flags_paths_and_unicode() {
        for value in [
            "",
            "--privileged",
            "../host",
            "with space",
            "中文",
            "Upper",
            "trailing-",
        ] {
            assert!(Identifier::new(value).is_err());
        }
    }
    #[test]
    fn image_rejects_options_archives_and_bad_digests() {
        for value in [
            "-v",
            "oci-archive:///tmp/x",
            "https://host/image",
            "x@sha256:abc",
            "x\0y",
        ] {
            assert!(ImageRef::new(value).is_err());
        }
        assert!(ImageRef::new("registry.example:5000/team/api:v1").is_ok());
        assert!(ImageRef::new(format!("team/api@sha256:{}", "a".repeat(64))).is_ok());
    }
    #[test]
    fn names_cannot_collide_on_delimiters() {
        let key = |p, n| AppKey {
            project: Identifier::new(p).unwrap(),
            name: Identifier::new(n).unwrap(),
        };
        assert_ne!(
            key("a-b", "c").instance_name(),
            key("a", "b-c").instance_name()
        );
    }
    #[test]
    fn debug_hides_environment_values() {
        let env = Environment(BTreeMap::from([("CONFIG".into(), "private-value".into())]));
        assert!(!format!("{env:?}").contains("private-value"));
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
            pull_policy: PullPolicy::Always,
        }
    }
    #[test]
    fn validates_resources_and_environment_before_execution() {
        let mut value = spec();
        value.resources.memory_mib = 0;
        assert!(value.validate().is_err());
        value = spec();
        value.resources.cpu_millis = 0;
        assert!(value.validate().is_err());
        value = spec();
        value.resources.pids = 0;
        assert!(value.validate().is_err());
        value = spec();
        value.environment.0.insert("BAD=KEY".into(), "value".into());
        assert!(value.validate().is_err());
        value = spec();
        value.args.push("embedded\0nul".into());
        assert!(value.validate().is_err());
    }
    #[test]
    fn debug_hides_application_arguments() {
        let mut value = spec();
        value.args.push("private-argument".into());
        assert!(!format!("{value:?}").contains("private-argument"));
    }
    #[test]
    fn ports_are_opt_in_unique_and_not_privileged() {
        let mut value = spec();
        value.ports.push(LocalPort {
            host: 18080,
            container: 8080,
        });
        assert!(value.validate().is_err());
        value.network = NetworkMode::Bridge;
        assert!(value.validate().is_ok());
        value.ports.push(LocalPort {
            host: 18080,
            container: 9090,
        });
        assert!(value.validate().is_err());
        value.ports = vec![LocalPort {
            host: 80,
            container: 8080,
        }];
        assert!(value.validate().is_err());
    }
    #[test]
    fn fingerprint_includes_configuration_and_is_deterministic() {
        let mut value = spec();
        let before = value.fingerprint().unwrap();
        assert_eq!(before, value.fingerprint().unwrap());
        value.resources.memory_mib += 1;
        assert_ne!(before, value.fingerprint().unwrap());
    }
    #[test]
    fn mounts_are_bounded_and_legacy_fingerprints_are_unchanged() {
        let mut value = spec();
        let before = value.fingerprint().unwrap();
        let old_json = serde_json::to_value(&value).unwrap();
        assert!(
            old_json.get("user").is_none()
                && old_json.get("volumes").is_none()
                && old_json.get("secrets").is_none()
        );
        let old: AppSpec = serde_json::from_value(old_json).unwrap();
        assert_eq!(before, old.fingerprint().unwrap());
        for path in [
            "/",
            "/etc",
            "/run/secrets",
            "/data/../etc",
            "/data,a=b",
            "/data//x",
        ] {
            value.volumes = vec![VolumeMount {
                name: Identifier::new("data").unwrap(),
                target: path.into(),
                read_only: false,
            }];
            assert!(value.validate().is_err());
        }
        value.volumes = vec![VolumeMount {
            name: Identifier::new("data").unwrap(),
            target: "/data".into(),
            read_only: false,
        }];
        assert_ne!(before, value.fingerprint().unwrap());
        value.volumes.push(VolumeMount {
            name: Identifier::new("other").unwrap(),
            target: "/data/inside".into(),
            read_only: false,
        });
        assert!(value.validate().is_err());
    }
    #[test]
    fn health_configuration_rejects_urls_headers_and_non_project_targets() {
        for path in [
            "http://169.254.169.254/",
            "//metadata/",
            "/health?url=http://host",
            "/ok\r\nHost: evil",
            "/a b",
        ] {
            assert!(
                HttpHealthCheck {
                    port: 8080,
                    path: path.into()
                }
                .validate()
                .is_err()
            );
        }
        assert!(
            HttpHealthCheck {
                port: 0,
                path: "/health".into()
            }
            .validate()
            .is_err()
        );
        assert!(
            HttpHealthCheck {
                port: 8080,
                path: "/ready/live".into()
            }
            .validate()
            .is_ok()
        );
    }
}
