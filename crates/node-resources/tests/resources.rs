use agent_api::{ErrorCode, SecretInput};
use app_model::*;
use node_resources::NodeResources;
use runtime_api::ResourceProvider;
use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
};
fn id(s: &str) -> Identifier {
    Identifier::new(s).unwrap()
}
fn spec() -> AppSpec {
    serde_json::from_value(serde_json::json!({"schema_version":1,"app":{"project":"demo","name":"db"},"image":"postgres:17-bookworm","resources":{"memory_mib":384,"cpu_millis":500,"pids":128},"user":{"uid":nix::unistd::geteuid().as_raw(),"gid":nix::unistd::getegid().as_raw()},"volumes":[{"name":"data","target":"/var/lib/postgresql/data"}]})).unwrap()
}
fn input(value: &str) -> SecretInput {
    SecretInput {
        project: id("demo"),
        name: id("password"),
        value: value.into(),
    }
}
#[test]
fn secrets_are_private_immutable_scoped_and_never_returned() {
    let temp = tempfile::tempdir().unwrap();
    fs::set_permissions(temp.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let store = NodeResources::open(temp.path().join("resources")).unwrap();
    let value = "do-not-leak-secret";
    let created = store.create_secret(input(value)).unwrap();
    assert!(!format!("{:?}", input(value)).contains(value));
    assert!(!serde_json::to_string(&created).unwrap().contains(value));
    store.create_secret(input(value)).unwrap();
    assert_eq!(
        store.create_secret(input("different")).unwrap_err().code,
        ErrorCode::Conflict
    );
    let mut app = spec();
    app.secrets = vec![SecretMount {
        name: id("password"),
    }];
    let mounts = store.prepare(&app).unwrap();
    let secret = &mounts[0];
    assert!(secret.read_only);
    assert_eq!(secret.target, "/run/secrets/password");
    assert_eq!(fs::read(&secret.source).unwrap(), value.as_bytes());
    assert_eq!(
        fs::metadata(secret.source.parent().unwrap())
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(&secret.source).unwrap().permissions().mode() & 0o777,
        0o444
    );
    app.app.project = id("other");
    assert!(store.prepare(&app).is_err());
    assert!(store.secrets(id("other")).unwrap().is_empty());
    let list = serde_json::to_string(&store.secrets(id("demo")).unwrap()).unwrap();
    assert!(!list.contains(value));
    fs::remove_file(&secret.source).unwrap();
    symlink(temp.path().join("outside"), &secret.source).unwrap();
    assert!(store.create_secret(input("attack")).is_err());
    assert!(!temp.path().join("outside").exists());
}
#[test]
fn volumes_survive_reopen_and_are_not_shared_or_adopted() {
    let temp = tempfile::tempdir().unwrap();
    fs::set_permissions(temp.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let root = temp.path().join("resources");
    let app = spec();
    let store = NodeResources::open(&root).unwrap();
    let mounts = store.prepare(&app).unwrap();
    let data = mounts[0].source.clone();
    fs::write(data.join("record"), "persisted").unwrap();
    drop(store);
    let store = NodeResources::open(&root).unwrap();
    let reopened = store.prepare(&app).unwrap();
    assert_eq!(data, reopened[0].source);
    assert_eq!(
        fs::read_to_string(data.join("record")).unwrap(),
        "persisted"
    );
    let mut other = app.clone();
    other.app.name = id("other");
    assert_ne!(data, store.prepare(&other).unwrap()[0].source);
    let mut changed = app.clone();
    changed.user.as_mut().unwrap().uid += 1;
    assert!(store.prepare(&changed).is_err());
    let volumes = store.volumes(app.app.clone()).unwrap();
    assert_eq!(volumes[0].name, id("data"));
    let mut rogue = app.clone();
    rogue.volumes[0].name = id("rogue");
    fs::create_dir(data.parent().unwrap().join("rogue")).unwrap();
    assert!(store.prepare(&rogue).is_err());
    fs::remove_dir(data.parent().unwrap().join("rogue")).unwrap();
    symlink(&data, data.parent().unwrap().join("rogue")).unwrap();
    assert!(store.prepare(&rogue).is_err());
}
#[test]
fn rejects_public_or_linked_resource_root_and_invalid_values() {
    let temp = tempfile::tempdir().unwrap();
    fs::set_permissions(temp.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let public = temp.path().join("public");
    fs::create_dir(&public).unwrap();
    fs::set_permissions(&public, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(NodeResources::open(&public).is_err());
    let linked = temp.path().join("linked");
    symlink(&public, &linked).unwrap();
    assert!(NodeResources::open(&linked).is_err());
    assert!(input("").validate().is_err());
    assert!(input("nul\0secret").validate().is_err());
    assert!(input(&"a".repeat(65537)).validate().is_err());
}
