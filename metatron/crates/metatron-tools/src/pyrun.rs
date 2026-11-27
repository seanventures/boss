//! Sandboxed on-the-fly Python analysis tool (`pyrun`, `MT-4`):
//! - Model supplies Python analysis code; Metatron writes it into `ctx.scratch_dir` and runs
//!   `python3` inside the OS sandbox (`metatron-sandbox`) with:
//!   - **ZERO network access** (`network_allowed: false`, hardcoded!)
//!   - **Read-only access strictly to `ctx.evidence_dir`** (+ minimal Python runtime install paths)
//!   - **Write access strictly to `ctx.scratch_dir`**
//! - Captures redacted stdout/stderr and lists any generated figures/artifacts in `ctx.scratch_dir`.
//! - Optional `mode: "remote"` runs on an approved host via `ssh_run` (`RiskClass::RemoteWrite`,
//!   hard prompt).

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use async_trait::async_trait;
use metatron_core::{Tool, ToolCtx, ToolOutcome};
use metatron_exec::{ExecRequest, run_command};
use metatron_protocol::{ClientToolSpec, RiskClass, ToolExposure, ToolResultStatus};
use metatron_sandbox::{SandboxError, SandboxPolicy, looks_like_sandbox_denial, wrap_command};
use metatron_secrets::redact_secrets;
use serde_json::{Value, json};
use uuid::Uuid;

pub struct PyrunTool {
    pub python_binary: String,
}

impl Default for PyrunTool {
    fn default() -> Self {
        Self {
            python_binary: resolve_python_binary(),
        }
    }
}

fn resolve_python_binary() -> String {
    if let Ok(explicit) = std::env::var("METATRON_PYTHON")
        && !explicit.trim().is_empty()
    {
        return explicit;
    }
    // Prefer direct Python binaries (Homebrew / Framework / Linux /usr/bin) over macOS's `/usr/bin/python3` xcrun shim.
    for candidate in [
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
        "/usr/bin/python3",
    ] {
        if Path::new(candidate).is_file() {
            return candidate.to_string();
        }
    }
    "python3".to_string()
}

/// Minimal read-only runtime directories required for `python3` itself to start inside Seatbelt/bwrap
/// without exposing user home directories or arbitrary files outside `evidence_dir`.
fn python_runtime_read_roots(python_bin: &str) -> Vec<PathBuf> {
    let mut roots = BTreeSet::new();
    let bin_path = Path::new(python_bin);
    if let Ok(canon) = fs::canonicalize(bin_path)
        && let Some(parent) = canon.parent().and_then(Path::parent)
    {
        roots.insert(parent.to_path_buf());
    }
    for std_dir in [
        "/opt/homebrew/Cellar",
        "/opt/homebrew/Frameworks",
        "/opt/homebrew/opt",
        "/opt/homebrew/lib",
        "/opt/homebrew/bin",
        "/Library/Developer/CommandLineTools/Library/Frameworks",
        "/Library/Frameworks/Python.framework",
        "/usr/local/Cellar",
        "/usr/local/lib",
    ] {
        let p = Path::new(std_dir);
        if p.exists() {
            roots.insert(p.to_path_buf());
        }
    }
    roots.into_iter().collect()
}

fn snapshot_dir_files(dir: &Path) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    if let Ok(entries) = fs::read_dir(dir) {
        for entry in entries.flatten() {
            if let Some(name) = entry.file_name().to_str() {
                out.insert(name.to_string());
            }
        }
    }
    out
}

#[async_trait]
impl Tool for PyrunTool {
    fn name(&self) -> &str {
        "pyrun"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "pyrun".to_string(),
            description: "Execute on-the-fly Python analysis in an OS sandbox with ZERO network access, read access strictly to the session evidence directory, and write access strictly to the session scratch directory.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["code"],
                "properties": {
                    "code": { "type": "string", "description": "Python 3 script to execute" },
                    "timeout_s": { "type": "integer", "minimum": 1, "maximum": 180, "default": 60 },
                    "mode": { "type": "string", "enum": ["local", "remote"], "default": "local" },
                    "host": { "type": "string", "description": "Required when mode='remote'" }
                }
            }),
            risk_class: "write-scratch".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, args: &Value) -> RiskClass {
        if args
            .get("mode")
            .and_then(Value::as_str)
            .is_some_and(|m| m.eq_ignore_ascii_case("remote"))
        {
            RiskClass::RemoteWrite
        } else {
            RiskClass::WriteScratch
        }
    }

    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let Some(code) = args
            .get("code")
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty())
        else {
            return err_outcome("pyrun requires non-empty 'code'");
        };
        let timeout_s = args
            .get("timeout_s")
            .and_then(Value::as_u64)
            .unwrap_or(60)
            .clamp(1, 180);
        let mode = args.get("mode").and_then(Value::as_str).unwrap_or("local");

        if mode.eq_ignore_ascii_case("remote") {
            let Some(host_name) = args
                .get("host")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
            else {
                return err_outcome("pyrun mode='remote' requires 'host'");
            };
            let resolved = match ctx
                .remote_runner
                .resolve_host(ctx.boss_client.as_ref(), &ctx.project_id, host_name)
                .await
            {
                Ok(h) => h,
                Err(e) => return err_outcome(&e.to_string()),
            };
            let argv = vec!["python3".to_string(), "-c".to_string(), code.to_string()];
            return match ctx
                .remote_runner
                .ssh_run(
                    ctx.boss_client.as_ref(),
                    &resolved,
                    &argv,
                    Some(&ctx.session_id),
                    "metatron remote pyrun",
                    &ctx.cwd,
                    ctx.cancel_rx.clone(),
                )
                .await
            {
                Ok(out) => ToolOutcome {
                    status: if out.exec.exit_code == 0 {
                        ToolResultStatus::Ok
                    } else {
                        ToolResultStatus::Error
                    },
                    inline_text: redact_secrets(&out.exec.inline_output),
                    full_text: redact_secrets(&out.exec.full_output),
                    truncated: out.exec.truncated,
                    exit_code: Some(out.exec.exit_code),
                    bytes: out.exec.total_bytes,
                    duration_ms: out.exec.duration_ms,
                    host: Some(out.host),
                    host_origin: Some(out.origin.as_str().to_string()),
                    sandbox_denied: false,
                    images: Vec::new(),
                    http_audit: None,
                },
                Err(e) => err_outcome(&e.to_string()),
            };
        }

        if let Err(e) = fs::create_dir_all(&ctx.scratch_dir) {
            return err_outcome(&format!("failed to create scratch dir: {e}"));
        }
        if let Err(e) = fs::create_dir_all(&ctx.evidence_dir) {
            return err_outcome(&format!("failed to create evidence dir: {e}"));
        }

        let before_files = snapshot_dir_files(&ctx.scratch_dir);
        let script_name = format!("pyrun_{}.py", &Uuid::new_v4().simple().to_string()[..12]);
        let script_path = ctx.scratch_dir.join(&script_name);
        if let Err(e) = fs::write(&script_path, code) {
            return err_outcome(&format!("failed to write pyrun script: {e}"));
        }

        // Build strict sandbox policy: NO NETWORK, read ONLY evidence_dir + Python runtime, write ONLY scratch_dir!
        let mut read_roots = vec![ctx.evidence_dir.clone()];
        read_roots.extend(python_runtime_read_roots(&self.python_binary));
        let policy = SandboxPolicy {
            allowed_read_roots: read_roots,
            writable_scratch_dir: ctx.scratch_dir.clone(),
            network_allowed: false,
        };

        let py_args = vec!["-I".to_string(), script_path.to_string_lossy().into_owned()];
        let (program, cmd_args, ran_sandboxed) = if ctx.allow_unsandboxed_retry {
            (self.python_binary.clone(), py_args, false)
        } else {
            match wrap_command(&policy, &self.python_binary, &py_args, &ctx.scratch_dir) {
                Ok(wrapped) => (wrapped.program, wrapped.args, true),
                Err(SandboxError::Unavailable) => {
                    return ToolOutcome {
                        status: ToolResultStatus::Error,
                        inline_text:
                            "pyrun requires an OS sandbox (unavailable on this host); explicit escalation approval required"
                                .to_string(),
                        full_text: "sandbox unavailable".to_string(),
                        truncated: false,
                        exit_code: Some(126),
                        bytes: 0,
                        duration_ms: 0,
                        host: None,
                        host_origin: None,
                        sandbox_denied: true,
                        images: Vec::new(),
                        http_audit: None,
                    };
                }
                Err(e) => return err_outcome(&e.to_string()),
            }
        };

        let mut req = ExecRequest::new(program, cmd_args, ctx.scratch_dir.clone());
        req.timeout = Duration::from_secs(timeout_s);
        req.env.insert(
            "METATRON_EVIDENCE_DIR".to_string(),
            ctx.evidence_dir.to_string_lossy().into_owned(),
        );
        req.env.insert(
            "METATRON_SCRATCH_DIR".to_string(),
            ctx.scratch_dir.to_string_lossy().into_owned(),
        );
        req.env
            .insert("PYTHONDONTWRITEBYTECODE".to_string(), "1".to_string());
        req.env.insert(
            "HOME".to_string(),
            ctx.scratch_dir.to_string_lossy().into_owned(),
        );

        match run_command(req, ctx.cancel_rx.clone()).await {
            Ok(out) => {
                let after_files = snapshot_dir_files(&ctx.scratch_dir);
                let created_artifacts: Vec<String> = after_files
                    .difference(&before_files)
                    .filter(|name| name.as_str() != script_name.as_str())
                    .map(|name| ctx.scratch_dir.join(name).display().to_string())
                    .collect();

                let sandbox_denied =
                    ran_sandboxed && looks_like_sandbox_denial(out.exit_code, &out.inline_output);
                let status = if out.cancelled {
                    ToolResultStatus::Cancelled
                } else if out.exit_code == 0 {
                    ToolResultStatus::Ok
                } else {
                    ToolResultStatus::Error
                };

                let mut inline = redact_secrets(&out.inline_output);
                if !created_artifacts.is_empty() {
                    inline.push_str(&format!(
                        "\n[scratch_artifacts: {}]",
                        created_artifacts.join(", ")
                    ));
                }

                ToolOutcome {
                    status,
                    inline_text: inline,
                    full_text: redact_secrets(&out.full_output),
                    truncated: out.truncated,
                    exit_code: Some(out.exit_code),
                    bytes: out.total_bytes,
                    duration_ms: out.duration_ms,
                    host: None,
                    host_origin: None,
                    sandbox_denied,
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

#[cfg(test)]
#[allow(clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::*;
    use metatron_remote::RemoteRunner;
    use metatron_sandbox::{SandboxBackend, detect_backend};
    use tempfile::TempDir;

    fn make_ctx(evidence: &Path, scratch: &Path) -> ToolCtx {
        ToolCtx {
            project_id: "boss-dogfood".to_string(),
            session_id: "msess_py".to_string(),
            cwd: scratch.to_path_buf(),
            scratch_dir: scratch.to_path_buf(),
            evidence_dir: evidence.to_path_buf(),
            allowed_read_roots: vec![evidence.to_path_buf()],
            allow_unsandboxed_retry: false,
            boss_client: None,
            remote_runner: RemoteRunner::default(),
            ticket_id: None,
            mcp_client: None,
            hooks: None,
            cancel_rx: None,
        }
    }

    #[tokio::test]
    async fn pyrun_sandbox_reads_evidence_writes_scratch_and_blocks_outside_reads_writes_and_network()
     {
        if detect_backend() == SandboxBackend::Unavailable {
            return;
        }
        let tmp = TempDir::new().expect("tmp");
        let evidence = tmp.path().join("evidence");
        let scratch = tmp.path().join("scratch");
        let forbidden = tmp.path().join("forbidden");
        fs::create_dir_all(&evidence).expect("mkdir");
        fs::create_dir_all(&scratch).expect("mkdir");
        fs::create_dir_all(&forbidden).expect("mkdir");

        fs::write(evidence.join("metrics.csv"), "ts,cpu\n1,12.5\n2,87.5\n").expect("write");
        let secret_file = forbidden.join("top_secret.txt");
        fs::write(&secret_file, "OUTSIDE_EVIDENCE_SECRET_XYZ").expect("write secret");
        let escaped_out = forbidden.join("pwn.txt");

        let tool = PyrunTool::default();
        let ctx = make_ctx(&evidence, &scratch);

        // 1. Positive: read evidence_dir and write figure/summary into scratch_dir.
        let ok_res = tool
            .run(
                &ctx,
                json!({
                    "code": "import os, csv, pathlib\nev = pathlib.Path(os.environ['METATRON_EVIDENCE_DIR'])\nsc = pathlib.Path(os.environ['METATRON_SCRATCH_DIR'])\nrows = list(csv.DictReader((ev / 'metrics.csv').open()))\npeak = max(float(r['cpu']) for r in rows)\n(sc / 'summary.txt').write_text(f'peak={peak}')\nprint(f'PEAK_CPU={peak}')\n"
                }),
            )
            .await;
        assert_eq!(
            ok_res.status,
            ToolResultStatus::Ok,
            "pyrun allowed analysis failed: {}",
            ok_res.inline_text
        );
        assert!(ok_res.inline_text.contains("PEAK_CPU=87.5"));
        assert!(scratch.join("summary.txt").is_file());

        // 2. Negative: reading a file OUTSIDE evidence_dir MUST FAIL!
        let bad_read = tool
            .run(
                &ctx,
                json!({
                    "code": format!("print(open({:?}).read())", secret_file.to_string_lossy())
                }),
            )
            .await;
        assert_eq!(
            bad_read.status,
            ToolResultStatus::Error,
            "pyrun MUST NOT read files outside evidence dir!"
        );
        assert!(!bad_read.inline_text.contains("OUTSIDE_EVIDENCE_SECRET_XYZ"));

        // 3. Negative: writing OUTSIDE scratch_dir MUST FAIL!
        let bad_write = tool
            .run(
                &ctx,
                json!({
                    "code": format!("open({:?}, 'w').write('pwned')", escaped_out.to_string_lossy())
                }),
            )
            .await;
        assert_eq!(
            bad_write.status,
            ToolResultStatus::Error,
            "pyrun MUST NOT write outside scratch dir!"
        );
        assert!(!escaped_out.exists());

        // 4. Negative: opening an outbound network socket MUST FAIL!
        let bad_net = tool
            .run(
                &ctx,
                json!({
                    "code": "import socket\ns = socket.create_connection(('1.1.1.1', 80), timeout=2)\nprint('CONNECTED')\n"
                }),
            )
            .await;
        assert_eq!(
            bad_net.status,
            ToolResultStatus::Error,
            "pyrun MUST NOT reach the network!"
        );
        assert!(!bad_net.inline_text.contains("CONNECTED"));
    }
}
