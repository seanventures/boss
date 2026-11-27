//! Local session JSONL persistence, `/resume`, `/fork`, `/compact`, `/copy`, `/export`,
//! and credential-filtered command history (`MT-2`).

use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use metatron_protocol::{ApprovalMode, DecisionSource};
use metatron_secrets::{is_credential_history_line, redact_secrets};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, Error)]
pub enum SessionError {
    #[error("session IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("session JSON error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("session '{0}' not found")]
    NotFound(String),
}

/// Typed items recorded in `rollout-<timestamp>-<session_id>.jsonl`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum SessionItem {
    Meta {
        session_id: String,
        project_id: String,
        #[serde(default)]
        thread_id: Option<String>,
        #[serde(default)]
        agent: Option<String>,
        mode: ApprovalMode,
        #[serde(default)]
        parent_session_id: Option<String>,
        created_at: DateTime<Utc>,
    },
    UserMessage {
        ts: DateTime<Utc>,
        text: String,
    },
    AssistantMessage {
        ts: DateTime<Utc>,
        #[serde(default)]
        turn_id: Option<String>,
        text: String,
    },
    ToolCall {
        ts: DateTime<Utc>,
        call_id: String,
        tool: String,
        argv_redacted: Vec<String>,
        #[serde(default)]
        host: Option<String>,
    },
    ToolResult {
        ts: DateTime<Utc>,
        call_id: String,
        status: String,
        full_output: String,
        #[serde(default)]
        exit_code: Option<i32>,
        truncated_for_model: bool,
    },
    ApprovalDecision {
        ts: DateTime<Utc>,
        call_id: String,
        tool: String,
        decision: String,
        decided_by: DecisionSource,
        #[serde(default)]
        rule_id: Option<String>,
    },
    CompactionSummary {
        ts: DateTime<Utc>,
        compacted_items: usize,
        summary: String,
    },
}

/// Lightweight summary returned by `list_sessions()` for the `/resume` picker.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionSummary {
    pub session_id: String,
    pub path: PathBuf,
    pub project_id: String,
    pub thread_id: Option<String>,
    pub agent: Option<String>,
    pub created_at: DateTime<Utc>,
    pub item_count: usize,
    pub preview: String,
}

/// Manages local JSONL rollout files in a directory.
#[derive(Debug, Clone)]
pub struct SessionStore {
    root: PathBuf,
}

impl SessionStore {
    pub fn new(root: PathBuf) -> Result<Self, SessionError> {
        fs::create_dir_all(&root)?;
        Ok(Self { root })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn create(
        &self,
        project_id: &str,
        thread_id: Option<String>,
        agent: Option<String>,
        mode: ApprovalMode,
    ) -> Result<(String, PathBuf), SessionError> {
        let session_id = format!("msess_{}", Uuid::new_v4().simple());
        let now = Utc::now();
        let filename = format!(
            "rollout-{}-{session_id}.jsonl",
            now.format("%Y%m%dT%H%M%SZ")
        );
        let path = self.root.join(filename);
        let meta = SessionItem::Meta {
            session_id: session_id.clone(),
            project_id: project_id.to_string(),
            thread_id,
            agent,
            mode,
            parent_session_id: None,
            created_at: now,
        };
        self.append_path(&path, &meta)?;
        Ok((session_id, path))
    }

    pub fn find_path(&self, session_id: &str) -> Result<PathBuf, SessionError> {
        for entry in fs::read_dir(&self.root)? {
            let entry = entry?;
            let path = entry.path();
            if let Some(name) = path.file_name().and_then(|n| n.to_str())
                && name.ends_with(&format!("-{session_id}.jsonl"))
            {
                return Ok(path);
            }
        }
        Err(SessionError::NotFound(session_id.to_string()))
    }

    pub fn append(&self, session_id: &str, item: &SessionItem) -> Result<(), SessionError> {
        let path = self.find_path(session_id)?;
        self.append_path(&path, item)
    }

    pub fn append_path(&self, path: &Path, item: &SessionItem) -> Result<(), SessionError> {
        let sanitized = sanitize_item(item);
        let line = serde_json::to_string(&sanitized)?;
        let mut f = OpenOptions::new().create(true).append(true).open(path)?;
        writeln!(f, "{line}")?;
        f.sync_all()?;
        Ok(())
    }

    pub fn load(&self, session_id: &str) -> Result<Vec<SessionItem>, SessionError> {
        let path = self.find_path(session_id)?;
        load_jsonl(&path)
    }

    pub fn list_sessions(&self) -> Result<Vec<SessionSummary>, SessionError> {
        let mut summaries = Vec::new();
        for entry in fs::read_dir(&self.root)? {
            let entry = entry?;
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) != Some("jsonl") {
                continue;
            }
            let Ok(items) = load_jsonl(&path) else {
                continue;
            };
            let Some(SessionItem::Meta {
                session_id,
                project_id,
                thread_id,
                agent,
                created_at,
                ..
            }) = items.first()
            else {
                continue;
            };
            let preview = items
                .iter()
                .rev()
                .find_map(|item| match item {
                    SessionItem::UserMessage { text, .. } => Some(text.chars().take(80).collect()),
                    _ => None,
                })
                .unwrap_or_else(|| "(empty session)".to_string());
            summaries.push(SessionSummary {
                session_id: session_id.clone(),
                path,
                project_id: project_id.clone(),
                thread_id: thread_id.clone(),
                agent: agent.clone(),
                created_at: *created_at,
                item_count: items.len(),
                preview,
            });
        }
        summaries.sort_by(|a, b| b.created_at.cmp(&a.created_at));
        Ok(summaries)
    }

    /// Fork an existing session into a new JSONL file preserving all prior items (`/fork`).
    pub fn fork(&self, source_session_id: &str) -> Result<(String, PathBuf), SessionError> {
        let items = self.load(source_session_id)?;
        let (project_id, thread_id, agent, mode) = match items.first() {
            Some(SessionItem::Meta {
                project_id,
                thread_id,
                agent,
                mode,
                ..
            }) => (project_id.clone(), thread_id.clone(), agent.clone(), *mode),
            _ => return Err(SessionError::NotFound(source_session_id.to_string())),
        };
        let new_id = format!("msess_{}", Uuid::new_v4().simple());
        let now = Utc::now();
        let filename = format!("rollout-{}-{new_id}.jsonl", now.format("%Y%m%dT%H%M%SZ"));
        let path = self.root.join(filename);
        let new_meta = SessionItem::Meta {
            session_id: new_id.clone(),
            project_id,
            thread_id,
            agent,
            mode,
            parent_session_id: Some(source_session_id.to_string()),
            created_at: now,
        };
        self.append_path(&path, &new_meta)?;
        for item in items.into_iter().skip(1) {
            self.append_path(&path, &item)?;
        }
        Ok((new_id, path))
    }

    /// Compact older items into a single `CompactionSummary` while preserving the most
    /// recent `keep_recent` conversation items (`/compact`).
    pub fn compact(&self, session_id: &str, keep_recent: usize) -> Result<usize, SessionError> {
        let path = self.find_path(session_id)?;
        let items = load_jsonl(&path)?;
        if items.len() <= keep_recent + 1 {
            return Ok(0);
        }
        let meta = items[0].clone();
        let body = &items[1..];
        let split_at = body.len().saturating_sub(keep_recent);
        let older = &body[..split_at];
        let recent = &body[split_at..];

        let mut summary_lines = Vec::new();
        for item in older {
            match item {
                SessionItem::UserMessage { text, .. } => {
                    let snippet: String = text.chars().take(120).collect();
                    summary_lines.push(format!("- User: {snippet}"));
                }
                SessionItem::AssistantMessage { text, .. } => {
                    let snippet: String = text.chars().take(120).collect();
                    summary_lines.push(format!("- Assistant: {snippet}"));
                }
                SessionItem::ToolCall { tool, .. } => {
                    summary_lines.push(format!("- Tool call: {tool}"));
                }
                SessionItem::CompactionSummary { summary, .. } => {
                    summary_lines.push(summary.clone());
                }
                _ => {}
            }
        }
        let summary_item = SessionItem::CompactionSummary {
            ts: Utc::now(),
            compacted_items: older.len(),
            summary: summary_lines.join("\n"),
        };

        let mut rebuilt = Vec::with_capacity(2 + recent.len());
        rebuilt.push(meta);
        rebuilt.push(summary_item);
        rebuilt.extend_from_slice(recent);

        let mut f = OpenOptions::new().write(true).truncate(true).open(&path)?;
        for item in &rebuilt {
            let line = serde_json::to_string(&sanitize_item(item))?;
            writeln!(f, "{line}")?;
        }
        f.sync_all()?;
        Ok(older.len())
    }

    /// Return the most recent assistant message text for `/copy`.
    pub fn last_assistant_message(&self, session_id: &str) -> Result<Option<String>, SessionError> {
        let items = self.load(session_id)?;
        Ok(items.into_iter().rev().find_map(|item| match item {
            SessionItem::AssistantMessage { text, .. } => Some(text),
            _ => None,
        }))
    }

    /// Export a session transcript as clean Markdown (`/export`).
    pub fn export_markdown(&self, session_id: &str) -> Result<String, SessionError> {
        let items = self.load(session_id)?;
        let mut out = String::new();
        for item in items {
            match item {
                SessionItem::Meta {
                    session_id,
                    project_id,
                    mode,
                    created_at,
                    ..
                } => {
                    out.push_str(&format!(
                        "# Metatron Session `{session_id}`\n\n- Project: `{project_id}`\n- Mode: `{mode}`\n- Created: `{}`\n\n",
                        created_at.to_rfc3339()
                    ));
                }
                SessionItem::UserMessage { text, .. } => {
                    out.push_str(&format!("## User\n\n{text}\n\n"));
                }
                SessionItem::AssistantMessage { text, .. } => {
                    out.push_str(&format!("## BOSS\n\n{text}\n\n"));
                }
                SessionItem::ToolCall {
                    tool,
                    argv_redacted,
                    host,
                    ..
                } => {
                    let host_suffix = host
                        .as_deref()
                        .map(|h| format!(" on `{h}`"))
                        .unwrap_or_default();
                    out.push_str(&format!(
                        "### Tool `{tool}`{host_suffix}\n\n```text\n{}\n```\n\n",
                        argv_redacted.join(" ")
                    ));
                }
                SessionItem::ToolResult {
                    status,
                    exit_code,
                    full_output,
                    ..
                } => {
                    let code = exit_code
                        .map(|c| format!(" (exit {c})"))
                        .unwrap_or_default();
                    out.push_str(&format!(
                        "Result: **{status}**{code}\n\n```text\n{full_output}\n```\n\n"
                    ));
                }
                SessionItem::ApprovalDecision {
                    tool,
                    decision,
                    decided_by,
                    rule_id,
                    ..
                } => {
                    let rule = rule_id.unwrap_or_else(|| "-".to_string());
                    out.push_str(&format!(
                        "> Decision on `{tool}`: `{decision}` by `{}` (rule `{rule}`)\n\n",
                        decided_by.as_str()
                    ));
                }
                SessionItem::CompactionSummary {
                    compacted_items,
                    summary,
                    ..
                } => {
                    out.push_str(&format!(
                        "## Summary ({compacted_items} compacted items)\n\n{summary}\n\n"
                    ));
                }
            }
        }
        Ok(out)
    }
}

fn load_jsonl(path: &Path) -> Result<Vec<SessionItem>, SessionError> {
    let f = fs::File::open(path)?;
    let reader = BufReader::new(f);
    let mut items = Vec::new();
    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        items.push(serde_json::from_str(&line)?);
    }
    Ok(items)
}

fn sanitize_item(item: &SessionItem) -> SessionItem {
    match item {
        SessionItem::UserMessage { ts, text } => SessionItem::UserMessage {
            ts: *ts,
            text: redact_secrets(text),
        },
        SessionItem::AssistantMessage { ts, turn_id, text } => SessionItem::AssistantMessage {
            ts: *ts,
            turn_id: turn_id.clone(),
            text: redact_secrets(text),
        },
        SessionItem::ToolResult {
            ts,
            call_id,
            status,
            full_output,
            exit_code,
            truncated_for_model,
        } => SessionItem::ToolResult {
            ts: *ts,
            call_id: call_id.clone(),
            status: status.clone(),
            full_output: redact_secrets(full_output),
            exit_code: *exit_code,
            truncated_for_model: *truncated_for_model,
        },
        other => other.clone(),
    }
}

/// Command history store that filters out credential-bearing lines before saving.
#[derive(Debug, Clone)]
pub struct HistoryStore {
    path: PathBuf,
}

impl HistoryStore {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    /// Record a line unless it contains credentials. Returns `true` if saved.
    pub fn record(&self, line: &str) -> Result<bool, SessionError> {
        let trimmed = line.trim();
        if trimmed.is_empty() || is_credential_history_line(trimmed) {
            return Ok(false);
        }
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut f = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        writeln!(f, "{trimmed}")?;
        Ok(true)
    }

    pub fn load(&self) -> Result<Vec<String>, SessionError> {
        if !self.path.exists() {
            return Ok(Vec::new());
        }
        let f = fs::File::open(&self.path)?;
        let reader = BufReader::new(f);
        let mut lines = Vec::new();
        for line in reader.lines() {
            let line = line?;
            if !line.trim().is_empty() && !is_credential_history_line(&line) {
                lines.push(line);
            }
        }
        Ok(lines)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn session_create_append_fork_compact_copy_export() {
        let tmp = TempDir::new().expect("tempdir");
        let store = SessionStore::new(tmp.path().to_path_buf()).expect("store");
        let (sid, _path) = store
            .create(
                "boss-dogfood",
                Some("athr_1".to_string()),
                None,
                ApprovalMode::Ask,
            )
            .expect("create");

        store
            .append(
                &sid,
                &SessionItem::UserMessage {
                    ts: Utc::now(),
                    text: "Check host with token boss_pat_1234567890abcdef".to_string(),
                },
            )
            .expect("append user");
        store
            .append(
                &sid,
                &SessionItem::AssistantMessage {
                    ts: Utc::now(),
                    turn_id: Some("t1".to_string()),
                    text: "Host looks healthy.".to_string(),
                },
            )
            .expect("append assistant");
        store
            .append(
                &sid,
                &SessionItem::UserMessage {
                    ts: Utc::now(),
                    text: "Second question".to_string(),
                },
            )
            .expect("append user 2");
        store
            .append(
                &sid,
                &SessionItem::AssistantMessage {
                    ts: Utc::now(),
                    turn_id: Some("t2".to_string()),
                    text: "Final answer".to_string(),
                },
            )
            .expect("append assistant 2");

        // Secret was redacted before writing to JSONL
        let loaded = store.load(&sid).expect("load");
        let serialized = serde_json::to_string(&loaded).expect("ser");
        assert!(!serialized.contains("boss_pat_1234567890abcdef"));

        // Fork
        let (forked_id, _) = store.fork(&sid).expect("fork");
        assert_ne!(forked_id, sid);
        assert_eq!(store.load(&forked_id).expect("load fork").len(), 5);

        // Compact
        let compacted = store.compact(&sid, 2).expect("compact");
        assert_eq!(compacted, 2);
        let after = store.load(&sid).expect("load compacted");
        assert_eq!(after.len(), 4); // Meta + CompactionSummary + 2 recent

        // Copy & Export
        assert_eq!(
            store.last_assistant_message(&sid).expect("last"),
            Some("Final answer".to_string())
        );
        let md = store.export_markdown(&sid).expect("export");
        assert!(md.contains("# Metatron Session"));
        assert!(md.contains("Final answer"));
    }

    #[test]
    fn history_store_drops_credential_lines() {
        let tmp = TempDir::new().expect("tempdir");
        let hist = HistoryStore::new(tmp.path().join("history.txt"));
        assert!(hist.record("/status").expect("record safe"));
        assert!(
            !hist
                .record("BOSS_API_KEY=boss_pat_1234567890abcdef")
                .expect("drop secret")
        );
        assert!(
            !hist
                .record("Authorization: Bearer secret")
                .expect("drop auth")
        );
        assert_eq!(hist.load().expect("load"), vec!["/status".to_string()]);
    }
}
