//! nerdctl adapter. Process execution is injectable; no daemon or database required.
//!
//! ```
//! use runtime_api::ContainerRuntime;
//! use runtime_nerdctl::{NerdctlConfig, NerdctlRuntime, TokioExecutor};
//!
//! // Construction performs validation only; no command runs here.
//! let runtime = NerdctlRuntime::new(NerdctlConfig::default(), TokioExecutor)?;
//! let interchangeable: Box<dyn ContainerRuntime> = Box::new(runtime);
//! # Ok::<(), runtime_api::RuntimeError>(())
//! ```
mod adapter;
mod config;
mod process;

pub use adapter::{CreatePreview, NerdctlRuntime};
pub use config::NerdctlConfig;
pub use process::{CommandExecutor, CommandOutput, CommandRequest, TokioExecutor};

mod diagnostics;
mod network;
