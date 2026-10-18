use crate::{CommandExecutor, NerdctlRuntime, network::network_name};
use app_model::{AppSpec, NetworkMode};
use bytes::Bytes;
use http_body_util::Empty;
use hyper_util::rt::TokioIo;
use runtime_api::{ContainerRuntime, ContainerState, Diagnostics, Result, RuntimeError};
use serde_json::Value;
use std::{
    net::{Ipv4Addr, SocketAddr},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

struct Driver(tokio::task::JoinHandle<()>);
impl Drop for Driver {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// No DNS, proxy, redirects, arbitrary host, response body, credentials or shell.
async fn probe(address: SocketAddr, path: &str) -> Option<u16> {
    tokio::time::timeout(Duration::from_secs(2), async {
        let socket = tokio::net::TcpStream::connect(address).await.ok()?;
        let (mut sender, connection) = hyper::client::conn::http1::Builder::new()
            .max_headers(32)
            .max_buf_size(8192)
            .handshake(TokioIo::new(socket))
            .await
            .ok()?;
        let _driver = Driver(tokio::spawn(async move {
            let _ = connection.await;
        }));
        let request = hyper::Request::builder()
            .uri(path)
            .header("host", address.to_string())
            .header("connection", "close")
            .body(Empty::<Bytes>::new())
            .ok()?;
        let response = sender.send_request(request).await.ok()?;
        Some(response.status().as_u16())
    })
    .await
    .ok()
    .flatten()
}
impl<E: CommandExecutor> NerdctlRuntime<E> {
    pub(crate) async fn owned_address(&self, spec: &AppSpec) -> Result<Option<Ipv4Addr>> {
        spec.validate()?;
        if spec.network != NetworkMode::Project {
            return Err(RuntimeError::Configuration(
                "publication requires project network",
            ));
        }
        let status = self.inspect(&spec.app).await?;
        if status.spec_fingerprint.as_deref() != Some(spec.fingerprint()?.as_str()) {
            return Err(RuntimeError::Ownership);
        }
        if status.state != ContainerState::Running {
            return Ok(None);
        }
        self.verify_project_network(&spec.app.project).await?;
        let output = self
            .execute(
                "publication-target",
                vec!["inspect".into(), "--".into(), status.id.clone().into()],
            )
            .await?;
        let entries: Vec<Value> = serde_json::from_slice(&output.stdout)
            .map_err(|_| RuntimeError::Protocol("publication target"))?;
        let raw = entries
            .first()
            .filter(|_| entries.len() == 1)
            .ok_or(RuntimeError::Protocol("publication target"))?;
        if raw["Id"] != status.id {
            return Err(RuntimeError::Ownership);
        }
        let network = network_name(&spec.app.project);
        let ip = raw["NetworkSettings"]["Networks"][&network]["IPAddress"]
            .as_str()
            .and_then(|s| s.parse::<Ipv4Addr>().ok())
            .filter(|ip| ip.is_private() && !ip.is_loopback() && !ip.is_unspecified())
            .ok_or(RuntimeError::Protocol("publication address"))?;
        Ok(Some(ip))
    }
    pub(crate) async fn read_diagnostics(&self, spec: &AppSpec) -> Result<Diagnostics> {
        spec.validate()?;
        let status = self.inspect(&spec.app).await?;
        if status.spec_fingerprint.as_deref() != Some(spec.fingerprint()?.as_str()) {
            return Err(RuntimeError::Ownership);
        }
        let project = spec.network == NetworkMode::Project;
        let mut result = Diagnostics {
            state: status.state,
            network: match spec.network {
                NetworkMode::None => None,
                NetworkMode::Bridge => Some("bridge".into()),
                NetworkMode::Project => Some(network_name(&spec.app.project)),
            },
            service_name: project.then(|| spec.app.name.as_str().to_owned()),
            health: "not_configured".into(),
            http_status: None,
            checked_at_ms: 0,
        };
        if let Some(check) = &spec.health_check {
            result.health = "not_running".into();
            if status.state == ContainerState::Running {
                let ip = self
                    .owned_address(spec)
                    .await?
                    .ok_or(RuntimeError::Protocol("publication address"))?;
                result.http_status = probe(SocketAddr::from((ip, check.port)), &check.path).await;
                result.health = if result.http_status.is_some_and(|c| (200..300).contains(&c)) {
                    "healthy"
                } else {
                    "unhealthy"
                }
                .into();
            }
        }
        result.checked_at_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .min(u64::MAX as u128) as u64;
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    #[tokio::test]
    async fn redirects_are_reported_without_following_or_reading_body() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut input = [0; 2048];
            let _ = stream.read(&mut input).await;
            stream.write_all(b"HTTP/1.1 302 Found\r\nLocation: http://169.254.169.254/\r\nContent-Length: 999999\r\n\r\n").await.unwrap();
        });
        assert_eq!(probe(address, "/ready").await, Some(302));
        server.await.unwrap();
    }
}
