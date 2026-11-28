//! Governed BOSS code-task handoff tool (`handoff_code_task`, `MT-5`).
//!
//! Hard architectural invariant (`README.md`, `ARCHITECTURE.md` §4.5):
//! Metatron NEVER edits or patches local source files. When an investigation uncovers a
//! code fix, `handoff_code_task` asks the operator for approval (`RiskClass::Handoff`),
//! creates a governed BOSS code task with findings, redacted logs, and evidence SHA-256
//! references attached, and reports the task's live status back in Metatron.

use async_trait::async_trait;
use metatron_core::{Tool, ToolCtx, ToolOutcome};
use metatron_protocol::{
    ClientToolSpec, CodeTaskHandoffRequest, RiskClass, ToolExposure, ToolResultStatus,
};
use metatron_secrets::redact_secrets;
use serde_json::{Value, json};
use uuid::Uuid;

use crate::boss_ticket::collect_evidence_refs;

pub struct HandoffCodeTaskTool;

#[async_trait]
impl Tool for HandoffCodeTaskTool {
    fn name(&self) -> &str {
        "handoff_code_task"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "handoff_code_task".to_string(),
            description: "Hand off a required code change to BOSS's governed code-task pipeline (build, test, review, PR) with investigation findings, redacted logs, and evidence links attached. Metatron never edits source files locally.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["title", "instructions", "findings"],
                "properties": {
                    "title": { "type": "string" },
                    "instructions": { "type": "string", "description": "What BOSS should implement and test" },
                    "findings": { "type": "string", "description": "Root cause and investigation evidence summary" },
                    "evidence_files": { "type": "array", "items": { "type": "string" } },
                    "log_excerpt": { "type": "string" },
                    "ticket_id": { "type": "string" },
                    "repo": { "type": "string" },
                    "base_branch": { "type": "string" },
                    "dedupe_key": { "type": "string" }
                }
            }),
            risk_class: "handoff".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, _args: &Value) -> RiskClass {
        RiskClass::Handoff
    }

    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let Some(ref client) = ctx.boss_client else {
            return err_outcome("BOSS client unavailable for handoff_code_task");
        };
        let Some(title) = args
            .get("title")
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty())
        else {
            return err_outcome("handoff_code_task requires 'title'");
        };
        let Some(instructions) = args
            .get("instructions")
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty())
        else {
            return err_outcome("handoff_code_task requires 'instructions'");
        };
        let Some(findings) = args
            .get("findings")
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty())
        else {
            return err_outcome("handoff_code_task requires 'findings'");
        };

        let file_names: Vec<String> = args
            .get("evidence_files")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(ToString::to_string)
                    .collect()
            })
            .unwrap_or_default();

        let evidence = match collect_evidence_refs(ctx, &file_names) {
            Ok(e) => e,
            Err(e) => return err_outcome(&e),
        };

        let dedupe_key = args
            .get("dedupe_key")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .unwrap_or_else(|| format!("handoff:{}:{}", ctx.session_id, Uuid::new_v4().simple()));

        let req = CodeTaskHandoffRequest {
            project_id: ctx.project_id.clone(),
            title: redact_secrets(title),
            instructions: redact_secrets(instructions),
            findings: redact_secrets(findings),
            evidence,
            log_excerpt: args
                .get("log_excerpt")
                .and_then(Value::as_str)
                .map(redact_secrets),
            session_id: ctx.session_id.clone(),
            ticket_id: args
                .get("ticket_id")
                .and_then(Value::as_str)
                .map(ToString::to_string)
                .or_else(|| ctx.ticket_id.clone()),
            repo: args
                .get("repo")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            base_branch: args
                .get("base_branch")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            dedupe_key,
        };

        let created = match client.create_code_task(&req).await {
            Ok(r) => r,
            Err(e) => return err_outcome(&format!("failed to create BOSS code task: {e}")),
        };

        // Fetch live status so Metatron displays current state right after handoff.
        let latest = client
            .get_code_task(&created.run_id)
            .await
            .unwrap_or(created);

        let text = serde_json::to_string_pretty(&json!({
            "run_id": latest.run_id,
            "task_id": latest.task_id,
            "status": latest.status,
            "url": latest.url,
            "message": latest.message,
            "attached_evidence_count": req.evidence.len(),
        }))
        .unwrap_or_else(|_| format!("created code task {}", latest.run_id));
        let bytes = text.len();

        ToolOutcome {
            status: ToolResultStatus::Ok,
            inline_text: text.clone(),
            full_text: text,
            truncated: false,
            exit_code: Some(0),
            bytes,
            duration_ms: 0,
            host: None,
            host_origin: None,
            sandbox_denied: false,
            images: Vec::new(),
            http_audit: None,
        }
    }
}

fn err_outcome(msg: &str) -> ToolOutcome {
    ToolOutcome {
        status: ToolResultStatus::Error,
        inline_text: msg.to_string(),
        full_text: msg.to_string(),
        truncated: false,
        exit_code: Some(2),
        bytes: 0,
        duration_ms: 0,
        host: None,
        host_origin: None,
        sandbox_denied: false,
        images: Vec::new(),
        http_audit: None,
    }
}
