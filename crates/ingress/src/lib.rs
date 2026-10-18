//! Durable desired ingress state and bounded reconciliation, independent from Caddy.
use agent_api::{AgentApi, ApiError, ErrorCode, Result};
use async_trait::async_trait;
use ingress_model::*;
use rusqlite::{Connection, OpenFlags, OptionalExtension, params};
use std::{
    collections::BTreeMap,
    os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt},
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::{Notify, Semaphore};
use tokio_util::sync::CancellationToken;

struct Store {
    db: Connection,
}
impl Store {
    fn open(path: &Path) -> Result<Self> {
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(path)
            .or_else(|e| {
                if e.kind() == std::io::ErrorKind::AlreadyExists {
                    Ok(())
                } else {
                    Err(e)
                }
            })
            .map_err(|_| ApiError::internal())?;
        let meta = std::fs::symlink_metadata(path).map_err(|_| ApiError::internal())?;
        if !meta.is_dir() || meta.permissions().mode() & 0o077 != 0 {
            return Err(ApiError::internal());
        }
        // Host wiring supplies an already leased private state parent.
        let parent = std::fs::symlink_metadata(path.parent().ok_or_else(ApiError::internal)?)
            .map_err(|_| ApiError::internal())?;
        if !parent.is_dir()
            || parent.uid() != meta.uid()
            || parent.permissions().mode() & 0o077 != 0
        {
            return Err(ApiError::internal());
        }
        let path = std::fs::canonicalize(path).map_err(|_| ApiError::internal())?;
        let db = Connection::open_with_flags(
            path.join("ingress.sqlite"),
            OpenFlags::SQLITE_OPEN_READ_WRITE
                | OpenFlags::SQLITE_OPEN_CREATE
                | OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )
        .map_err(|_| ApiError::internal())?;
        std::fs::set_permissions(
            path.join("ingress.sqlite"),
            std::fs::Permissions::from_mode(0o600),
        )
        .map_err(|_| ApiError::internal())?;
        db.busy_timeout(Duration::from_millis(300))
            .map_err(|_| ApiError::internal())?;
        let version: u32 = db
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .map_err(|_| ApiError::internal())?;
        if version != 0 {
            return Err(ApiError::new(
                ErrorCode::Unavailable,
                "unsupported ingress schema",
            ));
        }
        db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL); INSERT OR IGNORE INTO meta VALUES(1,0); CREATE TABLE IF NOT EXISTS routes(domain TEXT PRIMARY KEY,revision INTEGER NOT NULL,spec TEXT NOT NULL);").map_err(|_|ApiError::internal())?;
        Ok(Self { db })
    }
    fn revision(&self) -> Result<i64> {
        self.db
            .query_row("SELECT revision FROM meta WHERE id=1", [], |r| r.get(0))
            .map_err(|_| ApiError::internal())
    }
    fn rows(&self) -> Result<Vec<(i64, DomainRoute)>> {
        let mut query = self
            .db
            .prepare("SELECT revision,spec FROM routes ORDER BY domain")
            .map_err(|_| ApiError::internal())?;
        let rows = query
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
            .map_err(|_| ApiError::internal())?;
        rows.map(|r| {
            let (revision, json) = r.map_err(|_| ApiError::internal())?;
            let route: DomainRoute =
                serde_json::from_str(&json).map_err(|_| ApiError::internal())?;
            route.validate().map_err(|_| ApiError::internal())?;
            Ok((revision, route))
        })
        .collect()
    }
    fn mutate(
        &mut self,
        domain: &DomainName,
        expected: i64,
        route: Option<&DomainRoute>,
    ) -> Result<RouteReceipt> {
        let tx = self.db.transaction().map_err(|_| ApiError::internal())?;
        let actual: Option<i64> = tx
            .query_row(
                "SELECT revision FROM routes WHERE domain=?",
                [domain.as_str()],
                |r| r.get(0),
            )
            .optional()
            .map_err(|_| ApiError::internal())?;
        if actual.unwrap_or(0) != expected || (actual.is_none() && route.is_none()) {
            return Err(ApiError::new(
                ErrorCode::Conflict,
                "domain changed or removed; refresh before editing",
            ));
        }
        let count: u32 = tx
            .query_row("SELECT count(*) FROM routes", [], |r| r.get(0))
            .map_err(|_| ApiError::internal())?;
        if actual.is_none() && count >= 64 {
            return Err(ApiError::new(
                ErrorCode::QueueFull,
                "maximum 64 domain bindings per node",
            ));
        }
        tx.execute("UPDATE meta SET revision=revision+1 WHERE id=1", [])
            .map_err(|_| ApiError::internal())?;
        let revision = tx
            .query_row("SELECT revision FROM meta WHERE id=1", [], |r| {
                r.get::<_, i64>(0)
            })
            .map_err(|_| ApiError::internal())?;
        if let Some(route) = route {
            let json = serde_json::to_string(route).map_err(|_| ApiError::internal())?;
            tx.execute("INSERT INTO routes(domain,revision,spec) VALUES(?,?,?) ON CONFLICT(domain) DO UPDATE SET revision=excluded.revision,spec=excluded.spec",params![domain.as_str(),revision,json]).map_err(|_|ApiError::internal())?;
        } else {
            tx.execute("DELETE FROM routes WHERE domain=?", [domain.as_str()])
                .map_err(|_| ApiError::internal())?;
        }
        tx.commit().map_err(|_| ApiError::internal())?;
        Ok(RouteReceipt { revision })
    }
}
#[derive(Default)]
struct Observed {
    applied: i64,
    checked: u64,
    error: Option<String>,
    ready: BTreeMap<String, bool>,
}
pub struct IngressService {
    inner: Arc<dyn AgentApi>,
    proxy: Arc<dyn Proxy>,
    store: Mutex<Store>,
    observed: Mutex<Observed>,
    wake: Notify,
    mutation: Semaphore,
}
impl IngressService {
    /// One owner per node; node lease must cover this service's complete lifetime.
    pub fn open(inner: Arc<dyn AgentApi>, proxy: Arc<dyn Proxy>, path: &Path) -> Result<Arc<Self>> {
        Ok(Arc::new(Self {
            inner,
            proxy,
            store: Mutex::new(Store::open(path)?),
            observed: Mutex::new(Observed::default()),
            wake: Notify::new(),
            mutation: Semaphore::new(1),
        }))
    }
    pub async fn run(self: Arc<Self>, shutdown: CancellationToken, interval: Duration) {
        loop {
            tokio::select! {biased; _=shutdown.cancelled()=>return, _=self.reconcile_once()=>{}}
            tokio::select! {biased; _=shutdown.cancelled()=>return,_=self.wake.notified()=>{},_=tokio::time::sleep(interval)=>{}}
        }
    }
    pub async fn reconcile_once(&self) {
        let desired = (|| {
            let store = self.store.lock().map_err(|_| ApiError::internal())?;
            Ok::<_, ApiError>((store.revision()?, store.rows()?))
        })();
        let Ok((revision, rows)) = desired else {
            return;
        };
        let mut targets = BTreeMap::new();
        let mut ready = BTreeMap::new();
        let mut routes = Vec::new();
        for (_, route) in rows {
            if !route.enabled {
                continue;
            }
            let key = route.app.instance_name();
            if !targets.contains_key(&key) {
                let target = tokio::time::timeout(
                    Duration::from_secs(3),
                    self.inner.publication_target(route.app.clone()),
                )
                .await
                .ok()
                .and_then(std::result::Result::ok)
                .flatten();
                targets.insert(key.clone(), target);
            }
            let target = targets[&key].map(|ip| std::net::SocketAddrV4::new(ip, route.port));
            ready.insert(route.domain.as_str().to_owned(), target.is_some());
            routes.push(ResolvedRoute {
                route,
                upstream: target,
            });
        }
        let outcome = self.proxy.reconcile(&routes).await;
        if let Ok(mut observed) = self.observed.lock() {
            observed.checked = now();
            observed.ready = ready;
            if outcome.is_ok() {
                observed.applied = revision;
                observed.error = None;
            } else {
                observed.error = Some("proxy_unavailable".into());
            }
        }
    }
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(u64::MAX as u128) as u64
}
#[async_trait]
impl AgentApi for IngressService {
    fn acquire_write(&self) -> agent_api::Result<agent_api::WritePermit> {
        self.inner.acquire_write()
    }
    async fn maintenance(
        &self,
        draining: Option<bool>,
    ) -> agent_api::Result<agent_api::Maintenance> {
        self.inner.maintenance(draining).await
    }

    async fn routes(&self) -> Result<IngressSnapshot> {
        let store = self.store.lock().map_err(|_| ApiError::internal())?;
        let revision = store.revision()?;
        let observed = self.observed.lock().map_err(|_| ApiError::internal())?;
        let routes = store
            .rows()?
            .into_iter()
            .map(|(rev, route)| {
                let state = if observed.error.is_some() {
                    "failed"
                } else if observed.applied != revision || observed.checked == 0 {
                    "pending"
                } else {
                    "applied"
                };
                let upstream_ready = route.enabled
                    && observed
                        .ready
                        .get(route.domain.as_str())
                        .copied()
                        .unwrap_or(false);
                RouteStatus {
                    route,
                    revision: rev,
                    proxy_state: state.into(),
                    upstream_ready,
                    checked_at_ms: observed.checked,
                    error_code: observed.error.clone(),
                }
            })
            .collect();
        Ok(IngressSnapshot {
            routes,
            revision,
            applied_revision: observed.applied,
            checked_at_ms: observed.checked,
            error_code: observed.error.clone(),
        })
    }
    async fn change_route(&self, change: RouteChange) -> Result<RouteReceipt> {
        let _permit = self.acquire_write()?;
        change
            .route
            .validate()
            .map_err(|m| ApiError::new(ErrorCode::BadRequest, m))?;
        let _slot = self
            .mutation
            .try_acquire()
            .map_err(|_| ApiError::new(ErrorCode::QueueFull, "domain update busy"))?;
        // Validates persisted app, project network and runtime ownership, including disabled bindings.
        self.inner
            .publication_target(change.route.app.clone())
            .await?;
        let receipt = self
            .store
            .lock()
            .map_err(|_| ApiError::internal())?
            .mutate(
                &change.route.domain,
                change.expected_revision,
                Some(&change.route),
            )?;
        self.wake.notify_one();
        Ok(receipt)
    }
    async fn delete_route(&self, domain: DomainName, revision: i64) -> Result<RouteReceipt> {
        let _permit = self.acquire_write()?;
        let _slot = self
            .mutation
            .try_acquire()
            .map_err(|_| ApiError::new(ErrorCode::QueueFull, "domain update busy"))?;
        let receipt = self
            .store
            .lock()
            .map_err(|_| ApiError::internal())?
            .mutate(&domain, revision, None)?;
        self.wake.notify_one();
        Ok(receipt)
    }
    async fn health(&self) -> Result<agent_api::Health> {
        self.inner.health().await
    }
    async fn applications(&self) -> Result<Vec<agent_api::Application>> {
        self.inner.applications().await
    }
    async fn operations(&self, limit: u16) -> Result<Vec<agent_api::Operation>> {
        self.inner.operations(limit).await
    }
    async fn submit(&self, input: agent_api::Submission) -> Result<agent_api::Job> {
        self.inner.submit(input).await
    }
    async fn job(&self, id: i64) -> Result<agent_api::Job> {
        self.inner.job(id).await
    }
    async fn diagnostics(&self, app: app_model::AppKey) -> Result<agent_api::Diagnostics> {
        self.inner.diagnostics(app).await
    }
    async fn logs(&self, app: app_model::AppKey, tail: u16) -> Result<agent_api::LogSnapshot> {
        self.inner.logs(app, tail).await
    }
    async fn create_secret(&self, input: agent_api::SecretInput) -> Result<agent_api::SecretInfo> {
        self.inner.create_secret(input).await
    }
    async fn secrets(&self, p: app_model::Identifier) -> Result<Vec<agent_api::SecretInfo>> {
        self.inner.secrets(p).await
    }
    async fn volumes(&self, app: app_model::AppKey) -> Result<Vec<agent_api::VolumeInfo>> {
        self.inner.volumes(app).await
    }
    async fn publication_target(
        &self,
        app: app_model::AppKey,
    ) -> Result<Option<std::net::Ipv4Addr>> {
        self.inner.publication_target(app).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    struct Agent;
    fn unavailable() -> ApiError {
        ApiError::new(ErrorCode::Unavailable, "fixture")
    }
    #[async_trait]
    impl AgentApi for Agent {
        async fn health(&self) -> Result<agent_api::Health> {
            Err(unavailable())
        }
        async fn applications(&self) -> Result<Vec<agent_api::Application>> {
            Err(unavailable())
        }
        async fn operations(&self, _: u16) -> Result<Vec<agent_api::Operation>> {
            Err(unavailable())
        }
        async fn submit(&self, _: agent_api::Submission) -> Result<agent_api::Job> {
            Err(unavailable())
        }
        async fn job(&self, _: i64) -> Result<agent_api::Job> {
            Err(unavailable())
        }
        async fn publication_target(
            &self,
            app: app_model::AppKey,
        ) -> Result<Option<std::net::Ipv4Addr>> {
            if app.name.as_str() == "foreign" {
                Err(ApiError::new(ErrorCode::NotFound, "not registered"))
            } else {
                Ok(Some("10.4.0.2".parse().unwrap()))
            }
        }
    }
    #[derive(Default)]
    struct Adapter {
        fail: AtomicBool,
        seen: Mutex<Vec<ResolvedRoute>>,
    }
    #[async_trait]
    impl Proxy for Adapter {
        async fn reconcile(&self, r: &[ResolvedRoute]) -> std::result::Result<(), ProxyError> {
            if self.fail.load(Ordering::SeqCst) {
                Err(ProxyError)
            } else {
                *self.seen.lock().unwrap() = r.to_vec();
                Ok(())
            }
        }
    }
    fn route() -> DomainRoute {
        DomainRoute {
            domain: DomainName::new("web.example.com").unwrap(),
            app: app_model::AppKey {
                project: app_model::Identifier::new("demo").unwrap(),
                name: app_model::Identifier::new("web").unwrap(),
            },
            port: 8080,
            tls: TlsMode::Production,
            enabled: true,
        }
    }
    #[tokio::test]
    async fn durable_routes_conflicts_proxy_failure_and_restart_recovery() {
        let root = tempfile::tempdir().unwrap();
        std::fs::set_permissions(root.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let proxy = Arc::new(Adapter::default());
        let inner: Arc<dyn AgentApi> = Arc::new(Agent);
        let service =
            IngressService::open(inner.clone(), proxy.clone(), &root.path().join("ingress"))
                .unwrap();
        let revision = service
            .change_route(RouteChange {
                route: route(),
                expected_revision: 0,
            })
            .await
            .unwrap()
            .revision;
        assert_eq!(
            service.routes().await.unwrap().routes[0].proxy_state,
            "pending"
        );
        assert!(matches!(
            service
                .change_route(RouteChange {
                    route: route(),
                    expected_revision: 0
                })
                .await
                .unwrap_err()
                .code,
            ErrorCode::Conflict
        ));
        proxy.fail.store(true, Ordering::SeqCst);
        service.reconcile_once().await;
        assert_eq!(
            service.routes().await.unwrap().routes[0].proxy_state,
            "failed"
        );
        drop(service);
        proxy.fail.store(false, Ordering::SeqCst);
        let reopened =
            IngressService::open(inner, proxy.clone(), &root.path().join("ingress")).unwrap();
        reopened.reconcile_once().await;
        let snapshot = reopened.routes().await.unwrap();
        assert_eq!(snapshot.applied_revision, revision);
        assert_eq!(snapshot.routes[0].proxy_state, "applied");
        assert!(snapshot.routes[0].upstream_ready);
        let deleted = reopened
            .delete_route(route().domain, revision)
            .await
            .unwrap();
        assert!(deleted.revision > revision);
        reopened.reconcile_once().await;
        assert!(proxy.seen.lock().unwrap().is_empty());
        assert!(reopened.routes().await.unwrap().routes.is_empty());
    }
    #[tokio::test]
    async fn invalid_or_unregistered_binding_is_not_persisted() {
        let root = tempfile::tempdir().unwrap();
        std::fs::set_permissions(root.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let service = IngressService::open(
            Arc::new(Agent),
            Arc::new(Adapter::default()),
            &root.path().join("ingress"),
        )
        .unwrap();
        let mut binding = route();
        binding.app.name = app_model::Identifier::new("foreign").unwrap();
        assert!(
            service
                .change_route(RouteChange {
                    route: binding,
                    expected_revision: 0
                })
                .await
                .is_err()
        );
        assert!(service.routes().await.unwrap().routes.is_empty());
    }
}

#[cfg(all(unix, feature = "caddy"))]
pub mod caddy;

/// Offline schema and route validation for an installer-owned state copy.
pub fn validate_state(path: &Path) -> Result<()> {
    let store = Store::open(path)?;
    for (_, route) in store.rows()? {
        route
            .validate()
            .map_err(|_| ApiError::new(ErrorCode::BadRequest, "invalid persisted route"))?;
    }
    Ok(())
}
