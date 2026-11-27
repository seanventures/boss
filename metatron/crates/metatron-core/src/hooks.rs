//! Operator notification hooks (`MT-5`, `ARCHITECTURE.md` §4.5 / GAP §D).
//!
//! Hooks run user-configured commands on `on_decision`, `on_run_complete`, and `on_handoff`
//! events with a redacted JSON payload piped on stdin. Failures are returned visibly
//! (never silently ignored).

use std::process::Stdio;
use std::time::Duration;

use metatron_secrets::redact_secrets;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HookEventKind {
    OnDecision,
    OnRunComplete,
    OnHandoff,
}

impl HookEventKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::OnDecision => "on_decision",
            Self::OnRunComplete => "on_run_complete",
            Self::OnHandoff => "on_handoff",
        }
    }
}

/// Hook entry configurable in `~/.config/metatron/config.toml` (`[[hooks]]`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HookConfig {
    pub name: String,
    pub event: HookEventKind,
    pub command: Vec<String>,
    #[serde(default = "default_hook_timeout_s")]
    pub timeout_s: u64,
    #[serde(default = "default_enabled")]
    pub enabled: bool,
}

fn default_hook_timeout_s() -> u64 {
    10
}

fn default_enabled() -> bool {
    true
}

/// Visible outcome of executing one hook.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookOutcome {
    pub name: String,
    pub event: HookEventKind,
    pub ok: bool,
    pub exit_code: Option<i32>,
    pub message: String,
}

/// Runs configured notification hooks with redacted JSON stdin payloads.
#[derive(Debug, Clone, Default)]
pub struct HookRunner {
    hooks: Vec<HookConfig>,
}

impl HookRunner {
    pub fn new(hooks: Vec<HookConfig>) -> Self {
        Self { hooks }
    }

    pub fn hooks(&self) -> &[HookConfig] {
        &self.hooks
    }

    /// Fire all enabled hooks registered for `event`, returning visible outcomes for every hook.
    pub async fn fire(&self, event: HookEventKind, payload: &Value) -> Vec<HookOutcome> {
        let serialized = serde_json::to_string(payload).unwrap_or_else(|_| "{}".to_string());
        let redacted = redact_secrets(&serialized);
        let mut outcomes = Vec::new();

        for hook in self.hooks.iter().filter(|h| h.enabled && h.event == event) {
            outcomes.push(run_single_hook(hook, &redacted).await);
        }
        outcomes
    }
}

async fn run_single_hook(hook: &HookConfig, redacted_json: &str) -> HookOutcome {
    let Some((prog, args)) = hook.command.split_first() else {
        return HookOutcome {
            name: hook.name.clone(),
            event: hook.event,
            ok: false,
            exit_code: None,
            message: "hook command is empty".to_string(),
        };
    };

    let mut child = match Command::new(prog)
        .args(args)
        .env("METATRON_HOOK_EVENT", hook.event.as_str())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            return HookOutcome {
                name: hook.name.clone(),
                event: hook.event,
                ok: false,
                exit_code: None,
                message: format!("failed to spawn hook: {e}"),
            };
        }
    };

    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(redacted_json.as_bytes()).await;
        let _ = stdin.write_all(b"\n").await;
        let _ = stdin.shutdown().await;
    }

    let timeout = Duration::from_secs(hook.timeout_s.clamp(1, 60));
    match tokio::time::timeout(timeout, child.wait_with_output()).await {
        Ok(Ok(out)) => {
            let code = out.status.code();
            let ok = out.status.success();
            let text = if ok {
                String::from_utf8_lossy(&out.stdout).trim().to_string()
            } else {
                String::from_utf8_lossy(&out.stderr).trim().to_string()
            };
            HookOutcome {
                name: hook.name.clone(),
                event: hook.event,
                ok,
                exit_code: code,
                message: redact_secrets(&text),
            }
        }
        Ok(Err(e)) => HookOutcome {
            name: hook.name.clone(),
            event: hook.event,
            ok: false,
            exit_code: None,
            message: format!("hook wait error: {e}"),
        },
        Err(_) => HookOutcome {
            name: hook.name.clone(),
            event: hook.event,
            ok: false,
            exit_code: Some(124),
            message: format!("hook timed out after {}s", timeout.as_secs()),
        },
    }
}

#[cfg(test)]
#[allow(clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::TempDir;

    #[tokio::test]
    async fn hooks_fire_with_redacted_json_and_report_failures_visibly() {
        let tmp = TempDir::new().expect("tmp");
        let captured = tmp.path().join("hook_payload.json");
        let runner = HookRunner::new(vec![
            HookConfig {
                name: "notify-decision".to_string(),
                event: HookEventKind::OnDecision,
                command: vec![
                    "python3".to_string(),
                    "-c".to_string(),
                    format!(
                        "import sys, pathlib; pathlib.Path({:?}).write_text(sys.stdin.read()); print('notified')",
                        captured.to_string_lossy()
                    ),
                ],
                timeout_s: 5,
                enabled: true,
            },
            HookConfig {
                name: "failing-hook".to_string(),
                event: HookEventKind::OnDecision,
                command: vec![
                    "python3".to_string(),
                    "-c".to_string(),
                    "import sys; sys.stderr.write('webhook down'); sys.exit(2)".to_string(),
                ],
                timeout_s: 5,
                enabled: true,
            },
        ]);

        let outcomes = runner
            .fire(
                HookEventKind::OnDecision,
                &json!({
                    "tool": "handoff_code_task",
                    "decision": "allow",
                    "token_leak_check": "boss_pat_supersecret123456"
                }),
            )
            .await;

        assert_eq!(outcomes.len(), 2);
        assert!(outcomes[0].ok);
        assert_eq!(outcomes[0].message, "notified");
        assert!(!outcomes[1].ok);
        assert_eq!(outcomes[1].exit_code, Some(2));
        assert!(outcomes[1].message.contains("webhook down"));

        let saved = std::fs::read_to_string(&captured).expect("read captured");
        assert!(saved.contains("handoff_code_task"));
        assert!(!saved.contains("boss_pat_supersecret123456"));
    }
}
