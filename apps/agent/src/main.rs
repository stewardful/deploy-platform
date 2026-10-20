use agent_api::client::{Client, request_id};
use agent_api::{Command, JobState, Submission};
use app_model::{AppKey, AppSpec, Identifier};
use clap::{Args, Parser, Subcommand};
use std::{
    path::{Path, PathBuf},
    process::ExitCode,
    time::Duration,
};

#[derive(Parser)]
#[command(
    version,
    about = "Manage applications through an independent agentd",
    after_help = "Common usage:\n  agentctl apply -f app.toml     Deploy TOML or JSON\n  agentctl apps                 List saved applications\n  agentctl logs --project demo --name web\n  agentctl diagnostics --project demo --name web\n  agentctl routes               Inspect domain synchronization\n\nRun agentctl help COMMAND for details. validate/plan work offline; all other commands use the private Agent API."
)]
struct Cli {
    #[arg(long, global = true, default_value = "/run/deploy-platform/agent.sock")]
    socket: PathBuf,
    #[arg(long, global = true, default_value_t = 5, value_parser = clap::value_parser!(u64).range(1..=60))]
    request_timeout_seconds: u64,
    #[command(subcommand)]
    command: Commands,
}
#[derive(Clone, clap::ValueEnum)]
enum MaintenanceAction {
    Status,
    Enter,
    Exit,
}
#[derive(Args)]
struct SubmitOptions {
    /// Reuse this key with the same request after an uncertain submission.
    #[arg(long)]
    request_id: Option<String>,
    #[arg(long)]
    no_wait: bool,
    #[arg(long, default_value_t = 300, value_parser = clap::value_parser!(u64).range(1..=3600))]
    wait_seconds: u64,
}
#[derive(Subcommand)]
enum Commands {
    Validate {
        #[arg(short, long)]
        file: PathBuf,
    },
    Plan {
        #[arg(short, long)]
        file: PathBuf,
    },
    /// Check API/worker availability, not application or containerd health.
    Doctor,
    /// Node-local upgrade gate. Enter rejects writes and drains accepted jobs.
    Maintenance {
        #[arg(value_enum, default_value = "status")]
        action: MaintenanceAction,
    },
    /// Deploy a TOML/JSON application; default waits for the durable job result.
    Apply {
        #[arg(short, long)]
        file: PathBuf,
        #[arg(long, default_value_t=1000, value_parser=clap::value_parser!(u64).range(100..=30000))]
        startup_wait_ms: u64,
        #[command(flatten)]
        submit: SubmitOptions,
    },
    /// List applications and persisted running/stopped intent.
    Apps,
    /// List domains, saved revisions and proxy synchronization state.
    Routes,
    /// Save a domain binding JSON file; use the current revision when editing.
    RouteSet {
        #[arg(long)]
        file: PathBuf,
        #[arg(long,default_value_t=0,value_parser=clap::value_parser!(i64).range(0..))]
        expected_revision: i64,
    },
    RouteDelete {
        #[arg(long)]
        domain: String,
        #[arg(long,value_parser=clap::value_parser!(i64).range(1..))]
        expected_revision: i64,
    },
    /// Create an immutable project secret from a private UTF-8 file, never argv contents.
    SecretCreate {
        #[arg(long)]
        project: String,
        #[arg(long)]
        name: String,
        #[arg(long)]
        file: PathBuf,
    },
    Secrets {
        #[arg(long)]
        project: String,
    },
    Diagnostics {
        #[arg(long)]
        project: String,
        #[arg(long)]
        name: String,
    },
    Logs {
        #[arg(long)]
        project: String,
        #[arg(long)]
        name: String,
        #[arg(long,default_value_t=100,value_parser=clap::value_parser!(u16).range(1..=500))]
        tail: u16,
    },
    Volumes {
        #[arg(long)]
        project: String,
        #[arg(long)]
        name: String,
    },
    Stop {
        #[arg(long)]
        project: String,
        #[arg(long)]
        name: String,
        #[command(flatten)]
        submit: SubmitOptions,
    },
    Start {
        #[arg(long)]
        project: String,
        #[arg(long)]
        name: String,
        #[command(flatten)]
        submit: SubmitOptions,
    },
    Operations {
        #[arg(long,default_value_t=20,value_parser=clap::value_parser!(u16).range(1..=1000))]
        limit: u16,
    },
    /// Inspect a previously accepted job, including its result or error.
    Job { id: i64 },
}

fn load_spec(path: &Path) -> Result<AppSpec, String> {
    let metadata = std::fs::metadata(path).map_err(|_| "cannot read configuration file")?;
    if !metadata.is_file() || metadata.len() > 128 * 1024 {
        return Err("configuration must be a regular file <= 128 KiB".into());
    }
    // Bound the actual read as well: metadata is not a guarantee that the file stays unchanged.
    use std::io::Read;
    let mut bytes = Vec::new();
    std::fs::File::open(path)
        .map_err(|_| "cannot read configuration file")?
        .take(128 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "cannot read configuration file")?;
    if bytes.len() > 128 * 1024 {
        return Err("configuration exceeds 128 KiB".into());
    }
    let text = std::str::from_utf8(&bytes).map_err(|_| "configuration must be UTF-8")?;
    // Do not print parser diagnostics: TOML errors can quote input values.
    let spec: AppSpec = if text.trim_start().starts_with('{') {
        serde_json::from_str(text).map_err(|_| "invalid JSON application configuration")?
    } else {
        toml::from_str(text).map_err(|_| "invalid TOML application configuration")?
    };
    spec.validate().map_err(|e| e.to_string())?;
    Ok(spec)
}

fn print_json(value: &impl serde::Serialize) -> Result<(), String> {
    println!(
        "{}",
        serde_json::to_string_pretty(value).map_err(|_| "cannot serialize result")?
    );
    Ok(())
}
fn app_key(project: String, name: String) -> Result<AppKey, String> {
    Ok(AppKey {
        project: Identifier::new(project).map_err(|e| e.to_string())?,
        name: Identifier::new(name).map_err(|e| e.to_string())?,
    })
}
async fn submit_job(
    client: &Client,
    command: Command,
    options: SubmitOptions,
) -> Result<(), String> {
    let submission = Submission {
        request_id: options.request_id.unwrap_or_else(request_id),
        command,
    };
    submission.validate().map_err(|e| e.to_string())?;
    let job = client.submit(&submission).await.map_err(|e| {
        format!(
            "{e}; request_id={} (reuse it with the same command if retrying)",
            submission.request_id
        )
    })?;
    let job = if options.no_wait {
        job
    } else {
        client
            .wait(job.id, Duration::from_secs(options.wait_seconds))
            .await
            .map_err(|e| {
                format!(
                    "{e}; job_id={} (query with agentctl job {})",
                    job.id, job.id
                )
            })?
    };
    print_json(&job)?;
    if job.state == JobState::Failed {
        return Err(job
            .error
            .map(|e| e.to_string())
            .unwrap_or_else(|| "agent job failed".into()));
    }
    Ok(())
}
async fn run(cli: Cli) -> Result<(), String> {
    match cli.command {
        Commands::Validate { file } => {
            let spec = load_spec(&file)?;
            println!("valid: {}", spec.app.instance_name());
        }
        Commands::Plan { file } => {
            let spec = load_spec(&file)?;
            print_json(
                &serde_json::json!({"instance_name":spec.app.instance_name(), "image":spec.image, "resources":spec.resources,
                "network":spec.network, "ports":spec.ports, "environment_keys":spec.environment.0.keys().collect::<Vec<_>>(), "argument_count":spec.args.len(), "user":spec.user, "volumes":spec.volumes, "secrets":spec.secrets}),
            )?;
        }
        command => {
            let client = Client::new(cli.socket, Duration::from_secs(cli.request_timeout_seconds))
                .map_err(|e| e.to_string())?;
            match command {
                Commands::SecretCreate {
                    project,
                    name,
                    file,
                } => {
                    use std::io::Read;
                    let key = app_key(project, name)?;
                    let file =
                        std::fs::File::open(file).map_err(|_| "cannot open secret input file")?;
                    if !file
                        .metadata()
                        .map_err(|_| "cannot inspect secret input file")?
                        .is_file()
                    {
                        return Err("secret input must be a regular file".into());
                    }
                    let mut value = String::new();
                    file.take(65537)
                        .read_to_string(&mut value)
                        .map_err(|_| "secret input must be bounded UTF-8")?;
                    let input = agent_api::SecretInput {
                        project: key.project,
                        name: key.name,
                        value,
                    };
                    input.validate().map_err(|e| e.to_string())?;
                    print_json(
                        &client
                            .create_secret(&input)
                            .await
                            .map_err(|e| e.to_string())?,
                    )?;
                }
                Commands::Secrets { project } => {
                    let project = app_key(project, "check".into())?.project;
                    print_json(&client.secrets(&project).await.map_err(|e| e.to_string())?)?;
                }
                Commands::Diagnostics { project, name } => {
                    print_json(
                        &client
                            .diagnostics(&app_key(project, name)?)
                            .await
                            .map_err(|e| e.to_string())?,
                    )?;
                }
                Commands::Logs {
                    project,
                    name,
                    tail,
                } => {
                    print_json(
                        &client
                            .logs(&app_key(project, name)?, tail)
                            .await
                            .map_err(|e| e.to_string())?,
                    )?;
                }
                Commands::Volumes { project, name } => {
                    print_json(
                        &client
                            .volumes(&app_key(project, name)?)
                            .await
                            .map_err(|e| e.to_string())?,
                    )?;
                }
                Commands::Routes => print_json(&client.routes().await.map_err(|e| e.to_string())?)?,
                Commands::RouteSet {
                    file,
                    expected_revision,
                } => {
                    use std::io::Read;
                    let mut bytes = Vec::new();
                    std::fs::File::open(file)
                        .map_err(|_| "cannot read domain binding file")?
                        .take(16385)
                        .read_to_end(&mut bytes)
                        .map_err(|_| "cannot read domain binding file")?;
                    if bytes.len() > 16384 {
                        return Err("domain binding file too large".into());
                    }
                    let route: ingress_model::DomainRoute = serde_json::from_slice(&bytes)
                        .map_err(|_| "invalid domain binding JSON")?;
                    route.validate().map_err(str::to_owned)?;
                    print_json(
                        &client
                            .change_route(&ingress_model::RouteChange {
                                route,
                                expected_revision,
                            })
                            .await
                            .map_err(|e| e.to_string())?,
                    )?;
                }
                Commands::RouteDelete {
                    domain,
                    expected_revision,
                } => print_json(
                    &client
                        .delete_route(&ingress_model::RouteDelete {
                            domain: ingress_model::DomainName::new(domain)
                                .map_err(str::to_owned)?,
                            expected_revision,
                        })
                        .await
                        .map_err(|e| e.to_string())?,
                )?,
                Commands::Maintenance { action } => {
                    let draining = match action {
                        MaintenanceAction::Status => None,
                        MaintenanceAction::Enter => Some(true),
                        MaintenanceAction::Exit => Some(false),
                    };
                    print_json(
                        &client
                            .maintenance(draining)
                            .await
                            .map_err(|e| e.to_string())?,
                    )?;
                }
                Commands::Doctor => {
                    let health = client.health().await.map_err(|e| e.to_string())?;
                    if health.api_version != agent_api::API_VERSION || !health.accepting {
                        return Err("agent API is incompatible or unavailable".into());
                    }
                    print_json(&health)?;
                }
                Commands::Apps => {
                    print_json(&client.applications().await.map_err(|e| e.to_string())?)?
                }
                Commands::Operations { limit } => {
                    print_json(&client.operations(limit).await.map_err(|e| e.to_string())?)?
                }
                Commands::Job { id } => {
                    print_json(&client.job(id).await.map_err(|e| e.to_string())?)?
                }
                Commands::Apply {
                    file,
                    startup_wait_ms,
                    submit,
                } => {
                    submit_job(
                        &client,
                        Command::Apply {
                            spec: Box::new(load_spec(&file)?),
                            startup_wait_ms,
                        },
                        submit,
                    )
                    .await?
                }
                Commands::Start {
                    project,
                    name,
                    submit,
                } => {
                    submit_job(
                        &client,
                        Command::Start {
                            app: app_key(project, name)?,
                        },
                        submit,
                    )
                    .await?
                }
                Commands::Stop {
                    project,
                    name,
                    submit,
                } => {
                    submit_job(
                        &client,
                        Command::Stop {
                            app: app_key(project, name)?,
                        },
                        submit,
                    )
                    .await?
                }
                _ => unreachable!(),
            }
        }
    }
    Ok(())
}
#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    match run(Cli::parse()).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("error: {error}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shipped_examples_validate() {
        for file in ["http-service.toml", "ai-api.toml"] {
            let path = Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../examples")
                .join(file);
            load_spec(&path).unwrap();
        }
    }
    #[test]
    fn typo_and_future_schema_are_rejected() {
        let source = include_str!("../../../examples/http-service.toml");
        assert!(toml::from_str::<AppSpec>(&source.replace("memory_mib", "memory_mb")).is_err());
        let spec: AppSpec =
            toml::from_str(&source.replace("schema_version = 1", "schema_version = 2")).unwrap();
        assert!(spec.validate().is_err());
    }

    #[test]
    fn json_and_toml_share_the_same_model_and_validation() {
        let source = include_str!("../../../examples/http-service.toml");
        let spec: AppSpec = toml::from_str(source).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("app.json");
        let value = serde_json::to_value(&spec).unwrap();
        std::fs::write(&file, serde_json::to_vec(&value).unwrap()).unwrap();
        let loaded = load_spec(&file).unwrap();
        assert_eq!(loaded.fingerprint().unwrap(), spec.fingerprint().unwrap());
        let mut invalid = value;
        invalid["unexpected"] = true.into();
        std::fs::write(&file, serde_json::to_vec(&invalid).unwrap()).unwrap();
        let error = load_spec(&file).unwrap_err();
        assert_eq!(error, "invalid JSON application configuration");
    }

    #[test]
    fn canonical_commands_keep_defaults_and_reject_removed_aliases() {
        assert!(matches!(
            Cli::try_parse_from(["agentctl", "apps"]).unwrap().command,
            Commands::Apps
        ));
        match Cli::try_parse_from(["agentctl", "apply", "-f", "app.json"])
            .unwrap()
            .command
        {
            Commands::Apply { submit, .. } => {
                assert!(!submit.no_wait);
                assert_eq!(submit.wait_seconds, 300);
            }
            _ => panic!("expected apply command"),
        }
        assert!(matches!(
            Cli::try_parse_from(["agentctl", "doctor"]).unwrap().command,
            Commands::Doctor
        ));
        for alias in ["list", "deploy", "status"] {
            assert!(Cli::try_parse_from(["agentctl", alias]).is_err());
        }
    }
}
