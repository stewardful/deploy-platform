use crate::{AgentApi, ApiError, ErrorCode, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, Submission};
use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::{Method, Request, Response, StatusCode, body::Incoming};
use hyper_util::rt::{TokioIo, TokioTimer};
use std::{
    convert::Infallible,
    fs::{File, OpenOptions},
    io,
    os::unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::{
    net::{UnixListener, UnixStream},
    sync::Semaphore,
    task::JoinSet,
};
use tokio_util::sync::CancellationToken;

type Reply = Response<Full<Bytes>>;
pub struct BoundServer {
    listener: UnixListener,
    _guard: SocketGuard,
}
struct SocketGuard {
    path: PathBuf,
    device: u64,
    inode: u64,
    _lock: nix::fcntl::Flock<File>,
}
impl Drop for SocketGuard {
    fn drop(&mut self) {
        if let Ok(meta) = std::fs::symlink_metadata(&self.path) {
            if meta.file_type().is_socket() && meta.dev() == self.device && meta.ino() == self.inode
            {
                let _ = std::fs::remove_file(&self.path);
            }
        }
    }
}
impl BoundServer {
    pub async fn bind(path: &Path) -> io::Result<Self> {
        if !path.is_absolute() {
            return Err(io::Error::other("socket path must be absolute"));
        }
        let directory = path
            .parent()
            .ok_or_else(|| io::Error::other("socket parent missing"))?;
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(directory)?;
        let parent = std::fs::symlink_metadata(directory)?;
        if !parent.is_dir() || parent.permissions().mode() & 0o077 != 0 {
            return Err(io::Error::other(
                "socket parent must be a private directory (0700)",
            ));
        }
        let mut lock_path = path.as_os_str().to_os_string();
        lock_path.push(".lock");
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_CLOEXEC)
            .open(PathBuf::from(lock_path))?;
        if !lock.metadata()?.is_file() {
            return Err(io::Error::other("socket lease must be a regular file"));
        }
        let lock = nix::fcntl::Flock::lock(lock, nix::fcntl::FlockArg::LockExclusiveNonblock)
            .map_err(|_| io::Error::other("socket already leased"))?;
        match std::fs::symlink_metadata(path) {
            Ok(meta) => {
                if !meta.file_type().is_socket() {
                    return Err(io::Error::other("refusing to replace non-socket path"));
                }
                match tokio::time::timeout(Duration::from_millis(250), UnixStream::connect(path))
                    .await
                {
                    Ok(Err(error)) if error.kind() == io::ErrorKind::ConnectionRefused => {
                        std::fs::remove_file(path)?
                    }
                    _ => {
                        return Err(io::Error::other(
                            "socket is active or cannot be safely replaced",
                        ));
                    }
                }
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let listener = UnixListener::bind(path)?;
        let meta = std::fs::symlink_metadata(path)?;
        let guard = SocketGuard {
            path: path.into(),
            device: meta.dev(),
            inode: meta.ino(),
            _lock: lock,
        };
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
        Ok(Self {
            listener,
            _guard: guard,
        })
    }
    pub async fn serve(
        self,
        api: Arc<dyn AgentApi>,
        shutdown: CancellationToken,
    ) -> io::Result<()> {
        let slots = Arc::new(Semaphore::new(32));
        let mut connections = JoinSet::new();
        let result = loop {
            tokio::select! {
                biased;
                _ = shutdown.cancelled() => break Ok(()),
                _ = connections.join_next(), if !connections.is_empty() => {},
                accepted = self.listener.accept() => {
                    let (stream, _) = match accepted { Ok(value) => value, Err(error) => break Err(error) };
                    let Ok(slot) = slots.clone().try_acquire_owned() else { drop(stream); continue };
                    let api = api.clone();
                    connections.spawn(async move {
                        let _slot = slot;
                        let service = hyper::service::service_fn(move |request| {
                            let api = api.clone();
                            async move {
                                let reply = tokio::time::timeout(Duration::from_secs(5), route(request,api)).await.unwrap_or_else(|_| failure(StatusCode::SERVICE_UNAVAILABLE,ApiError::new(ErrorCode::Unavailable,"API request deadline exceeded")));
                                Ok::<_,Infallible>(reply)
                            }
                        });
                        let mut http = hyper::server::conn::http1::Builder::new();
                        http.keep_alive(false).max_headers(32).max_buf_size(16384).timer(TokioTimer::new()).header_read_timeout(Duration::from_secs(5));
                        let _ = http.serve_connection(TokioIo::new(stream),service).await;
                    });
                }
            }
        };
        connections.abort_all();
        while connections.join_next().await.is_some() {}
        result
    }
}

fn json<T: serde::Serialize>(status: StatusCode, value: &T) -> Reply {
    let body = serde_json::to_vec(value)
        .ok()
        .filter(|b| b.len() <= MAX_RESPONSE_BYTES);
    match body {
        Some(body) => Response::builder()
            .status(status)
            .header("content-type", "application/json")
            .header("connection", "close")
            .body(Full::new(Bytes::from(body)))
            .expect("static response headers"),
        None => failure(StatusCode::INTERNAL_SERVER_ERROR, ApiError::internal()),
    }
}
fn failure(status: StatusCode, error: ApiError) -> Reply {
    json(status, &error)
}
fn error(error: ApiError) -> Reply {
    let status = match error.code {
        ErrorCode::BadRequest => StatusCode::BAD_REQUEST,
        ErrorCode::NotFound => StatusCode::NOT_FOUND,
        ErrorCode::Conflict => StatusCode::CONFLICT,
        ErrorCode::QueueFull => StatusCode::TOO_MANY_REQUESTS,
        ErrorCode::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
        ErrorCode::Internal => StatusCode::INTERNAL_SERVER_ERROR,
    };
    failure(status, error)
}
async fn route(request: Request<Incoming>, api: Arc<dyn AgentApi>) -> Reply {
    let path = request.uri().path().to_owned();
    match (request.method(), path.as_str()) {
        (&Method::GET, "/v1/maintenance")
        | (&Method::POST, "/v1/maintenance/enter" | "/v1/maintenance/exit") => {
            if request.uri().query().is_some() {
                return error(ApiError::new(
                    ErrorCode::BadRequest,
                    "unexpected maintenance query",
                ));
            }
            let draining = match path.as_str() {
                "/v1/maintenance/enter" => Some(true),
                "/v1/maintenance/exit" => Some(false),
                _ => None,
            };
            match api.maintenance(draining).await {
                Ok(v) => json(StatusCode::OK, &v),
                Err(e) => error(e),
            }
        }
        (&Method::GET, "/v1/ingress") => match api.routes().await {
            Ok(v) => json(StatusCode::OK, &v),
            Err(e) => error(e),
        },
        (&Method::POST, "/v1/ingress" | "/v1/ingress/delete") => {
            if !request
                .headers()
                .get("content-type")
                .and_then(|h| h.to_str().ok())
                .is_some_and(|v| {
                    v.split(';')
                        .next()
                        .is_some_and(|v| v.trim().eq_ignore_ascii_case("application/json"))
                })
            {
                return error(ApiError::new(
                    ErrorCode::BadRequest,
                    "Content-Type must be application/json",
                ));
            }
            let remove = path.ends_with("/delete");
            let body = match Limited::new(request.into_body(), 16384).collect().await {
                Ok(b) => b.to_bytes(),
                Err(_) => {
                    return error(ApiError::new(
                        ErrorCode::BadRequest,
                        "invalid or oversized ingress request",
                    ));
                }
            };
            let result = if remove {
                match serde_json::from_slice::<ingress_model::RouteDelete>(&body) {
                    Ok(v) => api.delete_route(v.domain, v.expected_revision).await,
                    Err(_) => Err(ApiError::new(
                        ErrorCode::BadRequest,
                        "invalid domain deletion",
                    )),
                }
            } else {
                match serde_json::from_slice::<ingress_model::RouteChange>(&body) {
                    Ok(v) => api.change_route(v).await,
                    Err(_) => Err(ApiError::new(
                        ErrorCode::BadRequest,
                        "invalid domain binding",
                    )),
                }
            };
            match result {
                Ok(v) => json(StatusCode::ACCEPTED, &v),
                Err(e) => error(e),
            }
        }
        (&Method::GET, "/v1/health") => match api.health().await {
            Ok(health) if health.accepting => json(StatusCode::OK, &health),
            Ok(_) => error(ApiError::new(
                ErrorCode::Unavailable,
                "agent worker is unavailable",
            )),
            Err(e) => error(e),
        },
        (&Method::GET, "/v1/apps") => match api.applications().await {
            Ok(apps) => json(StatusCode::OK, &apps),
            Err(e) => error(e),
        },
        (&Method::GET, "/v1/operations") => {
            let limit = match request.uri().query() {
                None => 20,
                Some(query) => match query
                    .strip_prefix("limit=")
                    .filter(|v| !v.contains('&'))
                    .and_then(|v| v.parse::<u16>().ok())
                    .filter(|v| (1..=1000).contains(v))
                {
                    Some(limit) => limit,
                    None => return error(ApiError::new(ErrorCode::BadRequest, "invalid limit")),
                },
            };
            match api.operations(limit).await {
                Ok(items) => json(StatusCode::OK, &items),
                Err(e) => error(e),
            }
        }
        (&Method::POST, "/v1/secrets") => {
            if !request
                .headers()
                .get("content-type")
                .and_then(|h| h.to_str().ok())
                .is_some_and(|v| {
                    v.split(';')
                        .next()
                        .is_some_and(|v| v.trim().eq_ignore_ascii_case("application/json"))
                })
            {
                return failure(
                    StatusCode::UNSUPPORTED_MEDIA_TYPE,
                    ApiError::new(
                        ErrorCode::BadRequest,
                        "Content-Type must be application/json",
                    ),
                );
            }
            let body = match Limited::new(request.into_body(), MAX_REQUEST_BYTES)
                .collect()
                .await
            {
                Ok(body) => body.to_bytes(),
                Err(_) => {
                    return failure(
                        StatusCode::PAYLOAD_TOO_LARGE,
                        ApiError::new(ErrorCode::BadRequest, "invalid or oversized request body"),
                    );
                }
            };
            let input: crate::SecretInput = match serde_json::from_slice(&body) {
                Ok(v) => v,
                Err(_) => {
                    return error(ApiError::new(
                        ErrorCode::BadRequest,
                        "invalid secret request",
                    ));
                }
            };
            match api.create_secret(input).await {
                Ok(v) => json(StatusCode::ACCEPTED, &v),
                Err(e) => error(e),
            }
        }
        (&Method::GET, path) if path.starts_with("/v1/secrets/") => {
            let project = match app_model::Identifier::new(path.trim_start_matches("/v1/secrets/"))
            {
                Ok(v) => v,
                Err(_) => return error(ApiError::new(ErrorCode::BadRequest, "invalid project")),
            };
            match api.secrets(project).await {
                Ok(v) => json(StatusCode::OK, &v),
                Err(e) => error(e),
            }
        }
        (&Method::GET, path) if path.starts_with("/v1/volumes/") => {
            let parts: Vec<_> = path.trim_start_matches("/v1/volumes/").split('/').collect();
            if parts.len() != 2 {
                return error(ApiError::new(ErrorCode::BadRequest, "invalid application"));
            }
            let app = match (
                app_model::Identifier::new(parts[0]),
                app_model::Identifier::new(parts[1]),
            ) {
                (Ok(project), Ok(name)) => app_model::AppKey { project, name },
                _ => return error(ApiError::new(ErrorCode::BadRequest, "invalid application")),
            };
            match api.volumes(app).await {
                Ok(v) => json(StatusCode::OK, &v),
                Err(e) => error(e),
            }
        }
        (&Method::GET, path)
            if path.starts_with("/v1/diagnostics/") || path.starts_with("/v1/logs/") =>
        {
            let logs = path.starts_with("/v1/logs/");
            let parts: Vec<_> = path.split('/').collect();
            if parts.len() != 5 {
                return error(ApiError::new(ErrorCode::BadRequest, "invalid application"));
            }
            let app = match (
                app_model::Identifier::new(parts[3]),
                app_model::Identifier::new(parts[4]),
            ) {
                (Ok(project), Ok(name)) => app_model::AppKey { project, name },
                _ => return error(ApiError::new(ErrorCode::BadRequest, "invalid application")),
            };
            if logs {
                let tail = match request.uri().query() {
                    None => 100,
                    Some(q) => match q
                        .strip_prefix("tail=")
                        .and_then(|n| n.parse::<u16>().ok())
                        .filter(|n| (1..=500).contains(n))
                    {
                        Some(v) => v,
                        None => return error(ApiError::new(ErrorCode::BadRequest, "invalid tail")),
                    },
                };
                match api.logs(app, tail).await {
                    Ok(v) => json(StatusCode::OK, &v),
                    Err(e) => error(e),
                }
            } else {
                if request.uri().query().is_some() {
                    return error(ApiError::new(ErrorCode::BadRequest, "unexpected query"));
                }
                match api.diagnostics(app).await {
                    Ok(v) => json(StatusCode::OK, &v),
                    Err(e) => error(e),
                }
            }
        }
        (&Method::POST, "/v1/jobs") => {
            if request
                .headers()
                .get("content-length")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.parse::<u64>().ok())
                .is_some_and(|v| v > MAX_REQUEST_BYTES as u64)
            {
                return failure(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    ApiError::new(ErrorCode::BadRequest, "request body exceeds limit"),
                );
            }
            if !request
                .headers()
                .get("content-type")
                .and_then(|h| h.to_str().ok())
                .is_some_and(|h| {
                    h.split(';')
                        .next()
                        .is_some_and(|v| v.trim().eq_ignore_ascii_case("application/json"))
                })
            {
                return failure(
                    StatusCode::UNSUPPORTED_MEDIA_TYPE,
                    ApiError::new(
                        ErrorCode::BadRequest,
                        "Content-Type must be application/json",
                    ),
                );
            }
            let body = match Limited::new(request.into_body(), MAX_REQUEST_BYTES)
                .collect()
                .await
            {
                Ok(body) => body.to_bytes(),
                Err(_) => {
                    return failure(
                        StatusCode::PAYLOAD_TOO_LARGE,
                        ApiError::new(ErrorCode::BadRequest, "invalid or oversized request body"),
                    );
                }
            };
            let submission: Submission = match serde_json::from_slice(&body) {
                Ok(value) => value,
                Err(_) => {
                    return error(ApiError::new(ErrorCode::BadRequest, "invalid JSON request"));
                }
            };
            match submission.validate() {
                Ok(()) => {}
                Err(e) => return error(e),
            }
            match api.submit(submission).await {
                Ok(job) => json(StatusCode::ACCEPTED, &job),
                Err(e) => error(e),
            }
        }
        (&Method::GET, other) if other.starts_with("/v1/jobs/") => {
            let id = match other
                .strip_prefix("/v1/jobs/")
                .and_then(|v| v.parse::<i64>().ok())
                .filter(|v| *v > 0)
            {
                Some(id) => id,
                None => return error(ApiError::new(ErrorCode::BadRequest, "invalid job id")),
            };
            match api.job(id).await {
                Ok(job) => json(StatusCode::OK, &job),
                Err(e) => error(e),
            }
        }
        _ => error(ApiError::new(ErrorCode::NotFound, "endpoint not found")),
    }
}
