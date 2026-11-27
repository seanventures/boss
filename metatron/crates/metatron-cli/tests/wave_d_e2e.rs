#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::process::Command;

use chrono::{Duration, Utc};
use ed25519_dalek::SigningKey;
use metatron_policy::sign_policy_bundle_ed25519;
use metatron_protocol::{ApprovalMode, CookieModePolicy, JSONL_SCHEMA_VERSION, PolicyBundle};
use rand::rngs::OsRng;
use serde_json::{Value, json};
use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

fn metatron_bin() -> &'static str {
    env!("CARGO_BIN_EXE_metatron")
}

/// MT-5 schema snapshot test: `metatron schema` outputs valid versioned JSON Schema v1.0
/// covering every `ExecEvent` variant for TS/Python SDK consumers.
#[test]
fn cli_schema_outputs_versioned_jsonl_event_schema_v1() {
    let out = Command::new(metatron_bin())
        .arg("schema")
        .output()
        .expect("run schema");
    assert!(
        out.status.success(),
        "schema failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let doc: Value = serde_json::from_slice(&out.stdout).expect("valid schema json");
    assert_eq!(
        doc["properties"]["schema_version"]["const"].as_str(),
        Some(JSONL_SCHEMA_VERSION)
    );
    let variants: Vec<&str> = doc["properties"]["type"]["enum"]
        .as_array()
        .expect("enum")
        .iter()
        .filter_map(Value::as_str)
        .collect();
    assert_eq!(
        variants,
        vec![
            "thread.started",
            "turn.started",
            "item.started",
            "item.updated",
            "item.completed",
            "usage",
            "turn.completed",
            "turn.failed",
        ]
    );
    assert_eq!(doc["oneOf"].as_array().expect("oneOf").len(), 8);
}

/// MT-9 + MT-5 E2E: `metatron exec --json` emits versioned `schema_version: "1.0"` on every line,
/// records the exact per-step tool list (`turn_tools`) in the session JSONL, and `metatron mcp list`
/// displays BOSS admin-approved MCP servers while refusing unapproved servers.
#[tokio::test]
async fn exec_versioned_jsonl_records_per_step_tool_list_and_mcp_subcommands_enforce_registry() {
    let tmp = TempDir::new().expect("tmp");
    let config_path = tmp.path().join("config.toml");
    let data_dir = tmp.path().join("data");

    let sk = SigningKey::generate(&mut OsRng);
    let now = Utc::now();
    let bundle = PolicyBundle {
        version: "v-wd-cli".to_string(),
        project_id: Some("boss-dogfood".to_string()),
        issued_at: Some(now),
        expires_at: now + Duration::hours(1),
        mode_floor: ApprovalMode::RiskyOnly,
        tools_allowed: vec!["*".to_string()],
        rules: Vec::new(),
        cookie_mode: CookieModePolicy::default(),
        host_diagnostics: None,
        mcp: None,
    };
    let (signed, jwks) = sign_policy_bundle_ed25519(&bundle, "kid-wd", &sk).expect("sign");
    let signed_json = serde_json::to_string(&signed).expect("ser");
    let jwks_json = serde_json::to_string(&jwks).expect("ser");

    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let addr = listener.local_addr().expect("addr");
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                break;
            };
            let s_json = signed_json.clone();
            let j_json = jwks_json.clone();
            tokio::spawn(async move {
                let mut buf = vec![0u8; 65536];
                let n = sock.read(&mut buf).await.unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]);
                let first = req.lines().next().unwrap_or("");
                if first.starts_with("POST /v1/metatron/turns ") {
                    let sse = concat!(
                        "id: t1:1\nevent: turn.started\ndata: {\"turn_id\":\"t1\",\"thread_id\":\"th1\",\"model\":\"gpt-4o\",\"mode\":\"risky-only\",\"tools\":[\"shell_local\",\"pyrun\",\"dashboard\",\"handoff_code_task\"],\"dropped\":[{\"name\":\"apply_patch\",\"reason\":\"forbidden\"}]}\n\n",
                        "id: t1:2\nevent: text.delta\ndata: {\"text\":\"All checks green.\"}\n\n",
                        "id: t1:3\nevent: turn.completed\ndata: {\"turn_id\":\"t1\",\"stop_reason\":\"end_turn\"}\n\n"
                    );
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                        sse.len(),
                        sse
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                } else if first.starts_with("GET /v1/mcp/servers") {
                    let body = json!({
                        "servers": [{
                            "id": "mcp_1",
                            "name": "grafana",
                            "url": "http://127.0.0.1:8770/mcp",
                            "transport": "http",
                            "risk_tier": 1,
                            "enabled": true,
                            "tools": []
                        }]
                    })
                    .to_string();
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                        body.len(),
                        body
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                } else if first.starts_with("GET /v1/metatron/policy/keys ") {
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                        j_json.len(),
                        j_json
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                } else if first.starts_with("GET /v1/metatron/policy?") {
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                        s_json.len(),
                        s_json
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                } else {
                    let body = r#"{"accepted":[],"rejected":[]}"#;
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                        body.len(),
                        body
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                }
                let _ = sock.shutdown().await;
            });
        }
    });

    let base_url = format!("http://{addr}");
    std::fs::write(
        &config_path,
        format!(
            "default_profile = \"wd\"\n[profiles.wd]\nurl = \"{base_url}\"\nproject = \"boss-dogfood\"\nmode = \"risky-only\"\n"
        ),
    )
    .expect("write cfg");

    // 1. `metatron exec --json` emits `schema_version: "1.0"` on every line!
    let exec_out = tokio::task::spawn_blocking({
        let config_path = config_path.clone();
        let data_dir = data_dir.clone();
        move || {
            Command::new(metatron_bin())
                .env("METATRON_CONFIG", &config_path)
                .env("METATRON_DATA_DIR", &data_dir)
                .env("BOSS_API_KEY", "boss_pat_wd_key_12345678")
                .args(["exec", "--json", "--non-interactive", "status check"])
                .output()
                .expect("run exec")
        }
    })
    .await
    .expect("join");
    assert!(exec_out.status.success());
    let stdout = String::from_utf8_lossy(&exec_out.stdout);
    let lines: Vec<Value> = stdout
        .lines()
        .map(|l| serde_json::from_str(l).expect("valid jsonl"))
        .collect();
    assert!(!lines.is_empty());
    for line in &lines {
        assert_eq!(line["schema_version"].as_str(), Some(JSONL_SCHEMA_VERSION));
    }

    // Verify session JSONL recorded `turn_tools` matching `turn.started.tools`!
    let sessions_dir = data_dir.join("sessions");
    let mut found_turn_tools = false;
    for entry in std::fs::read_dir(&sessions_dir).expect("read sessions") {
        let content = std::fs::read_to_string(entry.expect("e").path()).expect("read");
        if content.contains(r#""type":"turn_tools""#) && content.contains("handoff_code_task") {
            found_turn_tools = true;
        }
    }
    assert!(
        found_turn_tools,
        "session JSONL must record per-step turn_tools from turn.started"
    );

    // 2. `metatron mcp list` prints approved server; `metatron mcp probe rogue` exits PolicyDenied (3)!
    let mcp_list = tokio::task::spawn_blocking({
        let config_path = config_path.clone();
        let data_dir = data_dir.clone();
        move || {
            Command::new(metatron_bin())
                .env("METATRON_CONFIG", &config_path)
                .env("METATRON_DATA_DIR", &data_dir)
                .env("BOSS_API_KEY", "boss_pat_wd_key_12345678")
                .args(["mcp", "list"])
                .output()
                .expect("mcp list")
        }
    })
    .await
    .expect("join");
    assert!(mcp_list.status.success());
    assert!(String::from_utf8_lossy(&mcp_list.stdout).contains("grafana"));

    let mcp_probe_rogue = tokio::task::spawn_blocking({
        let config_path = config_path.clone();
        let data_dir = data_dir.clone();
        move || {
            Command::new(metatron_bin())
                .env("METATRON_CONFIG", &config_path)
                .env("METATRON_DATA_DIR", &data_dir)
                .env("BOSS_API_KEY", "boss_pat_wd_key_12345678")
                .args(["mcp", "probe", "rogue_unapproved"])
                .output()
                .expect("mcp probe")
        }
    })
    .await
    .expect("join");
    assert_eq!(mcp_probe_rogue.status.code(), Some(3));
}
