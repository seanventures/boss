//! OS-level sandboxing wrappers for local Metatron tools (`MT-3`):
//! - macOS: Seatbelt (`sandbox-exec -p <SBPL>`)
//! - Linux: Bubblewrap (`bwrap --unshare-all --ro-bind ... --bind <scratch>`)
//! - Fail-closed fallback: returns `SandboxError::Unavailable` so the orchestrator
//!   prompts the human instead of silently running unsandboxed.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use thiserror::Error;

pub const SEATBELT_BINARY: &str = "/usr/bin/sandbox-exec";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SandboxBackend {
    Seatbelt,
    Bubblewrap,
    Unavailable,
}

impl SandboxBackend {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Seatbelt => "macos-seatbelt",
            Self::Bubblewrap => "linux-bwrap",
            Self::Unavailable => "unavailable",
        }
    }
}

#[derive(Debug, Clone)]
pub struct SandboxPolicy {
    pub allowed_read_roots: Vec<PathBuf>,
    pub writable_scratch_dir: PathBuf,
    pub network_allowed: bool,
}

#[derive(Debug, Clone)]
pub struct SandboxedCommand {
    pub backend: SandboxBackend,
    pub program: String,
    pub args: Vec<String>,
    pub profile_text: Option<String>,
}

#[derive(Debug, Error)]
pub enum SandboxError {
    #[error(
        "no OS sandbox backend is available on this machine (fallback to human prompt required)"
    )]
    Unavailable,
    #[error("invalid sandbox policy: {0}")]
    InvalidPolicy(String),
}

/// Detect the available OS sandbox backend on the current host.
pub fn detect_backend() -> SandboxBackend {
    #[cfg(target_os = "macos")]
    {
        if Path::new(SEATBELT_BINARY).is_file() {
            return SandboxBackend::Seatbelt;
        }
        SandboxBackend::Unavailable
    }
    #[cfg(target_os = "linux")]
    {
        if find_in_path("bwrap").is_some() {
            return SandboxBackend::Bubblewrap;
        }
        SandboxBackend::Unavailable
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        SandboxBackend::Unavailable
    }
}

#[cfg(target_os = "linux")]
fn find_in_path(bin: &str) -> Option<PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_var) {
        let candidate = dir.join(bin);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Wrap `program` + `args` in the active OS sandbox according to `policy`.
/// Fails closed with `SandboxError::Unavailable` if no backend exists.
pub fn wrap_command(
    policy: &SandboxPolicy,
    program: &str,
    args: &[String],
    cwd: &Path,
) -> Result<SandboxedCommand, SandboxError> {
    match detect_backend() {
        SandboxBackend::Seatbelt => wrap_seatbelt(policy, program, args),
        SandboxBackend::Bubblewrap => wrap_bwrap(policy, program, args, cwd),
        SandboxBackend::Unavailable => Err(SandboxError::Unavailable),
    }
}

/// Build a macOS Seatbelt SBPL profile and `sandbox-exec` command invocation.
pub fn build_seatbelt_profile(policy: &SandboxPolicy) -> Result<String, SandboxError> {
    if policy.writable_scratch_dir.as_os_str().is_empty() {
        return Err(SandboxError::InvalidPolicy(
            "writable_scratch_dir is required".to_string(),
        ));
    }

    let mut read_roots = BTreeSet::new();
    for root in &policy.allowed_read_roots {
        read_roots.insert(canonical_or_original(root));
    }
    let scratch = canonical_or_original(&policy.writable_scratch_dir);
    read_roots.insert(scratch.clone());

    let mut sbpl = String::new();
    sbpl.push_str("(version 1)\n");
    sbpl.push_str("(deny default)\n");
    sbpl.push_str("(allow process-exec process-fork)\n");
    sbpl.push_str("(allow process-info* (target self))\n");
    sbpl.push_str("(allow signal (target self))\n");
    sbpl.push_str("(allow sysctl-read)\n");
    sbpl.push_str("(allow mach-lookup)\n");
    sbpl.push_str("(allow file-read-metadata)\n");
    sbpl.push_str("(allow file-read* (literal \"/\"))\n");
    sbpl.push_str(
        "(allow file-map-executable (subpath \"/System\") (subpath \"/usr/lib\") (subpath \"/bin\") (subpath \"/usr/bin\") (subpath \"/usr/libexec\") (subpath \"/opt/homebrew\") (subpath \"/usr/local/Cellar\") (subpath \"/Library/Frameworks/Python.framework\") (subpath \"/Library/Developer/CommandLineTools\"))\n",
    );

    // Minimal system runtime paths needed to execute `/bin/*`, `/usr/bin/*`, dynamic libs.
    for sys_path in [
        "/bin",
        "/usr/bin",
        "/usr/lib",
        "/usr/libexec",
        "/usr/share",
        "/System",
        "/Library/Preferences",
        "/private/var/db",
        "/dev/null",
        "/dev/zero",
        "/dev/urandom",
        "/dev/random",
        "/dev/tty",
        "/dev/dtracehelper",
    ] {
        sbpl.push_str(&format!(
            "(allow file-read* (subpath \"{}\"))\n",
            escape_sbpl(sys_path)
        ));
    }

    for root in &read_roots {
        sbpl.push_str(&format!(
            "(allow file-read* (subpath \"{}\"))\n",
            escape_sbpl(&root.to_string_lossy())
        ));
    }

    // Writable strictly to scratch dir and /dev/null.
    sbpl.push_str(&format!(
        "(allow file-write* (subpath \"{}\"))\n",
        escape_sbpl(&scratch.to_string_lossy())
    ));
    sbpl.push_str("(allow file-write* (literal \"/dev/null\"))\n");
    sbpl.push_str("(allow file-write* (literal \"/dev/tty\"))\n");

    if policy.network_allowed {
        sbpl.push_str("(allow network-outbound)\n");
        sbpl.push_str("(allow network-inbound)\n");
    } else {
        sbpl.push_str("(deny network*)\n");
    }

    Ok(sbpl)
}

pub fn wrap_seatbelt(
    policy: &SandboxPolicy,
    program: &str,
    args: &[String],
) -> Result<SandboxedCommand, SandboxError> {
    let profile = build_seatbelt_profile(policy)?;
    let mut wrapped_args = Vec::with_capacity(args.len() + 3);
    wrapped_args.push("-p".to_string());
    wrapped_args.push(profile.clone());
    wrapped_args.push(program.to_string());
    wrapped_args.extend(args.iter().cloned());
    Ok(SandboxedCommand {
        backend: SandboxBackend::Seatbelt,
        program: SEATBELT_BINARY.to_string(),
        args: wrapped_args,
        profile_text: Some(profile),
    })
}

/// Build a Linux Bubblewrap (`bwrap`) command invocation with read-only binds,
/// per-session writable scratch dir, and network namespace isolation by default.
pub fn wrap_bwrap(
    policy: &SandboxPolicy,
    program: &str,
    args: &[String],
    cwd: &Path,
) -> Result<SandboxedCommand, SandboxError> {
    if policy.writable_scratch_dir.as_os_str().is_empty() {
        return Err(SandboxError::InvalidPolicy(
            "writable_scratch_dir is required".to_string(),
        ));
    }
    let mut bwrap_args = vec![
        "--unshare-all".to_string(),
        "--die-with-parent".to_string(),
        "--new-session".to_string(),
    ];
    if policy.network_allowed {
        bwrap_args.push("--share-net".to_string());
    }
    for sys_dir in ["/usr", "/bin", "/lib", "/lib64"] {
        if Path::new(sys_dir).exists() {
            bwrap_args.push("--ro-bind".to_string());
            bwrap_args.push(sys_dir.to_string());
            bwrap_args.push(sys_dir.to_string());
        }
    }
    bwrap_args.extend([
        "--dev".to_string(),
        "/dev".to_string(),
        "--proc".to_string(),
        "/proc".to_string(),
        "--tmpfs".to_string(),
        "/tmp".to_string(),
    ]);

    for root in &policy.allowed_read_roots {
        let s = root.to_string_lossy().into_owned();
        bwrap_args.push("--ro-bind".to_string());
        bwrap_args.push(s.clone());
        bwrap_args.push(s);
    }

    let scratch = policy.writable_scratch_dir.to_string_lossy().into_owned();
    bwrap_args.push("--bind".to_string());
    bwrap_args.push(scratch.clone());
    bwrap_args.push(scratch);

    bwrap_args.push("--chdir".to_string());
    bwrap_args.push(cwd.to_string_lossy().into_owned());
    bwrap_args.push("--".to_string());
    bwrap_args.push(program.to_string());
    bwrap_args.extend(args.iter().cloned());

    Ok(SandboxedCommand {
        backend: SandboxBackend::Bubblewrap,
        program: "bwrap".to_string(),
        args: bwrap_args,
        profile_text: None,
    })
}

/// Heuristic to detect whether a failed sandboxed command was blocked by the OS sandbox
/// (triggering the Codex orchestrator's fresh-decision escalation flow).
pub fn looks_like_sandbox_denial(exit_code: i32, output: &str) -> bool {
    if exit_code == 0 {
        return false;
    }
    let lower = output.to_ascii_lowercase();
    lower.contains("operation not permitted")
        || lower.contains("permission denied")
        || lower.contains("read-only file system")
        || lower.contains("sandbox")
}

fn canonical_or_original(p: &Path) -> PathBuf {
    std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf())
}

fn escape_sbpl(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;
    use tempfile::TempDir;

    #[test]
    fn bwrap_command_enforces_unshare_ro_bind_and_scratch() {
        let policy = SandboxPolicy {
            allowed_read_roots: vec![PathBuf::from("/opt/evidence")],
            writable_scratch_dir: PathBuf::from("/tmp/metatron-scratch"),
            network_allowed: false,
        };
        let cmd = wrap_bwrap(
            &policy,
            "/usr/bin/env",
            &["ls".to_string()],
            Path::new("/tmp/metatron-scratch"),
        )
        .expect("bwrap");
        assert_eq!(cmd.backend, SandboxBackend::Bubblewrap);
        assert!(cmd.args.contains(&"--unshare-all".to_string()));
        assert!(!cmd.args.contains(&"--share-net".to_string()));
        assert!(cmd.args.windows(3).any(|w| {
            w[0] == "--ro-bind" && w[1] == "/opt/evidence" && w[2] == "/opt/evidence"
        }));
        assert!(cmd.args.windows(3).any(|w| {
            w[0] == "--bind" && w[1] == "/tmp/metatron-scratch" && w[2] == "/tmp/metatron-scratch"
        }));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_seatbelt_blocks_reads_outside_allowed_roots_and_writes_outside_scratch() {
        if !Path::new(SEATBELT_BINARY).is_file() {
            return;
        }
        let allowed_dir = TempDir::new().expect("allowed");
        let scratch_dir = TempDir::new().expect("scratch");
        let forbidden_dir = TempDir::new().expect("forbidden");

        let allowed_file = allowed_dir.path().join("evidence.txt");
        let secret_file = forbidden_dir.path().join("secret.txt");
        let escaped_write = forbidden_dir.path().join("pwned.txt");
        let scratch_write = scratch_dir.path().join("ok.txt");

        std::fs::write(&allowed_file, "visible-evidence").expect("write allowed");
        std::fs::write(&secret_file, "top-secret-outside-root").expect("write secret");

        let policy = SandboxPolicy {
            allowed_read_roots: vec![allowed_dir.path().to_path_buf()],
            writable_scratch_dir: scratch_dir.path().to_path_buf(),
            network_allowed: false,
        };

        // 1. Reading inside allowed root MUST succeed.
        let ok_read = wrap_seatbelt(
            &policy,
            "/bin/cat",
            &[allowed_file.to_string_lossy().into_owned()],
        )
        .expect("wrap");
        let out = Command::new(&ok_read.program)
            .args(&ok_read.args)
            .output()
            .expect("exec");
        assert!(
            out.status.success(),
            "allowed read failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        assert_eq!(String::from_utf8_lossy(&out.stdout), "visible-evidence");

        // 2. Reading outside allowed roots MUST FAIL (sandbox escape blocked).
        let blocked_read = wrap_seatbelt(
            &policy,
            "/bin/cat",
            &[secret_file.to_string_lossy().into_owned()],
        )
        .expect("wrap");
        let out = Command::new(&blocked_read.program)
            .args(&blocked_read.args)
            .output()
            .expect("exec");
        assert!(
            !out.status.success(),
            "sandbox failed to block read outside allowed roots!"
        );
        assert!(!String::from_utf8_lossy(&out.stdout).contains("top-secret-outside-root"));
        assert!(looks_like_sandbox_denial(
            out.status.code().unwrap_or(1),
            &String::from_utf8_lossy(&out.stderr)
        ));

        // 3. Writing outside scratch dir MUST FAIL.
        let blocked_write = wrap_seatbelt(
            &policy,
            "/bin/sh",
            &[
                "-c".to_string(),
                format!("echo bad > '{}'", escaped_write.display()),
            ],
        )
        .expect("wrap");
        let out = Command::new(&blocked_write.program)
            .args(&blocked_write.args)
            .output()
            .expect("exec");
        assert!(
            !out.status.success(),
            "sandbox failed to block write outside scratch dir!"
        );
        assert!(!escaped_write.exists());

        // 4. Writing inside scratch dir MUST SUCCEED.
        let ok_write = wrap_seatbelt(
            &policy,
            "/bin/sh",
            &[
                "-c".to_string(),
                format!("echo good > '{}'", scratch_write.display()),
            ],
        )
        .expect("wrap");
        let out = Command::new(&ok_write.program)
            .args(&ok_write.args)
            .output()
            .expect("exec");
        assert!(
            out.status.success(),
            "scratch write failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        assert_eq!(
            std::fs::read_to_string(&scratch_write)
                .expect("read scratch")
                .trim(),
            "good"
        );
    }
}
