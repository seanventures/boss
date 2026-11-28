//! Ecosystem built-in tools (`MT-9`):
//! - `view_image`: reads a local image from `evidence_dir` or `scratch_dir` (elsewhere requires
//!   approval), checks magic bytes and size cap, and returns `ToolContentItem::Image`.
//! - `web_search`: prefers an admin-approved search MCP server (SearXNG/Brave/Tavily) in BOSS's
//!   registry, falling back to BOSS's model gateway search endpoint (`/v1/metatron/search`).
//!   Never uses an OpenAI-hosted search tool.
//! - `update_plan`: visible structured investigation checklist (`ReadLocal`, `parallel_ok = true`).

use std::fs;
use std::path::{Path, PathBuf};

use async_trait::async_trait;
use base64::{Engine as _, engine::general_purpose::STANDARD as B64};
use metatron_core::{Tool, ToolCtx, ToolOutcome};
use metatron_protocol::{
    ClientToolSpec, RiskClass, ToolExposure, ToolResultStatus, WebSearchRequest,
};
use metatron_secrets::redact_secrets;
use serde_json::{Value, json};

use crate::transfer::TransferTool;

pub const MAX_IMAGE_BYTES: usize = 4 * 1024 * 1024;

pub struct ViewImageTool;

impl ViewImageTool {
    fn detect_media_type(bytes: &[u8]) -> Option<&'static str> {
        if bytes.starts_with(&[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]) {
            Some("image/png")
        } else if bytes.starts_with(&[0xff, 0xd8, 0xff]) {
            Some("image/jpeg")
        } else if bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a") {
            Some("image/gif")
        } else if bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WEBP" {
            Some("image/webp")
        } else {
            None
        }
    }

    fn resolve_path(ctx: &ToolCtx, raw: &str) -> Result<(PathBuf, bool), String> {
        let p = Path::new(raw.trim());
        if !p.is_absolute() {
            if let Ok(c) = TransferTool::safe_child_path(&ctx.scratch_dir, raw)
                && c.is_file()
            {
                return Ok((c, true));
            }
            if let Ok(c) = TransferTool::safe_child_path(&ctx.evidence_dir, raw)
                && c.is_file()
            {
                return Ok((c, true));
            }
            let rel_cwd = ctx.cwd.join(p);
            if rel_cwd.is_file() {
                let canon = fs::canonicalize(&rel_cwd).map_err(|e| e.to_string())?;
                let in_safe =
                    is_under(&canon, &ctx.scratch_dir) || is_under(&canon, &ctx.evidence_dir);
                return Ok((canon, in_safe));
            }
            return Err(format!("image '{raw}' not found"));
        }
        let canon = fs::canonicalize(p).map_err(|e| format!("cannot read image '{raw}': {e}"))?;
        let in_safe = is_under(&canon, &ctx.scratch_dir) || is_under(&canon, &ctx.evidence_dir);
        Ok((canon, in_safe))
    }
}

fn is_under(path: &Path, dir: &Path) -> bool {
    let base = fs::canonicalize(dir).unwrap_or_else(|_| dir.to_path_buf());
    path.starts_with(&base)
}

#[async_trait]
impl Tool for ViewImageTool {
    fn name(&self) -> &str {
        "view_image"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "view_image".to_string(),
            description: "Inspect a local PNG/JPEG/GIF/WebP image from the scratch or evidence directory and attach it to the turn.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": { "type": "string" }
                }
            }),
            risk_class: "read-local".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, args: &Value) -> RiskClass {
        let p = args.get("path").and_then(Value::as_str).unwrap_or("");
        if p.contains("..") || p.starts_with('/') {
            RiskClass::WriteLocal
        } else {
            RiskClass::ReadLocal
        }
    }

    fn parallel_ok(&self) -> bool {
        true
    }

    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let Some(raw_path) = args.get("path").and_then(Value::as_str) else {
            return err_outcome("view_image requires 'path'");
        };
        let (path, _in_safe) = match Self::resolve_path(ctx, raw_path) {
            Ok(v) => v,
            Err(e) => return err_outcome(&e),
        };
        let bytes = match fs::read(&path) {
            Ok(b) => b,
            Err(e) => return err_outcome(&format!("failed to read image: {e}")),
        };
        if bytes.len() > MAX_IMAGE_BYTES {
            return err_outcome(&format!(
                "image exceeds maximum size ({} > {} bytes)",
                bytes.len(),
                MAX_IMAGE_BYTES
            ));
        }
        let Some(media_type) = Self::detect_media_type(&bytes) else {
            return err_outcome("unsupported image format (expected PNG, JPEG, GIF, or WebP)");
        };
        let b64 = B64.encode(&bytes);
        let summary = format!(
            "Attached image {} ({media_type}, {} bytes)",
            path.display(),
            bytes.len()
        );
        ToolOutcome {
            status: ToolResultStatus::Ok,
            inline_text: summary.clone(),
            full_text: summary,
            truncated: false,
            exit_code: Some(0),
            bytes: bytes.len(),
            duration_ms: 0,
            host: None,
            host_origin: None,
            sandbox_denied: false,
            images: vec![(media_type.to_string(), b64)],
            http_audit: None,
        }
    }
}

/// Web search tool routed through an approved BOSS search MCP server or BOSS model gateway (`MT-9`).
pub struct WebSearchTool;

#[async_trait]
impl Tool for WebSearchTool {
    fn name(&self) -> &str {
        "web_search"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "web_search".to_string(),
            description: "Search the web via a BOSS admin-approved search MCP server (SearXNG/Brave/Tavily) or BOSS's model gateway search endpoint.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": { "type": "string" },
                    "limit": { "type": "integer", "minimum": 1, "maximum": 20, "default": 5 },
                    "allowed_domains": { "type": "array", "items": { "type": "string" } }
                }
            }),
            risk_class: "network".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, _args: &Value) -> RiskClass {
        RiskClass::Network
    }

    fn parallel_ok(&self) -> bool {
        true
    }

    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let Some(query) = args
            .get("query")
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty())
        else {
            return err_outcome("web_search requires 'query'");
        };
        let limit = args
            .get("limit")
            .and_then(Value::as_u64)
            .unwrap_or(5)
            .clamp(1, 20) as usize;
        let allowed_domains: Vec<String> = args
            .get("allowed_domains")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(ToString::to_string)
                    .collect()
            })
            .unwrap_or_default();

        // 1. Prefer an admin-approved search MCP server in BOSS's registry.
        if let Some(ref mcp) = ctx.mcp_client {
            for candidate in ["searxng", "brave", "tavily", "search"] {
                if mcp.require_approved(candidate).is_ok() {
                    match mcp
                        .call_tool(
                            candidate,
                            "search",
                            json!({"query": query, "limit": limit, "allowed_domains": allowed_domains}),
                        )
                        .await
                    {
                        Ok(out) if !out.is_error => {
                            let text = format!("[via MCP {candidate}]\n{}", redact_secrets(&out.text));
                            let bytes = text.len();
                            return ToolOutcome {
                                status: ToolResultStatus::Ok,
                                inline_text: text.clone(),
                                full_text: text,
                                truncated: false,
                                exit_code: Some(0),
                                bytes,
                                duration_ms: 0,
                                host: None,
                                host_origin: Some(format!("mcp:{candidate}")),
                                sandbox_denied: false,
                                images: Vec::new(),
                                http_audit: None,
                            };
                        }
                        _ => {}
                    }
                }
            }
        }

        // 2. Fallback: BOSS model gateway search endpoint (`/v1/metatron/search`).
        let Some(ref boss) = ctx.boss_client else {
            return err_outcome(
                "no approved search MCP server or BOSS gateway client available for web_search",
            );
        };
        let req = WebSearchRequest {
            project_id: ctx.project_id.clone(),
            query: query.to_string(),
            limit,
            allowed_domains,
        };
        match boss.web_search(&req).await {
            Ok(resp) => {
                let text = redact_secrets(
                    &serde_json::to_string_pretty(&resp).unwrap_or_else(|_| "{}".to_string()),
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
                    host_origin: Some(format!("boss_gateway:{}", resp.provider)),
                    sandbox_denied: false,
                    images: Vec::new(),
                    http_audit: None,
                }
            }
            Err(e) => err_outcome(&format!("BOSS gateway web_search failed: {e}")),
        }
    }
}

/// Visible investigation checklist tool (`update_plan`, `MT-9`).
pub struct UpdatePlanTool;

#[async_trait]
impl Tool for UpdatePlanTool {
    fn name(&self) -> &str {
        "update_plan"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "update_plan".to_string(),
            description: "Update the visible investigation checklist.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["steps"],
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["title", "status"],
                            "properties": {
                                "title": { "type": "string" },
                                "status": { "type": "string", "enum": ["pending", "in_progress", "done"] }
                            }
                        }
                    }
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

    async fn run(&self, _ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let Some(steps) = args.get("steps").and_then(Value::as_array) else {
            return err_outcome("update_plan requires 'steps' array");
        };
        let mut lines = Vec::with_capacity(steps.len());
        for s in steps {
            let title = s.get("title").and_then(Value::as_str).unwrap_or("");
            let status = s.get("status").and_then(Value::as_str).unwrap_or("pending");
            let mark = match status {
                "done" => "[x]",
                "in_progress" => "[-]",
                _ => "[ ]",
            };
            lines.push(format!("{mark} {title}"));
        }
        let text = lines.join("\n");
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
