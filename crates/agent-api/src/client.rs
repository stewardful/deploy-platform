use crate::{
    ApiError, Application, Health, Job, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, Operation,
    Submission,
};
use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::{Method, Request};
use hyper_util::rt::TokioIo;
use serde::{Serialize, de::DeserializeOwned};
use std::{
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use thiserror::Error;
use tokio::net::UnixStream;

#[derive(Debug, Error)]
pub enum ClientError {
    #[error("cannot reach agentd Unix Socket; verify daemon status and socket permissions")]
    Transport,
    #[error("agent API request timed out; a submitted job may still have been accepted")]
    Timeout,
    #[error("invalid or oversized agent API response")]
    Protocol,
    #[error(transparent)]
    Api(#[from] ApiError),
    #[error("wait timed out; job {0} continues on agentd, query it before retrying")]
    WaitTimeout(i64),
}
struct Driver(tokio::task::JoinHandle<()>);
impl Drop for Driver {
    fn drop(&mut self) {
        self.0.abort();
    }
}

#[derive(Clone)]
pub struct Client {
    socket: PathBuf,
    timeout: Duration,
}
impl Client {
    pub fn new(socket: impl AsRef<Path>, timeout: Duration) -> Result<Self, ClientError> {
        if !socket.as_ref().is_absolute() || timeout.is_zero() {
            return Err(ClientError::Protocol);
        }
        Ok(Self {
            socket: socket.as_ref().into(),
            timeout,
        })
    }
    pub async fn maintenance(
        &self,
        draining: Option<bool>,
    ) -> Result<crate::Maintenance, ClientError> {
        let (method, path) = match draining {
            Some(true) => (Method::POST, "/v1/maintenance/enter"),
            Some(false) => (Method::POST, "/v1/maintenance/exit"),
            None => (Method::GET, "/v1/maintenance"),
        };
        self.request(method, path, None::<&()>).await
    }
    pub async fn create_secret(
        &self,
        input: &crate::SecretInput,
    ) -> Result<crate::SecretInfo, ClientError> {
        input.validate()?;
        self.request(Method::POST, "/v1/secrets", Some(input)).await
    }
    pub async fn secrets(
        &self,
        project: &app_model::Identifier,
    ) -> Result<Vec<crate::SecretInfo>, ClientError> {
        self.request(
            Method::GET,
            &format!("/v1/secrets/{}", project.as_str()),
            None::<&()>,
        )
        .await
    }
    pub async fn volumes(
        &self,
        app: &app_model::AppKey,
    ) -> Result<Vec<crate::VolumeInfo>, ClientError> {
        self.request(
            Method::GET,
            &format!("/v1/volumes/{}/{}", app.project.as_str(), app.name.as_str()),
            None::<&()>,
        )
        .await
    }
    pub async fn diagnostics(
        &self,
        app: &app_model::AppKey,
    ) -> Result<crate::Diagnostics, ClientError> {
        self.request(
            Method::GET,
            &format!(
                "/v1/diagnostics/{}/{}",
                app.project.as_str(),
                app.name.as_str()
            ),
            None::<&()>,
        )
        .await
    }
    pub async fn logs(
        &self,
        app: &app_model::AppKey,
        tail: u16,
    ) -> Result<crate::LogSnapshot, ClientError> {
        self.request(
            Method::GET,
            &format!(
                "/v1/logs/{}/{}?tail={tail}",
                app.project.as_str(),
                app.name.as_str()
            ),
            None::<&()>,
        )
        .await
    }
    pub async fn routes(&self) -> Result<ingress_model::IngressSnapshot, ClientError> {
        self.request(Method::GET, "/v1/ingress", None::<&()>).await
    }
    pub async fn change_route(
        &self,
        input: &ingress_model::RouteChange,
    ) -> Result<ingress_model::RouteReceipt, ClientError> {
        self.request(Method::POST, "/v1/ingress", Some(input)).await
    }
    pub async fn delete_route(
        &self,
        input: &ingress_model::RouteDelete,
    ) -> Result<ingress_model::RouteReceipt, ClientError> {
        self.request(Method::POST, "/v1/ingress/delete", Some(input))
            .await
    }
    pub async fn health(&self) -> Result<Health, ClientError> {
        self.request(Method::GET, "/v1/health", None::<&()>).await
    }
    pub async fn applications(&self) -> Result<Vec<Application>, ClientError> {
        self.request(Method::GET, "/v1/apps", None::<&()>).await
    }
    pub async fn operations(&self, limit: u16) -> Result<Vec<Operation>, ClientError> {
        self.request(
            Method::GET,
            &format!("/v1/operations?limit={limit}"),
            None::<&()>,
        )
        .await
    }
    pub async fn submit(&self, request: &Submission) -> Result<Job, ClientError> {
        request.validate()?;
        self.request(Method::POST, "/v1/jobs", Some(request)).await
    }
    pub async fn job(&self, id: i64) -> Result<Job, ClientError> {
        self.request(Method::GET, &format!("/v1/jobs/{id}"), None::<&()>)
            .await
    }
    pub async fn wait(&self, id: i64, maximum: Duration) -> Result<Job, ClientError> {
        tokio::time::timeout(maximum, async {
            loop {
                let job = self.job(id).await?;
                if job.terminal() {
                    return Ok(job);
                }
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
        })
        .await
        .map_err(|_| ClientError::WaitTimeout(id))?
    }
    async fn request<T: DeserializeOwned, B: Serialize>(
        &self,
        method: Method,
        path: &str,
        body: Option<&B>,
    ) -> Result<T, ClientError> {
        let bytes = body
            .map(serde_json::to_vec)
            .transpose()
            .map_err(|_| ClientError::Protocol)?
            .unwrap_or_default();
        if bytes.len() > MAX_REQUEST_BYTES {
            return Err(ClientError::Protocol);
        }
        tokio::time::timeout(self.timeout, async {
            let stream = UnixStream::connect(&self.socket)
                .await
                .map_err(|_| ClientError::Transport)?;
            let (mut sender, connection) =
                hyper::client::conn::http1::handshake(TokioIo::new(stream))
                    .await
                    .map_err(|_| ClientError::Transport)?;
            let _driver = Driver(tokio::spawn(async move {
                let _ = connection.await;
            }));
            let request = Request::builder()
                .method(method)
                .uri(path)
                .header("host", "localhost")
                .header("content-type", "application/json")
                .header("connection", "close")
                .body(Full::new(Bytes::from(bytes)))
                .map_err(|_| ClientError::Protocol)?;
            let response = sender
                .send_request(request)
                .await
                .map_err(|_| ClientError::Transport)?;
            let status = response.status();
            if !response
                .headers()
                .get("content-type")
                .and_then(|v| v.to_str().ok())
                .is_some_and(|v| {
                    v.split(';')
                        .next()
                        .is_some_and(|m| m.trim().eq_ignore_ascii_case("application/json"))
                })
            {
                return Err(ClientError::Protocol);
            }
            let data = Limited::new(response.into_body(), MAX_RESPONSE_BYTES)
                .collect()
                .await
                .map_err(|_| ClientError::Protocol)?
                .to_bytes();
            if !status.is_success() {
                return Err(ClientError::Api(
                    serde_json::from_slice(&data).map_err(|_| ClientError::Protocol)?,
                ));
            }
            serde_json::from_slice(&data).map_err(|_| ClientError::Protocol)
        })
        .await
        .map_err(|_| ClientError::Timeout)?
    }
}

/// Uniqueness token, not an authentication credential. Reuse it to retry an uncertain submission.
pub fn request_id() -> String {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    format!(
        "{}-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    )
}
