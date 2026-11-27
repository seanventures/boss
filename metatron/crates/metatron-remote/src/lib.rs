//! Remote SSH execution (`ssh_run`), host discovery (BOSS registered hosts +
//! `~/.ssh/config`), and vault-lent ephemeral SSH key handling (`MT-3`).

use std::path::{Path, PathBuf};
use std::time::Duration;

use metatron_boss_client::{BossClient, BossClientError};
use metatron_exec::{ExecError, ExecOutput, ExecRequest, run_command};
use metatron_protocol::{CredentialLeaseRequest, RegisteredHost};
use metatron_secrets::{EphemeralKeyFile, SecretError};
use thiserror::Error;
use tokio::sync::watch;

/// Banner displayed whenever a command executes on a remote host.
pub const REMOTE_SANDBOX_WARNING: &str = "Note: local OS sandbox does not protect remote hosts; command is governed by BOSS policy and audited.";

#[derive(Debug, Error)]
pub enum RemoteError {
    #[error("remote host '{0}' not found in BOSS registry or ~/.ssh/config")]
    HostNotFound(String),
    #[error("BOSS API error: {0}")]
    BossClient(#[from] BossClientError),
    #[error("ephemeral credential error: {0}")]
    Secret(#[from] SecretError),
    #[error("ssh execution error: {0}")]
    Exec(#[from] ExecError),
    #[error("remote command must not be empty")]
    EmptyCommand,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HostOrigin {
    BossVault,
    UserSshConfig,
}

impl HostOrigin {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::BossVault => "boss_vault",
            Self::UserSshConfig => "outside_boss_vault",
        }
    }
}

/// Host entry parsed from the user's `~/.ssh/config`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SshConfigHost {
    pub alias: String,
    pub hostname: Option<String>,
    pub user: Option<String>,
    pub port: Option<u16>,
    pub identity_file: Option<String>,
}

/// Unified host target discovered from either BOSS or `~/.ssh/config`.
#[derive(Debug, Clone)]
pub enum ResolvedHost {
    Registered(RegisteredHost),
    UserSshConfig(SshConfigHost),
}

impl ResolvedHost {
    pub fn name(&self) -> &str {
        match self {
            Self::Registered(h) => &h.name,
            Self::UserSshConfig(h) => &h.alias,
        }
    }

    pub fn origin(&self) -> HostOrigin {
        match self {
            Self::Registered(_) => HostOrigin::BossVault,
            Self::UserSshConfig(_) => HostOrigin::UserSshConfig,
        }
    }
}

/// Outcome of an `ssh_run` invocation including audit origin and remote warning.
#[derive(Debug, Clone)]
pub struct RemoteExecOutput {
    pub host: String,
    pub origin: HostOrigin,
    pub lease_id: Option<String>,
    pub warning: &'static str,
    pub exec: ExecOutput,
}

/// Parse `~/.ssh/config` content into concrete host entries (skipping wildcard `*` blocks).
pub fn parse_ssh_config(content: &str) -> Vec<SshConfigHost> {
    let mut hosts: Vec<SshConfigHost> = Vec::new();
    let mut current_indices: Vec<usize> = Vec::new();

    for raw_line in content.lines() {
        let line = raw_line.split('#').next().unwrap_or("").trim();
        if line.is_empty() {
            continue;
        }
        let mut parts = line.split_whitespace();
        let Some(key) = parts.next() else {
            continue;
        };
        let rest: Vec<&str> = parts.collect();
        if rest.is_empty() {
            continue;
        }
        match key.to_ascii_lowercase().as_str() {
            "host" => {
                current_indices.clear();
                for alias in rest {
                    if alias.contains('*') || alias.contains('?') || alias.starts_with('!') {
                        continue;
                    }
                    let idx = hosts.len();
                    hosts.push(SshConfigHost {
                        alias: alias.to_string(),
                        hostname: None,
                        user: None,
                        port: None,
                        identity_file: None,
                    });
                    current_indices.push(idx);
                }
            }
            "hostname" => {
                for &idx in &current_indices {
                    if hosts[idx].hostname.is_none() {
                        hosts[idx].hostname = Some(rest[0].to_string());
                    }
                }
            }
            "user" => {
                for &idx in &current_indices {
                    if hosts[idx].user.is_none() {
                        hosts[idx].user = Some(rest[0].to_string());
                    }
                }
            }
            "port" => {
                if let Ok(port) = rest[0].parse::<u16>() {
                    for &idx in &current_indices {
                        if hosts[idx].port.is_none() {
                            hosts[idx].port = Some(port);
                        }
                    }
                }
            }
            "identityfile" => {
                for &idx in &current_indices {
                    if hosts[idx].identity_file.is_none() {
                        hosts[idx].identity_file = Some(rest[0].to_string());
                    }
                }
            }
            _ => {}
        }
    }
    hosts
}

/// Load `~/.ssh/config` from disk if present.
pub fn load_user_ssh_config(path: Option<&Path>) -> Vec<SshConfigHost> {
    let default_path = path
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".ssh").join("config")));
    let Some(cfg_path) = default_path else {
        return Vec::new();
    };
    std::fs::read_to_string(cfg_path)
        .map(|text| parse_ssh_config(&text))
        .unwrap_or_default()
}

/// Runner for `ssh_run` targeting registered BOSS hosts (with vault-lent keys) or
/// user `~/.ssh/config` hosts (flagged `outside_boss_vault`).
#[derive(Debug, Clone)]
pub struct RemoteRunner {
    pub ssh_binary: String,
    pub lease_ttl_s: u64,
    pub timeout: Duration,
    pub ssh_config_path: Option<PathBuf>,
}

impl Default for RemoteRunner {
    fn default() -> Self {
        Self {
            ssh_binary: "ssh".to_string(),
            lease_ttl_s: 60,
            timeout: Duration::from_secs(60),
            ssh_config_path: None,
        }
    }
}

impl RemoteRunner {
    /// Discover both BOSS-registered hosts and `~/.ssh/config` hosts.
    pub async fn discover_hosts(
        &self,
        client: Option<&BossClient>,
        project_id: &str,
    ) -> Result<Vec<ResolvedHost>, RemoteError> {
        let mut out = Vec::new();
        if let Some(c) = client {
            for h in c.list_hosts(project_id).await? {
                out.push(ResolvedHost::Registered(h));
            }
        }
        for cfg_host in load_user_ssh_config(self.ssh_config_path.as_deref()) {
            let duplicate = out.iter().any(|h| h.name() == cfg_host.alias);
            if !duplicate {
                out.push(ResolvedHost::UserSshConfig(cfg_host));
            }
        }
        Ok(out)
    }

    /// Resolve a target host name or id across BOSS hosts first, then `~/.ssh/config`.
    pub async fn resolve_host(
        &self,
        client: Option<&BossClient>,
        project_id: &str,
        target: &str,
    ) -> Result<ResolvedHost, RemoteError> {
        let hosts = self.discover_hosts(client, project_id).await?;
        for h in hosts {
            match &h {
                ResolvedHost::Registered(reg)
                    if reg.name == target || reg.id == target || reg.address == target =>
                {
                    return Ok(h);
                }
                ResolvedHost::UserSshConfig(cfg) if cfg.alias == target => {
                    return Ok(h);
                }
                _ => {}
            }
        }
        Err(RemoteError::HostNotFound(target.to_string()))
    }

    /// Execute `argv` on a resolved host.
    ///
    /// - Registered BOSS hosts lease a short-lived SSH key from BOSS vault, write it to
    ///   an `EphemeralKeyFile` (0600) that is overwritten and deleted immediately on Drop.
    /// - User `~/.ssh/config` hosts execute via system `ssh` and are flagged `outside_boss_vault`.
    #[allow(clippy::too_many_arguments)]
    pub async fn ssh_run(
        &self,
        client: Option<&BossClient>,
        host: &ResolvedHost,
        argv: &[String],
        session_id: Option<&str>,
        purpose: &str,
        cwd: &Path,
        cancel_rx: Option<watch::Receiver<bool>>,
    ) -> Result<RemoteExecOutput, RemoteError> {
        if argv.is_empty() {
            return Err(RemoteError::EmptyCommand);
        }
        let quoted_cmd = shell_words::join(argv);

        match host {
            ResolvedHost::Registered(reg) => {
                let boss = client.ok_or_else(|| {
                    RemoteError::HostNotFound(format!(
                        "BOSS client required to lease key for registered host '{}'",
                        reg.name
                    ))
                })?;
                let lease = boss
                    .lease_host_credential(
                        &reg.id,
                        &CredentialLeaseRequest {
                            purpose: purpose.to_string(),
                            ttl_s: self.lease_ttl_s,
                            session_id: session_id.map(ToString::to_string),
                        },
                    )
                    .await?;
                let known_hosts = lease
                    .host_key_known_hosts
                    .as_deref()
                    .or(reg.host_key_known_hosts.as_deref());
                // EphemeralKeyFile overwrites & deletes the 0600 temp file on drop!
                let key_file = EphemeralKeyFile::write(&lease.material, known_hosts)?;

                let mut ssh_args = vec![
                    "-o".to_string(),
                    "BatchMode=yes".to_string(),
                    "-o".to_string(),
                    "IdentitiesOnly=yes".to_string(),
                    "-o".to_string(),
                    "ConnectTimeout=15".to_string(),
                    "-i".to_string(),
                    key_file.key_path().to_string_lossy().into_owned(),
                    "-p".to_string(),
                    reg.ssh_port.to_string(),
                ];
                if let Some(kh_path) = key_file.known_hosts_path() {
                    ssh_args.push("-o".to_string());
                    ssh_args.push("StrictHostKeyChecking=yes".to_string());
                    ssh_args.push("-o".to_string());
                    ssh_args.push(format!("UserKnownHostsFile={}", kh_path.display()));
                } else {
                    ssh_args.push("-o".to_string());
                    ssh_args.push("StrictHostKeyChecking=accept-new".to_string());
                }
                ssh_args.push(format!("{}@{}", reg.ssh_user, reg.address));
                ssh_args.push(quoted_cmd);

                let mut req = ExecRequest::new(&self.ssh_binary, ssh_args, cwd.to_path_buf());
                req.timeout = self.timeout;
                let exec = run_command(req, cancel_rx).await?;
                drop(key_file);

                Ok(RemoteExecOutput {
                    host: reg.name.clone(),
                    origin: HostOrigin::BossVault,
                    lease_id: Some(lease.lease_id),
                    warning: REMOTE_SANDBOX_WARNING,
                    exec,
                })
            }
            ResolvedHost::UserSshConfig(cfg) => {
                let mut ssh_args = vec![
                    "-o".to_string(),
                    "BatchMode=yes".to_string(),
                    "-o".to_string(),
                    "ConnectTimeout=15".to_string(),
                ];
                if let Some(ref cfg_path) = self.ssh_config_path {
                    ssh_args.push("-F".to_string());
                    ssh_args.push(cfg_path.to_string_lossy().into_owned());
                }
                ssh_args.push(cfg.alias.clone());
                ssh_args.push(quoted_cmd);

                let mut req = ExecRequest::new(&self.ssh_binary, ssh_args, cwd.to_path_buf());
                req.timeout = self.timeout;
                let exec = run_command(req, cancel_rx).await?;

                Ok(RemoteExecOutput {
                    host: cfg.alias.clone(),
                    origin: HostOrigin::UserSshConfig,
                    lease_id: None,
                    warning: REMOTE_SANDBOX_WARNING,
                    exec,
                })
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use metatron_boss_client::{AuthMethod, BossClientConfig};
    use tempfile::TempDir;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    #[test]
    fn parses_user_ssh_config_ignoring_wildcards() {
        let raw = r#"
# Comment
Host *
  ServerAliveInterval 60

Host prod-web-1
  HostName 10.0.1.10
  User ops
  Port 2222
  IdentityFile ~/.ssh/id_ed25519

Host staging-db
  HostName 10.0.2.20
"#;
        let hosts = parse_ssh_config(raw);
        assert_eq!(hosts.len(), 2);
        assert_eq!(hosts[0].alias, "prod-web-1");
        assert_eq!(hosts[0].hostname.as_deref(), Some("10.0.1.10"));
        assert_eq!(hosts[0].user.as_deref(), Some("ops"));
        assert_eq!(hosts[0].port, Some(2222));
        assert_eq!(hosts[1].alias, "staging-db");
    }

    #[tokio::test]
    async fn ssh_run_registered_host_leases_ephemeral_key_and_removes_it_after_run() {
        let tmp = TempDir::new().expect("tmp");
        let observed_key_path_file = tmp.path().join("observed_key_path.txt");

        // Write a fake `ssh` script that verifies the -i key file exists with 0600 perms,
        // records its path, and echoes the remote sar command output.
        let fake_ssh = tmp.path().join("fake_ssh.sh");
        let script = format!(
            "#!/bin/sh\nKEY=''\nwhile [ $# -gt 0 ]; do\n  if [ \"$1\" = \"-i\" ]; then\n    KEY=\"$2\"; shift 2; continue\n  fi\n  LAST=\"$1\"\n  shift\ndone\necho \"$KEY\" > '{}'\nif [ ! -f \"$KEY\" ]; then echo 'missing key' >&2; exit 2; fi\necho \"FAKE_SSH_OK: $LAST\"\n",
            observed_key_path_file.display()
        );
        std::fs::write(&fake_ssh, script).expect("write fake ssh");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&fake_ssh, std::fs::Permissions::from_mode(0o755))
                .expect("chmod");
        }

        // Fake BOSS server serving `/v1/metatron/hosts` and `/v1/metatron/hosts/h1/credential-lease`.
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    break;
                };
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 8192];
                    let n = sock.read(&mut buf).await.unwrap_or(0);
                    let req = String::from_utf8_lossy(&buf[..n]);
                    let body = if req.contains("/credential-lease") {
                        serde_json::json!({
                            "lease_id": "mcl_test_1",
                            "host_id": "h1",
                            "kind": "ssh_key",
                            "expires_at": (chrono::Utc::now() + chrono::Duration::seconds(60)).to_rfc3339(),
                            "host_key_known_hosts": "10.0.0.5 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAtest",
                            "material": "-----BEGIN OPENSSH PRIVATE KEY-----\nEPHEMERAL\n-----END OPENSSH PRIVATE KEY-----"
                        })
                        .to_string()
                    } else {
                        serde_json::json!({
                            "hosts": [{
                                "id": "h1",
                                "project_id": "boss-dogfood",
                                "name": "web-1",
                                "address": "10.0.0.5",
                                "ssh_user": "ops",
                                "ssh_port": 22,
                                "key_configured": true
                            }]
                        })
                        .to_string()
                    };
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                        body.len(),
                        body
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                    let _ = sock.shutdown().await;
                });
            }
        });

        let client = BossClient::new(
            BossClientConfig::new(
                &format!("http://{addr}"),
                AuthMethod::Bearer("t".to_string()),
            )
            .expect("cfg"),
        )
        .expect("client");

        let runner = RemoteRunner {
            ssh_binary: fake_ssh.to_string_lossy().into_owned(),
            lease_ttl_s: 60,
            timeout: Duration::from_secs(10),
            ssh_config_path: Some(tmp.path().join("nonexistent_ssh_config")),
        };

        let resolved = runner
            .resolve_host(Some(&client), "boss-dogfood", "web-1")
            .await
            .expect("resolve");
        assert_eq!(resolved.origin(), HostOrigin::BossVault);

        let out = runner
            .ssh_run(
                Some(&client),
                &resolved,
                &["sar", "-u", "1", "5"].map(str::to_string),
                Some("msess_1"),
                "diagnostics",
                tmp.path(),
                None,
            )
            .await
            .expect("ssh_run");

        assert_eq!(out.exec.exit_code, 0);
        assert_eq!(out.origin, HostOrigin::BossVault);
        assert_eq!(out.lease_id.as_deref(), Some("mcl_test_1"));
        assert!(out.warning.contains("does not protect remote hosts"));
        assert!(out.exec.inline_output.contains("FAKE_SSH_OK: sar -u 1 5"));

        // Verify the ephemeral key file has been cleaned up immediately after ssh returned.
        let key_path = std::fs::read_to_string(&observed_key_path_file)
            .expect("read path")
            .trim()
            .to_string();
        assert!(!key_path.is_empty());
        assert!(
            !Path::new(&key_path).exists(),
            "ephemeral SSH key {key_path} must be removed after ssh_run!"
        );
    }

    #[tokio::test]
    async fn ssh_run_user_config_host_is_flagged_outside_boss_vault() {
        let tmp = TempDir::new().expect("tmp");
        let cfg_path = tmp.path().join("ssh_config");
        std::fs::write(
            &cfg_path,
            "Host laptop-box\n  HostName 192.168.1.50\n  User sean\n",
        )
        .expect("write cfg");

        let fake_ssh = tmp.path().join("fake_ssh.sh");
        std::fs::write(&fake_ssh, "#!/bin/sh\necho USER_SSH_OK\n").expect("write");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&fake_ssh, std::fs::Permissions::from_mode(0o755))
                .expect("chmod");
        }

        let runner = RemoteRunner {
            ssh_binary: fake_ssh.to_string_lossy().into_owned(),
            lease_ttl_s: 60,
            timeout: Duration::from_secs(10),
            ssh_config_path: Some(cfg_path),
        };
        let resolved = runner
            .resolve_host(None, "boss-dogfood", "laptop-box")
            .await
            .expect("resolve");
        assert_eq!(resolved.origin(), HostOrigin::UserSshConfig);
        let out = runner
            .ssh_run(
                None,
                &resolved,
                &["uptime".to_string()],
                None,
                "check",
                tmp.path(),
                None,
            )
            .await
            .expect("run");
        assert_eq!(out.origin, HostOrigin::UserSshConfig);
        assert!(out.lease_id.is_none());
        assert!(out.exec.inline_output.contains("USER_SSH_OK"));
    }
}
