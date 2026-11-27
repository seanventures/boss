//! Hard-prompted remote packet capture tool (`pcap`, `MT-4`):
//! - Wraps `tcpdump -w` on the remote host with a REQUIRED BPF filter, plus host-enforced
//!   duration (`timeout <duration_s>`), packet count (`-c`), and file size (`-C`) caps.
//! - Risk class is ALWAYS `RiskClass::Capture` (hard-prompt -> asks a person every time,
//!   never offers "always allow", even in `act-on-my-behalf` mode).
//! - Transfers the capture file back into `ctx.evidence_dir` for local analysis (`tshark`/`pyrun`).

use std::fs;

use async_trait::async_trait;
use metatron_core::{Tool, ToolCtx, ToolOutcome};
use metatron_protocol::{ClientToolSpec, RiskClass, ToolExposure, ToolResultStatus};
use metatron_secrets::redact_secrets;
use serde_json::{Value, json};
use uuid::Uuid;

use crate::transfer::{TransferTool, sha256_file};

pub const MAX_PCAP_DURATION_S: u64 = 300;
pub const MAX_PCAP_PACKETS: u64 = 50_000;
pub const MAX_PCAP_SIZE_MB: u64 = 50;
pub const MAX_PCAP_SNAPLEN: u64 = 65_535;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PcapCaptureSpec {
    pub host: String,
    pub interface: String,
    pub filter: String,
    pub duration_s: u64,
    pub max_packets: u64,
    pub max_size_mb: u64,
    pub snaplen: u64,
    pub remote_output_path: String,
    pub local_name: String,
}

/// Validate and build the host-enforced `timeout <duration> tcpdump -w ...` argv (`MT-4`).
/// Caps (`duration_s`, `max_packets`, `max_size_mb`) are enforced directly on the host command,
/// never only in the UI!
pub fn build_tcpdump_argv(spec: &PcapCaptureSpec) -> Result<Vec<String>, String> {
    if spec.filter.trim().is_empty() {
        return Err("pcap requires a non-empty BPF filter (e.g. 'tcp port 443')".to_string());
    }
    if spec.filter.chars().any(|c| {
        (c as u32) < 32 || matches!(c, ';' | '&' | '|' | '`' | '$' | '>' | '<' | '\n' | '\r')
    }) {
        return Err("pcap BPF filter contains forbidden shell metacharacters".to_string());
    }
    if spec.interface.trim().is_empty()
        || !spec
            .interface
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.' | b':'))
    {
        return Err(format!("invalid capture interface '{}'", spec.interface));
    }
    if spec.duration_s == 0 || spec.duration_s > MAX_PCAP_DURATION_S {
        return Err(format!(
            "duration_s must be between 1 and {MAX_PCAP_DURATION_S}"
        ));
    }
    if spec.max_packets == 0 || spec.max_packets > MAX_PCAP_PACKETS {
        return Err(format!(
            "max_packets must be between 1 and {MAX_PCAP_PACKETS}"
        ));
    }
    if spec.max_size_mb == 0 || spec.max_size_mb > MAX_PCAP_SIZE_MB {
        return Err(format!(
            "max_size_mb must be between 1 and {MAX_PCAP_SIZE_MB}"
        ));
    }
    if !spec.remote_output_path.starts_with("/tmp/metatron-pcap-")
        || spec.remote_output_path.contains("..")
    {
        return Err("remote_output_path must reside under /tmp/metatron-pcap-*".to_string());
    }

    let filter_tokens =
        shell_words::split(&spec.filter).map_err(|e| format!("invalid BPF filter quoting: {e}"))?;
    if filter_tokens.is_empty() {
        return Err("pcap requires a non-empty BPF filter".to_string());
    }

    let mut argv = vec![
        "timeout".to_string(),
        spec.duration_s.to_string(),
        "tcpdump".to_string(),
        "-nn".to_string(),
        "-i".to_string(),
        spec.interface.clone(),
        "-c".to_string(),
        spec.max_packets.to_string(),
        "-C".to_string(),
        spec.max_size_mb.to_string(),
        "-W".to_string(),
        "1".to_string(),
        "-s".to_string(),
        spec.snaplen.clamp(64, MAX_PCAP_SNAPLEN).to_string(),
        "-w".to_string(),
        spec.remote_output_path.clone(),
    ];
    argv.extend(filter_tokens);
    Ok(argv)
}

pub struct PcapTool;

impl PcapTool {
    pub fn parse_spec(args: &Value) -> Result<PcapCaptureSpec, String> {
        let host = args
            .get("host")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| "pcap requires 'host'".to_string())?
            .to_string();
        let filter = args
            .get("filter")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| "pcap requires a non-empty BPF 'filter'".to_string())?
            .to_string();
        let interface = args
            .get("interface")
            .and_then(Value::as_str)
            .unwrap_or("any")
            .trim()
            .to_string();
        let duration_s = args.get("duration_s").and_then(Value::as_u64).unwrap_or(30);
        let max_packets = args
            .get("max_packets")
            .and_then(Value::as_u64)
            .unwrap_or(1_000);
        let max_size_mb = args
            .get("max_size_mb")
            .and_then(Value::as_u64)
            .unwrap_or(10);
        let snaplen = args.get("snaplen").and_then(Value::as_u64).unwrap_or(256);

        let id = Uuid::new_v4().simple().to_string();
        let short_id = &id[..12];
        let local_name = args
            .get("local_name")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(ToString::to_string)
            .unwrap_or_else(|| format!("capture_{short_id}.pcap"));

        Ok(PcapCaptureSpec {
            host,
            interface,
            filter,
            duration_s,
            max_packets,
            max_size_mb,
            snaplen,
            remote_output_path: format!("/tmp/metatron-pcap-{short_id}.pcap"),
            local_name,
        })
    }
}

#[async_trait]
impl Tool for PcapTool {
    fn name(&self) -> &str {
        "pcap"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "pcap".to_string(),
            description: "Run a bounded remote tcpdump -w capture with required BPF filter and host-enforced duration, packet-count, and size caps, then transfer the capture into the session evidence directory. Always asks for human approval.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["host", "filter"],
                "properties": {
                    "host": { "type": "string" },
                    "interface": { "type": "string", "default": "any" },
                    "filter": { "type": "string", "description": "Required BPF filter expression (e.g. 'tcp port 443')" },
                    "duration_s": { "type": "integer", "minimum": 1, "maximum": 300, "default": 30 },
                    "max_packets": { "type": "integer", "minimum": 1, "maximum": 50000, "default": 1000 },
                    "max_size_mb": { "type": "integer", "minimum": 1, "maximum": 50, "default": 10 },
                    "snaplen": { "type": "integer", "minimum": 64, "maximum": 65535, "default": 256 },
                    "local_name": { "type": "string" }
                }
            }),
            risk_class: "capture".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, _args: &Value) -> RiskClass {
        // Hard-prompt class: ALWAYS asks a person in EVERY approval mode.
        RiskClass::Capture
    }

    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let spec = match Self::parse_spec(&args) {
            Ok(s) => s,
            Err(e) => return err_outcome(&e),
        };
        let argv = match build_tcpdump_argv(&spec) {
            Ok(a) => a,
            Err(e) => return err_outcome(&e),
        };
        if let Err(e) = fs::create_dir_all(&ctx.evidence_dir) {
            return err_outcome(&format!("failed to create evidence dir: {e}"));
        }
        let local_dest = match TransferTool::safe_child_path(&ctx.evidence_dir, &spec.local_name) {
            Ok(p) => p,
            Err(e) => return err_outcome(&e),
        };

        let resolved = match ctx
            .remote_runner
            .resolve_host(ctx.boss_client.as_ref(), &ctx.project_id, &spec.host)
            .await
        {
            Ok(h) => h,
            Err(e) => return err_outcome(&e.to_string()),
        };

        // 1. Run bounded `timeout <duration> tcpdump -w ...` on the host.
        // Exit code 124 from `timeout` is expected when the duration cap ends capture cleanly.
        let cap_out = match ctx
            .remote_runner
            .ssh_run(
                ctx.boss_client.as_ref(),
                &resolved,
                &argv,
                Some(&ctx.session_id),
                "metatron pcap capture",
                &ctx.cwd,
                ctx.cancel_rx.clone(),
            )
            .await
        {
            Ok(o) => o,
            Err(e) => return err_outcome(&e.to_string()),
        };

        if cap_out.exec.exit_code != 0 && cap_out.exec.exit_code != 124 {
            let msg = format!(
                "remote tcpdump failed (exit {}): {}",
                cap_out.exec.exit_code,
                redact_secrets(&cap_out.exec.inline_output)
            );
            return ToolOutcome {
                status: ToolResultStatus::Error,
                inline_text: msg.clone(),
                full_text: msg,
                truncated: cap_out.exec.truncated,
                exit_code: Some(cap_out.exec.exit_code),
                bytes: cap_out.exec.total_bytes,
                duration_ms: cap_out.exec.duration_ms,
                host: Some(cap_out.host),
                host_origin: Some(cap_out.origin.as_str().to_string()),
                sandbox_denied: false,
                images: Vec::new(),
                http_audit: None,
            };
        }

        // 2. Transfer capture file into `ctx.evidence_dir`.
        let dl_out = match ctx
            .remote_runner
            .scp_download(
                ctx.boss_client.as_ref(),
                &resolved,
                &spec.remote_output_path,
                &local_dest,
                Some(&ctx.session_id),
                "metatron pcap transfer",
                &ctx.cwd,
                ctx.cancel_rx.clone(),
            )
            .await
        {
            Ok(o) => o,
            Err(e) => return err_outcome(&e.to_string()),
        };

        if dl_out.exec.exit_code != 0 {
            let msg = format!(
                "pcap transfer failed (exit {}): {}",
                dl_out.exec.exit_code,
                redact_secrets(&dl_out.exec.inline_output)
            );
            return err_outcome(&msg);
        }

        let (sha256, bytes) = sha256_file(&local_dest).unwrap_or_else(|_| (String::new(), 0));
        let summary = serde_json::to_string_pretty(&json!({
            "host": cap_out.host,
            "origin": cap_out.origin.as_str(),
            "interface": spec.interface,
            "filter": spec.filter,
            "duration_s": spec.duration_s,
            "max_packets": spec.max_packets,
            "max_size_mb": spec.max_size_mb,
            "remote_path": spec.remote_output_path,
            "local_path": local_dest.display().to_string(),
            "bytes": bytes,
            "sha256": sha256,
            "command": argv.join(" "),
        }))
        .unwrap_or_else(|_| "pcap ok".to_string());

        ToolOutcome {
            status: ToolResultStatus::Ok,
            inline_text: redact_secrets(&summary),
            full_text: redact_secrets(&summary),
            truncated: false,
            exit_code: Some(0),
            bytes: bytes as usize,
            duration_ms: cap_out.exec.duration_ms + dl_out.exec.duration_ms,
            host: Some(cap_out.host),
            host_origin: Some(cap_out.origin.as_str().to_string()),
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

    #[test]
    fn tcpdump_argv_enforces_filter_duration_packets_and_size_caps() {
        let ok_spec = PcapCaptureSpec {
            host: "web-1".to_string(),
            interface: "eth0".to_string(),
            filter: "tcp port 443".to_string(),
            duration_s: 30,
            max_packets: 500,
            max_size_mb: 5,
            snaplen: 128,
            remote_output_path: "/tmp/metatron-pcap-abc123.pcap".to_string(),
            local_name: "web1.pcap".to_string(),
        };
        let argv = build_tcpdump_argv(&ok_spec).expect("build argv");
        assert_eq!(argv[0], "timeout");
        assert_eq!(argv[1], "30");
        assert_eq!(argv[2], "tcpdump");
        assert!(argv.windows(2).any(|w| w[0] == "-c" && w[1] == "500"));
        assert!(argv.windows(2).any(|w| w[0] == "-C" && w[1] == "5"));
        assert!(argv.windows(2).any(|w| w[0] == "-W" && w[1] == "1"));
        assert!(
            argv.windows(2)
                .any(|w| w[0] == "-w" && w[1] == "/tmp/metatron-pcap-abc123.pcap")
        );
        assert_eq!(&argv[argv.len() - 3..], &["tcp", "port", "443"]);

        // Empty filter rejected
        let mut bad = ok_spec.clone();
        bad.filter = "   ".to_string();
        assert!(build_tcpdump_argv(&bad).is_err());

        // Metacharacters in filter rejected
        let mut inj = ok_spec.clone();
        inj.filter = "tcp port 443; id".to_string();
        assert!(build_tcpdump_argv(&inj).is_err());

        // Over-cap duration / packets / size rejected
        let mut over_dur = ok_spec.clone();
        over_dur.duration_s = MAX_PCAP_DURATION_S + 1;
        assert!(build_tcpdump_argv(&over_dur).is_err());

        let mut over_pkt = ok_spec.clone();
        over_pkt.max_packets = MAX_PCAP_PACKETS + 1;
        assert!(build_tcpdump_argv(&over_pkt).is_err());

        let mut over_sz = ok_spec;
        over_sz.max_size_mb = MAX_PCAP_SIZE_MB + 1;
        assert!(build_tcpdump_argv(&over_sz).is_err());
    }

    #[test]
    fn pcap_tool_is_always_hard_prompt_capture_class() {
        let tool = PcapTool;
        let rc = tool.risk(&json!({"host": "web-1", "filter": "tcp port 80"}));
        assert_eq!(rc, RiskClass::Capture);
        assert!(rc.is_hard_prompt());
    }
}
