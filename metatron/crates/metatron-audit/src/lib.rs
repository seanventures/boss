//! Disk-buffered audit queue and redacted idempotent batch upload (`MT-3`).
//!
//! Every executed or denied command is persisted to the local JSONL queue BEFORE
//! upload so network interruptions never drop audit history. Rejected events are
//! moved to a visible dead-letter JSONL file (never silently ignored).

use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use chrono::Utc;
use metatron_boss_client::{BossClient, BossClientError};
use metatron_protocol::{AuditBatchRequest, AuditEvent, DecisionSource, RiskClass};
use metatron_secrets::{redact_argv, redact_secrets};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, Error)]
pub enum AuditError {
    #[error("audit queue IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("audit JSON error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("audit upload failed: {0}")]
    Upload(#[from] BossClientError),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct QueuedAuditEntry {
    project_id: String,
    session_id: String,
    event: AuditEvent,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct DeadLetterEntry {
    project_id: String,
    session_id: String,
    event_id: String,
    reason: String,
    event: AuditEvent,
}

/// Summary returned by `AuditBuffer::flush`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FlushReport {
    pub accepted: usize,
    pub deadlettered: usize,
    pub remaining: usize,
}

/// Append-only disk-backed audit queue with secret redaction and batch upload.
#[derive(Debug, Clone)]
pub struct AuditBuffer {
    queue_path: PathBuf,
    deadletter_path: PathBuf,
}

impl AuditBuffer {
    pub fn new(queue_path: PathBuf) -> Result<Self, AuditError> {
        if let Some(parent) = queue_path.parent() {
            fs::create_dir_all(parent)?;
        }
        let deadletter_path = queue_path.with_extension("deadletter.jsonl");
        Ok(Self {
            queue_path,
            deadletter_path,
        })
    }

    pub fn queue_path(&self) -> &Path {
        &self.queue_path
    }

    pub fn deadletter_path(&self) -> &Path {
        &self.deadletter_path
    }

    /// Sanitize/redact `event` and append it to the local disk queue.
    pub fn record(
        &self,
        project_id: &str,
        session_id: &str,
        mut event: AuditEvent,
    ) -> Result<String, AuditError> {
        if event.event_id.trim().is_empty() {
            event.event_id = Uuid::new_v4().to_string();
        }
        event.argv_redacted = redact_argv(&event.argv_redacted);
        event.decision = redact_secrets(&event.decision);
        if let Some(ref s) = event.status {
            event.status = Some(redact_secrets(s));
        }

        let entry = QueuedAuditEntry {
            project_id: project_id.to_string(),
            session_id: session_id.to_string(),
            event: event.clone(),
        };
        let line = serde_json::to_string(&entry)?;
        let mut f = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.queue_path)?;
        writeln!(f, "{line}")?;
        f.sync_all()?;
        Ok(event.event_id)
    }

    /// Convenience helper to construct a redacted `AuditEvent` with a fresh UUID.
    #[allow(clippy::too_many_arguments)]
    pub fn build_event(
        kind: &str,
        tool: &str,
        risk_class: RiskClass,
        argv: &[String],
        host: Option<String>,
        host_origin: Option<String>,
        domain: Option<String>,
        status: Option<String>,
        decision: &str,
        decided_by: DecisionSource,
        rule_id: Option<String>,
        exit_code: Option<i32>,
        bytes: usize,
        duration_ms: u64,
    ) -> AuditEvent {
        AuditEvent {
            event_id: Uuid::new_v4().to_string(),
            ts: Utc::now(),
            kind: kind.to_string(),
            tool: tool.to_string(),
            risk_class,
            argv_redacted: redact_argv(argv),
            host,
            host_origin,
            domain,
            status,
            decision: redact_secrets(decision),
            decided_by,
            rule_id,
            exit_code,
            bytes,
            duration_ms,
            ticket_id: None,
        }
    }

    pub fn pending_count(&self) -> Result<usize, AuditError> {
        Ok(self.load_queue()?.len())
    }

    /// Upload all buffered entries in `(project_id, session_id)` batches via `BossClient`.
    pub async fn flush(&self, client: &BossClient) -> Result<FlushReport, AuditError> {
        let entries = self.load_queue()?;
        if entries.is_empty() {
            return Ok(FlushReport::default());
        }

        let mut report = FlushReport::default();
        let mut remaining: Vec<QueuedAuditEntry> = Vec::new();

        // Group entries by (project_id, session_id) preserving order.
        let mut groups: Vec<(String, String, Vec<QueuedAuditEntry>)> = Vec::new();
        for entry in entries {
            if let Some(group) = groups
                .iter_mut()
                .find(|(p, s, _)| p == &entry.project_id && s == &entry.session_id)
            {
                group.2.push(entry);
            } else {
                groups.push((
                    entry.project_id.clone(),
                    entry.session_id.clone(),
                    vec![entry],
                ));
            }
        }

        for (project_id, session_id, group_entries) in groups {
            let req = AuditBatchRequest {
                project_id: project_id.clone(),
                session_id: session_id.clone(),
                events: group_entries.iter().map(|e| e.event.clone()).collect(),
            };
            match client.upload_audit_batch(&req).await {
                Ok(resp) => {
                    let accepted_ids: HashSet<String> = resp.accepted.into_iter().collect();
                    let rejected_map: std::collections::HashMap<String, String> = resp
                        .rejected
                        .into_iter()
                        .map(|r| (r.event_id, r.reason))
                        .collect();

                    for entry in group_entries {
                        if accepted_ids.contains(&entry.event.event_id) {
                            report.accepted += 1;
                        } else if let Some(reason) = rejected_map.get(&entry.event.event_id) {
                            self.append_deadletter(&DeadLetterEntry {
                                project_id: entry.project_id,
                                session_id: entry.session_id,
                                event_id: entry.event.event_id.clone(),
                                reason: reason.clone(),
                                event: entry.event,
                            })?;
                            report.deadlettered += 1;
                        } else {
                            // Unacknowledged event stays in queue for retry.
                            remaining.push(entry);
                        }
                    }
                }
                Err(_) => {
                    // Retain unsent entries on disk for replay next flush.
                    remaining.extend(group_entries);
                }
            }
        }

        report.remaining = remaining.len();
        self.rewrite_queue(&remaining)?;
        Ok(report)
    }

    fn load_queue(&self) -> Result<Vec<QueuedAuditEntry>, AuditError> {
        if !self.queue_path.exists() {
            return Ok(Vec::new());
        }
        let f = fs::File::open(&self.queue_path)?;
        let reader = BufReader::new(f);
        let mut out = Vec::new();
        for line in reader.lines() {
            let line = line?;
            if line.trim().is_empty() {
                continue;
            }
            out.push(serde_json::from_str(&line)?);
        }
        Ok(out)
    }

    fn rewrite_queue(&self, entries: &[QueuedAuditEntry]) -> Result<(), AuditError> {
        let mut f = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&self.queue_path)?;
        for entry in entries {
            let line = serde_json::to_string(entry)?;
            writeln!(f, "{line}")?;
        }
        f.sync_all()?;
        Ok(())
    }

    fn append_deadletter(&self, entry: &DeadLetterEntry) -> Result<(), AuditError> {
        let line = serde_json::to_string(entry)?;
        let mut f = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.deadletter_path)?;
        writeln!(f, "{line}")?;
        f.sync_all()?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use metatron_boss_client::{AuthMethod, BossClientConfig};
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tempfile::TempDir;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    #[tokio::test]
    async fn buffers_redacts_replays_after_error_and_deadletters_rejections() {
        let tmp = TempDir::new().expect("tmp");
        let buf = AuditBuffer::new(tmp.path().join("audit.jsonl")).expect("buf");

        let ev1 = AuditBuffer::build_event(
            "ssh_run",
            "ssh_run",
            RiskClass::RemoteRead,
            &[
                "curl".to_string(),
                "--token".to_string(),
                "boss_pat_supersecret999".to_string(),
            ],
            Some("web-1".to_string()),
            Some("boss_vault".to_string()),
            None,
            Some("ok".to_string()),
            "allow",
            DecisionSource::Rule,
            Some("r-sar".to_string()),
            Some(0),
            128,
            42,
        );
        let ev2 = AuditBuffer::build_event(
            "ssh_run",
            "ssh_run",
            RiskClass::RemoteRead,
            &[
                "sar".to_string(),
                "-u".to_string(),
                "1".to_string(),
                "5".to_string(),
            ],
            Some("web-1".to_string()),
            Some("boss_vault".to_string()),
            None,
            Some("ok".to_string()),
            "allow",
            DecisionSource::Rule,
            Some("r-sar".to_string()),
            Some(0),
            256,
            55,
        );

        let id1 = buf.record("boss-dogfood", "msess_1", ev1).expect("rec1");
        let id2 = buf.record("boss-dogfood", "msess_1", ev2).expect("rec2");
        assert_eq!(buf.pending_count().expect("count"), 2);

        // Secret was redacted on disk before upload.
        let disk_text = fs::read_to_string(buf.queue_path()).expect("read");
        assert!(!disk_text.contains("boss_pat_supersecret999"));

        // Fake server: 1st request returns 503 (transient failure -> retained on disk);
        // 2nd request accepts id2 and rejects id1 (moves id1 to deadletter).
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        let hits = Arc::new(AtomicUsize::new(0));
        let id1_srv = id1.clone();
        let id2_srv = id2.clone();
        let hits_srv = Arc::clone(&hits);
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    break;
                };
                let n = hits_srv.fetch_add(1, Ordering::SeqCst);
                let i1 = id1_srv.clone();
                let i2 = id2_srv.clone();
                tokio::spawn(async move {
                    let mut b = vec![0u8; 16384];
                    let _ = sock.read(&mut b).await;
                    let resp = if n == 0 {
                        "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n".to_string()
                    } else {
                        let body = format!(
                            r#"{{"accepted":["{i2}"],"rejected":[{{"event_id":"{i1}","reason":"invalid_field"}}]}}"#
                        );
                        format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                            body.len(),
                            body
                        )
                    };
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

        // 1st flush hits 503 -> both retained for replay.
        let rep1 = buf.flush(&client).await.expect("flush 1");
        assert_eq!(rep1.accepted, 0);
        assert_eq!(rep1.remaining, 2);

        // 2nd flush replays -> 1 accepted, 1 deadlettered, 0 remaining.
        let rep2 = buf.flush(&client).await.expect("flush 2");
        assert_eq!(rep2.accepted, 1);
        assert_eq!(rep2.deadlettered, 1);
        assert_eq!(rep2.remaining, 0);
        assert!(buf.deadletter_path().exists());
        let dl = fs::read_to_string(buf.deadletter_path()).expect("read dl");
        assert!(dl.contains(&id1));
    }
}
