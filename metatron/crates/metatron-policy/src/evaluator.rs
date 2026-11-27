//! Four-mode BOSS policy evaluator (`MT-7`) enforcing the exact 7-step order in
//! `ARCHITECTURE.md` §5:
//! 1. Tool-list filter (`filter_tools`)
//! 2. Parse & classify (`argv`, `host`, `domain`, `RiskClass`)
//! 3. Policy rules (`allow | prompt | forbidden` + `hard: true`; BOSS rules cannot be weakened)
//! 4. Approval mode resolution (`read-only | ask | risky-only | act-on-my-behalf` + TTL grants)
//!
//! Steps 5-7 (execution, sandbox escalation, and audit) are driven by `metatron-core`.

use std::sync::{Mutex, PoisonError};

use chrono::{DateTime, Utc};
use metatron_protocol::{
    ApprovalMode, ClientToolSpec, DecisionSource, GrantRecord, PolicyBundle, PolicyDecision,
    PolicyRule, RiskClass,
};
use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::jws::VerifiedPolicyBundle;
use crate::verbs::{classify_command_argv, parse_command_argv};

#[derive(Debug, Error)]
pub enum PolicyConfigError {
    #[error("invalid local policy TOML: {0}")]
    Toml(#[from] toml::de::Error),
    #[error("local policy rule '{0}' attempted to allow an action; local policy may only tighten")]
    LocalCannotLoosen(String),
}

/// Optional local TOML tightening layer (`~/.config/metatron/policy.toml`).
///
/// Local rules can ONLY tighten (`prompt` or `forbidden`, or raise `mode_floor`),
/// never weaken BOSS policy.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct LocalPolicyConfig {
    #[serde(default)]
    pub mode_floor: Option<ApprovalMode>,
    #[serde(default)]
    pub rules: Vec<PolicyRule>,
}

impl LocalPolicyConfig {
    pub fn parse_toml(raw: &str) -> Result<Self, PolicyConfigError> {
        let cfg: Self = toml::from_str(raw)?;
        for rule in &cfg.rules {
            if rule.decision == PolicyDecision::Allow {
                return Err(PolicyConfigError::LocalCannotLoosen(rule.id.clone()));
            }
        }
        Ok(cfg)
    }
}

/// Input context for evaluating a single tool call.
#[derive(Debug, Clone)]
pub struct ToolCallContext {
    pub tool: String,
    pub argv: Vec<String>,
    pub host: Option<String>,
    pub domain: Option<String>,
    pub risk_class: RiskClass,
    pub is_remote: bool,
}

/// Outcome of policy + mode evaluation before executing a tool.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EvaluationDecision {
    AutoAllow {
        decided_by: DecisionSource,
        rule_id: Option<String>,
        reason: String,
    },
    AskHuman {
        rule_id: Option<String>,
        reason: String,
        /// `false` for `hard: true` rules and hard-prompt risk classes.
        allow_always: bool,
        hard: bool,
    },
    AskReviewer {
        grant_id: String,
        rule_id: Option<String>,
        reason: String,
    },
    Deny {
        decided_by: DecisionSource,
        rule_id: Option<String>,
        reason: String,
    },
}

/// In-memory + revocable store for `act-on-my-behalf` TTL grants (`/grants`).
#[derive(Debug, Default)]
pub struct GrantStore {
    grants: Mutex<Vec<GrantRecord>>,
}

impl GrantStore {
    pub fn new(initial: Vec<GrantRecord>) -> Self {
        Self {
            grants: Mutex::new(initial),
        }
    }

    pub fn add(&self, grant: GrantRecord) {
        let mut guard = self.grants.lock().unwrap_or_else(PoisonError::into_inner);
        guard.retain(|g| g.id != grant.id);
        guard.push(grant);
    }

    pub fn revoke(&self, grant_id: &str, now: DateTime<Utc>) -> bool {
        let mut guard = self.grants.lock().unwrap_or_else(PoisonError::into_inner);
        for g in guard.iter_mut() {
            if g.id == grant_id && g.revoked_at.is_none() {
                g.revoked_at = Some(now);
                return true;
            }
        }
        false
    }

    pub fn list(&self) -> Vec<GrantRecord> {
        let guard = self.grants.lock().unwrap_or_else(PoisonError::into_inner);
        guard.clone()
    }

    pub fn active_count(&self, now: DateTime<Utc>) -> usize {
        let guard = self.grants.lock().unwrap_or_else(PoisonError::into_inner);
        guard.iter().filter(|g| g.is_active(now)).count()
    }

    pub fn find_covering(&self, ctx: &ToolCallContext, now: DateTime<Utc>) -> Option<GrantRecord> {
        let guard = self.grants.lock().unwrap_or_else(PoisonError::into_inner);
        guard.iter().find(|g| grant_covers(g, ctx, now)).cloned()
    }
}

fn grant_covers(grant: &GrantRecord, ctx: &ToolCallContext, now: DateTime<Utc>) -> bool {
    if !grant.is_active(now) {
        return false;
    }
    if !grant.allowed_tools.is_empty()
        && !grant
            .allowed_tools
            .iter()
            .any(|t| t == "*" || t == &ctx.tool)
    {
        return false;
    }
    if let Some(max_risk) = grant.max_risk_class
        && ctx.risk_class.rank() > max_risk.rank()
    {
        return false;
    }
    if !grant.scope.hosts.is_empty() {
        let Some(ref host) = ctx.host else {
            return false;
        };
        if !grant.scope.hosts.iter().any(|h| h == "*" || h == host) {
            return false;
        }
    }
    if !grant.scope.domains.is_empty() {
        let Some(ref domain) = ctx.domain else {
            return false;
        };
        if !grant.scope.domains.iter().any(|d| d == "*" || d == domain) {
            return false;
        }
    }
    true
}

/// Core BOSS policy evaluator.
pub struct PolicyEvaluator {
    bundle: Option<VerifiedPolicyBundle>,
    requested_mode: ApprovalMode,
    local: LocalPolicyConfig,
    grants: GrantStore,
}

impl PolicyEvaluator {
    /// Construct an evaluator with a cryptographically verified BOSS bundle.
    pub fn with_verified_bundle(
        bundle: VerifiedPolicyBundle,
        requested_mode: ApprovalMode,
        local: LocalPolicyConfig,
        grants: GrantStore,
    ) -> Self {
        Self {
            bundle: Some(bundle),
            requested_mode,
            local,
            grants,
        }
    }

    /// Offline / unverified fallback (`ARCHITECTURE.md` §6.2):
    /// refuses mutating tools and allows only read-only local analysis.
    pub fn offline_readonly(local: LocalPolicyConfig) -> Self {
        Self {
            bundle: None,
            requested_mode: ApprovalMode::ReadOnly,
            local,
            grants: GrantStore::default(),
        }
    }

    pub fn grants(&self) -> &GrantStore {
        &self.grants
    }

    /// Compute the effective approval mode: local settings can ONLY tighten BOSS `mode_floor`.
    pub fn effective_mode(&self) -> ApprovalMode {
        let boss_floor = self
            .bundle
            .as_ref()
            .map(|b| b.bundle.mode_floor)
            .unwrap_or(ApprovalMode::ReadOnly);
        let mut mode = self.requested_mode.tighten_with(boss_floor);
        if let Some(local_floor) = self.local.mode_floor {
            mode = mode.tighten_with(local_floor);
        }
        mode
    }

    pub fn set_requested_mode(&mut self, mode: ApprovalMode) {
        self.requested_mode = mode;
    }

    pub fn bundle(&self) -> Option<&PolicyBundle> {
        self.bundle.as_ref().map(|b| &b.bundle)
    }

    /// Return the BOSS-governed cookie mode policy (default OFF when offline or unspecified).
    pub fn cookie_mode(&self) -> metatron_protocol::CookieModePolicy {
        self.bundle
            .as_ref()
            .map(|b| b.bundle.cookie_mode.clone())
            .unwrap_or_default()
    }

    /// Returns true if `tool_name` is permitted by BOSS `tools_allowed` and not unconditionally forbidden.
    /// Supports both plain tool names and MCP policy keys (`mcp__server__tool` / `mcp:server:tool`).
    pub fn is_tool_allowed(&self, tool_name: &str) -> bool {
        let Some(ref verified) = self.bundle else {
            return matches!(
                tool_name,
                "shell_local"
                    | "pyrun"
                    | "dashboard"
                    | "view_image"
                    | "update_plan"
                    | "tool_search"
            );
        };
        if !verified.bundle.tools_allowed.is_empty()
            && !verified
                .bundle
                .tools_allowed
                .iter()
                .any(|t| tool_spec_matches_pattern(tool_name, t))
        {
            return false;
        }
        !verified.bundle.rules.iter().any(|r| {
            r.decision == PolicyDecision::Forbidden
                && r.matcher
                    .tool
                    .as_deref()
                    .is_some_and(|pat| tool_spec_matches_pattern(tool_name, pat))
                && r.matcher.argv_prefix.is_none()
                && r.matcher.host.is_none()
                && r.matcher.domain.is_none()
                && r.matcher.conditions.is_empty()
        })
    }

    /// Step 1: filter client tools before advertising them to the model.
    pub fn filter_tools(&self, tools: &[ClientToolSpec]) -> Vec<ClientToolSpec> {
        let mode = self.effective_mode();
        tools
            .iter()
            .filter(|spec| {
                if let Some(ref verified) = self.bundle {
                    if !verified.bundle.tools_allowed.is_empty()
                        && !verified
                            .bundle
                            .tools_allowed
                            .iter()
                            .any(|t| tool_spec_matches_pattern(&spec.name, t))
                    {
                        return false;
                    }
                    // Hide tools unconditionally forbidden by BOSS rules (no argv/host filter).
                    let unconditionally_forbidden = verified.bundle.rules.iter().any(|r| {
                        r.decision == PolicyDecision::Forbidden
                            && r.matcher
                                .tool
                                .as_deref()
                                .is_some_and(|pat| tool_spec_matches_pattern(&spec.name, pat))
                            && r.matcher.argv_prefix.is_none()
                            && r.matcher.host.is_none()
                            && r.matcher.domain.is_none()
                            && r.matcher.conditions.is_empty()
                    });
                    if unconditionally_forbidden {
                        return false;
                    }
                }
                if mode == ApprovalMode::ReadOnly
                    && let Ok(rc) = spec.risk_class.parse::<RiskClass>()
                    && rc.is_mutating()
                {
                    return false;
                }
                true
            })
            .cloned()
            .collect()
    }

    /// Steps 2-4: classify, apply BOSS + verb + local tightening rules, and resolve mode.
    pub fn evaluate(&self, ctx: &ToolCallContext, now: DateTime<Utc>) -> EvaluationDecision {
        // Offline or expired bundle -> refuse any mutating tool (`ARCHITECTURE.md` §6.2).
        let Some(ref verified) = self.bundle else {
            if ctx.risk_class.is_mutating() || ctx.is_remote {
                return EvaluationDecision::Deny {
                    decided_by: DecisionSource::Policy,
                    rule_id: Some("offline-no-bundle".to_string()),
                    reason: "signed BOSS policy bundle required for remote or mutating tools"
                        .to_string(),
                };
            }
            return self.evaluate_readonly_local(ctx);
        };

        if verified.is_expired(now) && (ctx.risk_class.is_mutating() || ctx.is_remote) {
            return EvaluationDecision::Deny {
                decided_by: DecisionSource::Policy,
                rule_id: Some("policy-expired".to_string()),
                reason: "BOSS policy bundle has expired; refresh policy before running mutating or remote tools".to_string(),
            };
        }

        // Check `tools_allowed` list in bundle.
        if !verified.bundle.tools_allowed.is_empty()
            && !verified
                .bundle
                .tools_allowed
                .iter()
                .any(|t| tool_spec_matches_pattern(&ctx.tool, t))
        {
            return EvaluationDecision::Deny {
                decided_by: DecisionSource::Policy,
                rule_id: Some("tool-not-allowed".to_string()),
                reason: format!("tool '{}' is not in BOSS tools_allowed", ctx.tool),
            };
        }

        // Built-in command classification when argv is present (shell_local / ssh_run).
        let verb_verdict =
            if !ctx.argv.is_empty() && matches!(ctx.tool.as_str(), "shell_local" | "ssh_run") {
                Some(classify_command_argv(&ctx.argv, ctx.is_remote))
            } else {
                None
            };

        let effective_risk = verb_verdict
            .as_ref()
            .map(|v| v.risk_class)
            .unwrap_or(ctx.risk_class);
        let mut effective_ctx = ctx.clone();
        effective_ctx.risk_class = effective_risk;

        // Collect all matching BOSS rules (sorted by priority asc) + local tightening rules.
        let mut boss_matches: Vec<&PolicyRule> = verified
            .bundle
            .rules
            .iter()
            .filter(|r| rule_matches_call(r, &effective_ctx))
            .collect();
        boss_matches.sort_by_key(|r| r.priority);

        let local_matches: Vec<&PolicyRule> = self
            .local
            .rules
            .iter()
            .filter(|r| rule_matches_call(r, &effective_ctx))
            .collect();

        // Hard invariant 1: ANY `Forbidden` (BOSS, built-in verb, or local) ALWAYS wins.
        if let Some(r) = boss_matches
            .iter()
            .find(|r| r.decision == PolicyDecision::Forbidden)
        {
            return EvaluationDecision::Deny {
                decided_by: DecisionSource::Rule,
                rule_id: Some(r.id.clone()),
                reason: non_empty(&r.justification, "forbidden by BOSS policy rule"),
            };
        }
        if let Some(ref vv) = verb_verdict
            && vv.decision == PolicyDecision::Forbidden
        {
            return EvaluationDecision::Deny {
                decided_by: DecisionSource::Rule,
                rule_id: Some(vv.rule_id.clone()),
                reason: vv.justification.clone(),
            };
        }
        if let Some(r) = local_matches
            .iter()
            .find(|r| r.decision == PolicyDecision::Forbidden)
        {
            return EvaluationDecision::Deny {
                decided_by: DecisionSource::Rule,
                rule_id: Some(r.id.clone()),
                reason: non_empty(&r.justification, "forbidden by local policy rule"),
            };
        }

        // Compute effective decision (`Allow` vs `Prompt`) and `hard` flag.
        let mut any_hard = effective_risk.is_hard_prompt()
            || boss_matches.iter().any(|r| r.hard)
            || local_matches.iter().any(|r| r.hard)
            || verb_verdict.as_ref().is_some_and(|v| v.hard);

        let (mut base_decision, mut rule_id, mut reason) = if let Some(r) = boss_matches.first() {
            (
                r.decision,
                Some(r.id.clone()),
                non_empty(&r.justification, "matched BOSS policy rule"),
            )
        } else if let Some(ref vv) = verb_verdict {
            (
                vv.decision,
                Some(vv.rule_id.clone()),
                vv.justification.clone(),
            )
        } else {
            (
                PolicyDecision::Prompt,
                None,
                format!("default prompt for tool '{}'", effective_ctx.tool),
            )
        };

        // If any BOSS rule says `hard: true` or `Prompt` with higher strictness than `Allow`,
        // tighten to `Prompt` (hard rules can NEVER be weakened by another allow rule).
        if let Some(hard_rule) = boss_matches.iter().find(|r| r.hard) {
            any_hard = true;
            base_decision = PolicyDecision::Prompt;
            rule_id = Some(hard_rule.id.clone());
            reason = non_empty(
                &hard_rule.justification,
                "hard BOSS policy rule requires human approval",
            );
        }
        // If verb validator flagged an unsafe form (e.g. `sar -o`), do NOT let a broad prefix allow it!
        if let Some(ref vv) = verb_verdict
            && vv.decision == PolicyDecision::Prompt
            && base_decision == PolicyDecision::Allow
        {
            base_decision = PolicyDecision::Prompt;
            rule_id = Some(vv.rule_id.clone());
            reason = vv.justification.clone();
        }
        // Local tightening rules can turn `Allow` -> `Prompt`.
        if let Some(local_prompt) = local_matches
            .iter()
            .find(|r| r.decision == PolicyDecision::Prompt)
        {
            base_decision = PolicyDecision::Prompt;
            rule_id = Some(local_prompt.id.clone());
            reason = non_empty(
                &local_prompt.justification,
                "tightened by local policy rule",
            );
        }

        // Resolve by effective approval mode.
        let mode = self.effective_mode();
        match mode {
            ApprovalMode::ReadOnly => {
                if effective_risk.is_mutating() || any_hard {
                    return EvaluationDecision::Deny {
                        decided_by: DecisionSource::Mode,
                        rule_id,
                        reason: format!(
                            "blocked in read-only mode (risk class '{}')",
                            effective_risk.as_str()
                        ),
                    };
                }
                if base_decision == PolicyDecision::Allow {
                    EvaluationDecision::AutoAllow {
                        decided_by: DecisionSource::Rule,
                        rule_id,
                        reason,
                    }
                } else {
                    EvaluationDecision::Deny {
                        decided_by: DecisionSource::Mode,
                        rule_id,
                        reason: "read-only mode blocks non-allowlisted actions".to_string(),
                    }
                }
            }
            ApprovalMode::Ask => EvaluationDecision::AskHuman {
                rule_id,
                reason,
                allow_always: !any_hard,
                hard: any_hard,
            },
            ApprovalMode::RiskyOnly => {
                if any_hard {
                    EvaluationDecision::AskHuman {
                        rule_id,
                        reason,
                        allow_always: false,
                        hard: true,
                    }
                } else if base_decision == PolicyDecision::Allow {
                    EvaluationDecision::AutoAllow {
                        decided_by: DecisionSource::Rule,
                        rule_id,
                        reason,
                    }
                } else {
                    EvaluationDecision::AskHuman {
                        rule_id,
                        reason,
                        allow_always: true,
                        hard: false,
                    }
                }
            }
            ApprovalMode::ActOnMyBehalf => {
                // Hard-prompt classes and `hard: true` rules ALWAYS ask a human!
                if any_hard {
                    return EvaluationDecision::AskHuman {
                        rule_id,
                        reason,
                        allow_always: false,
                        hard: true,
                    };
                }
                if base_decision == PolicyDecision::Allow {
                    return EvaluationDecision::AutoAllow {
                        decided_by: DecisionSource::Rule,
                        rule_id,
                        reason,
                    };
                }
                if let Some(grant) = self.grants.find_covering(&effective_ctx, now) {
                    EvaluationDecision::AskReviewer {
                        grant_id: grant.id,
                        rule_id,
                        reason,
                    }
                } else {
                    EvaluationDecision::AskHuman {
                        rule_id,
                        reason: format!(
                            "{reason} (no active act-on-my-behalf grant covers this call)"
                        ),
                        allow_always: true,
                        hard: false,
                    }
                }
            }
        }
    }

    fn evaluate_readonly_local(&self, ctx: &ToolCallContext) -> EvaluationDecision {
        if !ctx.argv.is_empty() {
            let vv = classify_command_argv(&ctx.argv, false);
            if vv.decision == PolicyDecision::Allow && !vv.risk_class.is_mutating() {
                return EvaluationDecision::AutoAllow {
                    decided_by: DecisionSource::Rule,
                    rule_id: Some(vv.rule_id),
                    reason: vv.justification,
                };
            }
        }
        EvaluationDecision::Deny {
            decided_by: DecisionSource::Mode,
            rule_id: Some("offline-readonly".to_string()),
            reason: "offline mode allows only read-only local analysis".to_string(),
        }
    }
}

fn non_empty(s: &str, fallback: &str) -> String {
    if s.trim().is_empty() {
        fallback.to_string()
    } else {
        s.to_string()
    }
}

/// Convert a namespaced MCP tool name `mcp__<server>__<tool>` into its canonical
/// policy key `mcp:<server>:<tool>` (`MT-9`, `ARCHITECTURE.md` §8).
pub fn mcp_policy_key(tool_name: &str) -> Option<(String, String, String)> {
    let rest = tool_name.strip_prefix("mcp__")?;
    let (server, tool) = rest.split_once("__")?;
    if server.is_empty() || tool.is_empty() {
        return None;
    }
    Some((
        server.to_string(),
        tool.to_string(),
        format!("mcp:{server}:{tool}"),
    ))
}

fn tool_spec_matches_pattern(tool_name: &str, pattern: &str) -> bool {
    if pattern == "*" || pattern == tool_name {
        return true;
    }
    if let Some((server, tool, policy_key)) = mcp_policy_key(tool_name)
        && (pattern == policy_key
            || pattern == format!("mcp:{server}:*")
            || pattern == format!("mcp__{server}__*")
            || pattern == "mcp:*"
            || pattern == "mcp__*"
            || pattern == tool)
    {
        return true;
    }
    false
}

fn rule_matches_call(rule: &PolicyRule, ctx: &ToolCallContext) -> bool {
    let m = &rule.matcher;
    if let Some(ref server) = m.server
        && server != "*"
    {
        let Some((call_server, _, _)) = mcp_policy_key(&ctx.tool) else {
            return false;
        };
        if &call_server != server {
            return false;
        }
    }
    if let Some(ref tool) = m.tool
        && !tool_spec_matches_pattern(&ctx.tool, tool)
    {
        return false;
    }
    if let Some(ref host) = m.host
        && host != "*"
        && ctx.host.as_deref() != Some(host.as_str())
    {
        return false;
    }
    if let Some(ref domain) = m.domain
        && domain != "*"
        && ctx.domain.as_deref() != Some(domain.as_str())
    {
        return false;
    }
    if let Some(ref rc) = m.risk_class
        && rc != "*"
        && rc != ctx.risk_class.as_str()
    {
        return false;
    }
    if let Some(ref prefix) = m.argv_prefix {
        if ctx.argv.len() < prefix.len() {
            return false;
        }
        for (idx, expected) in prefix.iter().enumerate() {
            let actual = if idx == 0 {
                ctx.argv[0].rsplit('/').next().unwrap_or(&ctx.argv[0])
            } else {
                ctx.argv[idx].as_str()
            };
            if actual != expected {
                return false;
            }
        }
    }
    true
}

/// Evaluate a rule against an example string during bundle load validation.
pub(crate) fn rule_matches_example(rule: &PolicyRule, sample: &str) -> bool {
    let trimmed = sample.trim();
    if trimmed.is_empty() {
        return false;
    }
    // If rule specifies `argv_prefix`, parse the example into argv and validate prefix + diagnostic safety.
    if let Some(ref prefix) = rule.matcher.argv_prefix {
        let Ok(argv) = parse_command_argv(trimmed) else {
            return false;
        };
        if argv.len() < prefix.len() {
            return false;
        }
        for (idx, expected) in prefix.iter().enumerate() {
            let actual = if idx == 0 {
                argv[0].rsplit('/').next().unwrap_or(&argv[0])
            } else {
                argv[idx].as_str()
            };
            if actual != expected {
                return false;
            }
        }
        // For Allow rules on diagnostic verbs (e.g. `sar`), reject unsafe forms like `sar -o /tmp/x`.
        if rule.decision == PolicyDecision::Allow {
            let verdict = classify_command_argv(&argv, true);
            if verdict.decision != PolicyDecision::Allow {
                return false;
            }
        }
        return true;
    }
    // Otherwise match against tool name / prefix.
    if let Some(ref tool) = rule.matcher.tool {
        return tool == "*" || trimmed == tool || trimmed.starts_with(&format!("{tool} "));
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jws::{sign_policy_bundle_ed25519, verify_signed_policy_bundle};
    use chrono::Duration;
    use ed25519_dalek::SigningKey;
    use metatron_protocol::{
        CookieModePolicy, GrantScope, PolicyRuleExamples, PolicyRuleMatch, ToolExposure,
    };
    use rand::rngs::OsRng;

    fn make_verified_bundle(mode_floor: ApprovalMode, now: DateTime<Utc>) -> VerifiedPolicyBundle {
        let sk = SigningKey::generate(&mut OsRng);
        let bundle = PolicyBundle {
            version: "v-table".to_string(),
            project_id: Some("boss-dogfood".to_string()),
            issued_at: Some(now),
            expires_at: now + Duration::hours(1),
            mode_floor,
            tools_allowed: vec![
                "shell_local".to_string(),
                "ssh_run".to_string(),
                "pcap".to_string(),
                "pyrun".to_string(),
            ],
            rules: vec![
                PolicyRule {
                    id: "r-sar".to_string(),
                    scope: Some("project".to_string()),
                    priority: 10,
                    matcher: PolicyRuleMatch {
                        tool: Some("ssh_run".to_string()),
                        argv_prefix: Some(vec!["sar".to_string()]),
                        ..Default::default()
                    },
                    decision: PolicyDecision::Allow,
                    hard: false,
                    justification: "read-only sysstat".to_string(),
                    examples: PolicyRuleExamples {
                        should_match: vec!["sar -u 1 5".to_string()],
                        not_match: vec!["sar -o /tmp/x 1 5".to_string()],
                    },
                },
                PolicyRule {
                    id: "r-pcap-hard".to_string(),
                    scope: Some("org".to_string()),
                    priority: 5,
                    matcher: PolicyRuleMatch {
                        tool: Some("pcap".to_string()),
                        ..Default::default()
                    },
                    decision: PolicyDecision::Prompt,
                    hard: true,
                    justification: "capture always asks a person".to_string(),
                    examples: PolicyRuleExamples::default(),
                },
                PolicyRule {
                    id: "r-forbid-rm".to_string(),
                    scope: Some("org".to_string()),
                    priority: 1,
                    matcher: PolicyRuleMatch {
                        tool: Some("ssh_run".to_string()),
                        argv_prefix: Some(vec!["rm".to_string()]),
                        ..Default::default()
                    },
                    decision: PolicyDecision::Forbidden,
                    hard: true,
                    justification: "rm is forbidden".to_string(),
                    examples: PolicyRuleExamples::default(),
                },
            ],
            cookie_mode: CookieModePolicy::default(),
            host_diagnostics: None,
            mcp: None,
        };
        let (resp, jwks) = sign_policy_bundle_ed25519(&bundle, "kid-t", &sk).expect("sign");
        verify_signed_policy_bundle(&resp, &jwks, now).expect("verify")
    }

    fn make_grant(now: DateTime<Utc>, ttl_s: i64) -> GrantRecord {
        GrantRecord {
            id: "mgrant_1".to_string(),
            project_id: "boss-dogfood".to_string(),
            scope: GrantScope {
                hosts: vec!["web-1".to_string()],
                domains: Vec::new(),
            },
            allowed_tools: vec![
                "shell_local".to_string(),
                "ssh_run".to_string(),
                "pyrun".to_string(),
            ],
            max_risk_class: Some(RiskClass::WriteLocal),
            expires_at: now + Duration::seconds(ttl_s),
            revoked_at: None,
        }
    }

    #[test]
    fn four_modes_x_risk_classes_matrix() {
        let now = Utc::now();
        // Set mode_floor to ActOnMyBehalf so we can test all 4 modes freely.
        let verified = make_verified_bundle(ApprovalMode::ActOnMyBehalf, now);

        let sar_call = ToolCallContext {
            tool: "ssh_run".to_string(),
            argv: ["sar", "-u", "1", "5"].map(str::to_string).to_vec(),
            host: Some("web-1".to_string()),
            domain: None,
            risk_class: RiskClass::RemoteRead,
            is_remote: true,
        };
        let tcpdump_call = ToolCallContext {
            tool: "ssh_run".to_string(),
            argv: ["tcpdump", "-i", "eth0", "-c", "5"]
                .map(str::to_string)
                .to_vec(),
            host: Some("web-1".to_string()),
            domain: None,
            risk_class: RiskClass::Capture,
            is_remote: true,
        };
        let rm_call = ToolCallContext {
            tool: "ssh_run".to_string(),
            argv: ["rm", "-rf", "/tmp/a"].map(str::to_string).to_vec(),
            host: Some("web-1".to_string()),
            domain: None,
            risk_class: RiskClass::RemoteWrite,
            is_remote: true,
        };
        let write_local_call = ToolCallContext {
            tool: "shell_local".to_string(),
            argv: ["cp", "scratch/a", "out/a"].map(str::to_string).to_vec(),
            host: Some("web-1".to_string()),
            domain: None,
            risk_class: RiskClass::WriteLocal,
            is_remote: false,
        };

        // 1. read-only: allows sar (remote-read), blocks write_local, tcpdump, rm
        let ev_ro = PolicyEvaluator::with_verified_bundle(
            verified.clone(),
            ApprovalMode::ReadOnly,
            LocalPolicyConfig::default(),
            GrantStore::default(),
        );
        assert!(matches!(
            ev_ro.evaluate(&sar_call, now),
            EvaluationDecision::AutoAllow { .. }
        ));
        assert!(matches!(
            ev_ro.evaluate(&write_local_call, now),
            EvaluationDecision::Deny { .. }
        ));
        assert!(matches!(
            ev_ro.evaluate(&tcpdump_call, now),
            EvaluationDecision::Deny { .. }
        ));
        assert!(matches!(
            ev_ro.evaluate(&rm_call, now),
            EvaluationDecision::Deny { .. }
        ));

        // 2. ask: prompts for sar, write_local, tcpdump (hard, allow_always=false); denies rm
        let ev_ask = PolicyEvaluator::with_verified_bundle(
            verified.clone(),
            ApprovalMode::Ask,
            LocalPolicyConfig::default(),
            GrantStore::default(),
        );
        assert!(matches!(
            ev_ask.evaluate(&sar_call, now),
            EvaluationDecision::AskHuman { .. }
        ));
        assert!(matches!(
            ev_ask.evaluate(&tcpdump_call, now),
            EvaluationDecision::AskHuman {
                allow_always: false,
                hard: true,
                ..
            }
        ));
        assert!(matches!(
            ev_ask.evaluate(&rm_call, now),
            EvaluationDecision::Deny { .. }
        ));

        // 3. risky-only: auto-runs sar (decided_by=Rule), prompts for tcpdump and write_local, denies rm
        let ev_risky = PolicyEvaluator::with_verified_bundle(
            verified.clone(),
            ApprovalMode::RiskyOnly,
            LocalPolicyConfig::default(),
            GrantStore::default(),
        );
        assert!(matches!(
            ev_risky.evaluate(&sar_call, now),
            EvaluationDecision::AutoAllow {
                decided_by: DecisionSource::Rule,
                ..
            }
        ));
        assert!(matches!(
            ev_risky.evaluate(&tcpdump_call, now),
            EvaluationDecision::AskHuman {
                allow_always: false,
                hard: true,
                ..
            }
        ));
        assert!(matches!(
            ev_risky.evaluate(&write_local_call, now),
            EvaluationDecision::AskHuman { .. }
        ));
        assert!(matches!(
            ev_risky.evaluate(&rm_call, now),
            EvaluationDecision::Deny { .. }
        ));

        // 4. act-on-my-behalf with active grant: routes write_local to reviewer, STILL asks human for hard tcpdump/pcap, denies rm
        let ev_act = PolicyEvaluator::with_verified_bundle(
            verified,
            ApprovalMode::ActOnMyBehalf,
            LocalPolicyConfig::default(),
            GrantStore::new(vec![make_grant(now, 600)]),
        );
        assert!(matches!(
            ev_act.evaluate(&sar_call, now),
            EvaluationDecision::AutoAllow {
                decided_by: DecisionSource::Rule,
                ..
            }
        ));
        assert!(matches!(
            ev_act.evaluate(&write_local_call, now),
            EvaluationDecision::AskReviewer { grant_id, .. } if grant_id == "mgrant_1"
        ));
        assert!(matches!(
            ev_act.evaluate(&tcpdump_call, now),
            EvaluationDecision::AskHuman {
                allow_always: false,
                hard: true,
                ..
            }
        ));
        assert!(matches!(
            ev_act.evaluate(&rm_call, now),
            EvaluationDecision::Deny { .. }
        ));
    }

    #[test]
    fn grant_expiry_and_revocation_stop_reviewer_routing() {
        let now = Utc::now();
        let verified = make_verified_bundle(ApprovalMode::ActOnMyBehalf, now);
        let ev = PolicyEvaluator::with_verified_bundle(
            verified,
            ApprovalMode::ActOnMyBehalf,
            LocalPolicyConfig::default(),
            GrantStore::new(vec![make_grant(now, 60)]),
        );
        let call = ToolCallContext {
            tool: "shell_local".to_string(),
            argv: ["cp", "scratch/a", "out/a"].map(str::to_string).to_vec(),
            host: Some("web-1".to_string()),
            domain: None,
            risk_class: RiskClass::WriteLocal,
            is_remote: false,
        };

        // Active before expiry
        assert!(matches!(
            ev.evaluate(&call, now),
            EvaluationDecision::AskReviewer { .. }
        ));

        // Expired after TTL
        let later = now + Duration::seconds(61);
        assert!(matches!(
            ev.evaluate(&call, later),
            EvaluationDecision::AskHuman { .. }
        ));

        // Revoked before expiry
        assert!(ev.grants().revoke("mgrant_1", now));
        assert!(matches!(
            ev.evaluate(&call, now),
            EvaluationDecision::AskHuman { .. }
        ));
    }

    #[test]
    fn local_config_cannot_weaken_boss_mode_floor_or_allow_rules() {
        let now = Utc::now();
        let verified = make_verified_bundle(ApprovalMode::Ask, now);
        // Requesting act-on-my-behalf gets clamped to BOSS mode_floor = Ask.
        let ev = PolicyEvaluator::with_verified_bundle(
            verified,
            ApprovalMode::ActOnMyBehalf,
            LocalPolicyConfig::default(),
            GrantStore::default(),
        );
        assert_eq!(ev.effective_mode(), ApprovalMode::Ask);

        // Parsing a local TOML rule with decision="allow" must fail.
        let bad_toml = r#"
[[rules]]
id = "local-loosen"
decision = "allow"
[rules.match]
tool = "ssh_run"
"#;
        assert!(matches!(
            LocalPolicyConfig::parse_toml(bad_toml),
            Err(PolicyConfigError::LocalCannotLoosen(_))
        ));
    }

    #[test]
    fn filter_tools_hides_disallowed_and_mutating_in_readonly() {
        let now = Utc::now();
        let verified = make_verified_bundle(ApprovalMode::ReadOnly, now);
        let ev = PolicyEvaluator::with_verified_bundle(
            verified,
            ApprovalMode::ReadOnly,
            LocalPolicyConfig::default(),
            GrantStore::default(),
        );
        let specs = vec![
            ClientToolSpec {
                name: "shell_local".to_string(),
                description: "local".to_string(),
                input_schema: serde_json::json!({}),
                risk_class: "read-local".to_string(),
                exposure: ToolExposure::Direct,
            },
            ClientToolSpec {
                name: "pcap".to_string(),
                description: "pcap".to_string(),
                input_schema: serde_json::json!({}),
                risk_class: "capture".to_string(),
                exposure: ToolExposure::Direct,
            },
            ClientToolSpec {
                name: "apply_patch".to_string(),
                description: "never".to_string(),
                input_schema: serde_json::json!({}),
                risk_class: "write-local".to_string(),
                exposure: ToolExposure::Direct,
            },
        ];
        let visible = ev.filter_tools(&specs);
        assert_eq!(visible.len(), 1);
        assert_eq!(visible[0].name, "shell_local");
    }
}
