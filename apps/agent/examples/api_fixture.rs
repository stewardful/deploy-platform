//! Cross-language integration fixture. Real HTTP/service/SQLite, in-memory runtime only.
//! Never use this binary to deploy real applications.
#[cfg(unix)]
#[path = "../tests/support/mod.rs"]
mod support;

#[cfg(unix)]
#[tokio::main(flavor = "current_thread")]
async fn main() {
    use agent_api::server::BoundServer;
    use agent_core::service::{Service, ServiceOptions};
    use agent_store_sqlite::SqliteStore;
    use std::path::PathBuf;
    use tokio::signal::unix::{SignalKind, signal};
    use tokio_util::sync::CancellationToken;
    let args: Vec<_> = std::env::args_os().skip(1).map(PathBuf::from).collect();
    assert_eq!(args.len(), 2, "usage: api_fixture STATE_DIR SOCKET");
    let store = SqliteStore::open(&args[0], "go-test-fixture").unwrap();
    let server = BoundServer::bind(&args[1]).await.unwrap();
    let service = Service::start(
        support::Runtime::default(),
        store,
        ServiceOptions::default(),
    )
    .unwrap();
    let shutdown = CancellationToken::new();
    let resources = std::sync::Arc::new(
        node_resources::NodeResources::open(args[0].join("resources")).unwrap(),
    );
    let api = std::sync::Arc::new(node_resources::ResourceApi::new(service.api(), resources));
    let ingress = ingress::IngressService::open(
        api,
        std::sync::Arc::new(support::Proxy),
        &args[0].join("ingress"),
    )
    .unwrap();
    let ingress_task = tokio::spawn(
        ingress
            .clone()
            .run(shutdown.clone(), std::time::Duration::from_millis(100)),
    );
    let serving = tokio::spawn(server.serve(ingress, shutdown.clone()));
    let mut terminate = signal(SignalKind::terminate()).unwrap();
    tokio::select! { _ = terminate.recv() => {}, _ = tokio::signal::ctrl_c() => {} }
    shutdown.cancel();
    serving.await.unwrap().unwrap();
    ingress_task.await.unwrap();
    service.shutdown().await.unwrap();
}
#[cfg(not(unix))]
fn main() {
    panic!("Unix integration fixture requires Unix");
}
