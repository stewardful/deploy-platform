use agent_api::JobRepository;
use agent_api::server::BoundServer;
use agent_core::recovery::{Outcome, RecoveryOptions, SystemClock, reconcile_once};
use agent_core::service::{Service, ServiceOptions};
use agent_store_sqlite::{DaemonLease, SqliteStore};
use clap::Parser;
use node_agent::HostOptions;
use std::{path::PathBuf, process::ExitCode, time::Duration};
use tokio_util::sync::CancellationToken;

#[derive(Parser)]
#[command(version, about = "Serial, low-overhead desired-state recovery service")]
struct Options {
    #[command(flatten)]
    host: HostOptions,
    #[arg(long, default_value_t = 10, value_parser = clap::value_parser!(u64).range(1..=300))]
    poll_seconds: u64,
    #[arg(long, default_value_t = 5, value_parser = clap::value_parser!(u64).range(1..=3600))]
    retry_base_seconds: u64,
    #[arg(long, default_value_t = 300, value_parser = clap::value_parser!(u64).range(1..=86400))]
    retry_max_seconds: u64,
    #[arg(long, default_value_t = 60, value_parser = clap::value_parser!(u64).range(1..=86400))]
    stable_seconds: u64,
    /// Perform one recovery pass and exit (useful for explicit integration checks).
    #[arg(long)]
    once: bool,
    /// Offline validation/migration rehearsal. Only use on an isolated state copy.
    #[arg(long, conflicts_with = "once")]
    check_state: bool,
    #[arg(long, default_value = "/run/deploy-platform/agent.sock")]
    socket: PathBuf,
    #[arg(long, default_value_t = 32, value_parser = clap::value_parser!(u32).range(1..=128))]
    queue_capacity: u32,
    #[arg(long, default_value = "/run/deploy-platform-proxy/admin.sock")]
    proxy_socket: PathBuf,
    /// Root-only integration/private-CA override; never accepted through public API.
    #[arg(long)]
    ingress_acme_ca: Option<String>,
}

#[cfg(unix)]
async fn shutdown_signal() -> Result<(), String> {
    use tokio::signal::unix::{SignalKind, signal};
    let mut terminate = signal(SignalKind::terminate()).map_err(|_| "cannot register SIGTERM")?;
    let mut interrupt = signal(SignalKind::interrupt()).map_err(|_| "cannot register SIGINT")?;
    tokio::select! { _ = terminate.recv() => {}, _ = interrupt.recv() => {} }
    Ok(())
}
#[cfg(not(unix))]
async fn shutdown_signal() -> Result<(), String> {
    Err("agentd requires Linux".into())
}

async fn run(options: Options) -> Result<(), String> {
    if options.check_state {
        use agent_core::DeploymentStore;
        let target = options.host.target()?;
        let store =
            SqliteStore::open(&options.host.state_dir, &target).map_err(|e| e.to_string())?;
        for app in store.applications().map_err(|e| e.to_string())? {
            let spec = store
                .spec(&app.app)
                .map_err(|e| e.to_string())?
                .ok_or("missing app spec")?;
            spec.validate().map_err(|e| e.to_string())?;
            if spec.fingerprint().map_err(|e| e.to_string())? != app.fingerprint {
                return Err("persisted fingerprint mismatch".into());
            }
        }
        ingress::validate_state(&options.host.state_dir.join("ingress"))
            .map_err(|e| e.to_string())?;
        println!("state copy validated; no runtime operations executed");
        return Ok(());
    }
    if !cfg!(target_os = "linux") {
        return Err("agentd runtime recovery requires Linux".into());
    }
    let recovery = RecoveryOptions {
        retry_base: Duration::from_secs(options.retry_base_seconds),
        retry_max: Duration::from_secs(options.retry_max_seconds),
        stable_window: Duration::from_secs(options.stable_seconds),
        ..Default::default()
    };
    recovery.validate().map_err(|e| e.to_string())?;
    let target = options.host.target()?;
    let _daemon_lease = DaemonLease::acquire(&options.host.state_dir)
        .map_err(|e| format!("cannot acquire agentd lease: {e}"))?;
    let resources = std::sync::Arc::new(
        node_resources::NodeResources::open(options.host.state_dir.join("resources"))
            .map_err(|e| e.to_string())?,
    );
    let runtime = options.host.runtime()?.with_resources(resources.clone());
    if !options.once {
        let store =
            SqliteStore::open(&options.host.state_dir, &target).map_err(|e| e.to_string())?;
        let server = BoundServer::bind(&options.socket).await.map_err(
            |_| "cannot bind private API socket (check path, permissions and existing listener)",
        )?;
        let service = Service::start(
            runtime,
            store,
            ServiceOptions {
                poll_interval: Duration::from_secs(options.poll_seconds),
                queue_capacity: options.queue_capacity,
                start_drained: {
                    use std::os::unix::fs::PermissionsExt;
                    let marker = options.host.state_dir.join("upgrade-maintenance");
                    match std::fs::symlink_metadata(marker) {
                        Ok(meta) if meta.is_file() && meta.permissions().mode() & 0o077 == 0 => {
                            true
                        }
                        Ok(_) => return Err("upgrade marker must be a private regular file".into()),
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => false,
                        Err(_) => return Err("cannot inspect upgrade marker".into()),
                    }
                },
                recovery,
            },
        )
        .map_err(|e| e.to_string())?;
        let shutdown = CancellationToken::new();
        let resource_api = std::sync::Arc::new(node_resources::ResourceApi::new(
            service.api(),
            resources.clone(),
        ));
        let proxy = std::sync::Arc::new(
            ingress::caddy::CaddyProxy::new(options.proxy_socket, options.ingress_acme_ca)
                .map_err(|_| "invalid proxy configuration")?,
        );
        let ingress = ingress::IngressService::open(
            resource_api,
            proxy,
            &options.host.state_dir.join("ingress"),
        )
        .map_err(|e| e.to_string())?;
        let ingress_task = tokio::spawn(
            ingress
                .clone()
                .run(shutdown.clone(), Duration::from_secs(10)),
        );
        let serving = server.serve(ingress, shutdown.clone());
        tokio::pin!(serving);
        eprintln!("agentd started; private Unix Socket API enabled");
        let result = tokio::select! {
            signal = shutdown_signal() => signal,
            _ = service.finished() => Err("control worker stopped unexpectedly".into()),
            result = &mut serving => result.map_err(|_| "local API server stopped unexpectedly".into()),
        };
        shutdown.cancel();
        let _ = ingress_task.await;
        let stopped = service.shutdown().await.map_err(|e| e.to_string());
        // Dropping the serving future closes connections and releases the socket guard.
        eprintln!("agentd stopped; deployed applications were left running");
        return result.and(stopped);
    }
    // Offline maintenance holds the same lease for the pending-job check and recovery.
    let mut store =
        SqliteStore::open(&options.host.state_dir, &target).map_err(|e| e.to_string())?;
    if store.pending_jobs().map_err(|e| e.to_string())? > 0 {
        return Err("pending API jobs exist; run agentd normally to replay them first".into());
    }
    let events = tokio::select! {
        biased;
        signal = shutdown_signal() => { signal?; return Ok(()); }
        result = reconcile_once(&runtime, &mut store, &SystemClock, recovery) => result.map_err(|e| e.to_string())?,
    };
    let failed = events
        .iter()
        .any(|event| matches!(event.outcome, Outcome::Failed | Outcome::Deferred));
    for event in events {
        if !matches!(event.outcome, Outcome::Healthy | Outcome::Deferred) {
            eprintln!(
                "{}",
                serde_json::to_string(&event).map_err(|_| "cannot serialize recovery event")?
            );
        }
    }
    if failed {
        Err("one or more applications failed or are waiting for retry backoff".into())
    } else {
        Ok(())
    }
}

fn main() -> ExitCode {
    let executor = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .max_blocking_threads(2)
        .build()
    {
        Ok(executor) => executor,
        Err(_) => {
            eprintln!("error: cannot initialize agent runtime");
            return ExitCode::FAILURE;
        }
    };
    match executor.block_on(run(Options::parse())) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("error: {error}");
            ExitCode::FAILURE
        }
    }
}
