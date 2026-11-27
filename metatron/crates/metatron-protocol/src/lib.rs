//! Typed protocol schemas for Metatron turns, SSE frames, JSONL exec events,
//! signed policy bundles, approval modes, audit batches, and host leases.

use std::collections::BTreeMap;
use std::fmt;
use std::str::FromStr;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Canonical client product name.
pub const CLIENT_NAME: &str = "metatron";
/// Current client crate version.
pub const CLIENT_VERSION: &str = env!("CARGO_PKG_VERSION");
/// Current BOSS Metatron protocol contract version (`X-Boss-Contract`).
pub const BOSS_CONTRACT_VERSION: u32 = 1;
/// Versioned JSONL event schema identifier for `metatron exec --json` and SDK consumers (`MT-5`).
pub const JSONL_SCHEMA_VERSION: &str = "1.0";
/// Required JWS `typ` header for signed policy bundles.
pub const POLICY_JWS_TYP: &str = "boss-policy+jws";
/// Required JWS `alg` header for BOSS tokens and bundles.
pub const POLICY_JWS_ALG: &str = "EdDSA";
/// Default inline tool result byte limit enforced by MT-10 (`413` above this).
pub const DEFAULT_TOOL_RESULT_INLINE_BYTES: usize = 65_536;

/// Stable CLI exit codes for `metatron` and `metatron exec`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(i32)]
pub enum ExitCode {
    Ok = 0,
    RuntimeError = 1,
    UsageOrConfig = 2,
    PolicyDenied = 3,
    AuthRequired = 4,
    ClientTooOld = 5,
    BudgetExhausted = 6,
    SchemaValidationFailed = 7,
    Timeout = 124,
}

impl ExitCode {
    pub fn as_i32(self) -> i32 {
        self as i32
    }
}

/// The four Codex-inspired BOSS approval modes (`MT-7`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ApprovalMode {
    ReadOnly,
    Ask,
    RiskyOnly,
    ActOnMyBehalf,
}

impl ApprovalMode {
    /// Higher rank means stricter governance. A local setting can only tighten
    /// (pick a rank >= BOSS's `mode_floor`), never loosen.
    pub fn strictness_rank(self) -> u8 {
        match self {
            Self::ActOnMyBehalf => 0,
            Self::RiskyOnly => 1,
            Self::Ask => 2,
            Self::ReadOnly => 3,
        }
    }

    /// Return the stricter of `self` and `other`.
    pub fn tighten_with(self, floor: Self) -> Self {
        if self.strictness_rank() >= floor.strictness_rank() {
            self
        } else {
            floor
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::ReadOnly => "read-only",
            Self::Ask => "ask",
            Self::RiskyOnly => "risky-only",
            Self::ActOnMyBehalf => "act-on-my-behalf",
        }
    }
}

impl fmt::Display for ApprovalMode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for ApprovalMode {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.trim().to_ascii_lowercase().as_str() {
            "read-only" | "readonly" | "read_only" => Ok(Self::ReadOnly),
            "ask" => Ok(Self::Ask),
            "risky-only" | "riskyonly" | "risky_only" => Ok(Self::RiskyOnly),
            "act-on-my-behalf" | "act_on_my_behalf" | "trusted" => Ok(Self::ActOnMyBehalf),
            other => Err(format!(
                "unknown approval mode '{other}' (expected read-only, ask, risky-only, or act-on-my-behalf)"
            )),
        }
    }
}

/// Risk classification of a tool call (`ARCHITECTURE.md` §4.4).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum RiskClass {
    ReadLocal,
    WriteScratch,
    WriteLocal,
    RemoteRead,
    RemoteWrite,
    Capture,
    SecretUse,
    AppControl,
    Network,
    Handoff,
}

impl RiskClass {
    /// Hard-prompt classes always ask a person in every mode (including
    /// `act-on-my-behalf`) and never offer "always allow".
    pub fn is_hard_prompt(self) -> bool {
        matches!(
            self,
            Self::Capture | Self::SecretUse | Self::AppControl | Self::RemoteWrite
        )
    }

    /// True if this class mutates local, remote, or external state.
    pub fn is_mutating(self) -> bool {
        !matches!(self, Self::ReadLocal | Self::RemoteRead)
    }

    /// Numeric risk rank used for grant `max_risk_class` comparisons.
    pub fn rank(self) -> u8 {
        match self {
            Self::ReadLocal | Self::RemoteRead => 0,
            Self::WriteScratch | Self::Network | Self::Handoff => 1,
            Self::WriteLocal | Self::RemoteWrite => 2,
            Self::Capture => 3,
            Self::SecretUse => 4,
            Self::AppControl => 5,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::ReadLocal => "read-local",
            Self::WriteScratch => "write-scratch",
            Self::WriteLocal => "write-local",
            Self::RemoteRead => "remote-read",
            Self::RemoteWrite => "remote-write",
            Self::Capture => "capture",
            Self::SecretUse => "secret-use",
            Self::AppControl => "app-control",
            Self::Network => "network",
            Self::Handoff => "handoff",
        }
    }
}

impl fmt::Display for RiskClass {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for RiskClass {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.trim().to_ascii_lowercase().as_str() {
            "read-local" | "local-read" | "read" | "read-only" => Ok(Self::ReadLocal),
            "write-scratch" => Ok(Self::WriteScratch),
            "write-local" => Ok(Self::WriteLocal),
            "remote-read" => Ok(Self::RemoteRead),
            "remote-write" => Ok(Self::RemoteWrite),
            "capture" => Ok(Self::Capture),
            "secret-use" => Ok(Self::SecretUse),
            "app-control" => Ok(Self::AppControl),
            "network" => Ok(Self::Network),
            "handoff" => Ok(Self::Handoff),
            other => Err(format!("unknown risk class '{other}'")),
        }
    }
}

/// Policy decision produced by a rule.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum PolicyDecision {
    Allow,
    #[serde(alias = "ask")]
    Prompt,
    #[serde(alias = "deny")]
    Forbidden,
}

impl PolicyDecision {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Allow => "allow",
            Self::Prompt => "prompt",
            Self::Forbidden => "forbidden",
        }
    }
}

/// Who made an approval decision (audited in BOSS and session JSONL).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum DecisionSource {
    Rule,
    Person,
    Reviewer,
    Mode,
    Policy,
}

impl DecisionSource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Rule => "rule",
            Self::Person => "person",
            Self::Reviewer => "reviewer",
            Self::Mode => "mode",
            Self::Policy => "policy",
        }
    }
}

/// Tool exposure level in the turn tool-list handshake.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ToolExposure {
    Direct,
    Deferred,
}

/// Tool declaration sent to BOSS in `POST /v1/metatron/turns`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClientToolSpec {
    pub name: String,
    pub description: String,
    pub input_schema: Value,
    pub risk_class: String,
    pub exposure: ToolExposure,
}

/// Client metadata sent with every turn.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClientInfo {
    pub name: String,
    pub version: String,
    pub os: String,
    pub arch: String,
}

impl ClientInfo {
    pub fn current() -> Self {
        Self {
            name: CLIENT_NAME.to_string(),
            version: CLIENT_VERSION.to_string(),
            os: std::env::consts::OS.to_string(),
            arch: std::env::consts::ARCH.to_string(),
        }
    }

    pub fn header_value(&self) -> String {
        format!(
            "{}/{} ({}; {})",
            self.name, self.version, self.os, self.arch
        )
    }
}

/// Read-only local file attachment resolved from `@file` mentions.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FileContext {
    pub path: String,
    pub content: String,
    #[serde(default)]
    pub truncated: bool,
}

/// Local context shipped with a turn request.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct TurnContext {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cwd_label: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agents_md: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub files: Vec<FileContext>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub attachments: Vec<Value>,
}

/// Request body for `POST /v1/metatron/turns`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TurnRequest {
    pub project_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thread_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agent: Option<String>,
    pub message: String,
    pub mode: ApprovalMode,
    pub client: ClientInfo,
    #[serde(default)]
    pub context: TurnContext,
    #[serde(default)]
    pub client_tools: Vec<ClientToolSpec>,
}

/// Dropped tool notice inside `turn.started`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DroppedTool {
    pub name: String,
    pub reason: String,
}

/// Settled step cost summary inside `usage`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct UsageCost {
    pub amount: f64,
    pub currency: String,
    pub source: String,
}

/// Typed MT-10 Server-Sent Event payload.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum SseEvent {
    TurnStarted {
        turn_id: String,
        thread_id: String,
        model: String,
        mode: String,
        #[serde(default)]
        tools: Vec<String>,
        #[serde(default)]
        dropped: Vec<DroppedTool>,
    },
    TextDelta {
        text: String,
    },
    ServerToolStarted {
        call_id: String,
        name: String,
        summary: String,
        ok: bool,
    },
    ServerToolFinished {
        call_id: String,
        name: String,
        summary: String,
        ok: bool,
    },
    ToolCall {
        call_id: String,
        name: String,
        arguments: Value,
        #[serde(default)]
        risk_hint: Option<String>,
        #[serde(default = "default_deadline_s")]
        deadline_s: u64,
    },
    PolicyDenied {
        call_id: String,
        reason: String,
        rule_id: String,
    },
    Usage {
        input_tokens: u64,
        output_tokens: u64,
        cost: UsageCost,
    },
    TurnCompleted {
        turn_id: String,
        stop_reason: String,
    },
    TurnFailed {
        turn_id: String,
        code: String,
        message: String,
        retryable: bool,
    },
}

fn default_deadline_s() -> u64 {
    900
}

impl SseEvent {
    /// Parse an MT-10 SSE event name + JSON data string.
    pub fn from_sse(event_name: &str, data_json: &str) -> Result<Self, serde_json::Error> {
        match event_name {
            "turn.started" => {
                #[derive(Deserialize)]
                struct Payload {
                    turn_id: String,
                    thread_id: String,
                    model: String,
                    mode: String,
                    #[serde(default)]
                    tools: Vec<String>,
                    #[serde(default)]
                    dropped: Vec<DroppedTool>,
                }
                let p: Payload = serde_json::from_str(data_json)?;
                Ok(Self::TurnStarted {
                    turn_id: p.turn_id,
                    thread_id: p.thread_id,
                    model: p.model,
                    mode: p.mode,
                    tools: p.tools,
                    dropped: p.dropped,
                })
            }
            "text.delta" => {
                #[derive(Deserialize)]
                struct Payload {
                    text: String,
                }
                let p: Payload = serde_json::from_str(data_json)?;
                Ok(Self::TextDelta { text: p.text })
            }
            "server_tool.started" => {
                #[derive(Deserialize)]
                struct Payload {
                    call_id: String,
                    name: String,
                    summary: String,
                    ok: bool,
                }
                let p: Payload = serde_json::from_str(data_json)?;
                Ok(Self::ServerToolStarted {
                    call_id: p.call_id,
                    name: p.name,
                    summary: p.summary,
                    ok: p.ok,
                })
            }
            "server_tool.finished" => {
                #[derive(Deserialize)]
                struct Payload {
                    call_id: String,
                    name: String,
                    summary: String,
                    ok: bool,
                }
                let p: Payload = serde_json::from_str(data_json)?;
                Ok(Self::ServerToolFinished {
                    call_id: p.call_id,
                    name: p.name,
                    summary: p.summary,
                    ok: p.ok,
                })
            }
            "tool_call" => {
                #[derive(Deserialize)]
                struct Payload {
                    call_id: String,
                    name: String,
                    arguments: Value,
                    #[serde(default)]
                    risk_hint: Option<String>,
                    #[serde(default = "default_deadline_s")]
                    deadline_s: u64,
                }
                let p: Payload = serde_json::from_str(data_json)?;
                Ok(Self::ToolCall {
                    call_id: p.call_id,
                    name: p.name,
                    arguments: p.arguments,
                    risk_hint: p.risk_hint,
                    deadline_s: p.deadline_s,
                })
            }
            "policy.denied" => {
                #[derive(Deserialize)]
                struct Payload {
                    call_id: String,
                    reason: String,
                    rule_id: String,
                }
                let p: Payload = serde_json::from_str(data_json)?;
                Ok(Self::PolicyDenied {
                    call_id: p.call_id,
                    reason: p.reason,
                    rule_id: p.rule_id,
                })
            }
            "usage" => {
                #[derive(Deserialize)]
                struct Payload {
                    input_tokens: u64,
                    output_tokens: u64,
                    cost: UsageCost,
                }
                let p: Payload = serde_json::from_str(data_json)?;
                Ok(Self::Usage {
                    input_tokens: p.input_tokens,
                    output_tokens: p.output_tokens,
                    cost: p.cost,
                })
            }
            "turn.completed" => {
                #[derive(Deserialize)]
                struct Payload {
                    turn_id: String,
                    stop_reason: String,
                }
                let p: Payload = serde_json::from_str(data_json)?;
                Ok(Self::TurnCompleted {
                    turn_id: p.turn_id,
                    stop_reason: p.stop_reason,
                })
            }
            "turn.failed" => {
                #[derive(Deserialize)]
                struct Payload {
                    turn_id: String,
                    code: String,
                    message: String,
                    retryable: bool,
                }
                let p: Payload = serde_json::from_str(data_json)?;
                Ok(Self::TurnFailed {
                    turn_id: p.turn_id,
                    code: p.code,
                    message: p.message,
                    retryable: p.retryable,
                })
            }
            other => Err(serde::de::Error::custom(format!(
                "unknown SSE event type '{other}'"
            ))),
        }
    }

    pub fn is_terminal(&self) -> bool {
        matches!(self, Self::TurnCompleted { .. } | Self::TurnFailed { .. })
    }
}

/// Envelope for one parsed SSE frame including its resumable `id`.
#[derive(Debug, Clone, PartialEq)]
pub struct SseFrame {
    pub id: Option<String>,
    pub event: SseEvent,
}

/// Content item inside a tool result payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ToolContentItem {
    Text {
        text: String,
    },
    Image {
        media_type: String,
        data_b64: String,
    },
}

/// Tool output body inside `ToolResultSubmission`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolResultOutput {
    pub content: Vec<ToolContentItem>,
    pub truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exit_code: Option<i32>,
    pub bytes: usize,
    pub duration_ms: u64,
}

/// Decision metadata attached to a submitted tool result.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolResultDecision {
    pub by: DecisionSource,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rule_id: Option<String>,
    pub mode: ApprovalMode,
}

/// Request body for `POST /v1/metatron/turns/{turn_id}/tool_results`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolResultSubmission {
    pub call_id: String,
    pub status: ToolResultStatus,
    pub output: ToolResultOutput,
    pub decision: ToolResultDecision,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ToolResultStatus {
    Ok,
    Error,
    Denied,
    Cancelled,
}

/// JSONL event stream emitted by `metatron exec --json` (modeled on Codex `exec_events`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ExecEvent {
    #[serde(rename = "thread.started")]
    ThreadStarted { thread_id: String },
    #[serde(rename = "turn.started")]
    TurnStarted {
        turn_id: String,
        thread_id: String,
        model: String,
        mode: String,
        tools: Vec<String>,
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        dropped: Vec<DroppedTool>,
    },
    #[serde(rename = "item.started")]
    ItemStarted {
        id: String,
        kind: String,
        name: String,
        summary: String,
    },
    #[serde(rename = "item.updated")]
    ItemUpdated { id: String, delta: String },
    #[serde(rename = "item.completed")]
    ItemCompleted {
        id: String,
        kind: String,
        status: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        text: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        exit_code: Option<i32>,
        #[serde(default)]
        truncated: bool,
    },
    #[serde(rename = "usage")]
    Usage {
        input_tokens: u64,
        output_tokens: u64,
        cost: UsageCost,
    },
    #[serde(rename = "turn.completed")]
    TurnCompleted {
        turn_id: String,
        stop_reason: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        final_message: Option<String>,
    },
    #[serde(rename = "turn.failed")]
    TurnFailed {
        turn_id: String,
        code: String,
        message: String,
        retryable: bool,
    },
}

/// Versioned JSONL envelope emitted by `metatron exec --json` for TS/Python SDK consumers (`MT-5`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VersionedExecEvent {
    pub schema_version: String,
    #[serde(flatten)]
    pub event: ExecEvent,
}

impl ExecEvent {
    /// Wrap this event in the versioned JSONL envelope (`schema_version = "1.0"`).
    pub fn into_versioned(self) -> VersionedExecEvent {
        VersionedExecEvent {
            schema_version: JSONL_SCHEMA_VERSION.to_string(),
            event: self,
        }
    }

    /// Borrow-preserving versioned JSONL envelope for streaming emission.
    pub fn to_versioned(&self) -> VersionedExecEvent {
        VersionedExecEvent {
            schema_version: JSONL_SCHEMA_VERSION.to_string(),
            event: self.clone(),
        }
    }

    /// Canonical JSON Schema (draft 2020-12) for Metatron JSONL exec events (`MT-5`).
    pub fn jsonl_schema_v1() -> Value {
        serde_json::json!({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://boss.local/schemas/metatron-exec-event-v1.0.json",
            "title": "MetatronExecEventV1",
            "description": "Versioned JSONL event emitted by `metatron exec --json` (schema_version 1.0).",
            "type": "object",
            "required": ["schema_version", "type"],
            "properties": {
                "schema_version": { "const": JSONL_SCHEMA_VERSION },
                "type": {
                    "type": "string",
                    "enum": [
                        "thread.started",
                        "turn.started",
                        "item.started",
                        "item.updated",
                        "item.completed",
                        "usage",
                        "turn.completed",
                        "turn.failed"
                    ]
                }
            },
            "oneOf": [
                {
                    "properties": {
                        "type": { "const": "thread.started" },
                        "thread_id": { "type": "string" }
                    },
                    "required": ["schema_version", "type", "thread_id"]
                },
                {
                    "properties": {
                        "type": { "const": "turn.started" },
                        "turn_id": { "type": "string" },
                        "thread_id": { "type": "string" },
                        "model": { "type": "string" },
                        "mode": { "type": "string" },
                        "tools": { "type": "array", "items": { "type": "string" } },
                        "dropped": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["name", "reason"],
                                "properties": {
                                    "name": { "type": "string" },
                                    "reason": { "type": "string" }
                                }
                            }
                        }
                    },
                    "required": ["schema_version", "type", "turn_id", "thread_id", "model", "mode", "tools"]
                },
                {
                    "properties": {
                        "type": { "const": "item.started" },
                        "id": { "type": "string" },
                        "kind": { "type": "string" },
                        "name": { "type": "string" },
                        "summary": { "type": "string" }
                    },
                    "required": ["schema_version", "type", "id", "kind", "name", "summary"]
                },
                {
                    "properties": {
                        "type": { "const": "item.updated" },
                        "id": { "type": "string" },
                        "delta": { "type": "string" }
                    },
                    "required": ["schema_version", "type", "id", "delta"]
                },
                {
                    "properties": {
                        "type": { "const": "item.completed" },
                        "id": { "type": "string" },
                        "kind": { "type": "string" },
                        "status": { "type": "string" },
                        "text": { "type": ["string", "null"] },
                        "exit_code": { "type": ["integer", "null"] },
                        "truncated": { "type": "boolean" }
                    },
                    "required": ["schema_version", "type", "id", "kind", "status"]
                },
                {
                    "properties": {
                        "type": { "const": "usage" },
                        "input_tokens": { "type": "integer" },
                        "output_tokens": { "type": "integer" },
                        "cost": {
                            "type": "object",
                            "required": ["amount", "currency", "source"],
                            "properties": {
                                "amount": { "type": "number" },
                                "currency": { "type": "string" },
                                "source": { "type": "string" }
                            }
                        }
                    },
                    "required": ["schema_version", "type", "input_tokens", "output_tokens", "cost"]
                },
                {
                    "properties": {
                        "type": { "const": "turn.completed" },
                        "turn_id": { "type": "string" },
                        "stop_reason": { "type": "string" },
                        "final_message": { "type": ["string", "null"] }
                    },
                    "required": ["schema_version", "type", "turn_id", "stop_reason"]
                },
                {
                    "properties": {
                        "type": { "const": "turn.failed" },
                        "turn_id": { "type": "string" },
                        "code": { "type": "string" },
                        "message": { "type": "string" },
                        "retryable": { "type": "boolean" }
                    },
                    "required": ["schema_version", "type", "turn_id", "code", "message", "retryable"]
                }
            ]
        })
    }
}

// --- Auth & Capabilities DTOs ---

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CapabilitiesLimits {
    pub tool_result_inline_bytes: usize,
    pub turn_deadline_s: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CapabilitiesResponse {
    pub contract: u32,
    pub min_client: String,
    #[serde(default)]
    pub boss_version: Option<String>,
    pub features: Vec<String>,
    pub limits: CapabilitiesLimits,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeviceCodeRequest {
    pub client_id: String,
    pub scope: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeviceCodeResponse {
    pub device_code: String,
    pub user_code: String,
    pub verification_uri: String,
    #[serde(default)]
    pub verification_uri_complete: Option<String>,
    pub expires_in: u64,
    pub interval: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeviceTokenRequest {
    pub grant_type: String,
    pub device_code: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TokenPrincipal {
    pub id: String,
    pub actor: String,
    #[serde(default)]
    pub roles: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TokenResponse {
    pub access_token: String,
    #[serde(default)]
    pub refresh_token: Option<String>,
    pub token_type: String,
    pub expires_in: u64,
    #[serde(default)]
    pub principal: Option<TokenPrincipal>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeviceTokenError {
    pub error: String,
    #[serde(default)]
    pub error_description: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RefreshTokenRequest {
    pub grant_type: String,
    pub refresh_token: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RevokeTokenRequest {
    pub token: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BossErrorDetail {
    pub code: String,
    pub message: String,
    #[serde(default)]
    pub retryable: bool,
    #[serde(default)]
    pub min_client: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BossErrorEnvelope {
    pub error: BossErrorDetail,
}

// --- Policy Bundle, JWKS, Grants & Reviewer DTOs ---

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JwkKey {
    pub kty: String,
    pub crv: String,
    pub x: String,
    #[serde(default)]
    pub kid: Option<String>,
    #[serde(default)]
    pub alg: Option<String>,
    #[serde(default, rename = "use")]
    pub key_use: Option<String>,
    #[serde(default)]
    pub d: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JwksDocument {
    pub keys: Vec<JwkKey>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct PolicyRuleExamples {
    #[serde(default, rename = "match")]
    pub should_match: Vec<String>,
    #[serde(default)]
    pub not_match: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct PolicyRuleMatch {
    #[serde(default)]
    pub server: Option<String>,
    #[serde(default)]
    pub tool: Option<String>,
    #[serde(default)]
    pub argv_prefix: Option<Vec<String>>,
    #[serde(default)]
    pub host: Option<String>,
    #[serde(default)]
    pub domain: Option<String>,
    #[serde(default)]
    pub risk_class: Option<String>,
    #[serde(default)]
    pub conditions: Vec<Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyRule {
    pub id: String,
    #[serde(default)]
    pub scope: Option<String>,
    #[serde(default = "default_rule_priority")]
    pub priority: i32,
    #[serde(rename = "match")]
    pub matcher: PolicyRuleMatch,
    pub decision: PolicyDecision,
    #[serde(default)]
    pub hard: bool,
    #[serde(default)]
    pub justification: String,
    #[serde(default)]
    pub examples: PolicyRuleExamples,
}

fn default_rule_priority() -> i32 {
    100
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CookieModePolicy {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub domains: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PolicyBundle {
    pub version: String,
    #[serde(default)]
    pub project_id: Option<String>,
    #[serde(default)]
    pub issued_at: Option<DateTime<Utc>>,
    pub expires_at: DateTime<Utc>,
    #[serde(default = "default_mode_floor")]
    pub mode_floor: ApprovalMode,
    #[serde(default)]
    pub tools_allowed: Vec<String>,
    #[serde(default)]
    pub rules: Vec<PolicyRule>,
    #[serde(default)]
    pub cookie_mode: CookieModePolicy,
    #[serde(default)]
    pub host_diagnostics: Option<Value>,
    #[serde(default)]
    pub mcp: Option<Value>,
}

fn default_mode_floor() -> ApprovalMode {
    ApprovalMode::Ask
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SignedPolicyResponse {
    pub bundle: PolicyBundle,
    pub jws: String,
    pub kid: String,
    pub alg: String,
    pub typ: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct GrantScope {
    #[serde(default)]
    pub hosts: Vec<String>,
    #[serde(default)]
    pub domains: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GrantCreateRequest {
    pub project_id: String,
    #[serde(default)]
    pub scope: GrantScope,
    #[serde(default)]
    pub allowed_tools: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_risk_class: Option<RiskClass>,
    pub ttl_s: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GrantRecord {
    pub id: String,
    pub project_id: String,
    #[serde(default)]
    pub scope: GrantScope,
    #[serde(default)]
    pub allowed_tools: Vec<String>,
    #[serde(default)]
    pub max_risk_class: Option<RiskClass>,
    pub expires_at: DateTime<Utc>,
    #[serde(default)]
    pub revoked_at: Option<DateTime<Utc>>,
}

impl GrantRecord {
    pub fn is_active(&self, now: DateTime<Utc>) -> bool {
        self.revoked_at.is_none() && now < self.expires_at
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReviewCallPayload {
    pub tool: String,
    pub argv_redacted: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub host: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub domain: Option<String>,
    pub risk_class: RiskClass,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApprovalReviewRequest {
    pub project_id: String,
    pub session_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    pub call: ReviewCallPayload,
    pub mode: ApprovalMode,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ReviewerDecision {
    Approve,
    Deny,
    Escalate,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApprovalReviewResponse {
    pub decision: ReviewerDecision,
    pub reason: String,
    #[serde(default)]
    pub reviewer_receipt_id: Option<String>,
}

// --- Audit & Host Lease DTOs ---

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditEvent {
    pub event_id: String,
    pub ts: DateTime<Utc>,
    pub kind: String,
    pub tool: String,
    pub risk_class: RiskClass,
    pub argv_redacted: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub host: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub host_origin: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub domain: Option<String>,
    /// Cookie names attached to an HTTP request (NEVER cookie values! `MT-6`).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub cookie_names: Vec<String>,
    /// HTTP method for `http_session` audit records (`MT-6`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub method: Option<String>,
    /// URL path (no query secrets) for `http_session` audit records (`MT-6`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
    /// HTTP response status code for `http_session` audit records (`MT-6`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub http_status: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    pub decision: String,
    pub decided_by: DecisionSource,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rule_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exit_code: Option<i32>,
    #[serde(default)]
    pub bytes: usize,
    #[serde(default)]
    pub duration_ms: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ticket_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditBatchRequest {
    pub project_id: String,
    pub session_id: String,
    pub events: Vec<AuditEvent>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RejectedAuditEvent {
    pub event_id: String,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditBatchResponse {
    #[serde(default)]
    pub accepted: Vec<String>,
    #[serde(default)]
    pub rejected: Vec<RejectedAuditEvent>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RegisteredHost {
    pub id: String,
    pub project_id: String,
    pub name: String,
    pub address: String,
    pub ssh_user: String,
    #[serde(default = "default_ssh_port")]
    pub ssh_port: u16,
    #[serde(default)]
    pub arch: Option<String>,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub labels: BTreeMap<String, String>,
    #[serde(default)]
    pub key_configured: bool,
    #[serde(default)]
    pub host_key_known_hosts: Option<String>,
    #[serde(default)]
    pub host_key_fingerprint: Option<String>,
}

fn default_ssh_port() -> u16 {
    22
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CredentialLeaseRequest {
    pub purpose: String,
    pub ttl_s: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
}

#[derive(Clone, PartialEq, Eq, Deserialize)]
pub struct CredentialLeaseResponse {
    pub lease_id: String,
    pub host_id: String,
    pub kind: String,
    pub expires_at: DateTime<Utc>,
    #[serde(default)]
    pub host_key_known_hosts: Option<String>,
    #[serde(default)]
    pub host_key_fingerprint: Option<String>,
    pub material: String,
}

impl fmt::Debug for CredentialLeaseResponse {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CredentialLeaseResponse")
            .field("lease_id", &self.lease_id)
            .field("host_id", &self.host_id)
            .field("kind", &self.kind)
            .field("expires_at", &self.expires_at)
            .field("host_key_fingerprint", &self.host_key_fingerprint)
            .field("material", &"[REDACTED]")
            .finish()
    }
}

// --- Wave D DTOs: MCP Registry, Skills, Evidence, Handoff, Web Search (`MT-9`, `MT-4`, `MT-5`) ---

/// Tool schema published by an MCP server (`tools/list`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct McpToolSpec {
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default = "default_json_object", alias = "inputSchema")]
    pub input_schema: Value,
}

fn default_json_object() -> Value {
    serde_json::json!({"type": "object", "properties": {}})
}

/// Admin-approved MCP server entry from BOSS `/v1/mcp/servers` or `PolicyBundle.mcp`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct McpServerRecord {
    #[serde(default)]
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub url: String,
    #[serde(default = "default_mcp_transport")]
    pub transport: String,
    #[serde(default)]
    pub risk_tier: u8,
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default)]
    pub description: Option<String>,
    /// Optional local stdio command (for admin-approved local stdio MCP servers).
    #[serde(default)]
    pub command: Option<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub tool_risk_overrides: BTreeMap<String, u8>,
    #[serde(default)]
    pub tool_policy: BTreeMap<String, String>,
    #[serde(default)]
    pub tools: Vec<McpToolSpec>,
    #[serde(default)]
    pub auth: Option<Value>,
}

fn default_mcp_transport() -> String {
    "http".to_string()
}

fn default_true() -> bool {
    true
}

/// BOSS-granted skill record returned by `GET /v1/skills`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BossSkillRecord {
    #[serde(default)]
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub body: Option<String>,
    #[serde(default)]
    pub tools: Vec<String>,
    #[serde(default)]
    pub dependencies: Vec<String>,
    #[serde(default = "default_true")]
    pub enabled: bool,
}

/// Reference to a collected evidence artifact (redacted metadata, never secrets).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvidenceRef {
    pub name: String,
    pub path: String,
    pub sha256: String,
    pub bytes: u64,
    #[serde(default)]
    pub media_type: Option<String>,
}

/// Request body to post investigation findings & evidence to a BOSS ticket (`MT-4`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TicketFindingRequest {
    pub project_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ticket_id: Option<String>,
    pub title: String,
    pub summary: String,
    #[serde(default)]
    pub evidence: Vec<EvidenceRef>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dashboard_excerpt: Option<String>,
    pub session_id: String,
    pub dedupe_key: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TicketFindingResponse {
    pub ticket_id: String,
    #[serde(default)]
    pub comment_id: Option<String>,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub attached_count: usize,
}

/// Request body for `handoff_code_task` (`MT-5`, `POST /v1/code-tasks`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeTaskHandoffRequest {
    pub project_id: String,
    pub title: String,
    pub instructions: String,
    pub findings: String,
    #[serde(default)]
    pub evidence: Vec<EvidenceRef>,
    #[serde(default)]
    pub log_excerpt: Option<String>,
    pub session_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ticket_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub repo: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub base_branch: Option<String>,
    pub dedupe_key: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeTaskHandoffResponse {
    pub run_id: String,
    #[serde(default)]
    pub task_id: Option<String>,
    pub status: String,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub message: Option<String>,
}

/// Web search request routed through BOSS model gateway (`MT-9`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WebSearchRequest {
    pub project_id: String,
    pub query: String,
    #[serde(default = "default_search_limit")]
    pub limit: usize,
    #[serde(default)]
    pub allowed_domains: Vec<String>,
}

fn default_search_limit() -> usize {
    5
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WebSearchResultItem {
    pub title: String,
    pub url: String,
    pub snippet: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WebSearchResponse {
    pub provider: String,
    #[serde(default)]
    pub results: Vec<WebSearchResultItem>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn approval_mode_tighten_never_weakens() {
        assert_eq!(
            ApprovalMode::ActOnMyBehalf.tighten_with(ApprovalMode::Ask),
            ApprovalMode::Ask
        );
        assert_eq!(
            ApprovalMode::RiskyOnly.tighten_with(ApprovalMode::Ask),
            ApprovalMode::Ask
        );
        assert_eq!(
            ApprovalMode::ReadOnly.tighten_with(ApprovalMode::Ask),
            ApprovalMode::ReadOnly
        );
        assert_eq!(
            ApprovalMode::RiskyOnly.tighten_with(ApprovalMode::ActOnMyBehalf),
            ApprovalMode::RiskyOnly
        );
    }

    #[test]
    fn hard_prompt_classes_cover_capture_secret_app_and_remote_write() {
        assert!(RiskClass::Capture.is_hard_prompt());
        assert!(RiskClass::SecretUse.is_hard_prompt());
        assert!(RiskClass::AppControl.is_hard_prompt());
        assert!(RiskClass::RemoteWrite.is_hard_prompt());
        assert!(!RiskClass::ReadLocal.is_hard_prompt());
        assert!(!RiskClass::RemoteRead.is_hard_prompt());
        assert!(!RiskClass::WriteScratch.is_hard_prompt());
    }

    #[test]
    fn sse_events_round_trip_from_mt10_frames() {
        let ev = SseEvent::from_sse(
            "turn.started",
            r#"{"turn_id":"mturn_1","thread_id":"athr_1","model":"gpt-4o","mode":"ask","tools":["ssh_run"],"dropped":[{"name":"write_file","reason":"policy"}]}"#,
        )
        .expect("parse turn.started");
        assert!(!ev.is_terminal());

        let tc = SseEvent::from_sse(
            "tool_call",
            r#"{"call_id":"call_1","name":"ssh_run","arguments":{"host":"web-1","argv":["sar","-u","1","5"]},"risk_hint":"remote-read","deadline_s":900}"#,
        )
        .expect("parse tool_call");
        assert!(matches!(tc, SseEvent::ToolCall { call_id, .. } if call_id == "call_1"));

        let done = SseEvent::from_sse(
            "turn.completed",
            r#"{"turn_id":"mturn_1","stop_reason":"end_turn"}"#,
        )
        .expect("parse turn.completed");
        assert!(done.is_terminal());
    }

    #[test]
    fn credential_lease_debug_redacts_material() {
        let lease = CredentialLeaseResponse {
            lease_id: "mcl_1".to_string(),
            host_id: "host_1".to_string(),
            kind: "ssh_key".to_string(),
            expires_at: Utc::now(),
            host_key_known_hosts: None,
            host_key_fingerprint: Some("SHA256:abc".to_string()),
            material: "-----BEGIN OPENSSH PRIVATE KEY-----\nTOPSECRET".to_string(),
        };
        let rendered = format!("{lease:?}");
        assert!(!rendered.contains("TOPSECRET"));
        assert!(rendered.contains("[REDACTED]"));
    }

    #[test]
    fn exec_event_serializes_stable_jsonl_tags() {
        let ev = ExecEvent::TurnCompleted {
            turn_id: "mturn_1".to_string(),
            stop_reason: "end_turn".to_string(),
            final_message: Some("done".to_string()),
        };
        let line = serde_json::to_string(&ev).expect("serialize");
        assert!(line.contains(r#""type":"turn.completed""#));
    }
}
