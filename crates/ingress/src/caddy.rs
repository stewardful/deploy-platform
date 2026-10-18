//! Generated JSON and private Unix-socket adapter for one dedicated Caddy instance.
use async_trait::async_trait;
use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::{Method, Request};
use hyper_util::rt::TokioIo;
use ingress_model::{Proxy, ProxyError, ResolvedRoute, TlsMode};
use serde_json::{Value, json};
use std::{
    os::unix::fs::{FileTypeExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};

pub struct CaddyProxy {
    socket: PathBuf,
    ca_override: Option<String>,
}
fn template() -> Value {
    serde_json::from_str(include_str!("../bootstrap.json")).expect("compiled bootstrap JSON")
}
fn marker() -> Value {
    template()["logging"]["logs"]["deploy_platform"].clone()
}
fn base(socket: &Path) -> Value {
    let mut config = template();
    config["admin"]["listen"] = format!("unix/{}", socket.display()).into();
    config
}
impl CaddyProxy {
    pub fn new(socket: PathBuf, ca_override: Option<String>) -> Result<Self, ProxyError> {
        if !socket.is_absolute()
            || socket.to_str().is_none()
            || ca_override
                .as_ref()
                .is_some_and(|v| !v.starts_with("http://") && !v.starts_with("https://"))
        {
            return Err(ProxyError);
        }
        Ok(Self {
            socket,
            ca_override,
        })
    }
    pub fn config(&self, routes: &[ResolvedRoute]) -> Result<Value, ProxyError> {
        if routes.len() > 64 {
            return Err(ProxyError);
        }
        let mut config = base(&self.socket);
        if routes.is_empty() {
            return Ok(config);
        }
        let mut handlers = Vec::new();
        let mut production = Vec::new();
        let mut staging = Vec::new();
        for resolved in routes {
            resolved.route.validate().map_err(|_| ProxyError)?;
            if !resolved.route.enabled {
                return Err(ProxyError);
            }
            let domain = resolved.route.domain.as_str();
            match resolved.route.tls {
                TlsMode::Production => production.push(domain),
                TlsMode::Staging => staging.push(domain),
            }
            let handle = if let Some(address) = resolved.upstream {
                if !address.ip().is_private() || address.ip().is_loopback() || address.port() == 0 {
                    return Err(ProxyError);
                }
                json!({"handler":"reverse_proxy","upstreams":[{"dial":address.to_string()}],"transport":{"protocol":"http","dial_timeout":3000000000_u64,"response_header_timeout":30000000000_u64}})
            } else {
                json!({"handler":"static_response","status_code":503,"body":"application unavailable\n"})
            };
            handlers.push(json!({"match":[{"host":[domain]}],"handle":[handle],"terminal":true}));
        }
        handlers.push(json!({"handle":[{"handler":"static_response","status_code":404,"body":"unknown site\n"}],"terminal":true}));
        let mut policies = Vec::new();
        for (subjects, ca) in [
            (production, "https://acme-v02.api.letsencrypt.org/directory"),
            (
                staging,
                "https://acme-staging-v02.api.letsencrypt.org/directory",
            ),
        ] {
            if !subjects.is_empty() {
                policies.push(json!({"subjects":subjects,"issuers":[{"module":"acme","ca":self.ca_override.as_deref().unwrap_or(ca)}]}));
            }
        }
        config["apps"] = json!({"http":{"servers":{"cloud":{"listen":[":443"],"protocols":["h1","h2"],"strict_sni_host":true,"read_header_timeout":10000000000_u64,"idle_timeout":60000000000_u64,"max_header_bytes":16384,"routes":handlers}}},"tls":{"automation":{"policies":policies}}});
        Ok(config)
    }
    async fn request(
        &self,
        method: Method,
        path: &str,
        body: Vec<u8>,
    ) -> Result<Vec<u8>, ProxyError> {
        tokio::time::timeout(Duration::from_secs(3), async {
            let parent = self.socket.parent().ok_or(ProxyError)?;
            let meta = std::fs::symlink_metadata(parent).map_err(|_| ProxyError)?;
            let socket = std::fs::symlink_metadata(&self.socket).map_err(|_| ProxyError)?;
            if !meta.is_dir()
                || meta.permissions().mode() & 0o077 != 0
                || !socket.file_type().is_socket()
            {
                return Err(ProxyError);
            }
            let stream = tokio::net::UnixStream::connect(&self.socket)
                .await
                .map_err(|_| ProxyError)?;
            let (mut sender, connection) =
                hyper::client::conn::http1::handshake(TokioIo::new(stream))
                    .await
                    .map_err(|_| ProxyError)?;
            struct Driver(tokio::task::JoinHandle<()>);
            impl Drop for Driver {
                fn drop(&mut self) {
                    self.0.abort();
                }
            }
            let _driver = Driver(tokio::spawn(async move {
                let _ = connection.await;
            }));
            let request = Request::builder()
                .method(method)
                .uri(path)
                .header("host", "localhost")
                .header("content-type", "application/json")
                .header("connection", "close")
                .body(Full::new(Bytes::from(body)))
                .map_err(|_| ProxyError)?;
            let response = sender.send_request(request).await.map_err(|_| ProxyError)?;
            let status = response.status();
            let body = Limited::new(response.into_body(), 1024 * 1024)
                .collect()
                .await
                .map_err(|_| ProxyError)?
                .to_bytes();
            if status.as_u16() != 200 {
                return Err(ProxyError);
            }
            Ok(body.to_vec())
        })
        .await
        .map_err(|_| ProxyError)?
    }
}
#[async_trait]
impl Proxy for CaddyProxy {
    async fn reconcile(&self, routes: &[ResolvedRoute]) -> Result<(), ProxyError> {
        let desired = self.config(routes)?;
        let current: Value =
            serde_json::from_slice(&self.request(Method::GET, "/config/", vec![]).await?)
                .map_err(|_| ProxyError)?;
        if current["logging"]["logs"]["deploy_platform"] != marker()
            || current["admin"]["listen"] != desired["admin"]["listen"]
        {
            return Err(ProxyError);
        }
        if current != desired {
            self.request(
                Method::POST,
                "/load",
                serde_json::to_vec(&desired).map_err(|_| ProxyError)?,
            )
            .await?;
        }
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use ingress_model::{DomainName, DomainRoute};
    #[test]
    fn config_has_exact_hosts_no_on_demand_and_no_arbitrary_upstreams() {
        let proxy = CaddyProxy::new("/run/cloud/admin.sock".into(), None).unwrap();
        let route: DomainRoute = serde_json::from_value(
            json!({"domain":"web.example.com","app":{"project":"demo","name":"web"},"port":8080}),
        )
        .unwrap();
        let mut routes = vec![ResolvedRoute {
            route,
            upstream: Some("10.4.0.2:8080".parse().unwrap()),
        }];
        let c = proxy.config(&routes).unwrap();
        assert_eq!(
            c["apps"]["http"]["servers"]["cloud"]["routes"][0]["match"][0]["host"][0],
            "web.example.com"
        );
        assert!(!c.to_string().contains("on_demand"));
        assert_eq!(c["admin"]["config"]["persist"], false);
        routes[0].upstream = Some("127.0.0.1:2019".parse().unwrap());
        assert!(proxy.config(&routes).is_err());
        routes[0].upstream = Some("169.254.169.254:80".parse().unwrap());
        assert!(proxy.config(&routes).is_err());
        routes[0].upstream = None;
        routes[0].route.domain = DomainName::new("api.example.com").unwrap();
        assert_eq!(
            proxy.config(&routes).unwrap()["apps"]["http"]["servers"]["cloud"]["routes"][0]["handle"]
                [0]["status_code"],
            503
        );
    }
}
