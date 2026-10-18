//! Local trusted state directory. Holds an exclusive OS lease until dropped.
use agent_core::{
    AppRecord, DeploymentStore, DesiredState, OperationRecord, Phase, RecoveryState, StoreError,
};
use app_model::{AppKey, AppSpec};
mod jobs;
use rusqlite::{Connection, OptionalExtension, params};
use std::{
    fs::{File, OpenOptions},
    path::Path,
};

pub struct SqliteStore {
    connection: Connection,
    #[cfg(unix)]
    _lease: nix::fcntl::Flock<File>,
}

fn unavailable<T>(_: T) -> StoreError {
    StoreError::Unavailable
}
fn encode_phase(phase: Phase) -> Result<String, StoreError> {
    serde_json::to_string(&phase).map_err(unavailable)
}
fn decode_phase(value: &str) -> Result<Phase, StoreError> {
    serde_json::from_str(value).map_err(|_| StoreError::InvalidState)
}

impl SqliteStore {
    pub fn open(directory: &Path, runtime_target: &str) -> Result<Self, StoreError> {
        #[cfg(not(unix))]
        {
            let _ = (directory, runtime_target);
            return Err(StoreError::Unavailable);
        }
        #[cfg(unix)]
        {
            prepare_directory(directory)?;
            let private_file = |name: &str| open_private_file(directory, name);
            let file = private_file("agent.lock")?;
            let lease = nix::fcntl::Flock::lock(file, nix::fcntl::FlockArg::LockExclusiveNonblock)
                .map_err(|(_, error)| {
                    if error == nix::errno::Errno::EWOULDBLOCK {
                        StoreError::Busy
                    } else {
                        StoreError::Unavailable
                    }
                })?;
            let database = private_file("agent.sqlite")?;
            if !database.metadata().map_err(unavailable)?.is_file() {
                return Err(StoreError::Unavailable);
            }
            let connection =
                Connection::open(directory.join("agent.sqlite")).map_err(unavailable)?;
            connection
                .busy_timeout(std::time::Duration::from_secs(2))
                .map_err(unavailable)?;
            connection
                .execute_batch(
                    "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL;",
                )
                .map_err(unavailable)?;
            connection
                .execute_batch("BEGIN IMMEDIATE")
                .map_err(unavailable)?;
            let version: u32 = connection
                .query_row("PRAGMA user_version", [], |row| row.get(0))
                .map_err(unavailable)?;
            if version > 3 {
                return Err(StoreError::InvalidState);
            }
            if version == 0 {
                connection
                    .execute_batch(include_str!("../migrations/001_initial.sql"))
                    .map_err(unavailable)?;
            }
            if version < 2 {
                connection
                    .execute_batch(include_str!("../migrations/002_recovery.sql"))
                    .map_err(unavailable)?;
            }
            if version < 3 {
                connection
                    .execute_batch(include_str!("../migrations/003_api_jobs.sql"))
                    .map_err(unavailable)?;
            }
            let existing: Option<String> = connection
                .query_row(
                    "SELECT value FROM metadata WHERE key='runtime_target'",
                    [],
                    |r| r.get(0),
                )
                .optional()
                .map_err(unavailable)?;
            match existing {
                Some(target) if target != runtime_target => return Err(StoreError::TargetMismatch),
                None => {
                    connection
                        .execute(
                            "INSERT INTO metadata(key,value) VALUES('runtime_target',?1)",
                            [runtime_target],
                        )
                        .map_err(unavailable)?;
                }
                _ => {}
            }
            // Acquiring the exclusive lease proves there is no live writer using this directory.
            connection.execute("UPDATE applications SET phase=?1 WHERE name IN (SELECT name FROM operations WHERE phase NOT IN (?2,?3,?1))", params![encode_phase(Phase::Interrupted)?, encode_phase(Phase::Succeeded)?, encode_phase(Phase::Failed)?]).map_err(unavailable)?;
            connection.execute("UPDATE operations SET phase=?1, updated_at=CURRENT_TIMESTAMP WHERE phase NOT IN (?1,?2,?3)", params![encode_phase(Phase::Interrupted)?, encode_phase(Phase::Succeeded)?, encode_phase(Phase::Failed)?]).map_err(unavailable)?;
            connection.execute_batch("COMMIT").map_err(unavailable)?;
            Ok(Self {
                connection,
                _lease: lease,
            })
        }
    }
}

impl DeploymentStore for SqliteStore {
    fn begin(&mut self, spec: &AppSpec, fingerprint: &str) -> Result<i64, StoreError> {
        let computed = spec.fingerprint().map_err(|_| StoreError::InvalidState)?;
        if computed != fingerprint {
            return Err(StoreError::InvalidState);
        }
        let encoded = serde_json::to_string(spec).map_err(unavailable)?;
        let name = spec.app.instance_name();
        let tx = self.connection.transaction().map_err(unavailable)?;
        let previous: Option<String> = tx
            .query_row(
                "SELECT fingerprint FROM applications WHERE name=?1",
                [&name],
                |r| r.get(0),
            )
            .optional()
            .map_err(unavailable)?;
        if previous.as_deref().is_some_and(|old| old != fingerprint) {
            return Err(StoreError::ConfigurationChange);
        }
        tx.execute("INSERT INTO applications(name,spec,fingerprint,phase) VALUES(?1,?2,?3,?4) ON CONFLICT(name) DO UPDATE SET phase=excluded.phase, desired='running', updated_at=CURRENT_TIMESTAMP", params![name, encoded, fingerprint, encode_phase(Phase::Pending)?]).map_err(unavailable)?;
        // A caller may retry within one lease after cancellation of an earlier apply.
        tx.execute("UPDATE operations SET phase=?2, updated_at=CURRENT_TIMESTAMP WHERE name=?1 AND phase NOT IN (?2,?3,?4)", params![name, encode_phase(Phase::Interrupted)?, encode_phase(Phase::Succeeded)?, encode_phase(Phase::Failed)?]).map_err(unavailable)?;
        tx.execute(
            "INSERT INTO operations(name,phase) VALUES(?1,?2)",
            params![name, encode_phase(Phase::Pending)?],
        )
        .map_err(unavailable)?;
        let id = tx.last_insert_rowid();
        tx.commit().map_err(unavailable)?;
        Ok(id)
    }
    fn record(
        &mut self,
        operation: i64,
        phase: Phase,
        instance_id: Option<&str>,
        error_code: Option<&str>,
    ) -> Result<(), StoreError> {
        let tx = self.connection.transaction().map_err(unavailable)?;
        let name: String = tx
            .query_row(
                "SELECT name FROM operations WHERE id=?1",
                [operation],
                |r| r.get(0),
            )
            .map_err(unavailable)?;
        tx.execute(
            "UPDATE operations SET phase=?2,error_code=?3,updated_at=CURRENT_TIMESTAMP WHERE id=?1",
            params![operation, encode_phase(phase)?, error_code],
        )
        .map_err(unavailable)?;
        tx.execute("UPDATE applications SET phase=?2,instance_id=COALESCE(?3,instance_id),updated_at=CURRENT_TIMESTAMP WHERE name=?1", params![name, encode_phase(phase)?, instance_id]).map_err(unavailable)?;
        tx.commit().map_err(unavailable)
    }
    fn applications(&self) -> Result<Vec<AppRecord>, StoreError> {
        let mut statement = self
            .connection
            .prepare("SELECT spec,fingerprint,phase,instance_id,desired,recovery FROM applications ORDER BY name")
            .map_err(unavailable)?;
        let rows = statement
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, Option<String>>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                ))
            })
            .map_err(unavailable)?;
        rows.map(|row| {
            let (encoded, fingerprint, phase, instance_id, desired, recovery) =
                row.map_err(unavailable)?;
            let spec: AppSpec = serde_json::from_str(&encoded).map_err(unavailable)?;
            if spec.fingerprint().map_err(unavailable)? != fingerprint {
                return Err(StoreError::InvalidState);
            }
            Ok(AppRecord {
                app: spec.app,
                fingerprint,
                phase: decode_phase(&phase)?,
                instance_id,
                desired: match desired.as_str() {
                    "running" => DesiredState::Running,
                    "stopped" => DesiredState::Stopped,
                    _ => return Err(StoreError::InvalidState),
                },
                recovery: serde_json::from_str(&recovery).map_err(|_| StoreError::InvalidState)?,
            })
        })
        .collect()
    }
    fn operations(&self, limit: u16) -> Result<Vec<OperationRecord>, StoreError> {
        let mut statement = self.connection.prepare("SELECT o.id,a.spec,o.phase,o.error_code FROM operations o JOIN applications a USING(name) ORDER BY o.id DESC LIMIT ?1").map_err(unavailable)?;
        let rows = statement
            .query_map([limit], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, Option<String>>(3)?,
                ))
            })
            .map_err(unavailable)?;
        rows.map(|row| {
            let (id, encoded, phase, error_code) = row.map_err(unavailable)?;
            let spec: AppSpec = serde_json::from_str(&encoded).map_err(unavailable)?;
            Ok(OperationRecord {
                id,
                app: spec.app,
                phase: decode_phase(&phase)?,
                error_code,
            })
        })
        .collect()
    }
    fn spec(&self, app: &AppKey) -> Result<Option<AppSpec>, StoreError> {
        let encoded: Option<String> = self
            .connection
            .query_row(
                "SELECT spec FROM applications WHERE name=?1",
                [app.instance_name()],
                |r| r.get(0),
            )
            .optional()
            .map_err(unavailable)?;
        encoded
            .map(|value| {
                let spec: AppSpec = serde_json::from_str(&value).map_err(unavailable)?;
                spec.validate().map_err(unavailable)?;
                Ok(spec)
            })
            .transpose()
    }
    fn set_desired(&mut self, app: &AppKey, desired: DesiredState) -> Result<(), StoreError> {
        let value = match desired {
            DesiredState::Running => "running",
            DesiredState::Stopped => "stopped",
        };
        let changed = self.connection.execute(
            "UPDATE applications SET desired=?2,recovery=?3,updated_at=CURRENT_TIMESTAMP WHERE name=?1",
            params![app.instance_name(), value, serde_json::to_string(&RecoveryState::default()).map_err(unavailable)?],
        ).map_err(unavailable)?;
        if changed != 1 {
            return Err(StoreError::InvalidState);
        }
        Ok(())
    }
    fn set_recovery(&mut self, app: &AppKey, recovery: &RecoveryState) -> Result<(), StoreError> {
        let encoded = serde_json::to_string(recovery).map_err(unavailable)?;
        let changed = self.connection.execute("UPDATE applications SET recovery=?2,updated_at=CURRENT_TIMESTAMP WHERE name=?1 AND recovery!=?2", params![app.instance_name(), encoded]).map_err(unavailable)?;
        if changed == 0 && self.spec(app)?.is_none() {
            return Err(StoreError::InvalidState);
        }
        Ok(())
    }
    fn observe(
        &mut self,
        app: &AppKey,
        phase: Phase,
        instance_id: Option<&str>,
    ) -> Result<(), StoreError> {
        self.connection.execute("UPDATE applications SET phase=?2,instance_id=?3,updated_at=CURRENT_TIMESTAMP WHERE name=?1 AND (phase!=?2 OR instance_id IS NOT ?3)", params![app.instance_name(), encode_phase(phase)?, instance_id]).map_err(unavailable)?;
        Ok(())
    }
}

/// Process-lifetime daemon lease, separate from the store session lease.
/// agentd holds both for its lifetime; API clients acquire neither.
pub struct DaemonLease {
    #[cfg(unix)]
    _file: nix::fcntl::Flock<File>,
}
impl DaemonLease {
    pub fn acquire(directory: &Path) -> Result<Self, StoreError> {
        #[cfg(unix)]
        {
            prepare_directory(directory)?;
            let file = open_private_file(directory, "agentd.lock")?;
            let lease = nix::fcntl::Flock::lock(file, nix::fcntl::FlockArg::LockExclusiveNonblock)
                .map_err(|(_, error)| {
                    if error == nix::errno::Errno::EWOULDBLOCK {
                        StoreError::Busy
                    } else {
                        StoreError::Unavailable
                    }
                })?;
            Ok(Self { _file: lease })
        }
        #[cfg(not(unix))]
        {
            let _ = directory;
            Err(StoreError::Unavailable)
        }
    }
}
#[cfg(unix)]
fn prepare_directory(directory: &Path) -> Result<(), StoreError> {
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(directory)
        .map_err(unavailable)?;
    let metadata = std::fs::symlink_metadata(directory).map_err(unavailable)?;
    if !metadata.is_dir() || metadata.permissions().mode() & 0o077 != 0 {
        return Err(StoreError::Unavailable);
    }
    Ok(())
}
#[cfg(unix)]
fn open_private_file(directory: &Path, name: &str) -> Result<File, StoreError> {
    use std::os::unix::fs::OpenOptionsExt;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_CLOEXEC)
        .open(directory.join(name))
        .map_err(unavailable)?;
    if !file.metadata().map_err(unavailable)?.is_file() {
        return Err(StoreError::Unavailable);
    }
    Ok(file)
}
