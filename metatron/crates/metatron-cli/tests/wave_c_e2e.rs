#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::io::Write;
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

use chrono::{Duration, Utc};
use ed25519_dalek::SigningKey;
use metatron_policy::sign_policy_bundle_ed25519;
use metatron_protocol::{
    ApprovalMode, CookieModePolicy, PolicyBundle, PolicyDecision, PolicyRule, PolicyRuleExamples,
    PolicyRuleMatch,
};
use rand::rngs::OsRng;
use serde_json::{Value, json};
use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

fn metatron_bin() -> &'static str {
    env!("CARGO_BIN_EXE_metatron")
}

#[test]
fn shell_completion_generates_for_bash_zsh_and_fish() {
    for shell in ["bash", "zsh", "fish"] {
        let out = Command::new(metatron_bin())
            .args(["completion", shell])
            .output()
            .expect("run completion");
        assert!(
            out.status.success(),
            "completion {shell} failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        let text = String::from_utf8_lossy(&out.stdout);
        assert!(
            text.contains("metatron"),
            "completion {shell} output missing 'metatron'"
        );
    }
}

#[tokio::test]
async fn login_device_code_doctor_and_logout_against_fake_boss() {
    let tmp = TempDir::new().expect("tmp");
    let config_path = tmp.path().join("config.toml");
    let data_dir = tmp.path().join("data");

    let sk = SigningKey::generate(&mut OsRng);
    let now = Utc::now();
    let bundle = PolicyBundle {
        version: "v-e2e-login".to_string(),
        project_id: Some("boss-dogfood".to_string()),
        issued_at: Some(now),
        expires_at: now + Duration::hours(1),
        mode_floor: ApprovalMode::Ask,
        tools_allowed: vec!["shell_local".to_string(), "ssh_run".to_string()],
        rules: vec![],
        cookie_mode: CookieModePolicy::default(),
        host_diagnostics: None,
        mcp: None,
    };
    let (signed, jwks) = sign_policy_bundle_ed25519(&bundle, "kid-e2e", &sk).expect("sign");
    let signed_json = serde_json::to_string(&signed).expect("ser signed");
    let jwks_json = serde_json::to_string(&jwks).expect("ser jwks");

    let poll_count = Arc::new(AtomicUsize::new(0));
    let revoked_count = Arc::new(AtomicUsize::new(0));
    let poll_srv = Arc::clone(&poll_count);
    let rev_srv = Arc::clone(&revoked_count);

    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let addr = listener.local_addr().expect("addr");
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                break;
            };
            let poll = Arc::clone(&poll_srv);
            let rev = Arc::clone(&rev_srv);
            let s_json = signed_json.clone();
            let j_json = jwks_json.clone();
            tokio::spawn(async move {
                let mut buf = vec![0u8; 16384];
                let n = sock.read(&mut buf).await.unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]);
                let first_line = req.lines().next().unwrap_or("");
                let (status, body) = if first_line.starts_with("POST /v1/auth/device/code ") {
                    (
                        "200 OK",
                        json!({
                            "device_code": "dev_code_1",
                            "user_code": "ABCD-1234",
                            "verification_uri": "https://boss.local/device",
                            "verification_uri_complete": "https://boss.local/device?code=ABCD-1234",
                            "expires_in": 300,
                            "interval": 1
                        })
                        .to_string(),
                    )
                } else if first_line.starts_with("POST /v1/auth/device/token ") {
                    if poll.fetch_add(1, Ordering::SeqCst) == 0 {
                        (
                            "400 Bad Request",
                            json!({"error": "authorization_pending"}).to_string(),
                        )
                    } else {
                        (
                            "200 OK",
                            json!({
                                "access_token": "boss_pat_device_access_token_123456",
                                "refresh_token": "boss_pat_device_refresh_token_123456",
                                "token_type": "Bearer",
                                "expires_in": 3600,
                                "principal": {"id": "usr_1", "actor": "sean", "roles": ["operator"]}
                            })
                            .to_string(),
                        )
                    }
                } else if first_line.starts_with("POST /v1/auth/revoke ") {
                    rev.fetch_add(1, Ordering::SeqCst);
                    ("200 OK", "{}".to_string())
                } else if first_line.starts_with("GET /v1/metatron/capabilities ") {
                    (
                        "200 OK",
                        json!({
                            "contract": 1,
                            "min_client": "0.1.0",
                            "boss_version": "1.0.0",
                            "features": ["turns.stream", "tools.client", "policy", "audit", "device_login"],
                            "limits": {"tool_result_inline_bytes": 65536, "turn_deadline_s": 900}
                        })
                        .to_string(),
                    )
                } else if first_line.starts_with("GET /v1/metatron/policy/keys ") {
                    ("200 OK", j_json)
                } else if first_line.starts_with("GET /v1/metatron/policy?") {
                    ("200 OK", s_json)
                } else {
                    (
                        "404 Not Found",
                        r#"{"error":{"code":"not_found","message":"nope"}}"#.to_string(),
                    )
                };
                let resp = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = sock.write_all(resp.as_bytes()).await;
                let _ = sock.shutdown().await;
            });
        }
    });

    let base_url = format!("http://{addr}");

    // 1. Login via device-code flow (uses explicit --insecure-file-store for CI/headless tests)
    let login_out = tokio::task::spawn_blocking({
        let base_url = base_url.clone();
        let config_path = config_path.clone();
        let data_dir = data_dir.clone();
        move || {
            Command::new(metatron_bin())
                .env("METATRON_CONFIG", &config_path)
                .env("METATRON_DATA_DIR", &data_dir)
                .args(["--insecure-file-store", "login", "--url", &base_url])
                .output()
                .expect("run login")
        }
    })
    .await
    .expect("join login");
    assert!(
        login_out.status.success(),
        "login failed: {}",
        String::from_utf8_lossy(&login_out.stderr)
    );
    let login_stdout = String::from_utf8_lossy(&login_out.stdout);
    assert!(login_stdout.contains("Signed in as 'sean'"));
    assert!(!login_stdout.contains("boss_pat_device_access_token_123456"));
    assert_eq!(poll_count.load(Ordering::SeqCst), 2);

    // 2. Doctor verifies auth, capabilities, signed policy, and sandbox
    let doc_out = tokio::task::spawn_blocking({
        let config_path = config_path.clone();
        let data_dir = data_dir.clone();
        move || {
            Command::new(metatron_bin())
                .env("METATRON_CONFIG", &config_path)
                .env("METATRON_DATA_DIR", &data_dir)
                .args(["--insecure-file-store", "doctor"])
                .output()
                .expect("run doctor")
        }
    })
    .await
    .expect("join doctor");
    assert!(
        doc_out.status.success(),
        "doctor failed: {}",
        String::from_utf8_lossy(&doc_out.stdout)
    );
    let doc_stdout = String::from_utf8_lossy(&doc_out.stdout);
    assert!(
        doc_stdout.contains("Policy bundle: verified v-e2e-login"),
        "unexpected doctor output:\n{doc_stdout}"
    );

    // 3. Policy verify subcommand passes
    let pol_out = tokio::task::spawn_blocking({
        let config_path = config_path.clone();
        let data_dir = data_dir.clone();
        move || {
            Command::new(metatron_bin())
                .env("METATRON_CONFIG", &config_path)
                .env("METATRON_DATA_DIR", &data_dir)
                .args(["--insecure-file-store", "policy", "verify"])
                .output()
                .expect("run policy verify")
        }
    })
    .await
    .expect("join policy verify");
    assert!(pol_out.status.success());

    // 4. Logout revokes token on server and deletes local credentials
    let logout_out = tokio::task::spawn_blocking({
        let config_path = config_path.clone();
        let data_dir = data_dir.clone();
        move || {
            Command::new(metatron_bin())
                .env("METATRON_CONFIG", &config_path)
                .env("METATRON_DATA_DIR", &data_dir)
                .args(["--insecure-file-store", "logout"])
                .output()
                .expect("run logout")
        }
    })
    .await
    .expect("join logout");
    assert!(logout_out.status.success());
    assert_eq!(revoked_count.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn exec_jsonl_golden_transcript_with_stdin_context_schema_tool_and_audit() {
    let tmp = TempDir::new().expect("tmp");
    let repo_dir = tmp.path().join("repo");
    std::fs::create_dir_all(repo_dir.join(".git")).expect("git dir");
    std::fs::write(
        repo_dir.join("AGENTS.md"),
        "Always inspect CPU before restarting.",
    )
    .expect("agents");
    std::fs::write(repo_dir.join("incident.log"), "load average: 12.40").expect("incident");

    let config_path = tmp.path().join("config.toml");
    let data_dir = tmp.path().join("data");
    let out_file = tmp.path().join("final.json");
    let schema_file = tmp.path().join("schema.json");
    std::fs::write(
        &schema_file,
        r#"{"type":"object","required":["status","idle_pct"],"properties":{"status":{"type":"string"},"idle_pct":{"type":"number"}}}"#,
    )
    .expect("schema");

    // Signed policy allowing local `echo` in risky-only mode.
    let sk = SigningKey::generate(&mut OsRng);
    let now = Utc::now();
    let bundle = PolicyBundle {
        version: "v-exec-e2e".to_string(),
        project_id: Some("boss-dogfood".to_string()),
        issued_at: Some(now),
        expires_at: now + Duration::hours(1),
        mode_floor: ApprovalMode::RiskyOnly,
        tools_allowed: vec!["shell_local".to_string(), "ssh_run".to_string()],
        rules: vec![PolicyRule {
            id: "r-echo".to_string(),
            scope: Some("project".to_string()),
            priority: 10,
            matcher: PolicyRuleMatch {
                tool: Some("shell_local".to_string()),
                argv_prefix: Some(vec!["echo".to_string()]),
                ..Default::default()
            },
            decision: PolicyDecision::Allow,
            hard: false,
            justification: "echo is read-only".to_string(),
            examples: PolicyRuleExamples {
                should_match: vec!["echo hello".to_string()],
                not_match: vec!["rm -rf /".to_string()],
            },
        }],
        cookie_mode: CookieModePolicy::default(),
        host_diagnostics: None,
        mcp: None,
    };
    let (signed, jwks) = sign_policy_bundle_ed25519(&bundle, "kid-exec", &sk).expect("sign");
    let signed_json = serde_json::to_string(&signed).expect("ser");
    let jwks_json = serde_json::to_string(&jwks).expect("ser");

    let captured_turn_body = Arc::new(Mutex::new(String::new()));
    let captured_tool_result = Arc::new(Mutex::new(String::new()));
    let captured_audit = Arc::new(Mutex::new(String::new()));

    let turn_cap = Arc::clone(&captured_turn_body);
    let tool_cap = Arc::clone(&captured_tool_result);
    let audit_cap = Arc::clone(&captured_audit);

    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let addr = listener.local_addr().expect("addr");
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                break;
            };
            let s_json = signed_json.clone();
            let j_json = jwks_json.clone();
            let tc = Arc::clone(&turn_cap);
            let tr = Arc::clone(&tool_cap);
            let ac = Arc::clone(&audit_cap);
            tokio::spawn(async move {
                let mut buf = vec![0u8; 65536];
                let n = sock.read(&mut buf).await.unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                let first_line = req.lines().next().unwrap_or("");
                let body_part = req.split("\r\n\r\n").nth(1).unwrap_or("").to_string();

                if first_line.starts_with("POST /v1/metatron/turns ") {
                    *tc.lock().expect("lock") = body_part;
                    let sse = concat!(
                        "id: mturn_e2e:1\nevent: turn.started\ndata: {\"turn_id\":\"mturn_e2e\",\"thread_id\":\"athr_e2e\",\"model\":\"gpt-4o\",\"mode\":\"risky-only\",\"tools\":[\"shell_local\"],\"dropped\":[]}\n\n",
                        "id: mturn_e2e:2\nevent: tool_call\ndata: {\"call_id\":\"call_echo_1\",\"name\":\"shell_local\",\"arguments\":{\"argv\":[\"/bin/echo\",\"cpu_ok\"]},\"risk_hint\":\"read-local\",\"deadline_s\":900}\n\n",
                        "id: mturn_e2e:3\nevent: text.delta\ndata: {\"text\":\"{\\\"status\\\":\\\"ok\\\",\"}\n\n",
                        "id: mturn_e2e:4\nevent: text.delta\ndata: {\"text\":\"\\\"idle_pct\\\":94.2}\"}\n\n",
                        "id: mturn_e2e:5\nevent: usage\ndata: {\"input_tokens\":30,\"output_tokens\":12,\"cost\":{\"amount\":0.0002,\"currency\":\"USD\",\"source\":\"gpt-4o\"}}\n\n",
                        "id: mturn_e2e:6\nevent: turn.completed\ndata: {\"turn_id\":\"mturn_e2e\",\"stop_reason\":\"end_turn\"}\n\n"
                    );
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                        sse.len(),
                        sse
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                } else if first_line.starts_with("POST /v1/metatron/turns/mturn_e2e/tool_results ")
                {
                    *tr.lock().expect("lock") = body_part;
                    let body = r#"{"accepted":true}"#;
                    let resp = format!(
                        "HTTP/1.1 202 Accepted\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                        body.len(),
                        body
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                } else if first_line.starts_with("POST /v1/metatron/audit ") {
                    *ac.lock().expect("lock") = body_part.clone();
                    let parsed: Value =
                        serde_json::from_str(&body_part).unwrap_or_else(|_| json!({}));
                    let ids: Vec<String> = parsed["events"]
                        .as_array()
                        .map(|arr| {
                            arr.iter()
                                .filter_map(|e| e["event_id"].as_str().map(ToString::to_string))
                                .collect()
                        })
                        .unwrap_or_default();
                    let body = json!({"accepted": ids, "rejected": []}).to_string();
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                        body.len(),
                        body
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                } else if first_line.starts_with("GET /v1/metatron/policy/keys ") {
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                        j_json.len(),
                        j_json
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                } else if first_line.starts_with("GET /v1/metatron/policy?") {
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
                        s_json.len(),
                        s_json
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
            "default_profile = \"e2e\"\n[profiles.e2e]\nurl = \"{base_url}\"\nproject = \"boss-dogfood\"\nmode = \"risky-only\"\n"
        ),
    )
    .expect("write cfg");

    // Pipe prompt with @incident.log via stdin!
    let exec_out = tokio::task::spawn_blocking({
        let config_path = config_path.clone();
        let data_dir = data_dir.clone();
        let repo_dir = repo_dir.clone();
        let out_file = out_file.clone();
        let schema_file = schema_file.clone();
        move || {
            let mut child = Command::new(metatron_bin())
                .current_dir(&repo_dir)
                .env("METATRON_CONFIG", &config_path)
                .env("METATRON_DATA_DIR", &data_dir)
                .env("BOSS_API_KEY", "boss_pat_e2e_key_12345678")
                .args([
                    "exec",
                    "--json",
                    "-o",
                    &out_file.to_string_lossy(),
                    "--output-schema",
                    &schema_file.to_string_lossy(),
                    "--non-interactive",
                ])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .expect("spawn exec");
            {
                let mut stdin = child.stdin.take().expect("stdin");
                stdin
                    .write_all(b"Investigate @incident.log and return JSON")
                    .expect("write stdin");
            }
            child.wait_with_output().expect("wait")
        }
    })
    .await
    .expect("join exec");

    assert!(
        exec_out.status.success(),
        "exec failed: {}",
        String::from_utf8_lossy(&exec_out.stderr)
    );

    // Verify golden JSONL event sequence.
    let stdout = String::from_utf8_lossy(&exec_out.stdout);
    let types: Vec<String> = stdout
        .lines()
        .map(|l| {
            let v: Value = serde_json::from_str(l).expect("valid JSONL line");
            v["type"].as_str().expect("type").to_string()
        })
        .collect();
    assert_eq!(
        types,
        vec![
            "thread.started",
            "turn.started",
            "item.started",   // client_tool shell_local
            "item.completed", // client_tool shell_local
            "item.started",   // agent_message
            "item.updated",
            "item.updated",
            "usage",
            "item.completed", // agent_message
            "turn.completed",
        ]
    );

    // Verify `-o FILE` received the validated final JSON message.
    assert_eq!(
        std::fs::read_to_string(&out_file).expect("read -o"),
        r#"{"status":"ok","idle_pct":94.2}"#
    );

    // Verify AGENTS.md and @incident.log reached the server turn request!
    let turn_req_text = captured_turn_body.lock().expect("lock").clone();
    assert!(turn_req_text.contains("Always inspect CPU before restarting."));
    assert!(turn_req_text.contains("load average: 12.40"));

    // Verify client tool result was submitted with decided_by=rule!
    let tool_res_text = captured_tool_result.lock().expect("lock").clone();
    assert!(tool_res_text.contains("cpu_ok"));
    assert!(tool_res_text.contains(r#""by":"rule""#));

    // Verify audit event was uploaded to BOSS!
    let audit_text = captured_audit.lock().expect("lock").clone();
    assert!(audit_text.contains(r#""tool":"shell_local""#));
    assert!(audit_text.contains(r#""decided_by":"rule""#));
}
