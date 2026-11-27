//! Built-in Metatron operator tools (`MT-4`, `MT-5`, `MT-6`, `MT-9`):
//! - `transfer` (scp download into evidence dir; upload gated behind hard approval)
//! - `pcap` (hard-prompted `tcpdump -w` with required filter and host-enforced caps)
//! - `pyrun` (OS-sandboxed Python analysis: zero network, evidence dir read-only, scratch writable)
//! - `dashboard` (self-contained local HTML + TUI sparklines)
//! - `boss_get_ticket` / `boss_post_finding` (ticket inspection and evidence attachment)
//! - `http_session` + `grafana_window_analysis` (policy-gated cookie/vault/MCP web access)
//! - `handoff_code_task` (governed BOSS code task creation; zero local code-edit tools!)
//! - `view_image`, `web_search`, `update_plan`

pub mod boss_ticket;
pub mod dashboard;
pub mod extras;
pub mod grafana;
pub mod handoff;
pub mod http_session;
pub mod pcap;
pub mod pyrun;
pub mod transfer;

use std::sync::Arc;

use metatron_core::ToolOrchestrator;

pub use boss_ticket::{BossGetTicketTool, BossPostFindingTool, collect_evidence_refs};
pub use dashboard::{
    DashboardTool, DataSeries, SeriesStats, load_series_from_text, render_self_contained_html,
    render_sparkline,
};
pub use extras::{UpdatePlanTool, ViewImageTool, WebSearchTool};
pub use grafana::{GrafanaAccessPath, GrafanaPoint, GrafanaRecipeTool, extract_grafana_points};
pub use handoff::HandoffCodeTaskTool;
pub use http_session::{COOKIE_REVOCATION_NOTE, HttpSessionConfig, HttpSessionTool};
pub use pcap::{PcapCaptureSpec, PcapTool, build_tcpdump_argv};
pub use pyrun::PyrunTool;
pub use transfer::{TransferTool, sha256_file};

/// Register the complete Wave D built-in tool set onto `orchestrator`.
/// Enforces the `MT-5` hard invariant: ZERO local file-edit or patch tools are ever registered.
pub fn register_default_tools(
    orchestrator: &mut ToolOrchestrator,
    http_cfg: Arc<HttpSessionConfig>,
) {
    let http_tool = Arc::new(HttpSessionTool::new(http_cfg));
    orchestrator.register_tool(Arc::new(TransferTool));
    orchestrator.register_tool(Arc::new(PcapTool));
    orchestrator.register_tool(Arc::new(PyrunTool::default()));
    orchestrator.register_tool(Arc::new(DashboardTool));
    orchestrator.register_tool(Arc::new(BossGetTicketTool));
    orchestrator.register_tool(Arc::new(BossPostFindingTool));
    orchestrator.register_tool(http_tool.clone());
    orchestrator.register_tool(Arc::new(GrafanaRecipeTool::new(http_tool)));
    orchestrator.register_tool(Arc::new(HandoffCodeTaskTool));
    orchestrator.register_tool(Arc::new(ViewImageTool));
    orchestrator.register_tool(Arc::new(WebSearchTool));
    orchestrator.register_tool(Arc::new(UpdatePlanTool));
}

#[cfg(test)]
#[allow(clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use chrono::{Duration, Utc};
    use ed25519_dalek::SigningKey;
    use metatron_audit::AuditBuffer;
    use metatron_boss_client::{AuthMethod, BossClient, BossClientConfig};
    use metatron_core::{
        ApprovalPromptRequest, ApprovalPrompter, HumanApproval, Tool, ToolCtx, ToolOutcome,
    };
    use metatron_policy::{
        GrantStore, LocalPolicyConfig, PolicyEvaluator, sign_policy_bundle_ed25519,
        verify_signed_policy_bundle,
    };
    use metatron_protocol::{
        ApprovalMode, ClientToolSpec, CookieModePolicy, PolicyBundle, PolicyDecision, PolicyRule,
        PolicyRuleExamples, PolicyRuleMatch, RiskClass, ToolExposure, ToolResultStatus,
    };
    use metatron_remote::RemoteRunner;
    use metatron_sandbox::{SandboxBackend, detect_backend};
    use metatron_session::SessionStore;
    use rand::rngs::OsRng;
    use serde_json::{Value, json};
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tempfile::TempDir;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    struct AlwaysAllowPrompter {
        calls: AtomicUsize,
        prompts: Mutex<Vec<ApprovalPromptRequest>>,
    }

    impl AlwaysAllowPrompter {
        fn new() -> Self {
            Self {
                calls: AtomicUsize::new(0),
                prompts: Mutex::new(Vec::new()),
            }
        }
    }

    #[async_trait]
    impl ApprovalPrompter for AlwaysAllowPrompter {
        async fn prompt(&self, req: &ApprovalPromptRequest) -> HumanApproval {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.prompts.lock().unwrap().push(req.clone());
            HumanApproval::AllowOnce
        }
    }

    struct RogueApplyPatchTool;

    #[async_trait]
    impl Tool for RogueApplyPatchTool {
        fn name(&self) -> &str {
            "apply_patch"
        }

        fn spec(&self) -> ClientToolSpec {
            ClientToolSpec {
                name: "apply_patch".to_string(),
                description: "Apply a patch to local source files".to_string(),
                input_schema: json!({"type": "object", "properties": {"patch": {"type": "string"}}}),
                risk_class: "write-local".to_string(),
                exposure: ToolExposure::Direct,
            }
        }

        fn risk(&self, _args: &Value) -> RiskClass {
            RiskClass::WriteLocal
        }

        async fn run(&self, _ctx: &ToolCtx, _args: Value) -> ToolOutcome {
            unreachable!()
        }
    }

    fn signed_eval(
        mode: ApprovalMode,
        tools_allowed: Vec<&str>,
        rules: Vec<PolicyRule>,
        cookie_mode: CookieModePolicy,
    ) -> PolicyEvaluator {
        let now = Utc::now();
        let sk = SigningKey::generate(&mut OsRng);
        let bundle = PolicyBundle {
            version: "v-wave-d".to_string(),
            project_id: Some("boss-dogfood".to_string()),
            issued_at: Some(now),
            expires_at: now + Duration::hours(1),
            mode_floor: mode,
            tools_allowed: tools_allowed.into_iter().map(str::to_string).collect(),
            rules,
            cookie_mode,
            host_diagnostics: None,
            mcp: None,
        };
        let (signed, jwks) = sign_policy_bundle_ed25519(&bundle, "kid-wd", &sk).expect("sign");
        let verified = verify_signed_policy_bundle(&signed, &jwks, now).expect("verify");
        PolicyEvaluator::with_verified_bundle(
            verified,
            mode,
            LocalPolicyConfig::default(),
            GrantStore::default(),
        )
    }

    /// MT-5 HARD INVARIANT TEST:
    /// Scans the complete registered tool list and FAILS if ANY tool can edit/patch source files
    /// outside scratch. Also verifies that registering a rogue `apply_patch` tool trips the scan!
    #[test]
    fn registered_tools_never_include_local_code_edit_or_patch_capability() {
        let tmp = TempDir::new().expect("tmp");
        let audit = AuditBuffer::new(tmp.path().join("audit.jsonl")).expect("audit");
        let prompter = Arc::new(AlwaysAllowPrompter::new());
        let eval = signed_eval(
            ApprovalMode::RiskyOnly,
            vec!["*"],
            Vec::new(),
            CookieModePolicy::default(),
        );
        let mut orch = ToolOrchestrator::new(eval, prompter.clone(), None, audit, None);
        let http_cfg = Arc::new(HttpSessionConfig::new(
            CookieModePolicy::default(),
            prompter,
        ));
        register_default_tools(&mut orch, http_cfg);

        let names = orch.registry().all_names();
        assert!(names.contains(&"handoff_code_task".to_string()));
        assert!(names.contains(&"pyrun".to_string()));
        assert!(names.contains(&"pcap".to_string()));
        assert!(names.contains(&"http_session".to_string()));

        let violations = orch.registry().scan_for_forbidden_code_edit_tools();
        assert!(
            violations.is_empty(),
            "forbidden code-editing tools found in Metatron registry: {violations:?}"
        );

        // Negative verification: adding a rogue patch tool MUST be caught by the scanner!
        orch.register_tool(Arc::new(RogueApplyPatchTool));
        let rogue_violations = orch.registry().scan_for_forbidden_code_edit_tools();
        assert!(
            !rogue_violations.is_empty(),
            "scanner failed to detect rogue apply_patch tool!"
        );
    }

    /// MT-4 + MT-5 END-TO-END INVESTIGATION SCENARIO:
    /// sar -> pcap (hard prompt!) -> transfer -> sandboxed pyrun -> self-contained HTML dashboard ->
    /// post finding to ticket -> handoff_code_task (approval -> created + live status).
    #[tokio::test]
    async fn mt4_and_mt5_end_to_end_investigation_to_ticket_and_code_task_handoff() {
        if detect_backend() == SandboxBackend::Unavailable {
            return;
        }
        let tmp = TempDir::new().expect("tmp");
        let evidence = tmp.path().join("evidence");
        let scratch = tmp.path().join("scratch");
        std::fs::create_dir_all(&evidence).expect("mkdir");
        std::fs::create_dir_all(&scratch).expect("mkdir");

        // Fake BOSS server handling ticket finding post + code-task creation/status.
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        let posted_findings = Arc::new(Mutex::new(String::new()));
        let created_code_task = Arc::new(Mutex::new(String::new()));
        let pf_srv = Arc::clone(&posted_findings);
        let ct_srv = Arc::clone(&created_code_task);
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    break;
                };
                let pf = Arc::clone(&pf_srv);
                let ct = Arc::clone(&ct_srv);
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 65536];
                    let n = sock.read(&mut buf).await.unwrap_or(0);
                    let req = String::from_utf8_lossy(&buf[..n]).to_string();
                    let first = req.lines().next().unwrap_or("");
                    let body = req.split("\r\n\r\n").nth(1).unwrap_or("").to_string();
                    let resp_body = if first.starts_with("POST /v1/tasks/T-123/findings ") {
                        *pf.lock().unwrap() = body;
                        json!({"ticket_id": "T-123", "comment_id": "cmt_99", "status": "posted", "attached_count": 2}).to_string()
                    } else if first.starts_with("POST /v1/code-tasks ") {
                        *ct.lock().unwrap() = body;
                        json!({"run_id": "crun_mt5_1", "task_id": "task_mt5_1", "status": "queued", "url": "http://boss.local/runs/crun_mt5_1"}).to_string()
                    } else if first.starts_with("GET /v1/code-tasks/crun_mt5_1 ") {
                        json!({"run_id": "crun_mt5_1", "task_id": "task_mt5_1", "status": "running", "url": "http://boss.local/runs/crun_mt5_1", "message": "build in progress"}).to_string()
                    } else {
                        json!({"ok": true}).to_string()
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

        let ssh_cfg = tmp.path().join("ssh_config");
        std::fs::write(&ssh_cfg, "Host web-3\n  HostName 10.0.0.3\n  User ops\n").expect("cfg");
        let fake_ssh = tmp.path().join("fake_ssh.sh");
        std::fs::write(&fake_ssh, "#!/bin/sh\necho 'sar_ok cpu_iowait=48.2'\n").expect("ssh");
        let fake_scp = tmp.path().join("fake_scp.sh");
        std::fs::write(
            &fake_scp,
            "#!/bin/sh\nfor a in \"$@\"; do LAST=\"$a\"; done\nprintf 'ts,cpu,retrans\\n10:00,14.0,0\\n10:01,93.5,42\\n10:02,21.0,1\\n' > \"$LAST\"\n",
        )
        .expect("scp");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&fake_ssh, std::fs::Permissions::from_mode(0o755))
                .expect("chmod");
            std::fs::set_permissions(&fake_scp, std::fs::Permissions::from_mode(0o755))
                .expect("chmod");
        }

        let eval = signed_eval(
            ApprovalMode::RiskyOnly,
            vec!["*"],
            vec![
                PolicyRule {
                    id: "r-pyrun".to_string(),
                    scope: None,
                    priority: 10,
                    matcher: PolicyRuleMatch {
                        tool: Some("pyrun".to_string()),
                        ..Default::default()
                    },
                    decision: PolicyDecision::Allow,
                    hard: false,
                    justification: "sandboxed pyrun allowed".to_string(),
                    examples: PolicyRuleExamples::default(),
                },
                PolicyRule {
                    id: "r-dashboard".to_string(),
                    scope: None,
                    priority: 10,
                    matcher: PolicyRuleMatch {
                        tool: Some("dashboard".to_string()),
                        ..Default::default()
                    },
                    decision: PolicyDecision::Allow,
                    hard: false,
                    justification: "local dashboard allowed".to_string(),
                    examples: PolicyRuleExamples::default(),
                },
            ],
            CookieModePolicy::default(),
        );

        let prompter = Arc::new(AlwaysAllowPrompter::new());
        let audit = AuditBuffer::new(tmp.path().join("audit.jsonl")).expect("audit");
        let sess_store = SessionStore::new(tmp.path().join("sessions")).expect("sess");
        let (sid, _) = sess_store
            .create("boss-dogfood", None, None, ApprovalMode::RiskyOnly)
            .expect("create sess");

        let mut orch = ToolOrchestrator::new(eval, prompter.clone(), None, audit, Some(sess_store));
        let http_cfg = Arc::new(HttpSessionConfig::new(
            CookieModePolicy::default(),
            prompter.clone(),
        ));
        register_default_tools(&mut orch, http_cfg);

        let client = BossClient::new(
            BossClientConfig::new(
                &format!("http://{addr}"),
                AuthMethod::Bearer("t".to_string()),
            )
            .expect("cfg"),
        )
        .expect("client");

        let ctx = ToolCtx {
            project_id: "boss-dogfood".to_string(),
            session_id: sid,
            cwd: tmp.path().to_path_buf(),
            scratch_dir: scratch.clone(),
            evidence_dir: evidence.clone(),
            allowed_read_roots: vec![evidence.clone()],
            allow_unsandboxed_retry: false,
            boss_client: Some(client),
            remote_runner: RemoteRunner {
                ssh_binary: fake_ssh.to_string_lossy().into_owned(),
                scp_binary: fake_scp.to_string_lossy().into_owned(),
                lease_ttl_s: 60,
                timeout: std::time::Duration::from_secs(10),
                ssh_config_path: Some(ssh_cfg),
            },
            ticket_id: Some("T-123".to_string()),
            mcp_client: None,
            hooks: None,
            cancel_rx: None,
        };

        // 1. sar on web-3 (auto-allowed by MT-1 rule)
        let r_sar = orch
            .execute_tool_call(
                &ctx,
                None,
                "c1",
                "ssh_run",
                json!({"host": "web-3", "argv": ["sar", "-u", "1", "5"]}),
            )
            .await
            .expect("sar");
        assert_eq!(r_sar.status, ToolResultStatus::Ok);
        assert_eq!(prompter.calls.load(Ordering::SeqCst), 0);

        // 2. pcap on web-3 (ALWAYS prompts human — hard capture class!)
        let r_pcap = orch
            .execute_tool_call(
                &ctx,
                None,
                "c2",
                "pcap",
                json!({"host": "web-3", "filter": "tcp port 443", "duration_s": 10, "max_packets": 100, "local_name": "web3.pcap"}),
            )
            .await
            .expect("pcap");
        assert_eq!(r_pcap.status, ToolResultStatus::Ok);
        assert_eq!(prompter.calls.load(Ordering::SeqCst), 1);
        assert!(prompter.prompts.lock().unwrap()[0].hard);
        assert!(evidence.join("web3.pcap").is_file());

        // 3. transfer sar CSV into evidence dir
        let r_xfer = orch
            .execute_tool_call(
                &ctx,
                None,
                "c3",
                "transfer",
                json!({"direction": "download", "host": "web-3", "remote_path": "/var/log/sar.csv", "local_name": "sar_web3.csv"}),
            )
            .await
            .expect("transfer");
        assert_eq!(r_xfer.status, ToolResultStatus::Ok);
        assert!(evidence.join("sar_web3.csv").is_file());

        // 4. pyrun sandboxed analysis (auto-allowed inside sandbox)
        let r_py = orch
            .execute_tool_call(
                &ctx,
                None,
                "c4",
                "pyrun",
                json!({"code": "import csv, os, pathlib\nrows = list(csv.DictReader((pathlib.Path(os.environ['METATRON_EVIDENCE_DIR']) / 'sar_web3.csv').open()))\nprint('MAX_RETRANS=' + str(max(int(r['retrans']) for r in rows)))\n"}),
            )
            .await
            .expect("pyrun");
        assert_eq!(r_py.status, ToolResultStatus::Ok);

        // 5. dashboard (self-contained HTML + TUI sparklines)
        let r_dash = orch
            .execute_tool_call(
                &ctx,
                None,
                "c5",
                "dashboard",
                json!({"data_file": "sar_web3.csv", "title": "web-3 Incident", "output_name": "web3.html"}),
            )
            .await
            .expect("dashboard");
        assert_eq!(r_dash.status, ToolResultStatus::Ok);
        let html = std::fs::read_to_string(scratch.join("web3.html")).expect("html");
        assert!(!html.contains("<script"));

        // 6. post finding to ticket T-123
        let r_post = orch
            .execute_tool_call(
                &ctx,
                None,
                "c6",
                "boss_post_finding",
                json!({
                    "ticket_id": "T-123",
                    "title": "TCP retransmission spike on web-3",
                    "summary": "Peak CPU 93.5% and 42 TCP retransmissions at 10:01.",
                    "evidence_files": ["sar_web3.csv", "web3.html"]
                }),
            )
            .await
            .expect("post finding");
        assert_eq!(r_post.status, ToolResultStatus::Ok);
        assert!(posted_findings.lock().unwrap().contains("sar_web3.csv"));

        // 7. handoff_code_task (asks approval -> creates governed code task -> shows running status!)
        let r_ho = orch
            .execute_tool_call(
                &ctx,
                None,
                "c7",
                "handoff_code_task",
                json!({
                    "title": "Fix TCP retry storm in worker",
                    "instructions": "Add jittered exponential backoff to worker HTTP pool",
                    "findings": "42 retransmissions at 10:01 correlated with 93.5% CPU",
                    "evidence_files": ["sar_web3.csv", "web3.html"],
                    "ticket_id": "T-123"
                }),
            )
            .await
            .expect("handoff");
        assert_eq!(r_ho.status, ToolResultStatus::Ok);
        assert!(
            created_code_task
                .lock()
                .unwrap()
                .contains("Fix TCP retry storm")
        );
        let ho_text = match &r_ho.output.content[0] {
            metatron_protocol::ToolContentItem::Text { text } => text.clone(),
            _ => String::new(),
        };
        assert!(ho_text.contains("crun_mt5_1"));
        assert!(ho_text.contains("running"));

        // Every step produced an audit event!
        assert!(orch.audit().pending_count().expect("audit") >= 7);
    }

    /// MT-6 END-TO-END GRAFANA RECIPE:
    /// Explains "what happened between T1 and T2" via /api/dashboards/uid + /api/ds/query,
    /// analyzes with sandboxed pyrun citing panels & timestamps, and posts evidence to ticket!
    #[tokio::test]
    async fn mt6_grafana_window_recipe_cites_panels_timestamps_and_posts_ticket() {
        if detect_backend() == SandboxBackend::Unavailable {
            return;
        }
        let tmp = TempDir::new().expect("tmp");
        let evidence = tmp.path().join("evidence");
        let scratch = tmp.path().join("scratch");
        std::fs::create_dir_all(&evidence).expect("mkdir");
        std::fs::create_dir_all(&scratch).expect("mkdir");

        // Fake Grafana + BOSS server
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        let ticket_body = Arc::new(Mutex::new(String::new()));
        let tb_srv = Arc::clone(&ticket_body);
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    break;
                };
                let tb = Arc::clone(&tb_srv);
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 65536];
                    let n = sock.read(&mut buf).await.unwrap_or(0);
                    let req = String::from_utf8_lossy(&buf[..n]).to_string();
                    let first = req.lines().next().unwrap_or("");
                    let body = req.split("\r\n\r\n").nth(1).unwrap_or("").to_string();
                    let resp_body = if first.starts_with("GET /api/dashboards/uid/prod-api ") {
                        json!({
                            "dashboard": {
                                "title": "Prod API",
                                "panels": [{"title": "API p99 Latency (ms)", "targets": [{"refId": "A"}]}]
                            }
                        })
                        .to_string()
                    } else if first.starts_with("POST /api/ds/query ") {
                        json!({
                            "results": {
                                "A": {
                                    "frames": [{
                                        "schema": {"name": "http_request_duration_p99"},
                                        "data": {
                                            "values": [
                                                [1732700000000i64, 1732700060000i64, 1732700120000i64],
                                                [42.0, 912.5, 55.0]
                                            ]
                                        }
                                    }]
                                }
                            }
                        })
                        .to_string()
                    } else if first.starts_with("POST /v1/tasks/T-900/findings ") {
                        *tb.lock().unwrap() = body;
                        json!({"ticket_id": "T-900", "comment_id": "cmt_graf", "status": "posted", "attached_count": 2}).to_string()
                    } else {
                        json!({}).to_string()
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

        let cookie_file = tmp.path().join("grafana_cookies.txt");
        let planted = "GRAFANA_SUPER_SECRET_SESSION_COOKIE_VALUE";
        std::fs::write(
            &cookie_file,
            format!("127.0.0.1\tFALSE\t/\tFALSE\t0\tgrafana_session\t{planted}\n"),
        )
        .expect("write");

        let prompter = Arc::new(AlwaysAllowPrompter::new());
        let mut http_cfg = HttpSessionConfig::new(
            CookieModePolicy {
                enabled: true,
                domains: vec!["127.0.0.1".to_string()],
            },
            prompter,
        );
        http_cfg
            .allowed_private_hosts
            .insert("127.0.0.1".to_string());
        let http_tool = Arc::new(HttpSessionTool::new(Arc::new(http_cfg)));
        let grafana_tool = GrafanaRecipeTool::new(http_tool);

        let boss = BossClient::new(
            BossClientConfig::new(
                &format!("http://{addr}"),
                AuthMethod::Bearer("t".to_string()),
            )
            .expect("cfg"),
        )
        .expect("client");

        let ctx = ToolCtx {
            project_id: "boss-dogfood".to_string(),
            session_id: "msess_graf".to_string(),
            cwd: tmp.path().to_path_buf(),
            scratch_dir: scratch,
            evidence_dir: evidence,
            allowed_read_roots: vec![tmp.path().to_path_buf()],
            allow_unsandboxed_retry: false,
            boss_client: Some(boss),
            remote_runner: RemoteRunner::default(),
            ticket_id: Some("T-900".to_string()),
            mcp_client: None,
            hooks: None,
            cancel_rx: None,
        };

        let out = grafana_tool
            .run(
                &ctx,
                json!({
                    "base_url": format!("http://{addr}"),
                    "dashboard_uid": "prod-api",
                    "from_ms": 1732700000000i64,
                    "to_ms": 1732700120000i64,
                    "auth_mode": "cookie_file",
                    "cookie_file": cookie_file.to_string_lossy(),
                    "ticket_id": "T-900"
                }),
            )
            .await;

        assert_eq!(
            out.status,
            ToolResultStatus::Ok,
            "grafana failed: {}",
            out.inline_text
        );
        assert!(out.inline_text.contains("API p99 Latency (ms)"));
        assert!(out.inline_text.contains("http_request_duration_p99"));
        assert!(out.inline_text.contains("912.50"));
        assert!(out.inline_text.contains("2024-11-27T"));
        // Cookie value NEVER appears in analysis or ticket!
        assert!(!out.inline_text.contains(planted));
        let posted = ticket_body.lock().unwrap().clone();
        assert!(posted.contains("API p99 Latency (ms)"));
        assert!(!posted.contains(planted));
    }

    /// MT-9 WORKED EXAMPLE (`ARCHITECTURE.md` §8):
    /// Trim a video with ffmpeg in risky-only mode:
    /// 1. ffprobe -> auto-allowed ReadLocal
    /// 2. ffmpeg to scratch -> auto-allowed WriteScratch
    /// 3. view_image(scratch/check.png) -> auto-allowed ReadLocal + returns ToolContentItem::Image
    /// 4. cp scratch/demo_cut.mp4 out/demo_cut.mp4 -> WriteLocal prompts human
    /// 5. osascript -> AppControl ALWAYS hard-prompts human (never allow_always!)
    #[tokio::test]
    async fn mt9_worked_video_edit_example_follows_documented_decisions_in_risky_only_mode() {
        let tmp = TempDir::new().expect("tmp");
        let scratch = tmp.path().join("scratch");
        let out_dir = tmp.path().join("movies");
        std::fs::create_dir_all(&scratch).expect("mkdir");
        std::fs::create_dir_all(&out_dir).expect("mkdir");

        // Write a tiny valid PNG header in scratch/check.png for `view_image`.
        let png_bytes: &[u8] = &[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0];
        std::fs::write(scratch.join("check.png"), png_bytes).expect("write png");

        let eval = signed_eval(
            ApprovalMode::RiskyOnly,
            vec!["*"],
            vec![PolicyRule {
                id: "r-view-image".to_string(),
                scope: None,
                priority: 10,
                matcher: PolicyRuleMatch {
                    tool: Some("view_image".to_string()),
                    ..Default::default()
                },
                decision: PolicyDecision::Allow,
                hard: false,
                justification: "scratch view_image auto".to_string(),
                examples: PolicyRuleExamples::default(),
            }],
            CookieModePolicy::default(),
        );
        let prompter = Arc::new(AlwaysAllowPrompter::new());
        let audit = AuditBuffer::new(tmp.path().join("audit.jsonl")).expect("audit");
        let mut orch = ToolOrchestrator::new(eval, prompter.clone(), None, audit, None);
        let http_cfg = Arc::new(HttpSessionConfig::new(
            CookieModePolicy::default(),
            prompter.clone(),
        ));
        register_default_tools(&mut orch, http_cfg);

        // Policy decisions for every step of ARCHITECTURE.md §8 worked example:
        let now = Utc::now();
        // Step 1: ffprobe -> AutoAllow
        let d1 = orch.evaluator().evaluate(
            &metatron_policy::ToolCallContext {
                tool: "shell_local".to_string(),
                argv: ["ffprobe", "-v", "error", "-show_format", "demo.mov"]
                    .map(str::to_string)
                    .to_vec(),
                host: None,
                domain: None,
                risk_class: RiskClass::ReadLocal,
                is_remote: false,
            },
            now,
        );
        assert!(matches!(
            d1,
            metatron_policy::EvaluationDecision::AutoAllow { .. }
        ));

        // Step 3: ffmpeg writing strictly to scratch -> AutoAllow
        let d3 = orch.evaluator().evaluate(
            &metatron_policy::ToolCallContext {
                tool: "shell_local".to_string(),
                argv: [
                    "ffmpeg",
                    "-ss",
                    "12",
                    "-to",
                    "100",
                    "-i",
                    "demo.mov",
                    "scratch/demo_cut.mp4",
                ]
                .map(str::to_string)
                .to_vec(),
                host: None,
                domain: None,
                risk_class: RiskClass::WriteScratch,
                is_remote: false,
            },
            now,
        );
        assert!(matches!(
            d3,
            metatron_policy::EvaluationDecision::AutoAllow { .. }
        ));

        // Step 4: view_image(check.png) executes auto-allowed and returns Image item!
        let ctx = ToolCtx {
            project_id: "boss-dogfood".to_string(),
            session_id: "msess_vid".to_string(),
            cwd: tmp.path().to_path_buf(),
            scratch_dir: scratch.clone(),
            evidence_dir: scratch.clone(),
            allowed_read_roots: vec![tmp.path().to_path_buf()],
            allow_unsandboxed_retry: false,
            boss_client: None,
            remote_runner: RemoteRunner::default(),
            ticket_id: None,
            mcp_client: None,
            hooks: None,
            cancel_rx: None,
        };
        let r4 = orch
            .execute_tool_call(&ctx, None, "c4", "view_image", json!({"path": "check.png"}))
            .await
            .expect("view_image");
        assert_eq!(r4.status, ToolResultStatus::Ok);
        assert!(r4.output.content.iter().any(|c| matches!(
            c,
            metatron_protocol::ToolContentItem::Image { media_type, .. } if media_type == "image/png"
        )));
        assert_eq!(prompter.calls.load(Ordering::SeqCst), 0);

        // Step 5: cp scratch/demo_cut.mp4 movies/demo_cut.mp4 -> AskHuman (WriteLocal)
        let d5 = orch.evaluator().evaluate(
            &metatron_policy::ToolCallContext {
                tool: "shell_local".to_string(),
                argv: ["cp", "scratch/demo_cut.mp4", "movies/demo_cut.mp4"]
                    .map(str::to_string)
                    .to_vec(),
                host: None,
                domain: None,
                risk_class: RiskClass::WriteLocal,
                is_remote: false,
            },
            now,
        );
        assert!(matches!(
            d5,
            metatron_policy::EvaluationDecision::AskHuman { hard: false, .. }
        ));

        // Step 6: osascript to open in QuickTime -> AskHuman HARD (allow_always: false!)
        let d6 = orch.evaluator().evaluate(
            &metatron_policy::ToolCallContext {
                tool: "shell_local".to_string(),
                argv: [
                    "osascript",
                    "-e",
                    "tell application \"QuickTime Player\" to open POSIX file \"/tmp/demo.mp4\"",
                ]
                .map(str::to_string)
                .to_vec(),
                host: None,
                domain: None,
                risk_class: RiskClass::AppControl,
                is_remote: false,
            },
            now,
        );
        assert!(matches!(
            d6,
            metatron_policy::EvaluationDecision::AskHuman {
                hard: true,
                allow_always: false,
                ..
            }
        ));
    }
}
