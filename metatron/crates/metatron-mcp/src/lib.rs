//! Model Context Protocol (MCP) client restricted strictly to BOSS's admin-approved
//! MCP registry (`MT-9`, `MT-5`).
//!
//! Invariants (`ARCHITECTURE.md` §8):
//! - Only servers present in the BOSS admin-approved registry (`GET /v1/mcp/servers` or
//!   signed `PolicyBundle.mcp`) may be connected or invoked. Any unapproved server fails closed.
//! - Every MCP tool is namespaced as `mcp__<server>__<tool>` and governed by per-tool
//!   policy key `mcp:<server>:<tool>`.
//! - Supports both local stdio JSON-RPC 2.0 MCP servers and remote HTTP/streamable-HTTP
//!   MCP servers with Bearer/Header/OAuth token resolution (never logged).
//! - Large servers (> `deferred_threshold` tools) default to `ToolExposure::Deferred`
//!   so the model discovers them lazily via `tool_search`.

use std::collections::BTreeMap;
use std::fmt;
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use metatron_boss_client::{BossClient, BossClientError};
use metatron_protocol::{ClientToolSpec, McpServerRecord, McpToolSpec, RiskClass, ToolExposure};
use metatron_secrets::redact_secrets;
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE, HeaderMap, HeaderName, HeaderValue};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use thiserror::Error;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use url::Url;

/// Default threshold above which a server's tools are exposed as `Deferred`.
pub const DEFAULT_DEFERRED_TOOL_THRESHOLD: usize = 5;

#[derive(Debug, Error)]
pub enum McpError {
    #[error("MCP server '{0}' is not in the BOSS admin-approved registry")]
    ServerNotApproved(String),
    #[error("MCP server '{0}' is disabled in BOSS policy")]
    ServerDisabled(String),
    #[error("MCP tool '{server}:{tool}' is disabled by BOSS tool_policy")]
    ToolDisabled { server: String, tool: String },
    #[error("invalid MCP server configuration: {0}")]
    InvalidConfig(String),
    #[error("BOSS registry fetch failed: {0}")]
    BossClient(#[from] BossClientError),
    #[error("MCP transport error: {0}")]
    Transport(String),
    #[error("MCP protocol error (code {code}): {message}")]
    RpcError { code: i64, message: String },
    #[error("MCP timeout after {0}s")]
    Timeout(u64),
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
}

/// Optional OAuth / Bearer / static-header auth configuration for remote MCP servers (`MT-5`).
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum McpAuthConfig {
    None,
    BearerEnv {
        env_var: String,
    },
    HeaderEnv {
        header: String,
        env_var: String,
    },
    OAuth {
        token_url: String,
        client_id: String,
        #[serde(default)]
        scopes: Vec<String>,
        access_token_env: String,
    },
}

impl fmt::Debug for McpAuthConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::None => write!(f, "McpAuthConfig::None"),
            Self::BearerEnv { env_var } => f
                .debug_struct("BearerEnv")
                .field("env_var", env_var)
                .finish(),
            Self::HeaderEnv { header, env_var } => f
                .debug_struct("HeaderEnv")
                .field("header", header)
                .field("env_var", env_var)
                .finish(),
            Self::OAuth {
                token_url,
                client_id,
                scopes,
                access_token_env,
            } => f
                .debug_struct("OAuth")
                .field("token_url", token_url)
                .field("client_id", client_id)
                .field("scopes", scopes)
                .field("access_token_env", access_token_env)
                .finish(),
        }
    }
}

/// Canonical namespaced tool identifier: `mcp__<server>__<tool>`.
pub fn namespaced_tool_name(server: &str, tool: &str) -> String {
    format!(
        "mcp__{}__{}",
        sanitize_segment(server),
        sanitize_segment(tool)
    )
}

/// Canonical BOSS policy key: `mcp:<server>:<tool>`.
pub fn policy_key(server: &str, tool: &str) -> String {
    format!(
        "mcp:{}:{}",
        sanitize_segment(server),
        sanitize_segment(tool)
    )
}

fn sanitize_segment(s: &str) -> String {
    s.trim()
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

/// Map BOSS MCP `risk_tier` (0..=3) into a Metatron `RiskClass`.
pub fn risk_class_for_tier(tier: u8, server_tool_policy: Option<&str>) -> RiskClass {
    if matches!(server_tool_policy, Some("allow")) && tier == 0 {
        return RiskClass::ReadLocal;
    }
    match tier {
        0 => RiskClass::Network,
        1 => RiskClass::Network,
        2 => RiskClass::RemoteWrite,
        _ => RiskClass::SecretUse,
    }
}

/// Normalized, admin-approved MCP tool entry ready for registration in `metatron-core`.
#[derive(Debug, Clone, PartialEq)]
pub struct ApprovedMcpTool {
    pub server_name: String,
    pub tool_name: String,
    pub namespaced_name: String,
    pub policy_key: String,
    pub description: String,
    pub input_schema: Value,
    pub risk_class: RiskClass,
    pub exposure: ToolExposure,
}

impl ApprovedMcpTool {
    pub fn to_client_spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: self.namespaced_name.clone(),
            description: format!("[MCP {}] {}", self.server_name, self.description),
            input_schema: self.input_schema.clone(),
            risk_class: self.risk_class.as_str().to_string(),
            exposure: self.exposure,
        }
    }
}

/// Output of an MCP `tools/call` invocation.
#[derive(Debug, Clone, PartialEq)]
pub struct McpCallOutput {
    pub is_error: bool,
    pub text: String,
    pub raw_content: Vec<Value>,
}

/// Registry of BOSS admin-approved MCP servers + JSON-RPC 2.0 client.
#[derive(Debug)]
pub struct McpClient {
    approved: BTreeMap<String, McpServerRecord>,
    http: reqwest::Client,
    timeout: Duration,
    deferred_threshold: usize,
    next_id: AtomicU64,
}

impl McpClient {
    /// Create an MCP client strictly bounded to `approved_servers`.
    pub fn from_approved(approved_servers: Vec<McpServerRecord>) -> Result<Self, McpError> {
        let mut approved = BTreeMap::new();
        for srv in approved_servers {
            let key = srv.name.trim().to_string();
            if !key.is_empty() {
                approved.insert(key, srv);
            }
        }
        // Never follow cross-host redirects on MCP HTTP calls so auth headers cannot leak.
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(10))
            .build()
            .map_err(|e| McpError::Transport(e.to_string()))?;
        Ok(Self {
            approved,
            http,
            timeout: Duration::from_secs(30),
            deferred_threshold: DEFAULT_DEFERRED_TOOL_THRESHOLD,
            next_id: AtomicU64::new(1),
        })
    }

    /// Fetch the admin-approved MCP server list directly from BOSS (`GET /v1/mcp/servers`).
    pub async fn from_boss_registry(boss: &BossClient, project_id: &str) -> Result<Self, McpError> {
        let servers = boss.list_mcp_servers(project_id).await?;
        Self::from_approved(servers)
    }

    /// Extract approved MCP servers embedded in a signed `PolicyBundle.mcp` section.
    pub fn from_policy_value(mcp_val: Option<&Value>) -> Result<Self, McpError> {
        let servers: Vec<McpServerRecord> = match mcp_val {
            Some(v) => {
                if let Some(arr) = v.get("servers") {
                    serde_json::from_value(arr.clone())?
                } else if v.is_array() {
                    serde_json::from_value(v.clone())?
                } else {
                    Vec::new()
                }
            }
            None => Vec::new(),
        };
        Self::from_approved(servers)
    }

    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    pub fn with_deferred_threshold(mut self, threshold: usize) -> Self {
        self.deferred_threshold = threshold;
        self
    }

    pub fn approved_servers(&self) -> Vec<&McpServerRecord> {
        self.approved.values().collect()
    }

    /// Fail-closed check: returns the server record ONLY if it is in the BOSS admin-approved
    /// registry AND enabled.
    pub fn require_approved(&self, server_name: &str) -> Result<&McpServerRecord, McpError> {
        let srv = self
            .approved
            .get(server_name)
            .ok_or_else(|| McpError::ServerNotApproved(server_name.to_string()))?;
        if !srv.enabled {
            return Err(McpError::ServerDisabled(server_name.to_string()));
        }
        Ok(srv)
    }

    /// Discover tools for an approved server via live MCP `initialize` + `tools/list`
    /// (falling back to cached BOSS registry `tools` only if explicitly requested via `cached_tools`).
    pub async fn discover_tools(
        &self,
        server_name: &str,
    ) -> Result<Vec<ApprovedMcpTool>, McpError> {
        let srv = self.require_approved(server_name)?;
        let raw_tools: Vec<McpToolSpec> = if srv.transport.eq_ignore_ascii_case("stdio") {
            let resp = self.call_stdio_rpc(srv, "tools/list", json!({})).await?;
            parse_tools_list(&resp)?
        } else {
            let resp = self.call_http_rpc(srv, "tools/list", json!({})).await?;
            parse_tools_list(&resp)?
        };
        Ok(self.build_approved_tools(srv, raw_tools))
    }

    /// Build `ApprovedMcpTool` specs from BOSS registry cached tool metadata (no network probe).
    pub fn cached_tools(&self, server_name: &str) -> Result<Vec<ApprovedMcpTool>, McpError> {
        let srv = self.require_approved(server_name)?;
        Ok(self.build_approved_tools(srv, srv.tools.clone()))
    }

    fn build_approved_tools(
        &self,
        srv: &McpServerRecord,
        raw_tools: Vec<McpToolSpec>,
    ) -> Vec<ApprovedMcpTool> {
        let exposure = if raw_tools.len() > self.deferred_threshold {
            ToolExposure::Deferred
        } else {
            ToolExposure::Direct
        };
        let mut out = Vec::new();
        for t in raw_tools {
            let pol = srv.tool_policy.get(&t.name).map(String::as_str);
            if matches!(pol, Some("off" | "deny" | "forbidden")) {
                continue;
            }
            let tier = srv
                .tool_risk_overrides
                .get(&t.name)
                .copied()
                .unwrap_or(srv.risk_tier);
            out.push(ApprovedMcpTool {
                server_name: srv.name.clone(),
                tool_name: t.name.clone(),
                namespaced_name: namespaced_tool_name(&srv.name, &t.name),
                policy_key: policy_key(&srv.name, &t.name),
                description: t.description,
                input_schema: t.input_schema,
                risk_class: risk_class_for_tier(tier, pol),
                exposure,
            });
        }
        out
    }

    /// Invoke a tool on an approved MCP server (`tools/call`), redacting secrets in output.
    pub async fn call_tool(
        &self,
        server_name: &str,
        tool_name: &str,
        arguments: Value,
    ) -> Result<McpCallOutput, McpError> {
        let srv = self.require_approved(server_name)?;
        if matches!(
            srv.tool_policy.get(tool_name).map(String::as_str),
            Some("off" | "deny" | "forbidden")
        ) {
            return Err(McpError::ToolDisabled {
                server: server_name.to_string(),
                tool: tool_name.to_string(),
            });
        }

        let params = json!({
            "name": tool_name,
            "arguments": arguments,
        });
        let result = if srv.transport.eq_ignore_ascii_case("stdio") {
            self.call_stdio_rpc(srv, "tools/call", params).await?
        } else {
            self.call_http_rpc(srv, "tools/call", params).await?
        };
        Ok(parse_call_output(&result))
    }

    async fn call_http_rpc(
        &self,
        srv: &McpServerRecord,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError> {
        let url = Url::parse(&srv.url).map_err(|e| McpError::InvalidConfig(e.to_string()))?;
        if url.scheme() != "http" && url.scheme() != "https" {
            return Err(McpError::InvalidConfig(format!(
                "unsupported MCP HTTP scheme '{}'",
                url.scheme()
            )));
        }
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let payload = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params,
        });

        let mut headers = HeaderMap::new();
        headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        attach_mcp_auth_headers(&mut headers, srv.auth.as_ref())?;

        let resp = tokio::time::timeout(
            self.timeout,
            self.http.post(url).headers(headers).json(&payload).send(),
        )
        .await
        .map_err(|_| McpError::Timeout(self.timeout.as_secs()))?
        .map_err(|e| McpError::Transport(e.to_string()))?;

        let status = resp.status();
        let body = resp
            .text()
            .await
            .map_err(|e| McpError::Transport(e.to_string()))?;
        if !status.is_success() {
            return Err(McpError::Transport(format!(
                "MCP HTTP {}: {}",
                status.as_u16(),
                redact_secrets(&body.chars().take(300).collect::<String>())
            )));
        }
        let envelope: Value = serde_json::from_str(&body)?;
        extract_rpc_result(envelope)
    }

    async fn call_stdio_rpc(
        &self,
        srv: &McpServerRecord,
        method: &str,
        params: Value,
    ) -> Result<Value, McpError> {
        let cmd_str = srv
            .command
            .as_deref()
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| {
                McpError::InvalidConfig(format!(
                    "stdio MCP server '{}' has no command configured",
                    srv.name
                ))
            })?;
        let mut child = Command::new(cmd_str)
            .args(&srv.args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()?;

        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| McpError::Transport("failed to open stdio MCP stdin".to_string()))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| McpError::Transport("failed to open stdio MCP stdout".to_string()))?;
        let mut reader = BufReader::new(stdout);

        // 1. Send `initialize` handshake.
        let init_id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let init_req = json!({
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": { "name": "metatron", "version": env!("CARGO_PKG_VERSION") }
            }
        });
        write_jsonrpc_line(&mut stdin, &init_req).await?;
        let _init_resp = read_jsonrpc_response(&mut reader, init_id, self.timeout).await?;

        // 2. Send target method request.
        let req_id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let req = json!({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        });
        write_jsonrpc_line(&mut stdin, &req).await?;
        let envelope = read_jsonrpc_response(&mut reader, req_id, self.timeout).await?;
        drop(stdin);
        let _ = child.kill().await;
        extract_rpc_result(envelope)
    }
}

async fn write_jsonrpc_line(
    stdin: &mut tokio::process::ChildStdin,
    value: &Value,
) -> Result<(), McpError> {
    let mut line = serde_json::to_string(value)?;
    line.push('\n');
    stdin.write_all(line.as_bytes()).await?;
    stdin.flush().await?;
    Ok(())
}

async fn read_jsonrpc_response(
    reader: &mut BufReader<tokio::process::ChildStdout>,
    expected_id: u64,
    timeout: Duration,
) -> Result<Value, McpError> {
    let fut = async {
        let mut line = String::new();
        loop {
            line.clear();
            let n = reader.read_line(&mut line).await?;
            if n == 0 {
                return Err(McpError::Transport(
                    "stdio MCP server closed stdout before replying".to_string(),
                ));
            }
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let val: Value = serde_json::from_str(trimmed)?;
            if val.get("id").and_then(Value::as_u64) == Some(expected_id) {
                return Ok(val);
            }
        }
    };
    tokio::time::timeout(timeout, fut)
        .await
        .map_err(|_| McpError::Timeout(timeout.as_secs()))?
}

fn extract_rpc_result(envelope: Value) -> Result<Value, McpError> {
    if let Some(err) = envelope.get("error") {
        let code = err.get("code").and_then(Value::as_i64).unwrap_or(-32000);
        let message = err
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("unknown MCP error")
            .to_string();
        return Err(McpError::RpcError {
            code,
            message: redact_secrets(&message),
        });
    }
    envelope
        .get("result")
        .cloned()
        .ok_or_else(|| McpError::Transport("MCP JSON-RPC response missing 'result'".to_string()))
}

fn parse_tools_list(result: &Value) -> Result<Vec<McpToolSpec>, McpError> {
    let Some(arr) = result.get("tools") else {
        return Ok(Vec::new());
    };
    Ok(serde_json::from_value(arr.clone())?)
}

fn parse_call_output(result: &Value) -> McpCallOutput {
    let is_error = result
        .get("isError")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let raw_content = result
        .get("content")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut parts = Vec::new();
    for item in &raw_content {
        if let Some(text) = item.get("text").and_then(Value::as_str) {
            parts.push(redact_secrets(text));
        } else {
            parts.push(redact_secrets(&item.to_string()));
        }
    }
    let text = if parts.is_empty() {
        redact_secrets(&result.to_string())
    } else {
        parts.join("\n")
    };
    McpCallOutput {
        is_error,
        text,
        raw_content,
    }
}

fn attach_mcp_auth_headers(headers: &mut HeaderMap, auth: Option<&Value>) -> Result<(), McpError> {
    let Some(auth_val) = auth else {
        return Ok(());
    };
    let Ok(cfg) = serde_json::from_value::<McpAuthConfig>(auth_val.clone()) else {
        return Ok(());
    };
    match cfg {
        McpAuthConfig::None => Ok(()),
        McpAuthConfig::BearerEnv { env_var }
        | McpAuthConfig::OAuth {
            access_token_env: env_var,
            ..
        } => {
            if let Ok(tok) = std::env::var(&env_var)
                && !tok.trim().is_empty()
            {
                let hv = HeaderValue::from_str(&format!("Bearer {}", tok.trim()))
                    .map_err(|e| McpError::InvalidConfig(e.to_string()))?;
                headers.insert(AUTHORIZATION, hv);
            }
            Ok(())
        }
        McpAuthConfig::HeaderEnv { header, env_var } => {
            if let Ok(val) = std::env::var(&env_var)
                && !val.trim().is_empty()
            {
                let hn = HeaderName::from_bytes(header.as_bytes())
                    .map_err(|e| McpError::InvalidConfig(e.to_string()))?;
                let hv = HeaderValue::from_str(val.trim())
                    .map_err(|e| McpError::InvalidConfig(e.to_string()))?;
                headers.insert(hn, hv);
            }
            Ok(())
        }
    }
}

#[cfg(test)]
#[allow(clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use tempfile::TempDir;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    fn sample_http_server(name: &str, url: String) -> McpServerRecord {
        McpServerRecord {
            id: format!("mcp_{name}"),
            name: name.to_string(),
            url,
            transport: "http".to_string(),
            risk_tier: 1,
            enabled: true,
            description: Some("Test MCP server".to_string()),
            command: None,
            args: Vec::new(),
            tool_risk_overrides: BTreeMap::new(),
            tool_policy: BTreeMap::from([("forbidden_tool".to_string(), "off".to_string())]),
            tools: Vec::new(),
            auth: None,
        }
    }

    #[tokio::test]
    async fn unapproved_mcp_server_is_refused() {
        let client = McpClient::from_approved(vec![sample_http_server(
            "grafana",
            "http://127.0.0.1:9999/mcp".to_string(),
        )])
        .expect("client");

        let err = client
            .discover_tools("rogue_server")
            .await
            .expect_err("rogue server must fail closed");
        assert!(matches!(err, McpError::ServerNotApproved(ref s) if s == "rogue_server"));

        let call_err = client
            .call_tool("rogue_server", "query", json!({}))
            .await
            .expect_err("rogue call must fail closed");
        assert!(matches!(call_err, McpError::ServerNotApproved(_)));
    }

    #[tokio::test]
    async fn http_mcp_server_discovers_namespaced_tools_and_invokes() {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    break;
                };
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 16384];
                    let n = sock.read(&mut buf).await.unwrap_or(0);
                    let req = String::from_utf8_lossy(&buf[..n]);
                    let body = req.split("\r\n\r\n").nth(1).unwrap_or("");
                    let v: Value = serde_json::from_str(body).unwrap_or_else(|_| json!({}));
                    let id = v["id"].as_u64().unwrap_or(1);
                    let method = v["method"].as_str().unwrap_or("");
                    let resp_body = match method {
                        "tools/list" => json!({
                            "jsonrpc": "2.0",
                            "id": id,
                            "result": {
                                "tools": [
                                    {"name": "query_prometheus", "description": "Query PromQL", "inputSchema": {"type": "object"}},
                                    {"name": "forbidden_tool", "description": "Blocked by policy", "inputSchema": {"type": "object"}}
                                ]
                            }
                        })
                        .to_string(),
                        "tools/call" => json!({
                            "jsonrpc": "2.0",
                            "id": id,
                            "result": {
                                "isError": false,
                                "content": [{"type": "text", "text": "cpu_idle=92.4 secret=boss_pat_1234567890abcd"}]
                            }
                        })
                        .to_string(),
                        _ => json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": "not found"}}).to_string(),
                    };
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                        resp_body.len(),
                        resp_body
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                    let _ = sock.shutdown().await;
                });
            }
        });

        let client = McpClient::from_approved(vec![sample_http_server(
            "grafana",
            format!("http://{addr}/mcp"),
        )])
        .expect("client");

        let tools = client.discover_tools("grafana").await.expect("discover");
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0].namespaced_name, "mcp__grafana__query_prometheus");
        assert_eq!(tools[0].policy_key, "mcp:grafana:query_prometheus");
        assert_eq!(tools[0].exposure, ToolExposure::Direct);

        // Disabled tool is refused even on direct call.
        let disabled_err = client
            .call_tool("grafana", "forbidden_tool", json!({}))
            .await
            .expect_err("disabled tool must fail");
        assert!(matches!(disabled_err, McpError::ToolDisabled { .. }));

        // Approved tool succeeds and redacts secrets in returned text.
        let out = client
            .call_tool("grafana", "query_prometheus", json!({"expr": "up"}))
            .await
            .expect("call");
        assert!(!out.is_error);
        assert!(out.text.contains("cpu_idle=92.4"));
        assert!(!out.text.contains("boss_pat_1234567890abcd"));
    }

    #[tokio::test]
    async fn stdio_mcp_server_handshake_list_and_call() {
        let tmp = TempDir::new().expect("tmp");
        let fake_mcp = tmp.path().join("fake_mcp_stdio.py");
        std::fs::write(
            &fake_mcp,
            r#"import json, sys
for line in sys.stdin:
    req = json.loads(line)
    rid = req.get("id")
    m = req.get("method")
    if m == "initialize":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
    elif m == "tools/list":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": "search", "description": "SearXNG search", "inputSchema": {"type": "object"}}
        ]}}
    elif m == "tools/call":
        q = req.get("params", {}).get("arguments", {}).get("query", "")
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"isError": False, "content": [{"type": "text", "text": f"hit:{q}"}]}}
    else:
        resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}}
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
"#,
        )
        .expect("write script");

        let srv = McpServerRecord {
            id: "mcp_searx".to_string(),
            name: "searxng".to_string(),
            url: "stdio://boss-dogfood/searxng".to_string(),
            transport: "stdio".to_string(),
            risk_tier: 0,
            enabled: true,
            description: Some("Local search MCP".to_string()),
            command: Some("python3".to_string()),
            args: vec![fake_mcp.to_string_lossy().into_owned()],
            tool_risk_overrides: BTreeMap::new(),
            tool_policy: BTreeMap::new(),
            tools: Vec::new(),
            auth: None,
        };
        let client = McpClient::from_approved(vec![srv]).expect("client");
        let tools = client.discover_tools("searxng").await.expect("discover");
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0].namespaced_name, "mcp__searxng__search");
        assert_eq!(tools[0].policy_key, "mcp:searxng:search");

        let res = client
            .call_tool("searxng", "search", json!({"query": "boss metatron"}))
            .await
            .expect("call");
        assert!(!res.is_error);
        assert_eq!(res.text, "hit:boss metatron");
    }

    #[test]
    fn large_mcp_server_exposes_tools_as_deferred() {
        let mut srv = sample_http_server("big", "http://127.0.0.1:9999/mcp".to_string());
        srv.tools = (0..8)
            .map(|i| McpToolSpec {
                name: format!("tool_{i}"),
                description: format!("Tool {i}"),
                input_schema: json!({"type": "object"}),
            })
            .collect();
        let client = McpClient::from_approved(vec![srv])
            .expect("client")
            .with_deferred_threshold(5);
        let tools = client.cached_tools("big").expect("cached");
        assert_eq!(tools.len(), 8);
        assert!(tools.iter().all(|t| t.exposure == ToolExposure::Deferred));
    }
}
