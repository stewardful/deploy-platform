//! Filesystem host adapter and API decorator. Secret values never enter SQLite/jobs.
use agent_api::*;
use app_model::{AppKey, AppSpec, Identifier};
use async_trait::async_trait;
use runtime_api::{ResourceProvider, RuntimeMount};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

fn internal() -> ApiError {
    ApiError::new(ErrorCode::Internal, "node resource storage unavailable")
}
fn conflict() -> ApiError {
    ApiError::new(
        ErrorCode::Conflict,
        "resource already exists with different content or ownership",
    )
}
fn io<T>(r: std::io::Result<T>) -> Result<T> {
    r.map_err(|_| internal())
}

/// Only private directories owned by the service identity are traversed.
fn private(path: &Path) -> Result<()> {
    if !path.exists() {
        io(fs::DirBuilder::new().mode(0o700).create(path))?;
    }
    let meta = io(fs::symlink_metadata(path))?;
    if !meta.is_dir() || meta.uid() != nix::unistd::geteuid().as_raw() || meta.mode() & 0o077 != 0 {
        return Err(internal());
    }
    Ok(())
}
fn read_file(path: &Path) -> Result<Vec<u8>> {
    let mut file = io(OpenOptions::new()
        .read(true)
        .custom_flags(nix::libc::O_NOFOLLOW)
        .open(path))?;
    let meta = io(file.metadata())?;
    if !meta.is_file() || meta.len() > 65536 {
        return Err(internal());
    }
    let mut bytes = Vec::new();
    io((&mut file).take(65537).read_to_end(&mut bytes))?;
    if bytes.len() > 65536 {
        return Err(internal());
    }
    Ok(bytes)
}
/// Atomically publish an immutable value. Interrupted staging files are never read as secrets.
fn publish(directory: &Path, name: &str, value: &[u8], mode: u32) -> Result<()> {
    let target = directory.join(name);
    if fs::symlink_metadata(&target).is_ok() {
        return if read_file(&target)? == value {
            Ok(())
        } else {
            Err(conflict())
        };
    }
    let temp = directory.join(format!(".pending-{}", std::process::id()));
    // A leftover from this single-writer's previous attempt is safe to remove without following it.
    if fs::symlink_metadata(&temp).is_ok() {
        io(fs::remove_file(&temp))?;
    }
    let mut file = io(OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temp))?;
    io(file.write_all(value))?;
    io(file.set_permissions(fs::Permissions::from_mode(mode)))?;
    io(file.sync_all())?;
    io(fs::hard_link(&temp, &target))?;
    io(fs::remove_file(temp))?;
    io(File::open(directory).and_then(|f| f.sync_all()))?;
    Ok(())
}

pub struct NodeResources {
    root: PathBuf,
    lock: Mutex<()>,
}
impl NodeResources {
    pub fn open(root: impl Into<PathBuf>) -> Result<Self> {
        let root = root.into();
        if !root.is_absolute() || root.to_str().is_none_or(|p| p.contains([',', '\0'])) {
            return Err(internal());
        }
        // Parent must be a trusted/private Agent state directory, never user input.
        private(root.parent().ok_or_else(internal)?)?;
        private(&root)?;
        private(&root.join("secrets"))?;
        private(&root.join("volumes"))?;
        Ok(Self {
            root,
            lock: Mutex::new(()),
        })
    }
    fn secret_dir(&self, project: &Identifier, name: &Identifier, create: bool) -> Result<PathBuf> {
        let project_dir = self.root.join("secrets").join(project.as_str());
        let dir = project_dir.join(name.as_str());
        if !create && !dir.exists() {
            return Err(ApiError::new(
                ErrorCode::NotFound,
                "referenced secret does not exist in this project",
            ));
        }
        private(&project_dir)?;
        private(&dir)?;
        Ok(dir)
    }
    pub fn create_secret(&self, input: SecretInput) -> Result<SecretInfo> {
        input.validate()?;
        let _lock = self.lock.lock().map_err(|_| internal())?;
        let dir = self.secret_dir(&input.project, &input.name, true)?;
        // 0444 is needed inside a non-root container. Every host ancestor remains 0700.
        // This is plaintext root-private storage, not encryption or a multi-user secret vault.
        publish(&dir, "value", input.value.as_bytes(), 0o444)?;
        Ok(SecretInfo {
            project: input.project,
            name: input.name,
        })
    }
    pub fn secrets(&self, project: Identifier) -> Result<Vec<SecretInfo>> {
        let _lock = self.lock.lock().map_err(|_| internal())?;
        let directory = self.root.join("secrets").join(project.as_str());
        if !directory.exists() {
            return Ok(vec![]);
        }
        private(&directory)?;
        let mut names = Vec::new();
        for item in io(fs::read_dir(directory))? {
            let item = io(item)?;
            let Ok(name) = Identifier::new(item.file_name().to_string_lossy().as_ref()) else {
                continue;
            };
            private(&item.path())?;
            if item.path().join("value").is_file() {
                names.push(SecretInfo {
                    project: project.clone(),
                    name,
                });
            }
        }
        names.sort_by(|a, b| a.name.as_str().cmp(b.name.as_str()));
        Ok(names)
    }
    pub fn check(&self, spec: &AppSpec) -> Result<()> {
        spec.validate()
            .map_err(|_| ApiError::new(ErrorCode::BadRequest, "invalid resource configuration"))?;
        let _lock = self.lock.lock().map_err(|_| internal())?;
        for secret in &spec.secrets {
            read_file(
                &self
                    .secret_dir(&spec.app.project, &secret.name, false)?
                    .join("value"),
            )?;
        }
        Ok(())
    }
    pub fn volumes(&self, app: AppKey) -> Result<Vec<VolumeInfo>> {
        let _lock = self.lock.lock().map_err(|_| internal())?;
        let dir = self.root.join("volumes").join(app.instance_name());
        if !dir.exists() {
            return Ok(vec![]);
        }
        private(&dir)?;
        let mut values = Vec::new();
        for entry in io(fs::read_dir(dir))? {
            let entry = io(entry)?;
            if entry.path().extension().is_some_and(|s| s == "json") {
                values.push(
                    serde_json::from_slice::<VolumeInfo>(&read_file(&entry.path())?)
                        .map_err(|_| internal())?,
                );
            }
        }
        values.sort_by(|a, b| a.name.as_str().cmp(b.name.as_str()));
        Ok(values)
    }
    fn prepare_inner(&self, spec: &AppSpec) -> Result<Vec<RuntimeMount>> {
        self.check(spec)?;
        let _lock = self.lock.lock().map_err(|_| internal())?;
        let mut mounts = Vec::new();
        for secret in &spec.secrets {
            mounts.push(RuntimeMount {
                source: self
                    .secret_dir(&spec.app.project, &secret.name, false)?
                    .join("value"),
                target: format!("/run/secrets/{}", secret.name.as_str()),
                read_only: true,
            });
        }
        if !spec.volumes.is_empty() {
            let dir = self.root.join("volumes").join(spec.app.instance_name());
            private(&dir)?;
            let (uid, gid) = spec.user.as_ref().map(|u| (u.uid, u.gid)).unwrap_or((0, 0));
            for volume in &spec.volumes {
                let meta = VolumeInfo {
                    name: volume.name.clone(),
                    target: volume.target.clone(),
                    uid,
                    gid,
                };
                let marker = format!("{}.json", volume.name.as_str());
                let data = dir.join(volume.name.as_str());
                // Never adopt an unmarked directory or recursively chown existing database files.
                if fs::symlink_metadata(&data).is_ok() && !dir.join(&marker).exists() {
                    return Err(conflict());
                }
                publish(
                    &dir,
                    &marker,
                    &serde_json::to_vec(&meta).map_err(|_| internal())?,
                    0o600,
                )?;
                if !data.exists() {
                    let staging = dir.join(format!(".init-{}", volume.name.as_str()));
                    if fs::symlink_metadata(&staging).is_ok() {
                        io(fs::remove_dir(&staging))?;
                    }
                    io(fs::DirBuilder::new().mode(0o700).create(&staging))?;
                    nix::unistd::chown(
                        &staging,
                        Some(nix::unistd::Uid::from_raw(uid)),
                        Some(nix::unistd::Gid::from_raw(gid)),
                    )
                    .map_err(|_| internal())?;
                    io(fs::rename(staging, &data))?;
                    io(File::open(&dir).and_then(|f| f.sync_all()))?;
                }
                let actual = io(fs::symlink_metadata(&data))?;
                if !actual.is_dir() || actual.uid() != uid || actual.gid() != gid {
                    return Err(conflict());
                }
                mounts.push(RuntimeMount {
                    source: data,
                    target: volume.target.clone(),
                    read_only: volume.read_only,
                });
            }
        }
        Ok(mounts)
    }
}
impl ResourceProvider for NodeResources {
    fn prepare(&self, spec: &AppSpec) -> runtime_api::Result<Vec<RuntimeMount>> {
        self.prepare_inner(spec).map_err(|_| {
            runtime_api::RuntimeError::Configuration(
                "node resources unavailable; check secret references and volume ownership",
            )
        })
    }
}
/// Add resource endpoints to any AgentApi without changing its queue/store implementation.
pub struct ResourceApi {
    inner: Arc<dyn AgentApi>,
    resources: Arc<NodeResources>,
}
impl ResourceApi {
    pub fn new(inner: Arc<dyn AgentApi>, resources: Arc<NodeResources>) -> Self {
        Self { inner, resources }
    }
}
#[async_trait]
impl AgentApi for ResourceApi {
    async fn publication_target(&self, app: AppKey) -> Result<Option<std::net::Ipv4Addr>> {
        self.inner.publication_target(app).await
    }

    async fn diagnostics(&self, app: AppKey) -> Result<agent_api::Diagnostics> {
        self.inner.diagnostics(app).await
    }
    async fn logs(&self, app: AppKey, tail: u16) -> Result<agent_api::LogSnapshot> {
        self.inner.logs(app, tail).await
    }
    async fn health(&self) -> Result<Health> {
        self.inner.health().await
    }
    async fn applications(&self) -> Result<Vec<Application>> {
        self.inner.applications().await
    }
    async fn operations(&self, limit: u16) -> Result<Vec<Operation>> {
        self.inner.operations(limit).await
    }
    async fn job(&self, id: i64) -> Result<Job> {
        self.inner.job(id).await
    }
    async fn submit(&self, submission: Submission) -> Result<Job> {
        submission.validate()?;
        if let Command::Apply { spec, .. } = &submission.command {
            let resources = self.resources.clone();
            let spec = spec.clone();
            tokio::task::spawn_blocking(move || resources.check(&spec))
                .await
                .map_err(|_| internal())??;
        }
        self.inner.submit(submission).await
    }
    async fn create_secret(&self, input: SecretInput) -> Result<SecretInfo> {
        let resources = self.resources.clone();
        tokio::task::spawn_blocking(move || resources.create_secret(input))
            .await
            .map_err(|_| internal())?
    }
    async fn secrets(&self, project: Identifier) -> Result<Vec<SecretInfo>> {
        let resources = self.resources.clone();
        tokio::task::spawn_blocking(move || resources.secrets(project))
            .await
            .map_err(|_| internal())?
    }
    async fn volumes(&self, app: AppKey) -> Result<Vec<VolumeInfo>> {
        let resources = self.resources.clone();
        tokio::task::spawn_blocking(move || resources.volumes(app))
            .await
            .map_err(|_| internal())?
    }
}
