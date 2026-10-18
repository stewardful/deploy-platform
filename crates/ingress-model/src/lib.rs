//! Portable publication policy and proxy port. No runtime, HTTP or filesystem.
use app_model::AppKey;
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::net::{Ipv4Addr, SocketAddrV4};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct DomainName(String);
impl DomainName {
    pub fn new(value: impl Into<String>) -> Result<Self, &'static str> {
        let value = value.into();
        // Exact ASCII DNS names only. IDNs must be supplied as punycode.
        let labels: Vec<_> = value.split('.').collect();
        if value.len() > 253
            || labels.len() < 2
            || value != value.to_ascii_lowercase()
            || value.parse::<Ipv4Addr>().is_ok()
            || labels.iter().any(|s| {
                s.is_empty()
                    || s.len() > 63
                    || s.starts_with('-')
                    || s.ends_with('-')
                    || !s
                        .bytes()
                        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-')
            })
            || labels.last().is_none_or(|s| {
                s.len() < 2 || !(s.starts_with("xn--") || s.bytes().all(|c| c.is_ascii_lowercase()))
            })
            || [
                "localhost",
                "local",
                "internal",
                "invalid",
                "example",
                "test",
            ]
            .iter()
            .any(|suffix| labels.last() == Some(suffix))
        {
            return Err(
                "exact public lowercase DNS name required; no URL, IP, wildcard or private suffix",
            );
        }
        Ok(Self(value))
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}
impl TryFrom<String> for DomainName {
    type Error = &'static str;
    fn try_from(s: String) -> Result<Self, Self::Error> {
        Self::new(s)
    }
}
impl From<DomainName> for String {
    fn from(s: DomainName) -> Self {
        s.0
    }
}
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TlsMode {
    #[default]
    Production,
    Staging,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DomainRoute {
    pub domain: DomainName,
    pub app: AppKey,
    pub port: u16,
    #[serde(default)]
    pub tls: TlsMode,
    #[serde(default = "enabled")]
    pub enabled: bool,
}
fn enabled() -> bool {
    true
}
impl DomainRoute {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.port == 0 {
            Err("container port must be 1..65535")
        } else {
            Ok(())
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RouteChange {
    pub route: DomainRoute,
    pub expected_revision: i64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RouteReceipt {
    pub revision: i64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RouteStatus {
    pub route: DomainRoute,
    pub revision: i64,
    pub proxy_state: String,
    pub upstream_ready: bool,
    pub checked_at_ms: u64,
    pub error_code: Option<String>,
}
#[derive(Debug, Clone)]
pub struct ResolvedRoute {
    pub route: DomainRoute,
    pub upstream: Option<SocketAddrV4>,
}
#[derive(Debug, thiserror::Error)]
#[error("proxy configuration unavailable; previous configuration may still be active")]
pub struct ProxyError;
#[async_trait]
pub trait Proxy: Send + Sync {
    /// Reconcile complete managed config, including after proxy-only restart.
    /// Must not silently replace unrelated proxy instances/configurations.
    async fn reconcile(&self, routes: &[ResolvedRoute]) -> Result<(), ProxyError>;
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_injection_private_names_and_wildcards() {
        for s in [
            "https://example.com",
            "a.com:443",
            "a.com/path",
            "*.example.com",
            "a.com\n}",
            "127.0.0.1",
            "a.local",
            "a.test",
            "Example.com",
            "-bad.example.com",
            "bad..com",
        ] {
            assert!(DomainName::new(s).is_err(), "{s}");
        }
        assert!(DomainName::new("api.xn--fiqs8s.com").is_ok());
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IngressSnapshot {
    pub routes: Vec<RouteStatus>,
    pub revision: i64,
    pub applied_revision: i64,
    pub checked_at_ms: u64,
    pub error_code: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RouteDelete {
    pub domain: DomainName,
    pub expected_revision: i64,
}
