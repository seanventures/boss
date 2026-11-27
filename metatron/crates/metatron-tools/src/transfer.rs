//! Governed file transfer tool (`transfer`, `MT-4`):
//! - `download`: `scp` from remote host strictly into `ctx.evidence_dir` (`RiskClass::RemoteRead`).
//! - `upload`: `scp` from `evidence_dir` or `scratch_dir` to remote host (`RiskClass::RemoteWrite`,
//!   hard prompt!).

use std::fs;
use std::path::{Component, Path, PathBuf};

use async_trait::async_trait;
use metatron_core::{Tool, ToolCtx, ToolOutcome};
use metatron_protocol::{ClientToolSpec, RiskClass, ToolExposure, ToolResultStatus};
use metatron_secrets::redact_secrets;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub struct TransferTool;

impl TransferTool {
    fn direction(args: &Value) -> &str {
        args.get("direction")
            .and_then(Value::as_str)
            .unwrap_or("download")
    }

    /// Validate a relative filename strictly inside `base_dir` (forbids `..`, absolute paths, NUL).
    pub fn safe_child_path(base_dir: &Path, local_name: &str) -> Result<PathBuf, String> {
        let trimmed = local_name.trim();
        if trimmed.is_empty() || trimmed.contains('\0') {
            return Err("local_name must not be empty".to_string());
        }
        let rel = Path::new(trimmed);
        if rel.is_absolute() {
            return Err(
                "local_name must be relative to the evidence/scratch directory".to_string(),
            );
        }
        for comp in rel.components() {
            match comp {
                Component::Normal(_) => {}
                _ => {
                    return Err(format!(
                        "local_name '{trimmed}' must not contain path traversal components"
                    ));
                }
            }
        }
        Ok(base_dir.join(rel))
    }

    pub fn validate_remote_path(remote_path: &str) -> Result<&str, String> {
        let trimmed = remote_path.trim();
        if trimmed.is_empty() {
            return Err("remote_path must not be empty".to_string());
        }
        if trimmed
            .chars()
            .any(|c| (c as u32) < 32 || matches!(c, ';' | '&' | '|' | '`' | '$' | '\n' | '\r'))
        {
            return Err("remote_path contains forbidden shell metacharacters".to_string());
        }
        Ok(trimmed)
    }
}

pub fn sha256_file(path: &Path) -> Result<(String, u64), std::io::Error> {
    let bytes = fs::read(path)?;
    let len = bytes.len() as u64;
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    Ok((hex::encode(hasher.finalize()), len))
}

#[async_trait]
impl Tool for TransferTool {
    fn name(&self) -> &str {
        "transfer"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "transfer".to_string(),
            description: "Download evidence files from a remote host into the session evidence directory via scp, or upload a scratch/evidence file with explicit approval.".to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["host", "remote_path", "local_name"],
                "properties": {
                    "direction": { "type": "string", "enum": ["download", "upload"], "default": "download" },
                    "host": { "type": "string" },
                    "remote_path": { "type": "string" },
                    "local_name": { "type": "string", "description": "Filename inside session evidence dir (or scratch dir for upload)" },
                    "purpose": { "type": "string" }
                }
            }),
            risk_class: "remote-read".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, args: &Value) -> RiskClass {
        if Self::direction(args).eq_ignore_ascii_case("upload") {
            RiskClass::RemoteWrite
        } else {
            RiskClass::RemoteRead
        }
    }

    async fn run(&self, ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let direction = Self::direction(&args).to_ascii_lowercase();
        let Some(host_name) = args
            .get("host")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
        else {
            return err_outcome("transfer requires 'host'");
        };
        let remote_path = match args
            .get("remote_path")
            .and_then(Value::as_str)
            .ok_or_else(|| "transfer requires 'remote_path'".to_string())
            .and_then(Self::validate_remote_path)
        {
            Ok(p) => p,
            Err(e) => return err_outcome(&e),
        };
        let local_name = match args
            .get("local_name")
            .and_then(Value::as_str)
            .ok_or_else(|| "transfer requires 'local_name'".to_string())
        {
            Ok(n) => n,
            Err(e) => return err_outcome(&e),
        };
        let purpose = args
            .get("purpose")
            .and_then(Value::as_str)
            .unwrap_or("metatron evidence transfer");

        if let Err(e) = fs::create_dir_all(&ctx.evidence_dir) {
            return err_outcome(&format!("failed to create evidence dir: {e}"));
        }

        let resolved = match ctx
            .remote_runner
            .resolve_host(ctx.boss_client.as_ref(), &ctx.project_id, host_name)
            .await
        {
            Ok(h) => h,
            Err(e) => return err_outcome(&e.to_string()),
        };

        let is_upload = direction == "upload";
        let local_path = if is_upload {
            match Self::safe_child_path(&ctx.evidence_dir, local_name) {
                Ok(p) if p.is_file() => p,
                Ok(_) => match Self::safe_child_path(&ctx.scratch_dir, local_name) {
                    Ok(p) if p.is_file() => p,
                    Ok(p) => {
                        return err_outcome(&format!(
                            "upload source '{}' not found in evidence or scratch dir",
                            p.display()
                        ));
                    }
                    Err(e) => return err_outcome(&e),
                },
                Err(e) => return err_outcome(&e),
            }
        } else {
            let dest = match Self::safe_child_path(&ctx.evidence_dir, local_name) {
                Ok(p) => p,
                Err(e) => return err_outcome(&e),
            };
            if let Some(parent) = dest.parent() {
                let _ = fs::create_dir_all(parent);
            }
            dest
        };

        let res = if is_upload {
            ctx.remote_runner
                .scp_upload(
                    ctx.boss_client.as_ref(),
                    &resolved,
                    &local_path,
                    remote_path,
                    Some(&ctx.session_id),
                    purpose,
                    &ctx.cwd,
                    ctx.cancel_rx.clone(),
                )
                .await
        } else {
            ctx.remote_runner
                .scp_download(
                    ctx.boss_client.as_ref(),
                    &resolved,
                    remote_path,
                    &local_path,
                    Some(&ctx.session_id),
                    purpose,
                    &ctx.cwd,
                    ctx.cancel_rx.clone(),
                )
                .await
        };

        match res {
            Ok(out) if out.exec.exit_code == 0 => {
                let (sha256, bytes) =
                    sha256_file(&local_path).unwrap_or_else(|_| (String::new(), 0));
                let summary = serde_json::to_string_pretty(&json!({
                    "direction": direction,
                    "host": out.host,
                    "origin": out.origin.as_str(),
                    "remote_path": remote_path,
                    "local_path": local_path.display().to_string(),
                    "bytes": bytes,
                    "sha256": sha256,
                    "warning": out.warning,
                }))
                .unwrap_or_else(|_| "transfer ok".to_string());
                ToolOutcome {
                    status: ToolResultStatus::Ok,
                    inline_text: redact_secrets(&summary),
                    full_text: redact_secrets(&summary),
                    truncated: false,
                    exit_code: Some(0),
                    bytes: bytes as usize,
                    duration_ms: out.exec.duration_ms,
                    host: Some(out.host),
                    host_origin: Some(out.origin.as_str().to_string()),
                    sandbox_denied: false,
                    images: Vec::new(),
                    http_audit: None,
                }
            }
            Ok(out) => {
                let msg = format!(
                    "transfer failed (exit {}): {}",
                    out.exec.exit_code,
                    redact_secrets(&out.exec.inline_output)
                );
                ToolOutcome {
                    status: ToolResultStatus::Error,
                    inline_text: msg.clone(),
                    full_text: msg,
                    truncated: out.exec.truncated,
                    exit_code: Some(out.exec.exit_code),
                    bytes: out.exec.total_bytes,
                    duration_ms: out.exec.duration_ms,
                    host: Some(out.host),
                    host_origin: Some(out.origin.as_str().to_string()),
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

#[cfg(test)]
#[allow(clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::*;
    use metatron_remote::RemoteRunner;
    use tempfile::TempDir;

    #[test]
    fn rejects_path_traversal_and_shell_metacharacters() {
        let tmp = TempDir::new().expect("tmp");
        assert!(TransferTool::safe_child_path(tmp.path(), "../escape.txt").is_err());
        assert!(TransferTool::safe_child_path(tmp.path(), "/etc/passwd").is_err());
        assert!(TransferTool::safe_child_path(tmp.path(), "sar_web1.log").is_ok());
        assert!(TransferTool::validate_remote_path("/var/log/sa/sa27; rm -rf /").is_err());
        assert!(TransferTool::validate_remote_path("/var/log/sa/sa27").is_ok());
    }

    #[tokio::test]
    async fn transfer_download_copies_into_evidence_dir_and_upload_is_hard_remote_write() {
        let tmp = TempDir::new().expect("tmp");
        let evidence = tmp.path().join("evidence");
        let scratch = tmp.path().join("scratch");
        fs::create_dir_all(&evidence).expect("mkdir");
        fs::create_dir_all(&scratch).expect("mkdir");

        let ssh_cfg = tmp.path().join("ssh_config");
        fs::write(&ssh_cfg, "Host web-1\n  HostName 10.0.0.5\n  User ops\n").expect("cfg");

        // Fake `scp` script that writes sample sar output into the destination path (last arg).
        let fake_scp = tmp.path().join("fake_scp.sh");
        fs::write(
            &fake_scp,
            "#!/bin/sh\nfor a in \"$@\"; do LAST=\"$a\"; done\nprintf 'cpu_idle=42.5\\n' > \"$LAST\"\n",
        )
        .expect("write fake scp");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&fake_scp, fs::Permissions::from_mode(0o755)).expect("chmod");
        }

        let tool = TransferTool;
        assert_eq!(
            tool.risk(&json!({"direction": "download", "host": "web-1"})),
            RiskClass::RemoteRead
        );
        assert_eq!(
            tool.risk(&json!({"direction": "upload", "host": "web-1"})),
            RiskClass::RemoteWrite
        );
        assert!(
            tool.risk(&json!({"direction": "upload", "host": "web-1"}))
                .is_hard_prompt()
        );

        let ctx = ToolCtx {
            project_id: "boss-dogfood".to_string(),
            session_id: "msess_xfer".to_string(),
            cwd: tmp.path().to_path_buf(),
            scratch_dir: scratch,
            evidence_dir: evidence.clone(),
            allowed_read_roots: vec![evidence.clone()],
            allow_unsandboxed_retry: false,
            boss_client: None,
            remote_runner: RemoteRunner {
                ssh_binary: "ssh".to_string(),
                scp_binary: fake_scp.to_string_lossy().into_owned(),
                lease_ttl_s: 60,
                timeout: std::time::Duration::from_secs(10),
                ssh_config_path: Some(ssh_cfg),
            },
            ticket_id: None,
            mcp_client: None,
            hooks: None,
            cancel_rx: None,
        };

        let out = tool
            .run(
                &ctx,
                json!({
                    "direction": "download",
                    "host": "web-1",
                    "remote_path": "/var/log/sar.txt",
                    "local_name": "sar_web1.txt"
                }),
            )
            .await;
        assert_eq!(out.status, ToolResultStatus::Ok);
        assert!(evidence.join("sar_web1.txt").is_file());
        assert!(out.inline_text.contains("sha256"));
    }
}
