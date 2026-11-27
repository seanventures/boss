//! Quick local dashboards (`dashboard`, `MT-4`):
//! - Reads CSV, JSON, or JSONL data strictly inside `ctx.evidence_dir` or `ctx.scratch_dir`.
//! - Generates:
//!   1. A **100% self-contained local HTML file** in `ctx.scratch_dir` with inline SVG charts
//!      and ZERO remote scripts / external CDNs (verified by test!).
//!   2. **TUI Unicode sparklines** (`▁▂▃▄▅▆▇█`) + summary stats returned inline.

use std::fs;
use std::path::{Path, PathBuf};

use async_trait::async_trait;
use metatron_core::{Tool, ToolCtx, ToolOutcome};
use metatron_protocol::{ClientToolSpec, RiskClass, ToolExposure, ToolResultStatus};
use metatron_secrets::redact_secrets;
use serde_json::{Value, json};
use uuid::Uuid;

use crate::transfer::TransferTool;

const SPARK_CHARS: &[char] = &['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];

#[derive(Debug, Clone)]
pub struct DataSeries {
    pub name: String,
    pub labels: Vec<String>,
    pub values: Vec<f64>,
}

#[derive(Debug, Clone)]
pub struct SeriesStats {
    pub count: usize,
    pub min: f64,
    pub max: f64,
    pub mean: f64,
    pub p95: f64,
    pub peak_label: String,
}

impl DataSeries {
    pub fn stats(&self) -> SeriesStats {
        if self.values.is_empty() {
            return SeriesStats {
                count: 0,
                min: 0.0,
                max: 0.0,
                mean: 0.0,
                p95: 0.0,
                peak_label: "-".to_string(),
            };
        }
        let mut sorted = self.values.clone();
        sorted.sort_by(|a, b| a.total_cmp(b));
        let count = sorted.len();
        let min = sorted[0];
        let max = sorted[count - 1];
        let sum: f64 = sorted.iter().sum();
        let mean = sum / (count as f64);
        let p95_idx = (((count as f64) * 0.95).ceil() as usize)
            .saturating_sub(1)
            .min(count - 1);
        let p95 = sorted[p95_idx];
        let peak_idx = self
            .values
            .iter()
            .enumerate()
            .max_by(|(_, a), (_, b)| a.total_cmp(b))
            .map(|(i, _)| i)
            .unwrap_or(0);
        let peak_label = self
            .labels
            .get(peak_idx)
            .cloned()
            .unwrap_or_else(|| format!("#{peak_idx}"));
        SeriesStats {
            count,
            min,
            max,
            mean,
            p95,
            peak_label,
        }
    }
}

/// Render a Unicode TUI sparkline (`▁▂▃▄▅▆▇█`) from numeric values.
pub fn render_sparkline(values: &[f64]) -> String {
    if values.is_empty() {
        return String::new();
    }
    let min = values.iter().copied().fold(f64::INFINITY, f64::min);
    let max = values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let span = max - min;
    values
        .iter()
        .map(|&v| {
            let idx = if span.abs() < f64::EPSILON {
                3
            } else {
                let norm = ((v - min) / span).clamp(0.0, 1.0);
                ((norm * ((SPARK_CHARS.len() - 1) as f64)).round() as usize)
                    .min(SPARK_CHARS.len() - 1)
            };
            SPARK_CHARS[idx]
        })
        .collect()
}

/// Parse CSV or JSON/JSONL tabular data into one or more named numeric series.
pub fn load_series_from_text(
    raw: &str,
    label_col: Option<&str>,
    value_cols: &[String],
) -> Result<Vec<DataSeries>, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err("data file is empty".to_string());
    }
    if trimmed.starts_with('[') || trimmed.starts_with('{') {
        let rows: Vec<serde_json::Map<String, Value>> = if trimmed.starts_with('[') {
            let v: Value =
                serde_json::from_str(trimmed).map_err(|e| format!("invalid JSON array: {e}"))?;
            v.as_array()
                .ok_or_else(|| "expected JSON array".to_string())?
                .iter()
                .filter_map(|item| item.as_object().cloned())
                .collect()
        } else {
            // Try JSON object with `series`/`rows`, else JSONL.
            if let Ok(obj) = serde_json::from_str::<Value>(trimmed)
                && let Some(arr) = obj
                    .get("rows")
                    .or_else(|| obj.get("data"))
                    .and_then(Value::as_array)
            {
                arr.iter()
                    .filter_map(|item| item.as_object().cloned())
                    .collect()
            } else {
                trimmed
                    .lines()
                    .filter(|l| !l.trim().is_empty())
                    .filter_map(|l| serde_json::from_str::<Value>(l).ok())
                    .filter_map(|v| v.as_object().cloned())
                    .collect()
            }
        };
        return extract_series_from_maps(&rows, label_col, value_cols);
    }

    // Simple CSV parser (header line + comma-separated rows).
    let mut lines = trimmed.lines().filter(|l| !l.trim().is_empty());
    let header_line = lines
        .next()
        .ok_or_else(|| "missing CSV header".to_string())?;
    let headers: Vec<String> = header_line
        .split(',')
        .map(|h| h.trim().trim_matches('"').to_string())
        .collect();
    if headers.len() < 2 {
        return Err("CSV must have at least 2 columns (label + numeric value)".to_string());
    }
    let mut maps = Vec::new();
    for line in lines {
        let cols: Vec<&str> = line
            .split(',')
            .map(|c| c.trim().trim_matches('"'))
            .collect();
        let mut map = serde_json::Map::new();
        for (idx, h) in headers.iter().enumerate() {
            if let Some(cell) = cols.get(idx) {
                if let Ok(num) = cell.parse::<f64>() {
                    map.insert(h.clone(), json!(num));
                } else {
                    map.insert(h.clone(), json!(cell));
                }
            }
        }
        maps.push(map);
    }
    extract_series_from_maps(&maps, label_col, value_cols)
}

fn extract_series_from_maps(
    rows: &[serde_json::Map<String, Value>],
    label_col: Option<&str>,
    value_cols: &[String],
) -> Result<Vec<DataSeries>, String> {
    let Some(first) = rows.first() else {
        return Err("no data rows found".to_string());
    };
    let label_key = label_col
        .map(ToString::to_string)
        .or_else(|| {
            ["ts", "timestamp", "time", "label", "x"]
                .iter()
                .find(|k| first.contains_key(**k))
                .map(|s| (*s).to_string())
        })
        .unwrap_or_else(|| first.keys().next().cloned().unwrap_or_default());

    let numeric_keys: Vec<String> = if !value_cols.is_empty() {
        value_cols.to_vec()
    } else {
        first
            .iter()
            .filter(|(k, v)| k.as_str() != label_key.as_str() && v.is_number())
            .map(|(k, _)| k.clone())
            .collect()
    };
    if numeric_keys.is_empty() {
        return Err("no numeric value columns found for charting".to_string());
    }

    let mut out = Vec::new();
    for col in numeric_keys {
        let mut labels = Vec::new();
        let mut values = Vec::new();
        for (idx, row) in rows.iter().enumerate() {
            let Some(val) = row.get(&col).and_then(Value::as_f64) else {
                continue;
            };
            let lbl = row
                .get(&label_key)
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .unwrap_or_else(|| idx.to_string());
            labels.push(lbl);
            values.push(val);
        }
        if !values.is_empty() {
            out.push(DataSeries {
                name: col,
                labels,
                values,
            });
        }
    }
    if out.is_empty() {
        return Err("no numeric data points extracted".to_string());
    }
    Ok(out)
}

/// Render a **100% self-contained** HTML dashboard with inline SVG line charts, summary cards,
/// and a data table. Contains ZERO remote scripts, stylesheets, images, or CDN references.
pub fn render_self_contained_html(title: &str, series_list: &[DataSeries]) -> String {
    let safe_title = html_escape(&redact_secrets(title));
    let mut cards_html = String::new();
    let mut charts_html = String::new();

    let palette = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed"];
    for (idx, series) in series_list.iter().enumerate() {
        let stats = series.stats();
        let color = palette[idx % palette.len()];
        let safe_name = html_escape(&redact_secrets(&series.name));
        let safe_peak = html_escape(&redact_secrets(&stats.peak_label));
        cards_html.push_str(&format!(
            "<div class=\"card\"><div class=\"card-title\">{safe_name}</div>\
             <div class=\"card-val\">peak {max:.2}</div>\
             <div class=\"card-sub\">min {min:.2} · mean {mean:.2} · p95 {p95:.2} · at {safe_peak} (n={count})</div></div>\n",
            max = stats.max,
            min = stats.min,
            mean = stats.mean,
            p95 = stats.p95,
            count = stats.count,
        ));
        charts_html.push_str(&render_inline_svg_chart(series, color));
    }

    format!(
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n\
         <title>{safe_title}</title>\n\
         <style>\n\
         body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; background: #0f172a; color: #e2e8f0; }}\n\
         h1 {{ font-size: 20px; margin-bottom: 16px; }}\n\
         .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }}\n\
         .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 12px 16px; min-width: 220px; }}\n\
         .card-title {{ font-size: 12px; text-transform: uppercase; color: #94a3b8; }}\n\
         .card-val {{ font-size: 22px; font-weight: 700; margin: 4px 0; }}\n\
         .card-sub {{ font-size: 12px; color: #cbd5e1; }}\n\
         .chart-box {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}\n\
         .chart-title {{ font-size: 14px; font-weight: 600; margin-bottom: 8px; }}\n\
         svg {{ width: 100%; max-width: 820px; height: auto; display: block; }}\n\
         </style>\n</head>\n<body>\n\
         <h1>{safe_title}</h1>\n\
         <div class=\"cards\">\n{cards_html}</div>\n\
         {charts_html}\n\
         </body>\n</html>\n"
    )
}

fn render_inline_svg_chart(series: &DataSeries, color: &str) -> String {
    let w = 800.0f64;
    let h = 220.0f64;
    let pad = 36.0f64;
    let plot_w = w - 2.0 * pad;
    let plot_h = h - 2.0 * pad;
    let stats = series.stats();
    let span = (stats.max - stats.min).max(1e-9);
    let n = series.values.len().max(1);

    let mut points = String::new();
    for (i, &val) in series.values.iter().enumerate() {
        let x = if n == 1 {
            pad + plot_w / 2.0
        } else {
            pad + (i as f64 / (n - 1) as f64) * plot_w
        };
        let y = pad + plot_h - ((val - stats.min) / span) * plot_h;
        if !points.is_empty() {
            points.push(' ');
        }
        points.push_str(&format!("{x:.1},{y:.1}"));
    }

    let first_lbl = series
        .labels
        .first()
        .map(|s| html_escape(&redact_secrets(s)))
        .unwrap_or_default();
    let last_lbl = series
        .labels
        .last()
        .map(|s| html_escape(&redact_secrets(s)))
        .unwrap_or_default();
    let safe_name = html_escape(&redact_secrets(&series.name));

    format!(
        "<div class=\"chart-box\"><div class=\"chart-title\">{safe_name}</div>\
         <svg viewBox=\"0 0 800 220\" xmlns=\"http://www.w3.org/2000/svg\">\
         <rect x=\"0\" y=\"0\" width=\"800\" height=\"220\" fill=\"#0f172a\" rx=\"6\"/>\
         <line x1=\"{pad}\" y1=\"{y_bot}\" x2=\"{x_end}\" y2=\"{y_bot}\" stroke=\"#475569\" stroke-width=\"1\"/>\
         <polyline fill=\"none\" stroke=\"{color}\" stroke-width=\"2.5\" points=\"{points}\"/>\
         <text x=\"{pad}\" y=\"20\" fill=\"#94a3b8\" font-size=\"11\">max {max:.2}</text>\
         <text x=\"{pad}\" y=\"212\" fill=\"#94a3b8\" font-size=\"11\">{first_lbl}</text>\
         <text x=\"680\" y=\"212\" fill=\"#94a3b8\" font-size=\"11\">{last_lbl}</text>\
         </svg></div>\n",
        y_bot = pad + plot_h,
        x_end = pad + plot_w,
        max = stats.max,
    )
}

fn html_escape(input: &str) -> String {
    input
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

pub struct DashboardTool;

impl DashboardTool {
    fn resolve_data_path(ctx: &ToolCtx, raw_path: &str) -> Result<PathBuf, String> {
        let p = Path::new(raw_path.trim());
        if p.is_absolute() {
            let canon = fs::canonicalize(p).map_err(|e| format!("cannot read data file: {e}"))?;
            let ev_canon =
                fs::canonicalize(&ctx.evidence_dir).unwrap_or_else(|_| ctx.evidence_dir.clone());
            let sc_canon =
                fs::canonicalize(&ctx.scratch_dir).unwrap_or_else(|_| ctx.scratch_dir.clone());
            if canon.starts_with(&ev_canon) || canon.starts_with(&sc_canon) {
                return Ok(canon);
            }
            return Err(
                "dashboard data_file must live inside evidence_dir or scratch_dir".to_string(),
            );
        }
        if let Ok(candidate) = TransferTool::safe_child_path(&ctx.evidence_dir, raw_path)
            && candidate.is_file()
        {
            return Ok(candidate);
        }
        if let Ok(candidate) = TransferTool::safe_child_path(&ctx.scratch_dir, raw_path)
            && candidate.is_file()
        {
            return Ok(candidate);
        }
        Err(format!(
            "data_file '{raw_path}' not found in evidence_dir or scratch_dir"
        ))
    }
}

#[async_trait]
impl Tool for DashboardTool {
    fn name(&self) -> &str {
        "dashboard"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "dashboard".to_string(),
            description: "Build a self-contained local HTML chart dashboard (zero remote scripts) plus TUI sparklines from a CSV/JSON/JSONL file in the evidence or scratch directory.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["data_file"],
                "properties": {
                    "data_file": { "type": "string" },
                    "title": { "type": "string", "default": "Metatron Investigation Dashboard" },
                    "label_col": { "type": "string" },
                    "value_cols": { "type": "array", "items": { "type": "string" } },
                    "output_name": { "type": "string" }
                }
            }),
            risk_class: "write-scratch".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, _args: &Value) -> RiskClass {
        RiskClass::WriteScratch
    }

    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let Some(data_file) = args.get("data_file").and_then(Value::as_str) else {
            return err_outcome("dashboard requires 'data_file'");
        };
        let title = args
            .get("title")
            .and_then(Value::as_str)
            .unwrap_or("Metatron Investigation Dashboard");
        let label_col = args.get("label_col").and_then(Value::as_str);
        let value_cols: Vec<String> = args
            .get("value_cols")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(ToString::to_string)
                    .collect()
            })
            .unwrap_or_default();

        let data_path = match Self::resolve_data_path(ctx, data_file) {
            Ok(p) => p,
            Err(e) => return err_outcome(&e),
        };
        let raw = match fs::read_to_string(&data_path) {
            Ok(s) => s,
            Err(e) => return err_outcome(&format!("failed to read data file: {e}")),
        };
        let series_list = match load_series_from_text(&raw, label_col, &value_cols) {
            Ok(s) => s,
            Err(e) => return err_outcome(&e),
        };

        if let Err(e) = fs::create_dir_all(&ctx.scratch_dir) {
            return err_outcome(&format!("failed to create scratch dir: {e}"));
        }
        let default_name = format!(
            "dashboard_{}.html",
            &Uuid::new_v4().simple().to_string()[..10]
        );
        let output_name = args
            .get("output_name")
            .and_then(Value::as_str)
            .unwrap_or(&default_name);
        let out_path = match TransferTool::safe_child_path(&ctx.scratch_dir, output_name) {
            Ok(p) => p,
            Err(e) => return err_outcome(&e),
        };

        let html = render_self_contained_html(title, &series_list);
        if let Err(e) = fs::write(&out_path, &html) {
            return err_outcome(&format!("failed to write dashboard HTML: {e}"));
        }

        let mut tui_lines = vec![format!("Dashboard: {title} ({})", out_path.display())];
        for s in &series_list {
            let st = s.stats();
            tui_lines.push(format!(
                "  {:<18} {}  min={:.2} mean={:.2} p95={:.2} max={:.2} (peak@{})",
                s.name,
                render_sparkline(&s.values),
                st.min,
                st.mean,
                st.p95,
                st.max,
                st.peak_label
            ));
        }
        let text = redact_secrets(&tui_lines.join("\n"));
        let bytes = html.len();

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

#[cfg(test)]
#[allow(clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::*;
    use metatron_remote::RemoteRunner;
    use tempfile::TempDir;

    #[test]
    fn sparkline_and_self_contained_html_contain_zero_remote_scripts() {
        let csv =
            "ts,cpu_pct,latency_ms\n10:00,12.0,22\n10:01,45.0,80\n10:02,94.5,410\n10:03,18.0,28\n";
        let series = load_series_from_text(csv, None, &[]).expect("parse csv");
        assert_eq!(series.len(), 2);

        let spark = render_sparkline(&series[0].values);
        assert_eq!(spark.chars().count(), 4);
        assert!(spark.contains('█'));

        let html = render_self_contained_html("CPU & Latency Spike", &series);
        assert!(html.contains("<!DOCTYPE html>"));
        assert!(html.contains("<svg "));
        assert!(html.contains("<polyline "));
        // Hard MT-4 acceptance criterion: ZERO remote scripts, external stylesheets, or CDNs!
        assert!(!html.contains("<script"));
        assert!(!html.contains("src=\"http"));
        assert!(!html.contains("href=\"http"));
        assert!(!html.contains("https://"));
        assert!(!html.contains("cdn."));
    }

    #[tokio::test]
    async fn dashboard_tool_writes_html_in_scratch_and_refuses_outside_files() {
        let tmp = TempDir::new().expect("tmp");
        let evidence = tmp.path().join("evidence");
        let scratch = tmp.path().join("scratch");
        fs::create_dir_all(&evidence).expect("mkdir");
        fs::create_dir_all(&scratch).expect("mkdir");
        fs::write(
            evidence.join("sar.csv"),
            "ts,iowait\n12:00,1.2\n12:01,44.8\n12:02,2.1\n",
        )
        .expect("write");

        let ctx = ToolCtx {
            project_id: "boss-dogfood".to_string(),
            session_id: "msess_dash".to_string(),
            cwd: tmp.path().to_path_buf(),
            scratch_dir: scratch.clone(),
            evidence_dir: evidence.clone(),
            allowed_read_roots: vec![evidence],
            allow_unsandboxed_retry: false,
            boss_client: None,
            remote_runner: RemoteRunner::default(),
            ticket_id: None,
            mcp_client: None,
            hooks: None,
            cancel_rx: None,
        };
        let tool = DashboardTool;
        let out = tool
            .run(
                &ctx,
                json!({
                    "data_file": "sar.csv",
                    "title": "SAR I/O Wait",
                    "output_name": "sar_dash.html"
                }),
            )
            .await;
        assert_eq!(out.status, ToolResultStatus::Ok);
        assert!(scratch.join("sar_dash.html").is_file());
        assert!(out.inline_text.contains("iowait"));

        // Traversal outside evidence/scratch refused
        let bad = tool.run(&ctx, json!({"data_file": "/etc/hosts"})).await;
        assert_eq!(bad.status, ToolResultStatus::Error);
    }
}
