//! Grafana "what happened between T1 and T2" recipe (`MT-6`, `ARCHITECTURE.md` §7.3).
//!
//! Credential path preference order (enforced in code):
//! 1. Approved Grafana MCP server in BOSS's registry (`mcp__grafana__*`)
//! 2. Narrow API token from BOSS vault (`bearer_token_env`)
//! 3. User-supplied session cookie via `HttpSessionTool` (explicitly labeled "last resort")
//!
//! Fetches dashboard JSON (`/api/dashboards/uid/{uid}`) and `/api/ds/query` time-series
//! for `[from_ms, to_ms]`, writes normalized CSV into `ctx.evidence_dir`, runs sandboxed `pyrun`
//! analysis citing panels and RFC3339 timestamps, generates a self-contained HTML + sparkline
//! dashboard, and optionally posts findings to a BOSS ticket.

use std::fs;
use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use metatron_core::{Tool, ToolCtx, ToolOutcome};
use metatron_protocol::{ClientToolSpec, RiskClass, ToolExposure, ToolResultStatus};
use metatron_secrets::redact_secrets;
use serde_json::{Value, json};

use crate::boss_ticket::BossPostFindingTool;
use crate::dashboard::DashboardTool;
use crate::http_session::HttpSessionTool;
use crate::pyrun::PyrunTool;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GrafanaAccessPath {
    McpServer,
    VaultApiToken,
    SessionCookieLastResort,
}

impl GrafanaAccessPath {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::McpServer => "mcp_server",
            Self::VaultApiToken => "vault_api_token",
            Self::SessionCookieLastResort => "session_cookie_last_resort",
        }
    }
}

pub struct GrafanaRecipeTool {
    http_session: Arc<HttpSessionTool>,
}

impl GrafanaRecipeTool {
    pub fn new(http_session: Arc<HttpSessionTool>) -> Self {
        Self { http_session }
    }

    /// Resolve preferred Grafana access path: MCP -> narrow vault token -> cookie (last resort).
    pub fn select_access_path(ctx: &ToolCtx, args: &Value) -> GrafanaAccessPath {
        if let Some(ref mcp) = ctx.mcp_client
            && mcp.require_approved("grafana").is_ok()
            && args
                .get("prefer_mcp")
                .and_then(Value::as_bool)
                .unwrap_or(true)
        {
            return GrafanaAccessPath::McpServer;
        }
        if args
            .get("bearer_token_env")
            .and_then(Value::as_str)
            .is_some_and(|v| !v.trim().is_empty())
        {
            return GrafanaAccessPath::VaultApiToken;
        }
        GrafanaAccessPath::SessionCookieLastResort
    }
}

#[derive(Debug, Clone)]
pub struct GrafanaPoint {
    pub panel: String,
    pub series: String,
    pub ts_rfc3339: String,
    pub value: f64,
}

/// Extract normalized `(panel, series, timestamp, value)` points from a Grafana `/api/ds/query`
/// JSON response (`results.<refId>.frames[].data.values = [[timestamps_ms], [values]]`).
pub fn extract_grafana_points(
    ds_query_json: &Value,
    panel_titles: &std::collections::HashMap<String, String>,
) -> Vec<GrafanaPoint> {
    let mut points = Vec::new();
    let Some(results) = ds_query_json.get("results").and_then(Value::as_object) else {
        return points;
    };
    for (ref_id, entry) in results {
        let panel_name = panel_titles
            .get(ref_id)
            .cloned()
            .unwrap_or_else(|| format!("Panel {ref_id}"));
        let Some(frames) = entry.get("frames").and_then(Value::as_array) else {
            continue;
        };
        for frame in frames {
            let series_name = frame
                .get("schema")
                .and_then(|s| s.get("name"))
                .and_then(Value::as_str)
                .unwrap_or(ref_id)
                .to_string();
            let Some(values) = frame
                .get("data")
                .and_then(|d| d.get("values"))
                .and_then(Value::as_array)
            else {
                continue;
            };
            if values.len() < 2 {
                continue;
            }
            let Some(ts_arr) = values[0].as_array() else {
                continue;
            };
            let Some(val_arr) = values[1].as_array() else {
                continue;
            };
            for (t_val, v_val) in ts_arr.iter().zip(val_arr.iter()) {
                let Some(v) = v_val.as_f64() else {
                    continue;
                };
                let ts_str = if let Some(ms) = t_val.as_i64() {
                    DateTime::<Utc>::from_timestamp_millis(ms)
                        .map(|dt| dt.to_rfc3339())
                        .unwrap_or_else(|| ms.to_string())
                } else if let Some(s) = t_val.as_str() {
                    s.to_string()
                } else {
                    continue;
                };
                points.push(GrafanaPoint {
                    panel: panel_name.clone(),
                    series: series_name.clone(),
                    ts_rfc3339: ts_str,
                    value: v,
                });
            }
        }
    }
    points
}

#[async_trait]
impl Tool for GrafanaRecipeTool {
    fn name(&self) -> &str {
        "grafana_window_analysis"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "grafana_window_analysis".to_string(),
            description: "Investigate 'what happened between T1 and T2' on a Grafana dashboard: prefers Grafana MCP server, then narrow vault API token, then session cookie (last resort); queries /api/ds/query, runs sandboxed pyrun analysis citing panels & timestamps, builds a local dashboard, and optionally posts findings to a BOSS ticket.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["base_url", "dashboard_uid", "from_ms", "to_ms"],
                "properties": {
                    "base_url": { "type": "string" },
                    "dashboard_uid": { "type": "string" },
                    "from_ms": { "type": "integer" },
                    "to_ms": { "type": "integer" },
                    "queries": { "type": "array" },
                    "auth_mode": { "type": "string", "enum": ["bearer_token", "cookie_file", "chrome_cookie"] },
                    "bearer_token_env": { "type": "string" },
                    "cookie_file": { "type": "string" },
                    "chrome_db_path": { "type": "string" },
                    "ticket_id": { "type": "string" },
                    "prefer_mcp": { "type": "boolean", "default": true }
                }
            }),
            risk_class: "secret-use".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, _args: &Value) -> RiskClass {
        RiskClass::SecretUse
    }

    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let Some(base_url) = args.get("base_url").and_then(Value::as_str) else {
            return err_outcome("grafana_window_analysis requires 'base_url'");
        };
        let Some(uid) = args.get("dashboard_uid").and_then(Value::as_str) else {
            return err_outcome("grafana_window_analysis requires 'dashboard_uid'");
        };
        let Some(from_ms) = args.get("from_ms").and_then(Value::as_i64) else {
            return err_outcome("grafana_window_analysis requires integer 'from_ms'");
        };
        let Some(to_ms) = args.get("to_ms").and_then(Value::as_i64) else {
            return err_outcome("grafana_window_analysis requires integer 'to_ms'");
        };
        let base = base_url.trim_end_matches('/');
        let path_choice = Self::select_access_path(ctx, &args);

        let mut panel_titles = std::collections::HashMap::new();
        let ds_json: Value = match path_choice {
            GrafanaAccessPath::McpServer => {
                let Some(ref mcp) = ctx.mcp_client else {
                    return err_outcome("Grafana MCP selected but unavailable");
                };
                let res = match mcp
                    .call_tool(
                        "grafana",
                        "ds_query",
                        json!({
                            "dashboard_uid": uid,
                            "from_ms": from_ms,
                            "to_ms": to_ms,
                            "queries": args.get("queries").cloned().unwrap_or_else(|| json!([]))
                        }),
                    )
                    .await
                {
                    Ok(r) => r,
                    Err(e) => return err_outcome(&format!("Grafana MCP query failed: {e}")),
                };
                serde_json::from_str(&res.text).unwrap_or_else(|_| json!({}))
            }
            GrafanaAccessPath::VaultApiToken | GrafanaAccessPath::SessionCookieLastResort => {
                let auth_mode = if path_choice == GrafanaAccessPath::VaultApiToken {
                    "bearer_token"
                } else {
                    args.get("auth_mode")
                        .and_then(Value::as_str)
                        .unwrap_or("cookie_file")
                };

                // 1. Fetch dashboard metadata (`GET /api/dashboards/uid/{uid}`).
                let mut dash_req = json!({
                    "url": format!("{base}/api/dashboards/uid/{uid}"),
                    "method": "GET",
                    "auth_mode": auth_mode,
                });
                copy_auth_fields(&args, &mut dash_req);
                let dash_out = self.http_session.run(ctx, dash_req).await;
                if dash_out.status == ToolResultStatus::Ok
                    && let Ok(env) = serde_json::from_str::<Value>(&dash_out.inline_text)
                    && let Some(body_str) = env.get("body").and_then(Value::as_str)
                    && let Ok(dash_doc) = serde_json::from_str::<Value>(body_str)
                    && let Some(panels) = dash_doc
                        .get("dashboard")
                        .and_then(|d| d.get("panels"))
                        .and_then(Value::as_array)
                {
                    for (idx, p) in panels.iter().enumerate() {
                        let ref_id = p
                            .get("targets")
                            .and_then(Value::as_array)
                            .and_then(|t| t.first())
                            .and_then(|t| t.get("refId"))
                            .and_then(Value::as_str)
                            .map(ToString::to_string)
                            .unwrap_or_else(|| ((b'A' + (idx as u8)) as char).to_string());
                        if let Some(title) = p.get("title").and_then(Value::as_str) {
                            panel_titles.insert(ref_id, title.to_string());
                        }
                    }
                }

                // 2. Query metrics (`POST /api/ds/query` — asks human separately via HttpSessionTool!).
                let queries = args
                    .get("queries")
                    .cloned()
                    .unwrap_or_else(|| json!([{"refId": "A", "expr": "up"}]));
                let mut query_req = json!({
                    "url": format!("{base}/api/ds/query"),
                    "method": "POST",
                    "auth_mode": auth_mode,
                    "body": {
                        "from": from_ms.to_string(),
                        "to": to_ms.to_string(),
                        "queries": queries,
                    }
                });
                copy_auth_fields(&args, &mut query_req);
                let q_out = self.http_session.run(ctx, query_req).await;
                if q_out.status != ToolResultStatus::Ok {
                    return q_out;
                }
                let env: Value =
                    serde_json::from_str(&q_out.inline_text).unwrap_or_else(|_| json!({}));
                let body_str = env.get("body").and_then(Value::as_str).unwrap_or("{}");
                serde_json::from_str(body_str).unwrap_or_else(|_| json!({}))
            }
        };

        let points = extract_grafana_points(&ds_json, &panel_titles);
        if points.is_empty() {
            return err_outcome("Grafana /api/ds/query returned zero time-series points");
        }

        // 3. Persist normalized CSV into `ctx.evidence_dir` (redacted!).
        if let Err(e) = fs::create_dir_all(&ctx.evidence_dir) {
            return err_outcome(&format!("failed to create evidence dir: {e}"));
        }
        let csv_name = format!("grafana_{uid}.csv");
        let csv_path = ctx.evidence_dir.join(&csv_name);
        let mut csv_buf = String::from("ts,panel,series,value\n");
        for pt in &points {
            csv_buf.push_str(&format!(
                "{},{},{},{}\n",
                pt.ts_rfc3339,
                pt.panel.replace(',', " "),
                pt.series.replace(',', " "),
                pt.value
            ));
        }
        if let Err(e) = fs::write(&csv_path, &csv_buf) {
            return err_outcome(&format!("failed to write grafana CSV: {e}"));
        }

        // 4. Run sandboxed `pyrun` (no network, evidence dir only) to explain spikes with panels & timestamps.
        let py_code = format!(
            r#"import csv, os, pathlib, statistics
ev = pathlib.Path(os.environ['METATRON_EVIDENCE_DIR'])
rows = list(csv.DictReader((ev / {csv_name:?}).open()))
by_panel = {{}}
for r in rows:
    key = (r['panel'], r['series'])
    by_panel.setdefault(key, []).append((r['ts'], float(r['value'])))
for (panel, series), pts in sorted(by_panel.items()):
    vals = [v for _, v in pts]
    peak_ts, peak_val = max(pts, key=lambda p: p[1])
    mean_val = statistics.mean(vals)
    print(f"PANEL={{panel}} SERIES={{series}} count={{len(vals)}} mean={{mean_val:.2f}} peak={{peak_val:.2f}} peak_ts={{peak_ts}}")
"#
        );
        let py_out = PyrunTool::default()
            .run(ctx, json!({"code": py_code, "timeout_s": 30}))
            .await;

        // 5. Render self-contained HTML + TUI sparkline dashboard.
        let dash_html_name = format!("grafana_{uid}_dashboard.html");
        let dash_out = DashboardTool
            .run(
                ctx,
                json!({
                    "data_file": csv_name,
                    "title": format!("Grafana Window {uid} ({from_ms}..{to_ms})"),
                    "label_col": "ts",
                    "value_cols": ["value"],
                    "output_name": dash_html_name
                }),
            )
            .await;

        let findings_text = format!(
            "Grafana analysis ({uid}, access_path={}):\n{}\n\n{}",
            path_choice.as_str(),
            py_out.inline_text.trim(),
            dash_out.inline_text.trim()
        );

        // 6. Optional ticket attachment when `ticket_id` + `boss_client` are present.
        let mut ticket_status = None;
        let ticket_id = args
            .get("ticket_id")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .or_else(|| ctx.ticket_id.clone());
        if let (Some(tid), Some(_)) = (ticket_id, ctx.boss_client.as_ref()) {
            let post_out = BossPostFindingTool
                .run(
                    ctx,
                    json!({
                        "ticket_id": tid,
                        "title": format!("Grafana window analysis ({uid})"),
                        "summary": findings_text,
                        "evidence_files": [csv_name, dash_html_name],
                        "dashboard_excerpt": dash_out.inline_text,
                    }),
                )
                .await;
            ticket_status = Some(post_out.inline_text);
        }

        let summary_json = serde_json::to_string_pretty(&json!({
            "dashboard_uid": uid,
            "access_path": path_choice.as_str(),
            "from_ms": from_ms,
            "to_ms": to_ms,
            "points": points.len(),
            "evidence_csv": csv_path.display().to_string(),
            "pyrun_analysis": py_out.inline_text,
            "dashboard": dash_out.inline_text,
            "ticket_posted": ticket_status,
        }))
        .unwrap_or(findings_text);
        let scrubbed = self
            .http_session
            .cookie_manager()
            .redact_text(&redact_secrets(&summary_json));
        let bytes = scrubbed.len();

        ToolOutcome {
            status: ToolResultStatus::Ok,
            inline_text: scrubbed.clone(),
            full_text: scrubbed,
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

fn copy_auth_fields(src: &Value, dst: &mut Value) {
    for key in ["bearer_token_env", "cookie_file", "chrome_db_path"] {
        if let Some(v) = src.get(key) {
            dst[key] = v.clone();
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
