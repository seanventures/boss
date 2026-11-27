//! TUI application state machine (`TuiApp`) supporting streaming replies, pickers,
//! slash commands, credential-filtered history, numbered offers, and approval overlays.

use metatron_protocol::ApprovalMode;
use metatron_secrets::is_credential_history_line;
use metatron_session::HistoryStore;

use crate::approval_overlay::ApprovalOverlay;
use crate::commands::complete_slash_commands;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TranscriptKind {
    System,
    User,
    Assistant,
    Tool,
    Warning,
    Error,
    Diff,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptEntry {
    pub kind: TranscriptKind,
    pub title: String,
    pub body: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PickerItem {
    pub id: String,
    pub label: String,
    pub detail: String,
}

#[derive(Debug, Clone)]
pub struct PickerState {
    pub title: String,
    pub items: Vec<PickerItem>,
    pub selected: usize,
    pub filter: String,
}

impl PickerState {
    pub fn new(title: impl Into<String>, items: Vec<PickerItem>) -> Self {
        Self {
            title: title.into(),
            items,
            selected: 0,
            filter: String::new(),
        }
    }

    pub fn filtered_items(&self) -> Vec<&PickerItem> {
        let f = self.filter.to_ascii_lowercase();
        self.items
            .iter()
            .filter(|i| {
                f.is_empty()
                    || i.label.to_ascii_lowercase().contains(&f)
                    || i.detail.to_ascii_lowercase().contains(&f)
            })
            .collect()
    }

    pub fn move_up(&mut self) {
        self.selected = self.selected.saturating_sub(1);
    }

    pub fn move_down(&mut self) {
        let len = self.filtered_items().len();
        if len > 0 {
            self.selected = (self.selected + 1).min(len - 1);
        }
    }

    pub fn current(&self) -> Option<&PickerItem> {
        self.filtered_items().get(self.selected).copied()
    }
}

/// Core state rendered by `metatron-tui`.
pub struct TuiApp {
    pub project_id: String,
    pub agent: Option<String>,
    pub model: Option<String>,
    pub mode: ApprovalMode,
    pub api_url: String,
    pub session_id: String,
    pub active_grants: usize,
    pub status_notice: String,
    pub transcript: Vec<TranscriptEntry>,
    pub input: String,
    pub history: Vec<String>,
    pub history_cursor: Option<usize>,
    pub history_store: Option<HistoryStore>,
    pub picker: Option<PickerState>,
    pub approval_overlay: Option<ApprovalOverlay>,
    pub last_offers: Vec<PickerItem>,
    pub scroll_offset: u16,
    pub should_quit: bool,
}

impl TuiApp {
    pub fn new(
        api_url: String,
        project_id: String,
        agent: Option<String>,
        mode: ApprovalMode,
        session_id: String,
        history_store: Option<HistoryStore>,
    ) -> Self {
        let history = history_store
            .as_ref()
            .and_then(|s| s.load().ok())
            .unwrap_or_default();
        let mut app = Self {
            project_id,
            agent,
            model: None,
            mode,
            api_url,
            session_id,
            active_grants: 0,
            status_notice: "Ready · /help for commands".to_string(),
            transcript: Vec::new(),
            input: String::new(),
            history,
            history_cursor: None,
            history_store,
            picker: None,
            approval_overlay: None,
            last_offers: Vec::new(),
            scroll_offset: 0,
            should_quit: false,
        };
        app.push_entry(
            TranscriptKind::System,
            "BOSS Metatron (Rust)",
            "Type to talk to BOSS — same agent, thread, policy, and audit trail as the console. Type / for commands.",
        );
        app
    }

    pub fn push_entry(
        &mut self,
        kind: TranscriptKind,
        title: impl Into<String>,
        body: impl Into<String>,
    ) {
        self.transcript.push(TranscriptEntry {
            kind,
            title: title.into(),
            body: body.into(),
        });
    }

    /// Stream an incremental `text.delta` onto the active assistant message cell.
    pub fn append_assistant_delta(&mut self, delta: &str) {
        if let Some(last) = self.transcript.last_mut()
            && last.kind == TranscriptKind::Assistant
        {
            last.body.push_str(delta);
            return;
        }
        self.push_entry(TranscriptKind::Assistant, "BOSS", delta);
    }

    /// Commit current composer input, filtering credential lines from persisted history.
    pub fn take_input(&mut self) -> Option<String> {
        let text = self.input.trim().to_string();
        self.input.clear();
        self.history_cursor = None;
        if text.is_empty() {
            return None;
        }
        if !is_credential_history_line(&text) {
            self.history.push(text.clone());
            if let Some(ref store) = self.history_store {
                let _ = store.record(&text);
            }
        }
        Some(text)
    }

    pub fn history_prev(&mut self) {
        if self.history.is_empty() {
            return;
        }
        let next = match self.history_cursor {
            None => self.history.len() - 1,
            Some(idx) => idx.saturating_sub(1),
        };
        self.history_cursor = Some(next);
        self.input = self.history[next].clone();
    }

    pub fn history_next(&mut self) {
        let Some(idx) = self.history_cursor else {
            return;
        };
        if idx + 1 >= self.history.len() {
            self.history_cursor = None;
            self.input.clear();
        } else {
            self.history_cursor = Some(idx + 1);
            self.input = self.history[idx + 1].clone();
        }
    }

    pub fn tab_complete(&mut self) {
        if !self.input.starts_with('/') {
            return;
        }
        let matches = complete_slash_commands(&self.input);
        if let Some(first) = matches.first() {
            self.input = format!("{} ", first.name);
        }
    }

    pub fn clear_transcript(&mut self) {
        self.transcript.clear();
        self.scroll_offset = 0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn tui_app_streams_deltas_and_filters_credential_history() {
        let tmp = TempDir::new().expect("tmp");
        let store = HistoryStore::new(tmp.path().join("history.txt"));
        let mut app = TuiApp::new(
            "http://127.0.0.1:8000".to_string(),
            "boss-dogfood".to_string(),
            None,
            ApprovalMode::Ask,
            "msess_1".to_string(),
            Some(store.clone()),
        );

        app.append_assistant_delta("Hello ");
        app.append_assistant_delta("operator!");
        assert_eq!(
            app.transcript.last().map(|e| e.body.as_str()),
            Some("Hello operator!")
        );

        app.input = "/status".to_string();
        assert_eq!(app.take_input().as_deref(), Some("/status"));
        app.input = "BOSS_API_KEY=boss_pat_secret_1234567890".to_string();
        let _ = app.take_input();

        assert_eq!(app.history, vec!["/status".to_string()]);
        assert_eq!(store.load().expect("load"), vec!["/status".to_string()]);
    }
}
