use async_trait::async_trait;
use runtime_api::{ExecutionFailure, Result, RuntimeError};
use std::{ffi::OsString, fmt, path::PathBuf, process::Stdio, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncReadExt},
    process::Command,
};

/// Typed argv, not shell text. Debug deliberately omits all arguments and captured output.
pub struct CommandRequest {
    pub operation: &'static str,
    pub program: PathBuf,
    pub args: Vec<OsString>,
    pub timeout: Duration,
    /// Per stream. Total retained output is at most twice this limit.
    pub max_output_bytes: usize,
}
impl fmt::Debug for CommandRequest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CommandRequest")
            .field("operation", &self.operation)
            .field("program", &self.program)
            .field("argument_count", &self.args.len())
            .finish()
    }
}
pub struct CommandOutput {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}
impl fmt::Debug for CommandOutput {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CommandOutput")
            .field("stdout_bytes", &self.stdout.len())
            .field("stderr_bytes", &self.stderr.len())
            .finish()
    }
}

#[async_trait]
pub trait CommandExecutor: Send + Sync {
    /// Implementations return an error for nonzero exits. Never expose raw stderr in errors.
    async fn execute(&self, request: CommandRequest) -> Result<CommandOutput>;
}

#[derive(Debug, Default)]
pub struct TokioExecutor;

async fn read_bounded(
    mut reader: impl AsyncRead + Unpin,
    limit: usize,
) -> std::result::Result<Vec<u8>, ExecutionFailure> {
    let mut output = Vec::new();
    let mut chunk = [0; 8192];
    loop {
        let count = reader
            .read(&mut chunk)
            .await
            .map_err(|_| ExecutionFailure::Io)?;
        if count == 0 {
            return Ok(output);
        }
        if count > limit.saturating_sub(output.len()) {
            return Err(ExecutionFailure::OutputLimit);
        }
        output.extend_from_slice(&chunk[..count]);
    }
}

// A separate process group lets timeout/cancellation clean up CLI children as well.
// It cannot undo containers or work already submitted to containerd.
#[cfg(unix)]
struct ProcessGroup {
    pid: i32,
    armed: bool,
}
#[cfg(unix)]
impl Drop for ProcessGroup {
    fn drop(&mut self) {
        if self.armed {
            let _ = nix::sys::signal::killpg(
                nix::unistd::Pid::from_raw(self.pid),
                nix::sys::signal::Signal::SIGKILL,
            );
        }
    }
}

#[async_trait]
impl CommandExecutor for TokioExecutor {
    async fn execute(&self, request: CommandRequest) -> Result<CommandOutput> {
        let fail = |kind| RuntimeError::Execution {
            operation: request.operation,
            kind,
        };
        if request.timeout.is_zero()
            || request.max_output_bytes == 0
            || !request.program.is_absolute()
        {
            return Err(RuntimeError::Configuration(
                "invalid command execution limits or program path",
            ));
        }
        let mut command = Command::new(&request.program);
        command
            .args(&request.args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        #[cfg(unix)]
        command.process_group(0);
        let mut child = command.spawn().map_err(|_| fail(ExecutionFailure::Io))?;
        #[cfg(unix)]
        let mut group = ProcessGroup {
            pid: child.id().ok_or_else(|| fail(ExecutionFailure::Io))? as i32,
            armed: true,
        };
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| fail(ExecutionFailure::Io))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| fail(ExecutionFailure::Io))?;
        let collected = tokio::time::timeout(request.timeout, async {
            tokio::try_join!(
                async { child.wait().await.map_err(|_| ExecutionFailure::Io) },
                read_bounded(stdout, request.max_output_bytes),
                read_bounded(stderr, request.max_output_bytes),
            )
        })
        .await;
        match collected {
            Ok(Ok((status, stdout, stderr))) if status.success() => {
                // Successful commands may deliberately spawn persistent runtime helpers.
                #[cfg(unix)]
                {
                    group.armed = false;
                }
                Ok(CommandOutput { stdout, stderr })
            }
            Ok(Ok(_)) => Err(fail(ExecutionFailure::Exit)),
            other => {
                #[cfg(unix)]
                drop(group);
                let _ = child.start_kill();
                let _ = child.wait().await;
                Err(fail(match other {
                    Ok(Err(kind)) => kind,
                    _ => ExecutionFailure::Timeout,
                }))
            }
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    fn request(program: &str, args: &[&str], timeout_ms: u64, limit: usize) -> CommandRequest {
        CommandRequest {
            operation: "test",
            program: program.into(),
            args: args.iter().map(OsString::from).collect(),
            timeout: Duration::from_millis(timeout_ms),
            max_output_bytes: limit,
        }
    }
    #[tokio::test]
    async fn metacharacters_are_literal_arguments() {
        let output = TokioExecutor
            .execute(request("/bin/echo", &["$(exit 99); hello"], 2000, 1024))
            .await
            .unwrap();
        assert_eq!(
            String::from_utf8(output.stdout).unwrap(),
            "$(exit 99); hello\n"
        );
    }
    #[tokio::test]
    async fn enforces_deadline() {
        let result = TokioExecutor
            .execute(request("/bin/sleep", &["5"], 30, 1024))
            .await;
        assert!(matches!(
            result,
            Err(RuntimeError::Execution {
                kind: ExecutionFailure::Timeout,
                ..
            })
        ));
    }
    #[tokio::test]
    async fn deadline_also_covers_descendant_held_pipes() {
        // Fixed test fixture only; production never wraps user input in a shell.
        let result = TokioExecutor
            .execute(request("/bin/sh", &["-c", "sleep 5 &"], 100, 1024))
            .await;
        assert!(matches!(
            result,
            Err(RuntimeError::Execution {
                kind: ExecutionFailure::Timeout,
                ..
            })
        ));
    }
    #[tokio::test]
    async fn bounds_output_and_hides_stderr() {
        let result = TokioExecutor
            .execute(request("/bin/echo", &["too much data"], 2000, 2))
            .await;
        assert!(matches!(
            result,
            Err(RuntimeError::Execution {
                kind: ExecutionFailure::OutputLimit,
                ..
            })
        ));
        let error = TokioExecutor
            .execute(request(
                "/bin/ls",
                &["/missing-sensitive-value"],
                2000,
                1024,
            ))
            .await
            .unwrap_err();
        assert!(!error.to_string().contains("sensitive-value"));
    }
}
