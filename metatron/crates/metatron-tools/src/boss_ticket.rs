//! BOSS ticket / findings attachment tools (`boss_get_ticket`, `boss_post_finding`, `MT-4`).

use std::fs;
use std::path::Path;

use async_trait::async_trait;
use metatron_core::{Tool, ToolCtx, ToolOutcome};
use metatron_protocol::{
    ClientToolSpec, EvidenceRef, RiskClass, TicketFindingRequest, ToolExposure, ToolResultStatus,
};
use metatron_secrets::redact_secrets;
use serde_json::{Value, json};
use uuid::Uuid;

use crate::transfer::{TransferTool, sha256_file};

pub struct BossGetTicketTool;

#[async_trait]
impl Tool for BossGetTicketTool {
    fn name(&self) -> &str {
        "boss_get_ticket"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "boss_get_ticket".to_string(),
            description: "Read a BOSS task/ticket record by ID.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["ticket_id"],
                "properties": {
                    "ticket_id": { "type": "string" }
                }
            }),
            risk_class: "read-local".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, _args: &Value) -> RiskClass {
        RiskClass::ReadLocal
    }

    fn parallel_ok(&self) -> bool {
        true
    }

    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let Some(ticket_id) = args.get("ticket_id").and_then(Value::as_str) else {
            return err_outcome("boss_get_ticket requires 'ticket_id'");
        };
        let Some(ref client) = ctx.boss_client else {
            return err_outcome("BOSS client unavailable");
        };
        match client.get_ticket(ticket_id).await {
            Ok(val) => {
                let text = redact_secrets(
                    &serde_json::to_string_pretty(&val).unwrap_or_else(|_| val.to_string()),
                );
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
            Err(e) => err_outcome(&e.to_string()),
        }
    }
}

pub struct BossPostFindingTool;

/// Collect redacted `EvidenceRef` metadata (name, path, SHA-256, bytes) strictly for files inside
/// `evidence_dir` or `scratch_dir`.
pub fn collect_evidence_refs(ctx: &ToolCtx, names: &[String]) -> Result<Vec<EvidenceRef>, String> {
    let mut out = Vec::new();
    for raw in names {
        let candidate = if let Ok(p) = TransferTool::safe_child_path(&ctx.evidence_dir, raw)
            && p.is_file()
        {
            p
        } else if let Ok(p) = TransferTool::safe_child_path(&ctx.scratch_dir, raw)
            && p.is_file()
        {
            p
        } else {
            let abs = Path::new(raw);
            if abs.is_absolute() && abs.is_file() {
                let canon = fs::canonicalize(abs).map_err(|e| e.to_string())?;
                let ev = fs::canonicalize(&ctx.evidence_dir)
                    .unwrap_or_else(|_| ctx.evidence_dir.clone());
                let sc =
                    fs::canonicalize(&ctx.scratch_dir).unwrap_or_else(|_| ctx.scratch_dir.clone());
                if canon.starts_with(&ev) || canon.starts_with(&sc) {
                    canon
                } else {
                    return Err(format!(
                        "evidence file '{raw}' is outside session evidence/scratch dirs"
                    ));
                }
            } else {
                return Err(format!(
                    "evidence file '{raw}' not found in evidence or scratch dir"
                ));
            }
        };

        let (sha256, bytes) = sha256_file(&candidate).map_err(|e| e.to_string())?;
        let name = candidate
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("artifact")
            .to_string();
        let media_type = match candidate.extension().and_then(|e| e.to_str()) {
            Some("html") => Some("text/html".to_string()),
            Some("csv") => Some("text/csv".to_string()),
            Some("json") => Some("application/json".to_string()),
            Some("pcap") | Some("pcapng") => Some("application/vnd.tcpdump.pcap".to_string()),
            Some("png") => Some("image/png".to_string()),
            _ => Some("text/plain".to_string()),
        };
        out.push(EvidenceRef {
            name,
            path: candidate.display().to_string(),
            sha256,
            bytes,
            media_type,
        });
    }
    Ok(out)
}

#[async_trait]
impl Tool for BossPostFindingTool {
    fn name(&self) -> &str {
        "boss_post_finding"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "boss_post_finding".to_string(),
            description: "Post redacted investigation findings and evidence artifact references (SHA-256 manifest + optional dashboard excerpt) to a BOSS ticket/task.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["title", "summary"],
                "properties": {
                    "ticket_id": { "type": "string" },
                    "title": { "type": "string" },
                    "summary": { "type": "string" },
                    "evidence_files": { "type": "array", "items": { "type": "string" } },
                    "dashboard_excerpt": { "type": "string" },
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
            return err_outcome("BOSS client unavailable");
        };
        let Some(title) = args
            .get("title")
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty())
        else {
            return err_outcome("boss_post_finding requires 'title'");
        };
        let Some(summary) = args
            .get("summary")
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty())
        else {
            return err_outcome("boss_post_finding requires 'summary'");
        };
        let ticket_id = args
            .get("ticket_id")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .or_else(|| ctx.ticket_id.clone());

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

        let dashboard_excerpt = args
            .get("dashboard_excerpt")
            .and_then(Value::as_str)
            .map(redact_secrets);
        let dedupe_key = args
            .get("dedupe_key")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .unwrap_or_else(|| format!("finding:{}:{}", ctx.session_id, Uuid::new_v4().simple()));

        let req = TicketFindingRequest {
            project_id: ctx.project_id.clone(),
            ticket_id,
            title: redact_secrets(title),
            summary: redact_secrets(summary),
            evidence,
            dashboard_excerpt,
            session_id: ctx.session_id.clone(),
            dedupe_key,
        };

        match client.post_ticket_finding(&req).await {
            Ok(resp) => {
                let text = serde_json::to_string_pretty(&resp)
                    .unwrap_or_else(|_| format!("posted to ticket {}", resp.ticket_id));
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
            Err(e) => err_outcome(&e.to_string()),
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
