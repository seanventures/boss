//! Governed operator CLI entry point for BOSS (`MT-2`, `MT-7`, `MT-3`).

use std::io::{self, IsTerminal, Read, Write};
use std::path::PathBuf;
use std::process;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use chrono::Utc;
use clap::{CommandFactory, Parser, Subcommand, ValueEnum};
use clap_complete::{Shell, generate};
use metatron_audit::AuditBuffer;
use metatron_boss_client::{
    AuthMethod, BossClient, BossClientConfig, BossClientError, DevicePollResult, PolicyFetchResult,
};
use metatron_core::{
    ApprovalPromptRequest, ApprovalPrompter, BossReviewerClient, HumanApproval, MetatronConfig,
    ResolvedProfile, ToolCtx, ToolOrchestrator, TurnDriverOptions, drive_turn,
    turn::TurnDriverError,
};
use metatron_policy::{
    GrantStore, LocalPolicyConfig, PolicyEvaluator, verify_signed_policy_bundle,
};
use metatron_protocol::{ApprovalMode, ExecEvent, ExitCode};
use metatron_remote::RemoteRunner;
use metatron_sandbox::detect_backend;
use metatron_secrets::{FileSecretStore, KeyringSecretStore, SecretStore, StoredAuth};
use metatron_session::{HistoryStore, SessionStore};
use metatron_tui::{TuiApp, run_interactive, run_plain};

#[derive(Debug, Parser)]
#[command(name = "metatron", version, about = "BOSS governed operator CLI")]
struct Cli {
    /// Named profile from ~/.config/metatron/config.toml
    #[arg(long, global = true)]
    profile: Option<String>,

    /// Config override in key=value form (repeatable, e.g. -c project=foo -c mode=risky-only)
    #[arg(short = 'c', long = "config", global = true)]
    overrides: Vec<String>,

    /// Explicit opt-in 0600 file credential store instead of the OS keychain
    #[arg(long, global = true)]
    insecure_file_store: bool,

    /// Launch plain-text REPL instead of ratatui TUI
    #[arg(long)]
    plain: bool,

    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Debug, Subcommand)]
enum Commands {
    /// Sign in to a BOSS install (RFC 8628 device code or personal API key)
    Login {
        /// BOSS API base URL (e.g. https://boss.example.com)
        #[arg(long)]
        url: String,
        /// Optional personal API key (dev / CI path instead of device-code flow)
        #[arg(long)]
        api_key: Option<String>,
        /// OAuth scope for device-code login
        #[arg(long, default_value = "metatron")]
        scope: String,
    },
    /// Revoke and delete saved credentials for the active profile
    Logout,
    /// Check config, credentials, BOSS API capabilities, policy signature, sandbox, and ssh
    Doctor,
    /// Run a single non-interactive agent turn (reads prompt from arg or stdin)
    Exec {
        /// Prompt text; omit or pass '-' to read from stdin
        prompt: Option<String>,
        /// Emit typed JSONL event stream on stdout
        #[arg(long)]
        json: bool,
        /// Write the final assistant message to FILE
        #[arg(short = 'o', long = "output-file")]
        output_file: Option<PathBuf>,
        /// Validate final JSON message against a JSON Schema file
        #[arg(long)]
        output_schema: Option<PathBuf>,
        /// Approval mode override (read-only | ask | risky-only | act-on-my-behalf)
        #[arg(long)]
        mode: Option<String>,
        /// Project ID override
        #[arg(long)]
        project: Option<String>,
        /// Agent key override
        #[arg(long)]
        agent: Option<String>,
        /// Existing server thread ID to continue
        #[arg(long)]
        thread: Option<String>,
        /// Auto-deny all human approval prompts in non-interactive scripts
        #[arg(long)]
        non_interactive: bool,
    },
    /// Generate shell completion script for bash, zsh, or fish
    Completion {
        #[arg(value_enum)]
        shell: CompletionShell,
    },
    /// Resume a saved local session JSONL
    Resume {
        /// Session ID (omit to list recent sessions)
        session_id: Option<String>,
    },
    /// Fetch and cryptographically verify the BOSS signed policy bundle
    Policy {
        #[command(subcommand)]
        action: PolicyAction,
    },
    /// List registered BOSS hosts and local ~/.ssh/config hosts
    Hosts,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum CompletionShell {
    Bash,
    Zsh,
    Fish,
}

#[derive(Debug, Subcommand)]
enum PolicyAction {
    /// Fetch /v1/metatron/policy + /v1/metatron/policy/keys and verify JWS + rule examples
    Verify,
}

struct StdioPrompter {
    non_interactive: bool,
}

#[async_trait]
impl ApprovalPrompter for StdioPrompter {
    async fn prompt(&self, req: &ApprovalPromptRequest) -> HumanApproval {
        if self.non_interactive || !io::stdin().is_terminal() {
            return HumanApproval::Deny;
        }
        let mut stderr = io::stderr();
        let _ = writeln!(
            stderr,
            "\n[APPROVAL REQUIRED] tool={} risk={}",
            req.tool, req.risk_class
        );
        let _ = writeln!(stderr, "  command: {}", req.argv_redacted.join(" "));
        if let Some(ref host) = req.host {
            let _ = writeln!(stderr, "  host: {host}");
        }
        let _ = writeln!(stderr, "  reason: {}", req.reason);
        if let Some(ref warn) = req.sandbox_warning {
            let _ = writeln!(stderr, "  WARNING: {warn}");
        }
        if req.allow_always && !req.hard && !req.is_escalation {
            let _ = write!(stderr, "Allow? [y=once / a=session / n=deny]: ");
        } else {
            let _ = write!(stderr, "Allow? [y=once / n=deny] (hard gate): ");
        }
        let _ = stderr.flush();

        let mut buf = String::new();
        if io::stdin().read_line(&mut buf).is_err() {
            return HumanApproval::Deny;
        }
        match buf.trim().to_ascii_lowercase().as_str() {
            "y" | "yes" => HumanApproval::AllowOnce,
            "a" | "always" if req.allow_always && !req.hard && !req.is_escalation => {
                HumanApproval::AllowSession
            }
            _ => HumanApproval::Deny,
        }
    }
}

fn secret_store(insecure_file_store: bool) -> Box<dyn SecretStore> {
    if insecure_file_store
        || std::env::var("METATRON_INSECURE_FILE_STORE")
            .ok()
            .as_deref()
            == Some("1")
    {
        let path = MetatronConfig::default_data_dir().join("credentials.json");
        Box::new(FileSecretStore::new(path))
    } else {
        Box::new(KeyringSecretStore::os_default())
    }
}

fn resolve_auth(store: &dyn SecretStore, profile: &str) -> AuthMethod {
    if let Ok(key) = std::env::var("BOSS_API_KEY")
        && !key.trim().is_empty()
    {
        return AuthMethod::ApiKey(key.trim().to_string());
    }
    if let Ok(Some(saved)) = store.load_auth(profile) {
        if let Some(tok) = saved.access_token {
            return AuthMethod::Bearer(tok);
        }
        if let Some(key) = saved.api_key {
            return AuthMethod::ApiKey(key);
        }
    }
    AuthMethod::None
}

async fn load_evaluator(
    client: &BossClient,
    project_id: &str,
    requested_mode: ApprovalMode,
) -> PolicyEvaluator {
    let local_policy_path = MetatronConfig::default_config_path()
        .parent()
        .map(|p| p.join("policy.toml"))
        .unwrap_or_else(|| PathBuf::from("policy.toml"));
    let local = std::fs::read_to_string(&local_policy_path)
        .ok()
        .and_then(|txt| LocalPolicyConfig::parse_toml(&txt).ok())
        .unwrap_or_default();

    let (Ok(PolicyFetchResult::Updated { response, .. }), Ok(jwks)) = (
        client.fetch_policy(project_id, None).await,
        client.fetch_policy_keys().await,
    ) else {
        return PolicyEvaluator::offline_readonly(local);
    };

    match verify_signed_policy_bundle(&response, &jwks, Utc::now()) {
        Ok(verified) => PolicyEvaluator::with_verified_bundle(
            verified,
            requested_mode,
            local,
            GrantStore::default(),
        ),
        Err(_) => PolicyEvaluator::offline_readonly(local),
    }
}

fn map_client_error_to_exit_code(err: &BossClientError) -> ExitCode {
    match err {
        BossClientError::ClientTooOld { .. } => ExitCode::ClientTooOld,
        BossClientError::Unauthorized(_) => ExitCode::AuthRequired,
        BossClientError::Forbidden(_) => ExitCode::PolicyDenied,
        BossClientError::BudgetExhausted(_) => ExitCode::BudgetExhausted,
        _ => ExitCode::RuntimeError,
    }
}

async fn cmd_login(
    url: &str,
    api_key: Option<String>,
    scope: &str,
    profile_name: &str,
    store: &dyn SecretStore,
) -> ExitCode {
    let cfg_path = MetatronConfig::default_config_path();
    let mut cfg = MetatronConfig::load_from(&cfg_path).unwrap_or_default();

    if let Some(key) = api_key {
        let auth = StoredAuth {
            url: url.trim_end_matches('/').to_string(),
            access_token: None,
            refresh_token: None,
            api_key: Some(key),
            expires_at: None,
            principal: None,
        };
        if let Err(e) = store.save_auth(profile_name, &auth) {
            eprintln!("Failed to store credentials: {e}");
            return ExitCode::RuntimeError;
        }
        cfg.upsert_profile_url(profile_name, url);
        if let Err(e) = cfg.save_to(&cfg_path) {
            eprintln!("Failed to update config.toml: {e}");
            return ExitCode::RuntimeError;
        }
        println!("Saved personal API key for profile '{profile_name}' ({url}).");
        return ExitCode::Ok;
    }

    let client = match BossClientConfig::new(url, AuthMethod::None).and_then(BossClient::new) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Invalid BOSS URL: {e}");
            return ExitCode::UsageOrConfig;
        }
    };

    let device = match client.start_device_login(scope).await {
        Ok(d) => d,
        Err(e) => {
            eprintln!("Device login start failed: {e}");
            return map_client_error_to_exit_code(&e);
        }
    };

    let verify_url = device
        .verification_uri_complete
        .as_deref()
        .unwrap_or(&device.verification_uri);
    println!("Open this URL in your browser to authorize Metatron:");
    println!("  {verify_url}");
    println!("User code: {}", device.user_code);

    let deadline = tokio::time::Instant::now() + Duration::from_secs(device.expires_in.max(30));
    let mut interval_s = device.interval.max(1);

    loop {
        if tokio::time::Instant::now() >= deadline {
            eprintln!("Device login timed out.");
            return ExitCode::Timeout;
        }
        tokio::time::sleep(Duration::from_secs(interval_s)).await;
        match client.poll_device_token(&device.device_code).await {
            Ok(DevicePollResult::Pending) => continue,
            Ok(DevicePollResult::SlowDown) => {
                interval_s = (interval_s + 5).min(30);
                continue;
            }
            Ok(DevicePollResult::Expired) => {
                eprintln!("Device code expired.");
                return ExitCode::Timeout;
            }
            Ok(DevicePollResult::AccessDenied) => {
                eprintln!("Device authorization denied.");
                return ExitCode::AuthRequired;
            }
            Ok(DevicePollResult::Token(tok)) => {
                let auth = StoredAuth {
                    url: url.trim_end_matches('/').to_string(),
                    access_token: Some(tok.access_token),
                    refresh_token: tok.refresh_token,
                    api_key: None,
                    expires_at: Some(Utc::now() + chrono::Duration::seconds(tok.expires_in as i64)),
                    principal: tok.principal.clone(),
                };
                if let Err(e) = store.save_auth(profile_name, &auth) {
                    eprintln!("Failed to store credentials in keychain: {e}");
                    return ExitCode::RuntimeError;
                }
                cfg.upsert_profile_url(profile_name, url);
                if let Err(e) = cfg.save_to(&cfg_path) {
                    eprintln!("Failed to save config.toml: {e}");
                    return ExitCode::RuntimeError;
                }
                let actor = tok
                    .principal
                    .map(|p| p.actor)
                    .unwrap_or_else(|| "operator".to_string());
                println!("Signed in as '{actor}' on {url} (profile '{profile_name}').");
                return ExitCode::Ok;
            }
            Err(e) => {
                eprintln!("Device token poll failed: {e}");
                return map_client_error_to_exit_code(&e);
            }
        }
    }
}

async fn cmd_logout(resolved: &ResolvedProfile, store: &dyn SecretStore) -> ExitCode {
    if let Ok(Some(saved)) = store.load_auth(&resolved.profile_name) {
        let token_to_revoke = saved.refresh_token.or(saved.access_token);
        if let Some(tok) = token_to_revoke
            && let Ok(cfg) = BossClientConfig::new(&saved.url, AuthMethod::Bearer(tok.clone()))
            && let Ok(client) = BossClient::new(cfg)
        {
            let _ = client.revoke_token(&tok).await;
        }
    }
    match store.delete_auth(&resolved.profile_name) {
        Ok(true) => {
            println!("Logged out profile '{}'.", resolved.profile_name);
            ExitCode::Ok
        }
        Ok(false) => {
            println!(
                "No stored credentials for profile '{}'.",
                resolved.profile_name
            );
            ExitCode::Ok
        }
        Err(e) => {
            eprintln!("Failed to delete stored credentials: {e}");
            ExitCode::RuntimeError
        }
    }
}

async fn cmd_doctor(resolved: &ResolvedProfile, store: &dyn SecretStore) -> ExitCode {
    let mut healthy = true;
    println!("Metatron Doctor (profile: {})", resolved.profile_name);
    println!("  URL:     {}", resolved.url);
    println!("  Project: {}", resolved.project);
    println!("  Mode:    {}", resolved.mode);

    let auth = resolve_auth(store, &resolved.profile_name);
    match &auth {
        AuthMethod::None => {
            println!("  [WARN] Auth: not signed in (run `metatron login --url ...`)");
            healthy = false;
        }
        AuthMethod::Bearer(_) => println!("  [OK]   Auth: OAuth Bearer token stored"),
        AuthMethod::ApiKey(_) => println!("  [OK]   Auth: personal API key configured"),
    }

    let sandbox = detect_backend();
    println!("  [OK]   Sandbox backend: {}", sandbox.as_str());

    if let Ok(cfg) = BossClientConfig::new(&resolved.url, auth).and_then(BossClient::new) {
        match cfg.capabilities().await {
            Ok(caps) => {
                println!(
                    "  [OK]   BOSS API: contract={} min_client={} features={}",
                    caps.contract,
                    caps.min_client,
                    caps.features.join(",")
                );
            }
            Err(e) => {
                println!("  [FAIL] BOSS API: {e}");
                healthy = false;
            }
        }
        match (
            cfg.fetch_policy(&resolved.project, None).await,
            cfg.fetch_policy_keys().await,
        ) {
            (Ok(PolicyFetchResult::Updated { response, .. }), Ok(jwks)) => {
                match verify_signed_policy_bundle(&response, &jwks, Utc::now()) {
                    Ok(v) => println!(
                        "  [OK]   Policy bundle: verified {} (kid={}, floor={})",
                        v.bundle.version, v.kid, v.bundle.mode_floor
                    ),
                    Err(e) => {
                        println!("  [FAIL] Policy bundle verification: {e}");
                        healthy = false;
                    }
                }
            }
            _ => println!("  [WARN] Policy bundle: unavailable (offline read-only fallback)"),
        }
    } else {
        println!("  [FAIL] Invalid BOSS URL: {}", resolved.url);
        healthy = false;
    }

    if healthy {
        ExitCode::Ok
    } else {
        ExitCode::RuntimeError
    }
}

#[allow(clippy::too_many_arguments)]
async fn cmd_exec(
    resolved: &ResolvedProfile,
    store: &dyn SecretStore,
    prompt_arg: Option<String>,
    json_mode: bool,
    output_file: Option<PathBuf>,
    output_schema_path: Option<PathBuf>,
    mode_override: Option<String>,
    project_override: Option<String>,
    agent_override: Option<String>,
    thread_override: Option<String>,
    non_interactive: bool,
) -> ExitCode {
    let prompt = match prompt_arg {
        Some(p) if p != "-" => p,
        _ => {
            let mut buf = String::new();
            if let Err(e) = io::stdin().read_to_string(&mut buf) {
                eprintln!("Failed to read prompt from stdin: {e}");
                return ExitCode::UsageOrConfig;
            }
            buf.trim().to_string()
        }
    };
    if prompt.is_empty() {
        eprintln!("Prompt is required (pass as argument or via stdin).");
        return ExitCode::UsageOrConfig;
    }

    let output_schema = if let Some(path) = output_schema_path {
        match std::fs::read_to_string(&path).and_then(|s| {
            serde_json::from_str(&s).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
        }) {
            Ok(v) => Some(v),
            Err(e) => {
                eprintln!("Invalid --output-schema file: {e}");
                return ExitCode::UsageOrConfig;
            }
        }
    } else {
        None
    };

    let mode = if let Some(m) = mode_override {
        match m.parse::<ApprovalMode>() {
            Ok(parsed) => parsed,
            Err(e) => {
                eprintln!("{e}");
                return ExitCode::UsageOrConfig;
            }
        }
    } else {
        resolved.mode
    };
    let project_id = project_override.unwrap_or_else(|| resolved.project.clone());
    let agent = agent_override.or_else(|| resolved.agent.clone());

    let auth = resolve_auth(store, &resolved.profile_name);
    let client = match BossClientConfig::new(&resolved.url, auth).and_then(BossClient::new) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Invalid BOSS client config: {e}");
            return ExitCode::UsageOrConfig;
        }
    };

    let data_dir = MetatronConfig::default_data_dir();
    let sessions_dir = data_dir.join("sessions");
    let scratch_dir = data_dir.join("scratch");
    let _ = std::fs::create_dir_all(&scratch_dir);
    let session_store = match SessionStore::new(sessions_dir) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("Failed to open session store: {e}");
            return ExitCode::RuntimeError;
        }
    };
    let (session_id, _) =
        match session_store.create(&project_id, thread_override.clone(), agent.clone(), mode) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("Failed to create session: {e}");
                return ExitCode::RuntimeError;
            }
        };
    let audit = match AuditBuffer::new(data_dir.join("audit-queue.jsonl")) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("Failed to open audit queue: {e}");
            return ExitCode::RuntimeError;
        }
    };

    let evaluator = load_evaluator(&client, &project_id, mode).await;
    let effective_mode = evaluator.effective_mode();
    let prompter = Arc::new(StdioPrompter { non_interactive });
    let reviewer = Arc::new(BossReviewerClient::new(client.clone()));
    let orchestrator = ToolOrchestrator::new(
        evaluator,
        prompter,
        Some(reviewer),
        audit,
        Some(session_store.clone()),
    );

    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let tool_ctx = ToolCtx {
        project_id: project_id.clone(),
        session_id: session_id.clone(),
        cwd: cwd.clone(),
        scratch_dir,
        allowed_read_roots: vec![cwd.clone()],
        allow_unsandboxed_retry: false,
        boss_client: Some(client.clone()),
        remote_runner: RemoteRunner::default(),
        cancel_rx: None,
    };

    let opts = TurnDriverOptions {
        project_id,
        thread_id: thread_override,
        agent,
        mode: effective_mode,
        cwd,
        session_id,
        max_reconnects: 3,
        output_file,
        output_schema,
    };

    let mut stdout = io::stdout();
    let res = drive_turn(
        &client,
        &orchestrator,
        Some(&session_store),
        &tool_ctx,
        &opts,
        &prompt,
        |ev| {
            if json_mode {
                if let Ok(line) = serde_json::to_string(ev) {
                    let _ = writeln!(stdout, "{line}");
                    let _ = stdout.flush();
                }
            } else if let ExecEvent::ItemUpdated { delta, .. } = ev {
                let _ = write!(stdout, "{delta}");
                let _ = stdout.flush();
            }
        },
    )
    .await;

    if !json_mode {
        let _ = writeln!(stdout);
    }

    match res {
        Ok(_) => ExitCode::Ok,
        Err(TurnDriverError::BossClient(ref e)) => {
            eprintln!("{e}");
            map_client_error_to_exit_code(e)
        }
        Err(TurnDriverError::TurnFailed { code, message, .. }) => {
            eprintln!("Turn failed ({code}): {message}");
            match code.as_str() {
                "budget_exhausted" => ExitCode::BudgetExhausted,
                "policy_denied" => ExitCode::PolicyDenied,
                "tool_result_timeout" => ExitCode::Timeout,
                _ => ExitCode::RuntimeError,
            }
        }
        Err(TurnDriverError::SchemaMismatch(msg)) => {
            eprintln!("Schema validation error: {msg}");
            ExitCode::SchemaValidationFailed
        }
        Err(e) => {
            eprintln!("{e}");
            ExitCode::RuntimeError
        }
    }
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();

    // Completion needs no config/network access.
    if let Some(Commands::Completion { shell }) = cli.command {
        let mut cmd = Cli::command();
        let target = match shell {
            CompletionShell::Bash => Shell::Bash,
            CompletionShell::Zsh => Shell::Zsh,
            CompletionShell::Fish => Shell::Fish,
        };
        generate(target, &mut cmd, "metatron", &mut io::stdout());
        return;
    }

    let store = secret_store(cli.insecure_file_store);
    let cfg_path = MetatronConfig::default_config_path();
    let cfg = match MetatronConfig::load_from(&cfg_path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to read config {}: {e}", cfg_path.display());
            process::exit(ExitCode::UsageOrConfig.as_i32());
        }
    };

    if let Some(Commands::Login {
        url,
        api_key,
        scope,
    }) = cli.command
    {
        let profile = cli
            .profile
            .or_else(|| cfg.default_profile.clone())
            .unwrap_or_else(|| "default".to_string());
        let code = cmd_login(&url, api_key, &scope, &profile, store.as_ref()).await;
        process::exit(code.as_i32());
    }

    let resolved = match cfg.resolve(cli.profile.as_deref(), &cli.overrides) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("Configuration error: {e}");
            process::exit(ExitCode::UsageOrConfig.as_i32());
        }
    };

    let code = match cli.command {
        Some(Commands::Logout) => cmd_logout(&resolved, store.as_ref()).await,
        Some(Commands::Doctor) => cmd_doctor(&resolved, store.as_ref()).await,
        Some(Commands::Exec {
            prompt,
            json,
            output_file,
            output_schema,
            mode,
            project,
            agent,
            thread,
            non_interactive,
        }) => {
            cmd_exec(
                &resolved,
                store.as_ref(),
                prompt,
                json,
                output_file,
                output_schema,
                mode,
                project,
                agent,
                thread,
                non_interactive,
            )
            .await
        }
        Some(Commands::Resume { session_id }) => {
            let store_dir = MetatronConfig::default_data_dir().join("sessions");
            match SessionStore::new(store_dir) {
                Ok(ss) => {
                    if let Some(sid) = session_id {
                        match ss.export_markdown(&sid) {
                            Ok(md) => {
                                println!("{md}");
                                ExitCode::Ok
                            }
                            Err(e) => {
                                eprintln!("{e}");
                                ExitCode::RuntimeError
                            }
                        }
                    } else {
                        match ss.list_sessions() {
                            Ok(list) => {
                                for s in list {
                                    println!(
                                        "{}  {}  {}  {}",
                                        s.session_id,
                                        s.project_id,
                                        s.created_at.to_rfc3339(),
                                        s.preview
                                    );
                                }
                                ExitCode::Ok
                            }
                            Err(e) => {
                                eprintln!("{e}");
                                ExitCode::RuntimeError
                            }
                        }
                    }
                }
                Err(e) => {
                    eprintln!("{e}");
                    ExitCode::RuntimeError
                }
            }
        }
        Some(Commands::Policy {
            action: PolicyAction::Verify,
        }) => {
            let auth = resolve_auth(store.as_ref(), &resolved.profile_name);
            let Ok(client) = BossClientConfig::new(&resolved.url, auth).and_then(BossClient::new)
            else {
                eprintln!("Invalid URL: {}", resolved.url);
                process::exit(ExitCode::UsageOrConfig.as_i32());
            };
            match (
                client.fetch_policy(&resolved.project, None).await,
                client.fetch_policy_keys().await,
            ) {
                (Ok(PolicyFetchResult::Updated { response, .. }), Ok(jwks)) => {
                    match verify_signed_policy_bundle(&response, &jwks, Utc::now()) {
                        Ok(v) => {
                            println!(
                                "Policy verified: version={} kid={} floor={} rules={}",
                                v.bundle.version,
                                v.kid,
                                v.bundle.mode_floor,
                                v.bundle.rules.len()
                            );
                            ExitCode::Ok
                        }
                        Err(e) => {
                            eprintln!("Policy verification FAILED: {e}");
                            ExitCode::PolicyDenied
                        }
                    }
                }
                (Err(e), _) | (_, Err(e)) => {
                    eprintln!("Policy fetch failed: {e}");
                    map_client_error_to_exit_code(&e)
                }
                _ => ExitCode::Ok,
            }
        }
        Some(Commands::Hosts) => {
            let auth = resolve_auth(store.as_ref(), &resolved.profile_name);
            let client = BossClientConfig::new(&resolved.url, auth)
                .and_then(BossClient::new)
                .ok();
            let runner = RemoteRunner::default();
            match runner
                .discover_hosts(client.as_ref(), &resolved.project)
                .await
            {
                Ok(hosts) => {
                    for h in hosts {
                        println!("{:<24} [{}]", h.name(), h.origin().as_str());
                    }
                    ExitCode::Ok
                }
                Err(e) => {
                    eprintln!("Host discovery failed: {e}");
                    ExitCode::RuntimeError
                }
            }
        }
        Some(Commands::Login { .. } | Commands::Completion { .. }) => ExitCode::Ok,
        None => {
            // Default interactive TUI (or plain REPL if --plain or non-TTY).
            let auth = resolve_auth(store.as_ref(), &resolved.profile_name);
            let client = match BossClientConfig::new(&resolved.url, auth).and_then(BossClient::new)
            {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("Invalid BOSS URL: {e}");
                    process::exit(ExitCode::UsageOrConfig.as_i32());
                }
            };
            let data_dir = MetatronConfig::default_data_dir();
            let session_store = match SessionStore::new(data_dir.join("sessions")) {
                Ok(s) => s,
                Err(e) => {
                    eprintln!("Session store error: {e}");
                    process::exit(ExitCode::RuntimeError.as_i32());
                }
            };
            let (session_id, _) = match session_store.create(
                &resolved.project,
                None,
                resolved.agent.clone(),
                resolved.mode,
            ) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("Session create error: {e}");
                    process::exit(ExitCode::RuntimeError.as_i32());
                }
            };
            let audit = match AuditBuffer::new(data_dir.join("audit-queue.jsonl")) {
                Ok(a) => a,
                Err(e) => {
                    eprintln!("Audit queue error: {e}");
                    process::exit(ExitCode::RuntimeError.as_i32());
                }
            };
            let evaluator = load_evaluator(&client, &resolved.project, resolved.mode).await;
            let prompter = Arc::new(StdioPrompter {
                non_interactive: false,
            });
            let reviewer = Arc::new(BossReviewerClient::new(client.clone()));
            let mut orchestrator = ToolOrchestrator::new(
                evaluator,
                prompter,
                Some(reviewer),
                audit,
                Some(session_store.clone()),
            );

            let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
            let scratch_dir = data_dir.join("scratch");
            let _ = std::fs::create_dir_all(&scratch_dir);
            let tool_ctx = ToolCtx {
                project_id: resolved.project.clone(),
                session_id: session_id.clone(),
                cwd: cwd.clone(),
                scratch_dir,
                allowed_read_roots: vec![cwd],
                allow_unsandboxed_retry: false,
                boss_client: Some(client.clone()),
                remote_runner: RemoteRunner::default(),
                cancel_rx: None,
            };

            let history = HistoryStore::new(data_dir.join("history.txt"));
            let mut app = TuiApp::new(
                resolved.url.clone(),
                resolved.project.clone(),
                resolved.agent.clone(),
                orchestrator.evaluator().effective_mode(),
                session_id,
                Some(history),
            );

            let res = if cli.plain || !io::stdout().is_terminal() || !io::stdin().is_terminal() {
                run_plain(
                    &mut app,
                    &client,
                    &mut orchestrator,
                    &session_store,
                    &tool_ctx,
                )
                .await
            } else {
                run_interactive(
                    &mut app,
                    &client,
                    &mut orchestrator,
                    &session_store,
                    &tool_ctx,
                )
                .await
            };
            match res {
                Ok(()) => ExitCode::Ok,
                Err(e) => {
                    eprintln!("TUI error: {e}");
                    ExitCode::RuntimeError
                }
            }
        }
    };

    process::exit(code.as_i32());
}
