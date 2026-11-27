//! Governed Tool trait, built-in `shell_local` and `ssh_run` tools, and `ToolOrchestrator`.
//!
//! Enforces the 7-step evaluation order from `ARCHITECTURE.md` §5:
//! 1. Filter tools shown to the model.
//! 2. Parse & classify (`argv`, `host`, `domain`, `RiskClass`).
//! 3. Evaluate BOSS signed policy + MT-1 verb rules + local tightening.
//! 4. Mode resolution: auto-allow, human prompt, or server-side reviewer (never for hard rules).
//! 5. Run inside OS sandbox (`shell_local`) or over SSH with vault-lent ephemeral key (`ssh_run`).
//! 6. Escalate on sandbox denial with a **fresh** human decision.
//! 7. Record redacted `AuditEvent` in `AuditBuffer` and full output in `SessionStore`.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use async_trait::async_trait;
use chrono::Utc;
use metatron_audit::AuditBuffer;
use metatron_boss_client::{BossClient, BossClientError};
use metatron_exec::{ExecRequest, run_command};
use metatron_policy::{
    EvaluationDecision, PolicyEvaluator, ToolCallContext, classify_command_argv, parse_command_argv,
};
use metatron_protocol::{
    ApprovalReviewRequest, ApprovalReviewResponse, ClientToolSpec, DecisionSource,
    ReviewCallPayload, ReviewerDecision, RiskClass, ToolContentItem, ToolExposure,
    ToolResultDecision, ToolResultOutput, ToolResultStatus, ToolResultSubmission,
};
use metatron_remote::{REMOTE_SANDBOX_WARNING, RemoteRunner};
use metatron_sandbox::{SandboxError, SandboxPolicy, looks_like_sandbox_denial, wrap_command};
use metatron_secrets::{redact_argv, redact_secrets};
use metatron_session::{SessionItem, SessionStore};
use serde_json::{Value, json};
use thiserror::Error;
use tokio::sync::watch;

#[derive(Debug, Error)]
pub enum OrchestratorError {
    #[error("unknown client tool '{0}'")]
    UnknownTool(String),
    #[error("invalid tool arguments: {0}")]
    InvalidArgs(String),
    #[error("reviewer call failed: {0}")]
    Reviewer(#[from] BossClientError),
}

#[derive(Debug, Clone)]
pub struct ApprovalPromptRequest {
    pub call_id: String,
    pub tool: String,
    pub risk_class: RiskClass,
    pub argv_redacted: Vec<String>,
    pub host: Option<String>,
    pub domain: Option<String>,
    pub reason: String,
    pub rule_id: Option<String>,
    pub hard: bool,
    pub allow_always: bool,
    pub is_escalation: bool,
    pub sandbox_warning: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HumanApproval {
    AllowOnce,
    AllowSession,
    Deny,
}

#[async_trait]
pub trait ApprovalPrompter: Send + Sync {
    async fn prompt(&self, req: &ApprovalPromptRequest) -> HumanApproval;
}

#[async_trait]
pub trait ReviewerBackend: Send + Sync {
    async fn review(
        &self,
        req: &ApprovalReviewRequest,
    ) -> Result<ApprovalReviewResponse, BossClientError>;
}

pub struct BossReviewerClient {
    client: BossClient,
}

impl BossReviewerClient {
    pub fn new(client: BossClient) -> Self {
        Self { client }
    }
}

#[async_trait]
impl ReviewerBackend for BossReviewerClient {
    async fn review(
        &self,
        req: &ApprovalReviewRequest,
    ) -> Result<ApprovalReviewResponse, BossClientError> {
        self.client.review_approval(req).await
    }
}

#[derive(Debug, Clone)]
pub struct ToolCtx {
    pub project_id: String,
    pub session_id: String,
    pub cwd: PathBuf,
    pub scratch_dir: PathBuf,
    pub allowed_read_roots: Vec<PathBuf>,
    pub allow_unsandboxed_retry: bool,
    pub boss_client: Option<BossClient>,
    pub remote_runner: RemoteRunner,
    pub cancel_rx: Option<watch::Receiver<bool>>,
}

#[derive(Debug, Clone)]
pub struct ToolOutcome {
    pub status: ToolResultStatus,
    pub inline_text: String,
    pub full_text: String,
    pub truncated: bool,
    pub exit_code: Option<i32>,
    pub bytes: usize,
    pub duration_ms: u64,
    pub host: Option<String>,
    pub host_origin: Option<String>,
    pub sandbox_denied: bool,
}

#[async_trait]
pub trait Tool: Send + Sync {
    fn name(&self) -> &str;
    fn spec(&self) -> ClientToolSpec;
    fn exposure(&self) -> ToolExposure {
        ToolExposure::Direct
    }
    fn risk(&self, args: &Value) -> RiskClass;
    fn parallel_ok(&self) -> bool {
        false
    }
    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome;
}

/// Built-in sandboxed local shell tool (`MT-3a`).
pub struct ShellLocalTool;

impl ShellLocalTool {
    fn extract_argv(args: &Value) -> Result<Vec<String>, String> {
        if let Some(arr) = args.get("argv").and_then(Value::as_array) {
            let mut out = Vec::with_capacity(arr.len());
            for item in arr {
                let s = item
                    .as_str()
                    .ok_or_else(|| "argv elements must be strings".to_string())?;
                out.push(s.to_string());
            }
            if out.is_empty() {
                return Err("argv must not be empty".to_string());
            }
            return Ok(out);
        }
        if let Some(cmd) = args.get("command").and_then(Value::as_str) {
            return parse_command_argv(cmd).map_err(|e| e.to_string());
        }
        Err("shell_local requires 'argv' or 'command'".to_string())
    }
}

#[async_trait]
impl Tool for ShellLocalTool {
    fn name(&self) -> &str {
        "shell_local"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "shell_local".to_string(),
            description: "Run a read-only or analysis command locally inside the OS sandbox (read-only roots + session scratch, network off by default).".to_string(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "argv": { "type": "array", "items": { "type": "string" } },
                    "command": { "type": "string" },
                    "timeout_s": { "type": "integer", "minimum": 1, "maximum": 300 },
                    "network": { "type": "boolean" }
                }
            }),
            risk_class: "read-local".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, args: &Value) -> RiskClass {
        match Self::extract_argv(args) {
            Ok(argv) => classify_command_argv(&argv, false).risk_class,
            Err(_) => RiskClass::WriteLocal,
        }
    }

    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let argv = match Self::extract_argv(&args) {
            Ok(a) => a,
            Err(e) => {
                return ToolOutcome {
                    status: ToolResultStatus::Error,
                    inline_text: e.clone(),
                    full_text: e,
                    truncated: false,
                    exit_code: Some(2),
                    bytes: 0,
                    duration_ms: 0,
                    host: None,
                    host_origin: None,
                    sandbox_denied: false,
                };
            }
        };
        let timeout_s = args
            .get("timeout_s")
            .and_then(Value::as_u64)
            .unwrap_or(60)
            .clamp(1, 300);
        let network_allowed = args
            .get("network")
            .and_then(Value::as_bool)
            .unwrap_or(false);

        let (program, cmd_args, ran_sandboxed) = if ctx.allow_unsandboxed_retry {
            (argv[0].clone(), argv[1..].to_vec(), false)
        } else {
            let policy = SandboxPolicy {
                allowed_read_roots: ctx.allowed_read_roots.clone(),
                writable_scratch_dir: ctx.scratch_dir.clone(),
                network_allowed,
            };
            match wrap_command(&policy, &argv[0], &argv[1..], &ctx.cwd) {
                Ok(wrapped) => (wrapped.program, wrapped.args, true),
                Err(SandboxError::Unavailable) => {
                    return ToolOutcome {
                        status: ToolResultStatus::Error,
                        inline_text: "sandbox unavailable on this host; requires explicit escalation approval".to_string(),
                        full_text: "sandbox unavailable".to_string(),
                        truncated: false,
                        exit_code: Some(126),
                        bytes: 0,
                        duration_ms: 0,
                        host: None,
                        host_origin: None,
                        sandbox_denied: true,
                    };
                }
                Err(e) => {
                    let msg = e.to_string();
                    return ToolOutcome {
                        status: ToolResultStatus::Error,
                        inline_text: msg.clone(),
                        full_text: msg,
                        truncated: false,
                        exit_code: Some(2),
                        bytes: 0,
                        duration_ms: 0,
                        host: None,
                        host_origin: None,
                        sandbox_denied: false,
                    };
                }
            }
        };

        let mut req = ExecRequest::new(program, cmd_args, ctx.cwd.clone());
        req.timeout = Duration::from_secs(timeout_s);
        match run_command(req, ctx.cancel_rx.clone()).await {
            Ok(out) => {
                let sandbox_denied =
                    ran_sandboxed && looks_like_sandbox_denial(out.exit_code, &out.inline_output);
                let status = if out.cancelled {
                    ToolResultStatus::Cancelled
                } else if out.exit_code == 0 {
                    ToolResultStatus::Ok
                } else {
                    ToolResultStatus::Error
                };
                ToolOutcome {
                    status,
                    inline_text: redact_secrets(&out.inline_output),
                    full_text: redact_secrets(&out.full_output),
                    truncated: out.truncated,
                    exit_code: Some(out.exit_code),
                    bytes: out.total_bytes,
                    duration_ms: out.duration_ms,
                    host: None,
                    host_origin: None,
                    sandbox_denied,
                }
            }
            Err(e) => {
                let msg = e.to_string();
                ToolOutcome {
                    status: ToolResultStatus::Error,
                    inline_text: msg.clone(),
                    full_text: msg,
                    truncated: false,
                    exit_code: Some(1),
                    bytes: 0,
                    duration_ms: 0,
                    host: None,
                    host_origin: None,
                    sandbox_denied: false,
                }
            }
        }
    }
}

/// Built-in governed remote SSH tool (`MT-3b`).
pub struct SshRunTool;

impl SshRunTool {
    fn extract_host_and_argv(args: &Value) -> Result<(String, Vec<String>), String> {
        let host = args
            .get("host")
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| "ssh_run requires 'host'".to_string())?
            .to_string();
        let argv = ShellLocalTool::extract_argv(args)?;
        Ok((host, argv))
    }
}

#[async_trait]
impl Tool for SshRunTool {
    fn name(&self) -> &str {
        "ssh_run"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "ssh_run".to_string(),
            description: "Run a governed command over SSH on a registered BOSS host (vault-lent ephemeral key) or a host from ~/.ssh/config.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["host"],
                "properties": {
                    "host": { "type": "string" },
                    "argv": { "type": "array", "items": { "type": "string" } },
                    "command": { "type": "string" },
                    "purpose": { "type": "string" }
                }
            }),
            risk_class: "remote-read".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, args: &Value) -> RiskClass {
        match Self::extract_host_and_argv(args) {
            Ok((_h, argv)) => classify_command_argv(&argv, true).risk_class,
            Err(_) => RiskClass::RemoteWrite,
        }
    }

    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let (host_name, argv) = match Self::extract_host_and_argv(&args) {
            Ok(v) => v,
            Err(e) => {
                return ToolOutcome {
                    status: ToolResultStatus::Error,
                    inline_text: e.clone(),
                    full_text: e,
                    truncated: false,
                    exit_code: Some(2),
                    bytes: 0,
                    duration_ms: 0,
                    host: None,
                    host_origin: None,
                    sandbox_denied: false,
                };
            }
        };
        let purpose = args
            .get("purpose")
            .and_then(Value::as_str)
            .unwrap_or("metatron ssh_run");

        let resolved = match ctx
            .remote_runner
            .resolve_host(ctx.boss_client.as_ref(), &ctx.project_id, &host_name)
            .await
        {
            Ok(h) => h,
            Err(e) => {
                let msg = e.to_string();
                return ToolOutcome {
                    status: ToolResultStatus::Error,
                    inline_text: msg.clone(),
                    full_text: msg,
                    truncated: false,
                    exit_code: Some(1),
                    bytes: 0,
                    duration_ms: 0,
                    host: Some(host_name),
                    host_origin: None,
                    sandbox_denied: false,
                };
            }
        };

        match ctx
            .remote_runner
            .ssh_run(
                ctx.boss_client.as_ref(),
                &resolved,
                &argv,
                Some(&ctx.session_id),
                purpose,
                &ctx.cwd,
                ctx.cancel_rx.clone(),
            )
            .await
        {
            Ok(out) => {
                let status = if out.exec.cancelled {
                    ToolResultStatus::Cancelled
                } else if out.exec.exit_code == 0 {
                    ToolResultStatus::Ok
                } else {
                    ToolResultStatus::Error
                };
                let inline = format!(
                    "[{}; origin={}]\n{}",
                    out.warning,
                    out.origin.as_str(),
                    redact_secrets(&out.exec.inline_output)
                );
                ToolOutcome {
                    status,
                    inline_text: inline,
                    full_text: redact_secrets(&out.exec.full_output),
                    truncated: out.exec.truncated,
                    exit_code: Some(out.exec.exit_code),
                    bytes: out.exec.total_bytes,
                    duration_ms: out.exec.duration_ms,
                    host: Some(out.host),
                    host_origin: Some(out.origin.as_str().to_string()),
                    sandbox_denied: false,
                }
            }
            Err(e) => {
                let msg = e.to_string();
                ToolOutcome {
                    status: ToolResultStatus::Error,
                    inline_text: msg.clone(),
                    full_text: msg,
                    truncated: false,
                    exit_code: Some(1),
                    bytes: 0,
                    duration_ms: 0,
                    host: Some(host_name),
                    host_origin: Some(resolved.origin().as_str().to_string()),
                    sandbox_denied: false,
                }
            }
        }
    }
}

/// Central tool router and governance orchestrator (`ARCHITECTURE.md` §5).
pub struct ToolOrchestrator {
    tools: HashMap<String, Arc<dyn Tool>>,
    evaluator: PolicyEvaluator,
    prompter: Arc<dyn ApprovalPrompter>,
    reviewer: Option<Arc<dyn ReviewerBackend>>,
    audit: AuditBuffer,
    session_store: Option<SessionStore>,
    session_allowlist: Mutex<HashSet<String>>,
}

impl ToolOrchestrator {
    pub fn new(
        evaluator: PolicyEvaluator,
        prompter: Arc<dyn ApprovalPrompter>,
        reviewer: Option<Arc<dyn ReviewerBackend>>,
        audit: AuditBuffer,
        session_store: Option<SessionStore>,
    ) -> Self {
        let mut tools: HashMap<String, Arc<dyn Tool>> = HashMap::new();
        let local: Arc<dyn Tool> = Arc::new(ShellLocalTool);
        let remote: Arc<dyn Tool> = Arc::new(SshRunTool);
        tools.insert(local.name().to_string(), local);
        tools.insert(remote.name().to_string(), remote);
        Self {
            tools,
            evaluator,
            prompter,
            reviewer,
            audit,
            session_store,
            session_allowlist: Mutex::new(HashSet::new()),
        }
    }

    pub fn register_tool(&mut self, tool: Arc<dyn Tool>) {
        self.tools.insert(tool.name().to_string(), tool);
    }

    pub fn evaluator(&self) -> &PolicyEvaluator {
        &self.evaluator
    }

    pub fn evaluator_mut(&mut self) -> &mut PolicyEvaluator {
        &mut self.evaluator
    }

    pub fn audit(&self) -> &AuditBuffer {
        &self.audit
    }

    /// Step 1: list tools allowed to be advertised to the model.
    pub fn visible_tools(&self) -> Vec<ClientToolSpec> {
        let all: Vec<ClientToolSpec> = self.tools.values().map(|t| t.spec()).collect();
        self.evaluator.filter_tools(&all)
    }

    /// Steps 2-7: classify, evaluate policy + mode, prompt/review, execute, escalate on
    /// sandbox denial with a fresh decision, record local session JSONL, and buffer audit.
    #[allow(clippy::too_many_arguments)]
    pub async fn execute_tool_call(
        &self,
        ctx: &ToolCtx,
        turn_id: Option<&str>,
        call_id: &str,
        tool_name: &str,
        args: Value,
    ) -> Result<ToolResultSubmission, OrchestratorError> {
        let tool = self
            .tools
            .get(tool_name)
            .cloned()
            .ok_or_else(|| OrchestratorError::UnknownTool(tool_name.to_string()))?;

        let is_remote = tool_name == "ssh_run";
        let host = args
            .get("host")
            .and_then(Value::as_str)
            .map(ToString::to_string);
        let domain = args
            .get("domain")
            .and_then(Value::as_str)
            .map(ToString::to_string);

        // Step 2: parse argv fail-closed for shell_local / ssh_run.
        let argv = if matches!(tool_name, "shell_local" | "ssh_run") {
            match ShellLocalTool::extract_argv(&args) {
                Ok(a) => a,
                Err(err_msg) => {
                    return Ok(self.record_denied(
                        ctx,
                        call_id,
                        tool_name,
                        if is_remote {
                            RiskClass::RemoteWrite
                        } else {
                            RiskClass::WriteLocal
                        },
                        &[],
                        host,
                        domain,
                        DecisionSource::Policy,
                        Some("argv-parse-fail-closed".to_string()),
                        &err_msg,
                    ));
                }
            }
        } else {
            Vec::new()
        };

        let risk_class = tool.risk(&args);
        let call_ctx = ToolCallContext {
            tool: tool_name.to_string(),
            argv: argv.clone(),
            host: host.clone(),
            domain: domain.clone(),
            risk_class,
            is_remote,
        };
        let argv_redacted = redact_argv(&argv);
        let effective_mode = self.evaluator.effective_mode();
        let now = Utc::now();

        // Step 3 & 4: policy & mode evaluation.
        let eval = self.evaluator.evaluate(&call_ctx, now);
        let (decided_by, rule_id) = match eval {
            EvaluationDecision::AutoAllow {
                decided_by,
                rule_id,
                ..
            } => (decided_by, rule_id),
            EvaluationDecision::Deny {
                decided_by,
                rule_id,
                reason,
            } => {
                return Ok(self.record_denied(
                    ctx,
                    call_id,
                    tool_name,
                    risk_class,
                    &argv_redacted,
                    host,
                    domain,
                    decided_by,
                    rule_id,
                    &reason,
                ));
            }
            EvaluationDecision::AskReviewer {
                rule_id, reason, ..
            } => {
                // Hard invariant: never trust reviewer for hard classes even if misrouted.
                if risk_class.is_hard_prompt() {
                    let approved = self
                        .ask_human(
                            call_id,
                            tool_name,
                            risk_class,
                            &argv_redacted,
                            host.clone(),
                            domain.clone(),
                            &reason,
                            rule_id.clone(),
                            true,
                            false,
                            false,
                            is_remote.then(|| REMOTE_SANDBOX_WARNING.to_string()),
                        )
                        .await;
                    if approved == HumanApproval::Deny {
                        return Ok(self.record_denied(
                            ctx,
                            call_id,
                            tool_name,
                            risk_class,
                            &argv_redacted,
                            host,
                            domain,
                            DecisionSource::Person,
                            rule_id,
                            "denied by operator",
                        ));
                    }
                    (DecisionSource::Person, rule_id)
                } else if let Some(ref reviewer) = self.reviewer {
                    let rev_req = ApprovalReviewRequest {
                        project_id: ctx.project_id.clone(),
                        session_id: ctx.session_id.clone(),
                        turn_id: turn_id.map(ToString::to_string),
                        call: ReviewCallPayload {
                            tool: tool_name.to_string(),
                            argv_redacted: argv_redacted.clone(),
                            host: host.clone(),
                            domain: domain.clone(),
                            risk_class,
                        },
                        mode: effective_mode,
                    };
                    match reviewer.review(&rev_req).await {
                        Ok(resp) => match resp.decision {
                            ReviewerDecision::Approve => (DecisionSource::Reviewer, rule_id),
                            ReviewerDecision::Deny => {
                                return Ok(self.record_denied(
                                    ctx,
                                    call_id,
                                    tool_name,
                                    risk_class,
                                    &argv_redacted,
                                    host,
                                    domain,
                                    DecisionSource::Reviewer,
                                    rule_id,
                                    &resp.reason,
                                ));
                            }
                            ReviewerDecision::Escalate => {
                                let ans = self
                                    .ask_human(
                                        call_id,
                                        tool_name,
                                        risk_class,
                                        &argv_redacted,
                                        host.clone(),
                                        domain.clone(),
                                        &format!("Reviewer escalated: {}", resp.reason),
                                        rule_id.clone(),
                                        false,
                                        true,
                                        false,
                                        is_remote.then(|| REMOTE_SANDBOX_WARNING.to_string()),
                                    )
                                    .await;
                                if ans == HumanApproval::Deny {
                                    return Ok(self.record_denied(
                                        ctx,
                                        call_id,
                                        tool_name,
                                        risk_class,
                                        &argv_redacted,
                                        host,
                                        domain,
                                        DecisionSource::Person,
                                        rule_id,
                                        "denied by operator after reviewer escalation",
                                    ));
                                }
                                (DecisionSource::Person, rule_id)
                            }
                        },
                        Err(e) => {
                            // Reviewer failure fails closed -> ask the person!
                            let ans = self
                                .ask_human(
                                    call_id,
                                    tool_name,
                                    risk_class,
                                    &argv_redacted,
                                    host.clone(),
                                    domain.clone(),
                                    &format!("Reviewer unavailable ({e}); asking operator"),
                                    rule_id.clone(),
                                    false,
                                    true,
                                    false,
                                    is_remote.then(|| REMOTE_SANDBOX_WARNING.to_string()),
                                )
                                .await;
                            if ans == HumanApproval::Deny {
                                return Ok(self.record_denied(
                                    ctx,
                                    call_id,
                                    tool_name,
                                    risk_class,
                                    &argv_redacted,
                                    host,
                                    domain,
                                    DecisionSource::Person,
                                    rule_id,
                                    "denied by operator",
                                ));
                            }
                            (DecisionSource::Person, rule_id)
                        }
                    }
                } else {
                    let ans = self
                        .ask_human(
                            call_id,
                            tool_name,
                            risk_class,
                            &argv_redacted,
                            host.clone(),
                            domain.clone(),
                            &reason,
                            rule_id.clone(),
                            false,
                            true,
                            false,
                            is_remote.then(|| REMOTE_SANDBOX_WARNING.to_string()),
                        )
                        .await;
                    if ans == HumanApproval::Deny {
                        return Ok(self.record_denied(
                            ctx,
                            call_id,
                            tool_name,
                            risk_class,
                            &argv_redacted,
                            host,
                            domain,
                            DecisionSource::Person,
                            rule_id,
                            "denied by operator",
                        ));
                    }
                    (DecisionSource::Person, rule_id)
                }
            }
            EvaluationDecision::AskHuman {
                rule_id,
                reason,
                allow_always,
                hard,
            } => {
                let cache_key = format!("{tool_name}:{}", argv_redacted.join(" "));
                let session_cached = if allow_always && !hard {
                    let guard = self
                        .session_allowlist
                        .lock()
                        .unwrap_or_else(PoisonError::into_inner);
                    guard.contains(&cache_key)
                } else {
                    false
                };

                if !session_cached {
                    let ans = self
                        .ask_human(
                            call_id,
                            tool_name,
                            risk_class,
                            &argv_redacted,
                            host.clone(),
                            domain.clone(),
                            &reason,
                            rule_id.clone(),
                            hard,
                            allow_always,
                            false,
                            is_remote.then(|| REMOTE_SANDBOX_WARNING.to_string()),
                        )
                        .await;
                    match ans {
                        HumanApproval::Deny => {
                            return Ok(self.record_denied(
                                ctx,
                                call_id,
                                tool_name,
                                risk_class,
                                &argv_redacted,
                                host,
                                domain,
                                DecisionSource::Person,
                                rule_id,
                                "denied by operator",
                            ));
                        }
                        HumanApproval::AllowSession if allow_always && !hard => {
                            let mut guard = self
                                .session_allowlist
                                .lock()
                                .unwrap_or_else(PoisonError::into_inner);
                            guard.insert(cache_key);
                        }
                        HumanApproval::AllowOnce | HumanApproval::AllowSession => {}
                    }
                }
                (DecisionSource::Person, rule_id)
            }
        };

        // Record tool call + approval in local session JSONL.
        if let Some(ref store) = self.session_store {
            let _ = store.append(
                &ctx.session_id,
                &SessionItem::ToolCall {
                    ts: Utc::now(),
                    call_id: call_id.to_string(),
                    tool: tool_name.to_string(),
                    argv_redacted: argv_redacted.clone(),
                    host: host.clone(),
                },
            );
            let _ = store.append(
                &ctx.session_id,
                &SessionItem::ApprovalDecision {
                    ts: Utc::now(),
                    call_id: call_id.to_string(),
                    tool: tool_name.to_string(),
                    decision: "allow".to_string(),
                    decided_by,
                    rule_id: rule_id.clone(),
                },
            );
        }

        // Step 5: Execute (sandboxed for shell_local).
        let mut outcome = tool.run(ctx, args.clone()).await;
        let mut final_decided_by = decided_by;

        // Step 6: Escalation on sandbox denial — ALWAYS asks for a FRESH human decision!
        if outcome.sandbox_denied && !ctx.allow_unsandboxed_retry {
            // Audit the initial sandbox denial before asking to escalate.
            let denied_ev = AuditBuffer::build_event(
                tool_name,
                tool_name,
                risk_class,
                &argv_redacted,
                outcome.host.clone().or_else(|| host.clone()),
                outcome.host_origin.clone(),
                domain.clone(),
                Some("sandbox_denied".to_string()),
                "sandbox_denied",
                final_decided_by,
                rule_id.clone(),
                outcome.exit_code,
                outcome.bytes,
                outcome.duration_ms,
            );
            let _ = self
                .audit
                .record(&ctx.project_id, &ctx.session_id, denied_ev);

            let esc_ans = self
                .ask_human(
                    call_id,
                    tool_name,
                    risk_class,
                    &argv_redacted,
                    host.clone(),
                    domain.clone(),
                    "Sandbox denied this command; retry unsandboxed with wider access? (fresh decision required)",
                    rule_id.clone(),
                    true,
                    false,
                    true,
                    Some("Escalation runs OUTSIDE the local OS sandbox.".to_string()),
                )
                .await;

            if esc_ans != HumanApproval::Deny {
                let mut wider_ctx = ctx.clone();
                wider_ctx.allow_unsandboxed_retry = true;
                final_decided_by = DecisionSource::Person;
                outcome = tool.run(&wider_ctx, args).await;
            }
        }

        // Step 7: Persist full output in local session JSONL and buffer redacted audit event.
        if let Some(ref store) = self.session_store {
            let _ = store.append(
                &ctx.session_id,
                &SessionItem::ToolResult {
                    ts: Utc::now(),
                    call_id: call_id.to_string(),
                    status: format!("{:?}", outcome.status).to_ascii_lowercase(),
                    full_output: outcome.full_text.clone(),
                    exit_code: outcome.exit_code,
                    truncated_for_model: outcome.truncated,
                },
            );
        }

        let audit_ev = AuditBuffer::build_event(
            tool_name,
            tool_name,
            risk_class,
            &argv_redacted,
            outcome.host.clone().or(host),
            outcome.host_origin.clone(),
            domain,
            Some(format!("{:?}", outcome.status).to_ascii_lowercase()),
            "allow",
            final_decided_by,
            rule_id.clone(),
            outcome.exit_code,
            outcome.bytes,
            outcome.duration_ms,
        );
        let _ = self
            .audit
            .record(&ctx.project_id, &ctx.session_id, audit_ev);

        Ok(ToolResultSubmission {
            call_id: call_id.to_string(),
            status: outcome.status,
            output: ToolResultOutput {
                content: vec![ToolContentItem::Text {
                    text: outcome.inline_text,
                }],
                truncated: outcome.truncated,
                exit_code: outcome.exit_code,
                bytes: outcome.bytes,
                duration_ms: outcome.duration_ms,
            },
            decision: ToolResultDecision {
                by: final_decided_by,
                rule_id,
                mode: effective_mode,
            },
        })
    }

    #[allow(clippy::too_many_arguments)]
    async fn ask_human(
        &self,
        call_id: &str,
        tool: &str,
        risk_class: RiskClass,
        argv_redacted: &[String],
        host: Option<String>,
        domain: Option<String>,
        reason: &str,
        rule_id: Option<String>,
        hard: bool,
        allow_always: bool,
        is_escalation: bool,
        sandbox_warning: Option<String>,
    ) -> HumanApproval {
        let req = ApprovalPromptRequest {
            call_id: call_id.to_string(),
            tool: tool.to_string(),
            risk_class,
            argv_redacted: argv_redacted.to_vec(),
            host,
            domain,
            reason: reason.to_string(),
            rule_id,
            hard,
            allow_always: allow_always && !hard,
            is_escalation,
            sandbox_warning,
        };
        self.prompter.prompt(&req).await
    }

    #[allow(clippy::too_many_arguments)]
    fn record_denied(
        &self,
        ctx: &ToolCtx,
        call_id: &str,
        tool: &str,
        risk_class: RiskClass,
        argv_redacted: &[String],
        host: Option<String>,
        domain: Option<String>,
        decided_by: DecisionSource,
        rule_id: Option<String>,
        reason: &str,
    ) -> ToolResultSubmission {
        if let Some(ref store) = self.session_store {
            let _ = store.append(
                &ctx.session_id,
                &SessionItem::ApprovalDecision {
                    ts: Utc::now(),
                    call_id: call_id.to_string(),
                    tool: tool.to_string(),
                    decision: format!("deny: {reason}"),
                    decided_by,
                    rule_id: rule_id.clone(),
                },
            );
        }
        let ev = AuditBuffer::build_event(
            tool,
            tool,
            risk_class,
            argv_redacted,
            host,
            None,
            domain,
            Some("denied".to_string()),
            "deny",
            decided_by,
            rule_id.clone(),
            None,
            0,
            0,
        );
        let _ = self.audit.record(&ctx.project_id, &ctx.session_id, ev);

        ToolResultSubmission {
            call_id: call_id.to_string(),
            status: ToolResultStatus::Denied,
            output: ToolResultOutput {
                content: vec![ToolContentItem::Text {
                    text: format!("Denied: {reason}"),
                }],
                truncated: false,
                exit_code: None,
                bytes: 0,
                duration_ms: 0,
            },
            decision: ToolResultDecision {
                by: decided_by,
                rule_id,
                mode: self.evaluator.effective_mode(),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration as ChronoDuration;
    use ed25519_dalek::SigningKey;
    use metatron_policy::{
        GrantStore, LocalPolicyConfig, sign_policy_bundle_ed25519, verify_signed_policy_bundle,
    };
    use metatron_protocol::{
        ApprovalMode, CookieModePolicy, GrantRecord, GrantScope, PolicyBundle, PolicyDecision,
        PolicyRule, PolicyRuleExamples, PolicyRuleMatch,
    };
    use rand::rngs::OsRng;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tempfile::TempDir;

    struct ScriptedPrompter {
        answers: Mutex<Vec<HumanApproval>>,
        calls: AtomicUsize,
        saw_escalation: Mutex<bool>,
    }

    impl ScriptedPrompter {
        fn new(answers: Vec<HumanApproval>) -> Self {
            Self {
                answers: Mutex::new(answers),
                calls: AtomicUsize::new(0),
                saw_escalation: Mutex::new(false),
            }
        }
    }

    #[async_trait]
    impl ApprovalPrompter for ScriptedPrompter {
        async fn prompt(&self, req: &ApprovalPromptRequest) -> HumanApproval {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if req.is_escalation {
                *self
                    .saw_escalation
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner) = true;
            }
            let mut guard = self.answers.lock().unwrap_or_else(PoisonError::into_inner);
            if guard.is_empty() {
                HumanApproval::Deny
            } else {
                guard.remove(0)
            }
        }
    }

    struct FakeReviewer {
        decision: ReviewerDecision,
        calls: AtomicUsize,
    }

    #[async_trait]
    impl ReviewerBackend for FakeReviewer {
        async fn review(
            &self,
            _req: &ApprovalReviewRequest,
        ) -> Result<ApprovalReviewResponse, BossClientError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            Ok(ApprovalReviewResponse {
                decision: self.decision,
                reason: "fake reviewer verdict".to_string(),
                reviewer_receipt_id: Some("rcpt_rev_1".to_string()),
            })
        }
    }

    fn make_bundle(mode_floor: ApprovalMode) -> metatron_policy::VerifiedPolicyBundle {
        let now = Utc::now();
        let sk = SigningKey::generate(&mut OsRng);
        let bundle = PolicyBundle {
            version: "v-orch".to_string(),
            project_id: Some("boss-dogfood".to_string()),
            issued_at: Some(now),
            expires_at: now + ChronoDuration::hours(1),
            mode_floor,
            tools_allowed: vec!["shell_local".to_string(), "ssh_run".to_string()],
            rules: vec![PolicyRule {
                id: "r-echo".to_string(),
                scope: Some("project".to_string()),
                priority: 10,
                matcher: PolicyRuleMatch {
                    tool: Some("shell_local".to_string()),
                    argv_prefix: Some(vec!["echo".to_string()]),
                    ..Default::default()
                },
                decision: PolicyDecision::Allow,
                hard: false,
                justification: "echo safe".to_string(),
                examples: PolicyRuleExamples::default(),
            }],
            cookie_mode: CookieModePolicy::default(),
            host_diagnostics: None,
            mcp: None,
        };
        let (resp, jwks) = sign_policy_bundle_ed25519(&bundle, "kid-1", &sk).expect("sign");
        verify_signed_policy_bundle(&resp, &jwks, now).expect("verify")
    }

    #[tokio::test]
    async fn reviewer_approves_inside_grant_but_hard_rule_still_asks_human() {
        let tmp = TempDir::new().expect("tmp");
        let audit = AuditBuffer::new(tmp.path().join("audit.jsonl")).expect("audit");
        let verified = make_bundle(ApprovalMode::ActOnMyBehalf);
        let grant = GrantRecord {
            id: "g1".to_string(),
            project_id: "boss-dogfood".to_string(),
            scope: GrantScope::default(),
            allowed_tools: vec!["shell_local".to_string()],
            max_risk_class: Some(RiskClass::WriteLocal),
            expires_at: Utc::now() + ChronoDuration::hours(1),
            revoked_at: None,
        };
        let eval = PolicyEvaluator::with_verified_bundle(
            verified,
            ApprovalMode::ActOnMyBehalf,
            LocalPolicyConfig::default(),
            GrantStore::new(vec![grant]),
        );

        let prompter = Arc::new(ScriptedPrompter::new(vec![HumanApproval::Deny]));
        let reviewer = Arc::new(FakeReviewer {
            decision: ReviewerDecision::Approve,
            calls: AtomicUsize::new(0),
        });
        let orch =
            ToolOrchestrator::new(eval, prompter.clone(), Some(reviewer.clone()), audit, None);

        let scratch = tmp.path().join("scratch");
        std::fs::create_dir_all(&scratch).expect("scratch");
        let ctx = ToolCtx {
            project_id: "boss-dogfood".to_string(),
            session_id: "msess_1".to_string(),
            cwd: scratch.clone(),
            scratch_dir: scratch.clone(),
            allowed_read_roots: vec![scratch.clone()],
            allow_unsandboxed_retry: false,
            boss_client: None,
            remote_runner: RemoteRunner::default(),
            cancel_rx: None,
        };

        // 1. Non-hard write-local (`touch`) covered by grant -> goes to reviewer, approved!
        let touch_target = scratch.join("touched.txt");
        let res = orch
            .execute_tool_call(
                &ctx,
                Some("t1"),
                "c1",
                "shell_local",
                json!({"argv": ["/usr/bin/touch", touch_target.to_string_lossy()]}),
            )
            .await
            .expect("exec");
        assert_eq!(res.status, ToolResultStatus::Ok);
        assert_eq!(res.decision.by, DecisionSource::Reviewer);
        assert_eq!(reviewer.calls.load(Ordering::SeqCst), 1);
        assert_eq!(prompter.calls.load(Ordering::SeqCst), 0);

        // 2. Hard-prompt (`osascript`, AppControl) -> bypasses reviewer and asks human!
        let hard_res = orch
            .execute_tool_call(
                &ctx,
                Some("t1"),
                "c2",
                "shell_local",
                json!({"argv": ["osascript", "-e", "beep"]}),
            )
            .await
            .expect("exec hard");
        assert_eq!(hard_res.status, ToolResultStatus::Denied);
        assert_eq!(hard_res.decision.by, DecisionSource::Person);
        assert_eq!(reviewer.calls.load(Ordering::SeqCst), 1);
        assert_eq!(prompter.calls.load(Ordering::SeqCst), 1);
    }

    #[cfg(target_os = "macos")]
    #[tokio::test]
    async fn sandbox_denial_triggers_fresh_escalation_prompt() {
        let tmp = TempDir::new().expect("tmp");
        let allowed = tmp.path().join("allowed");
        let secret_dir = tmp.path().join("outside");
        std::fs::create_dir_all(&allowed).expect("allowed");
        std::fs::create_dir_all(&secret_dir).expect("outside");
        let secret_file = secret_dir.join("data.txt");
        std::fs::write(&secret_file, "escalated-ok").expect("write");

        let audit = AuditBuffer::new(tmp.path().join("audit.jsonl")).expect("audit");
        let eval = PolicyEvaluator::with_verified_bundle(
            make_bundle(ApprovalMode::RiskyOnly),
            ApprovalMode::RiskyOnly,
            LocalPolicyConfig::default(),
            GrantStore::default(),
        );
        // `cat` is auto-allowed locally, fails inside sandbox on outside file, then asks fresh escalation -> AllowOnce!
        let prompter = Arc::new(ScriptedPrompter::new(vec![HumanApproval::AllowOnce]));
        let orch = ToolOrchestrator::new(eval, prompter.clone(), None, audit, None);

        let ctx = ToolCtx {
            project_id: "boss-dogfood".to_string(),
            session_id: "msess_esc".to_string(),
            cwd: allowed.clone(),
            scratch_dir: allowed.clone(),
            allowed_read_roots: vec![allowed.clone()],
            allow_unsandboxed_retry: false,
            boss_client: None,
            remote_runner: RemoteRunner::default(),
            cancel_rx: None,
        };

        let res = orch
            .execute_tool_call(
                &ctx,
                None,
                "c_esc",
                "shell_local",
                json!({"argv": ["/bin/cat", secret_file.to_string_lossy()]}),
            )
            .await
            .expect("exec");

        assert!(
            *prompter
                .saw_escalation
                .lock()
                .unwrap_or_else(PoisonError::into_inner),
            "expected fresh escalation prompt after sandbox denial"
        );
        assert_eq!(res.status, ToolResultStatus::Ok);
        assert_eq!(res.decision.by, DecisionSource::Person);
        assert_eq!(orch.audit().pending_count().expect("audit count"), 2);
    }
}
