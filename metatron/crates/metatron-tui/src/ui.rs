//! Ratatui rendering and interactive / plain terminal runners (`MT-2b`, `MT-7`).

use std::io::{self, BufRead, Write};
use std::time::Duration;

use crossterm::event::{self, Event, KeyCode, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use metatron_boss_client::BossClient;
use metatron_core::{ToolCtx, ToolOrchestrator, TurnDriverOptions, collect_git_diff, drive_turn};
use metatron_protocol::{ExecEvent, SseEvent};
use metatron_session::SessionStore;
use ratatui::backend::{CrosstermBackend, TestBackend};
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, List, ListItem, Paragraph, Wrap};
use ratatui::{Frame, Terminal};
use thiserror::Error;

use crate::app::{PickerItem, PickerState, TranscriptKind, TuiApp};
use crate::approval_overlay::ApprovalKeyAction;
use crate::commands::{ParsedCommand, SLASH_COMMANDS, complete_slash_commands, parse_input};

#[derive(Debug, Error)]
pub enum TuiError {
    #[error("terminal IO error: {0}")]
    Io(#[from] io::Error),
}

/// Render `TuiApp` into a ratatui frame.
pub fn render_app(frame: &mut Frame<'_>, app: &TuiApp) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(5),
            Constraint::Length(3),
            Constraint::Length(2),
        ])
        .split(frame.area());

    render_transcript(frame, app, chunks[0]);
    render_composer(frame, app, chunks[1]);
    render_status_bar(frame, app, chunks[2]);

    if let Some(ref picker) = app.picker {
        render_picker(frame, picker, chunks[0]);
    }
    if let Some(ref overlay) = app.approval_overlay {
        overlay.render(frame, frame.area());
    }
}

fn render_transcript(frame: &mut Frame<'_>, app: &TuiApp, area: Rect) {
    let mut lines: Vec<Line<'_>> = Vec::new();
    for entry in &app.transcript {
        let (prefix, color) = match entry.kind {
            TranscriptKind::System => ("◆ ", Color::Cyan),
            TranscriptKind::User => ("❯ ", Color::Green),
            TranscriptKind::Assistant => ("● ", Color::White),
            TranscriptKind::Tool => ("⚙ ", Color::Blue),
            TranscriptKind::Warning => ("▲ ", Color::Yellow),
            TranscriptKind::Error => ("✗ ", Color::Red),
            TranscriptKind::Diff => ("± ", Color::Magenta),
        };
        lines.push(Line::from(vec![
            Span::styled(
                prefix,
                Style::default().fg(color).add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                entry.title.clone(),
                Style::default().fg(color).add_modifier(Modifier::BOLD),
            ),
        ]));
        for body_line in entry.body.lines() {
            let style = if entry.kind == TranscriptKind::Diff {
                if body_line.starts_with('+') && !body_line.starts_with("+++") {
                    Style::default().fg(Color::Green)
                } else if body_line.starts_with('-') && !body_line.starts_with("---") {
                    Style::default().fg(Color::Red)
                } else {
                    Style::default()
                }
            } else {
                Style::default()
            };
            lines.push(Line::from(Span::styled(format!("  {body_line}"), style)));
        }
        lines.push(Line::from(""));
    }

    let block = Block::default()
        .title(format!(" Metatron · {} · {} ", app.project_id, app.api_url))
        .borders(Borders::ALL);
    let paragraph = Paragraph::new(lines)
        .block(block)
        .wrap(Wrap { trim: false })
        .scroll((app.scroll_offset, 0));
    frame.render_widget(paragraph, area);
}

fn render_composer(frame: &mut Frame<'_>, app: &TuiApp, area: Rect) {
    let hint = if app.input.starts_with('/') {
        let matches = complete_slash_commands(&app.input);
        let names: Vec<&str> = matches.iter().take(4).map(|c| c.name).collect();
        if names.is_empty() {
            " Composer ".to_string()
        } else {
            format!(" Tab: {} ", names.join(" · "))
        }
    } else {
        " Composer (Enter send · / commands · @file context) ".to_string()
    };

    let block = Block::default().title(hint).borders(Borders::ALL);
    let text = format!("❯ {}", app.input);
    let paragraph = Paragraph::new(text).block(block);
    frame.render_widget(paragraph, area);
}

fn render_status_bar(frame: &mut Frame<'_>, app: &TuiApp, area: Rect) {
    let agent_label = app.agent.as_deref().unwrap_or("BOSS");
    let model_label = app.model.as_deref().unwrap_or("auto");
    let bar1 = format!(
        " ⬡ {} · {} · model:{} · mode:{} · grants:{} · {}",
        app.project_id, agent_label, model_label, app.mode, app.active_grants, app.status_notice
    );
    let bar2 = " Enter send  Tab complete  ↑↓ history  /help commands  Ctrl-C quit";
    let paragraph = Paragraph::new(vec![
        Line::from(Span::styled(bar1, Style::default().fg(Color::Cyan))),
        Line::from(Span::styled(bar2, Style::default().fg(Color::DarkGray))),
    ]);
    frame.render_widget(paragraph, area);
}

fn render_picker(frame: &mut Frame<'_>, picker: &PickerState, area: Rect) {
    let popup = Rect::new(
        area.x + 2,
        area.y + 1,
        area.width.saturating_sub(4).max(20),
        area.height.saturating_sub(2).clamp(6, 14),
    );
    frame.render_widget(Clear, popup);
    let items: Vec<ListItem<'_>> = picker
        .filtered_items()
        .iter()
        .enumerate()
        .map(|(i, item)| {
            let marker = if i == picker.selected { "▸ " } else { "  " };
            let style = if i == picker.selected {
                Style::default()
                    .fg(Color::Black)
                    .bg(Color::Cyan)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default()
            };
            ListItem::new(format!("{marker}{} — {}", item.label, item.detail)).style(style)
        })
        .collect();
    let list = List::new(items).block(
        Block::default()
            .title(format!(
                " {} (↑↓ select · Enter pick · Esc cancel) ",
                picker.title
            ))
            .borders(Borders::ALL)
            .border_style(Style::default().fg(Color::Cyan)),
    );
    frame.render_widget(list, popup);
}

/// Render `TuiApp` to a deterministic string buffer using `ratatui::backend::TestBackend`.
pub fn render_to_string(app: &TuiApp, width: u16, height: u16) -> Result<String, TuiError> {
    let backend = TestBackend::new(width, height);
    let mut terminal = Terminal::new(backend)?;
    terminal.draw(|frame| render_app(frame, app))?;
    let buf = terminal.backend().buffer();
    let mut out = String::new();
    for y in 0..buf.area.height {
        for x in 0..buf.area.width {
            out.push_str(buf[(x, y)].symbol());
        }
        out.push('\n');
    }
    Ok(out)
}

/// Dispatch one parsed command inside the TUI or plain REPL.
pub async fn handle_command(
    app: &mut TuiApp,
    client: &BossClient,
    orchestrator: &mut ToolOrchestrator,
    session_store: &SessionStore,
    tool_ctx: &ToolCtx,
    cmd: ParsedCommand,
) {
    match cmd {
        ParsedCommand::Quit => {
            app.should_quit = true;
        }
        ParsedCommand::Clear => {
            app.clear_transcript();
        }
        ParsedCommand::Help => {
            let mut body = String::new();
            for spec in SLASH_COMMANDS {
                body.push_str(&format!(
                    "{:<12} {:<26} {}\n",
                    spec.name, spec.args, spec.description
                ));
            }
            app.push_entry(TranscriptKind::System, "Commands", body);
        }
        ParsedCommand::Status => {
            let caps = client.capabilities().await;
            let sandbox = metatron_sandbox::detect_backend().as_str();
            let policy_info = orchestrator
                .evaluator()
                .bundle()
                .map(|b| format!("verified v{} (floor={})", b.version, b.mode_floor))
                .unwrap_or_else(|| "offline read-only".to_string());
            let api_line = match caps {
                Ok(c) => format!(
                    "✓ API reachable (contract={}, version={})",
                    c.contract,
                    c.boss_version.unwrap_or_else(|| "unknown".to_string())
                ),
                Err(e) => format!("✗ API unreachable: {e}"),
            };
            app.push_entry(
                TranscriptKind::System,
                "Status",
                format!(
                    "{api_line}\nProject: {}\nMode: {} (effective: {})\nPolicy: {policy_info}\nSandbox: {sandbox}\nSession: {}",
                    app.project_id,
                    app.mode,
                    orchestrator.evaluator().effective_mode(),
                    app.session_id
                ),
            );
        }
        ParsedCommand::Project(Some(id)) => {
            app.project_id = id.clone();
            app.push_entry(
                TranscriptKind::System,
                "Project switched",
                format!("Active project set to {id}."),
            );
        }
        ParsedCommand::Project(None) | ParsedCommand::Projects => {
            match client
                .raw_v1_request(reqwest::Method::GET, "/v1/projects", None)
                .await
            {
                Ok(v) => app.push_entry(
                    TranscriptKind::System,
                    "Projects",
                    serde_json::to_string_pretty(&v).unwrap_or_default(),
                ),
                Err(e) => app.push_entry(TranscriptKind::Error, "Projects failed", e.to_string()),
            }
        }
        ParsedCommand::Agent(Some(a)) => {
            app.agent = if a.eq_ignore_ascii_case("auto") {
                None
            } else {
                Some(a)
            };
            app.push_entry(
                TranscriptKind::System,
                "Agent updated",
                format!("Active agent: {}", app.agent.as_deref().unwrap_or("auto")),
            );
        }
        ParsedCommand::Agent(None) => {
            app.picker = Some(PickerState::new(
                "Select Agent",
                vec![
                    PickerItem {
                        id: "auto".to_string(),
                        label: "auto".to_string(),
                        detail: "Default BOSS agent".to_string(),
                    },
                    PickerItem {
                        id: "sre-captain".to_string(),
                        label: "sre-captain".to_string(),
                        detail: "SRE & host diagnostics".to_string(),
                    },
                ],
            ));
        }
        ParsedCommand::Model(Some(m)) => {
            app.model = if m.eq_ignore_ascii_case("auto") {
                None
            } else {
                Some(m)
            };
            app.push_entry(
                TranscriptKind::System,
                "Model updated",
                format!("Chat model: {}", app.model.as_deref().unwrap_or("auto")),
            );
        }
        ParsedCommand::Model(None) => {
            app.push_entry(
                TranscriptKind::System,
                "Model",
                format!("Current model: {}", app.model.as_deref().unwrap_or("auto")),
            );
        }
        ParsedCommand::Mode(None) => {
            app.push_entry(
                TranscriptKind::System,
                "Approval Mode",
                format!(
                    "Requested: {} · Effective (with BOSS floor): {}",
                    app.mode,
                    orchestrator.evaluator().effective_mode()
                ),
            );
        }
        ParsedCommand::Mode(Some(mode)) => {
            app.mode = mode;
            orchestrator.evaluator_mut().set_requested_mode(mode);
            let effective = orchestrator.evaluator().effective_mode();
            app.push_entry(
                TranscriptKind::System,
                "Approval Mode Updated",
                format!("Requested: {mode} · Effective: {effective}"),
            );
        }
        ParsedCommand::GrantsList => {
            let now = chrono::Utc::now();
            let grants = orchestrator.evaluator().grants().list();
            app.active_grants = orchestrator.evaluator().grants().active_count(now);
            if grants.is_empty() {
                app.push_entry(
                    TranscriptKind::System,
                    "Grants",
                    "(no act-on-my-behalf grants)",
                );
            } else {
                let mut lines = Vec::new();
                for g in grants {
                    let state = if g.is_active(now) {
                        "active"
                    } else {
                        "inactive"
                    };
                    lines.push(format!(
                        "{} [{}] tools={:?} expires={}",
                        g.id,
                        state,
                        g.allowed_tools,
                        g.expires_at.to_rfc3339()
                    ));
                }
                app.push_entry(TranscriptKind::System, "Grants", lines.join("\n"));
            }
        }
        ParsedCommand::GrantRevoke(id) => {
            let _ = client.revoke_grant(&id).await;
            let local_revoked = orchestrator
                .evaluator()
                .grants()
                .revoke(&id, chrono::Utc::now());
            app.active_grants = orchestrator
                .evaluator()
                .grants()
                .active_count(chrono::Utc::now());
            app.push_entry(
                TranscriptKind::System,
                "Grant Revoked",
                format!("Grant {id} revoked (local={local_revoked})."),
            );
        }
        ParsedCommand::PolicyStatus => {
            let summary = match orchestrator.evaluator().bundle() {
                Some(b) => format!(
                    "Version: {}\nMode floor: {}\nExpires: {}\nRules: {}\nTools allowed: {}",
                    b.version,
                    b.mode_floor,
                    b.expires_at.to_rfc3339(),
                    b.rules.len(),
                    b.tools_allowed.join(", ")
                ),
                None => "Offline read-only mode (no signed policy bundle loaded).".to_string(),
            };
            app.push_entry(TranscriptKind::System, "Policy", summary);
        }
        ParsedCommand::Hosts => {
            match tool_ctx
                .remote_runner
                .discover_hosts(Some(client), &app.project_id)
                .await
            {
                Ok(hosts) => {
                    let lines: Vec<String> = hosts
                        .iter()
                        .map(|h| format!("{} [{}]", h.name(), h.origin().as_str()))
                        .collect();
                    app.push_entry(
                        TranscriptKind::System,
                        "Hosts",
                        if lines.is_empty() {
                            "(no hosts discovered)".to_string()
                        } else {
                            lines.join("\n")
                        },
                    );
                }
                Err(e) => app.push_entry(TranscriptKind::Error, "Hosts error", e.to_string()),
            }
        }
        ParsedCommand::Resume(None) => match session_store.list_sessions() {
            Ok(list) => {
                let items = list
                    .into_iter()
                    .map(|s| PickerItem {
                        id: s.session_id.clone(),
                        label: s.session_id,
                        detail: format!("{} · {}", s.project_id, s.preview),
                    })
                    .collect();
                app.picker = Some(PickerState::new("Resume Session", items));
            }
            Err(e) => app.push_entry(TranscriptKind::Error, "Resume failed", e.to_string()),
        },
        ParsedCommand::Resume(Some(sid)) => match session_store.load(&sid) {
            Ok(items) => {
                app.session_id = sid.clone();
                app.push_entry(
                    TranscriptKind::System,
                    "Session Resumed",
                    format!("Loaded session {sid} ({} items).", items.len()),
                );
            }
            Err(e) => app.push_entry(TranscriptKind::Error, "Resume failed", e.to_string()),
        },
        ParsedCommand::Fork => match session_store.fork(&app.session_id) {
            Ok((new_id, _)) => {
                app.session_id = new_id.clone();
                app.push_entry(
                    TranscriptKind::System,
                    "Session Forked",
                    format!("Forked into new session {new_id}."),
                );
            }
            Err(e) => app.push_entry(TranscriptKind::Error, "Fork failed", e.to_string()),
        },
        ParsedCommand::Compact { keep_recent } => {
            match session_store.compact(&app.session_id, keep_recent) {
                Ok(n) => app.push_entry(
                    TranscriptKind::System,
                    "Session Compacted",
                    format!("Compacted {n} older items."),
                ),
                Err(e) => app.push_entry(TranscriptKind::Error, "Compact failed", e.to_string()),
            }
        }
        ParsedCommand::Copy => match session_store.last_assistant_message(&app.session_id) {
            Ok(Some(msg)) => app.push_entry(
                TranscriptKind::System,
                "Copied Last Reply",
                format!("{} chars ready:\n{msg}", msg.len()),
            ),
            Ok(None) => app.push_entry(TranscriptKind::Warning, "Copy", "No assistant reply yet."),
            Err(e) => app.push_entry(TranscriptKind::Error, "Copy failed", e.to_string()),
        },
        ParsedCommand::Export(target) => match session_store.export_markdown(&app.session_id) {
            Ok(md) => {
                if let Some(path) = target {
                    match std::fs::write(&path, &md) {
                        Ok(()) => app.push_entry(
                            TranscriptKind::System,
                            "Exported Session",
                            format!("Wrote {} bytes to {path}.", md.len()),
                        ),
                        Err(e) => {
                            app.push_entry(TranscriptKind::Error, "Export failed", e.to_string())
                        }
                    }
                } else {
                    app.push_entry(TranscriptKind::System, "Export Markdown", md);
                }
            }
            Err(e) => app.push_entry(TranscriptKind::Error, "Export failed", e.to_string()),
        },
        ParsedCommand::Diff => match collect_git_diff(&tool_ctx.cwd) {
            Ok(diff) => app.push_entry(TranscriptKind::Diff, "git diff", diff),
            Err(e) => app.push_entry(TranscriptKind::Error, "git diff failed", e.to_string()),
        },
        ParsedCommand::ApiRaw { method, path, body } => {
            let m = reqwest::Method::from_bytes(method.as_bytes()).unwrap_or(reqwest::Method::GET);
            match client.raw_v1_request(m, &path, body.as_ref()).await {
                Ok(v) => app.push_entry(
                    TranscriptKind::System,
                    format!("{method} {path}"),
                    serde_json::to_string_pretty(&v).unwrap_or_default(),
                ),
                Err(e) => app.push_entry(TranscriptKind::Error, "API error", e.to_string()),
            }
        }
        ParsedCommand::Tasks { page } => {
            let path = format!("/v1/tasks?project_id={}&page={page}", app.project_id);
            call_and_display(app, client, reqwest::Method::GET, &path, None, "Tasks").await;
        }
        ParsedCommand::TaskShow(id) => {
            let path = format!("/v1/tasks/{id}?project_id={}", app.project_id);
            call_and_display(app, client, reqwest::Method::GET, &path, None, "Task").await;
        }
        ParsedCommand::TaskCreate { dedupe_key, title } => {
            let body = serde_json::json!({
                "project_id": app.project_id,
                "dedupe_key": dedupe_key,
                "title": title
            });
            call_and_display(
                app,
                client,
                reqwest::Method::POST,
                "/v1/tasks",
                Some(&body),
                "Task Created",
            )
            .await;
        }
        ParsedCommand::Decisions { scope, page } => {
            let path = format!(
                "/v1/decisions?project_id={}&scope={scope}&page={page}",
                app.project_id
            );
            call_and_display(app, client, reqwest::Method::GET, &path, None, "Decisions").await;
        }
        ParsedCommand::DecisionShow(id) => {
            let path = format!("/v1/decisions/{id}?project_id={}", app.project_id);
            call_and_display(app, client, reqwest::Method::GET, &path, None, "Decision").await;
        }
        ParsedCommand::DecisionAct { action, id, note } => {
            let path = format!("/v1/decisions/{id}/{action}");
            let body = serde_json::json!({ "project_id": app.project_id, "note": note });
            call_and_display(
                app,
                client,
                reqwest::Method::POST,
                &path,
                Some(&body),
                &format!("Decision {action}"),
            )
            .await;
        }
        ParsedCommand::Runs { status } => {
            let q = status.map(|s| format!("&status={s}")).unwrap_or_default();
            let path = format!("/v1/workflow-runs?project_id={}{q}", app.project_id);
            call_and_display(app, client, reqwest::Method::GET, &path, None, "Runs").await;
        }
        ParsedCommand::Run { id, .. } => {
            let path = format!("/v1/workflow-runs/{id}?project_id={}", app.project_id);
            call_and_display(app, client, reqwest::Method::GET, &path, None, "Run").await;
        }
        ParsedCommand::Alerts { status } => {
            let path = format!("/v1/alerts?project_id={}&status={status}", app.project_id);
            call_and_display(app, client, reqwest::Method::GET, &path, None, "Alerts").await;
        }
        ParsedCommand::Goals => {
            let path = format!("/v1/metatron/goals?project_id={}", app.project_id);
            call_and_display(app, client, reqwest::Method::GET, &path, None, "Goals").await;
        }
        ParsedCommand::Watch => {
            let body = serde_json::json!({ "project_id": app.project_id });
            call_and_display(
                app,
                client,
                reqwest::Method::POST,
                "/v1/metatron/watch",
                Some(&body),
                "Watch",
            )
            .await;
        }
        ParsedCommand::Overwatch => {
            let body = serde_json::json!({ "project_id": app.project_id });
            call_and_display(
                app,
                client,
                reqwest::Method::POST,
                "/v1/metatron/overwatch",
                Some(&body),
                "Overwatch",
            )
            .await;
        }
        ParsedCommand::Drive { auto } => {
            let body = serde_json::json!({ "project_id": app.project_id, "auto": auto });
            call_and_display(
                app,
                client,
                reqwest::Method::POST,
                "/v1/metatron/drive",
                Some(&body),
                "Drive",
            )
            .await;
        }
        ParsedCommand::New => {
            if let Ok((sid, _)) =
                session_store.create(&app.project_id, None, app.agent.clone(), app.mode)
            {
                app.session_id = sid.clone();
                app.push_entry(
                    TranscriptKind::System,
                    "New Session",
                    format!("Started new session {sid}."),
                );
            }
        }
        ParsedCommand::Prompt(prompt) => {
            app.push_entry(TranscriptKind::User, "You", prompt.clone());
            let opts = TurnDriverOptions {
                project_id: app.project_id.clone(),
                thread_id: None,
                agent: app.agent.clone(),
                mode: orchestrator.evaluator().effective_mode(),
                cwd: tool_ctx.cwd.clone(),
                session_id: app.session_id.clone(),
                max_reconnects: 3,
                output_file: None,
                output_schema: None,
            };
            match drive_turn(
                client,
                orchestrator,
                Some(session_store),
                tool_ctx,
                &opts,
                &prompt,
                |ev| match ev {
                    ExecEvent::ItemUpdated { delta, .. } => {
                        app.append_assistant_delta(delta);
                    }
                    ExecEvent::ItemStarted {
                        kind,
                        name,
                        summary,
                        ..
                    } if kind != "agent_message" => {
                        app.push_entry(TranscriptKind::Tool, name.clone(), summary.clone());
                    }
                    _ => {}
                },
            )
            .await
            {
                Ok(_) => {}
                Err(e) => app.push_entry(TranscriptKind::Error, "Turn error", e.to_string()),
            }
        }
        ParsedCommand::Error(msg) => {
            app.push_entry(TranscriptKind::Error, "Command Error", msg);
        }
        other => {
            // Remaining OpenAPI/offer inspection helpers (`/ops`, `/describe`, `/op`, `/api`, `/pick`, `/confirm`, `/expand`, `/thread`, `/coverage`)
            app.push_entry(
                TranscriptKind::System,
                "Command",
                format!("Dispatched: {other:?}"),
            );
        }
    }
}

async fn call_and_display(
    app: &mut TuiApp,
    client: &BossClient,
    method: reqwest::Method,
    path: &str,
    body: Option<&serde_json::Value>,
    title: &str,
) {
    match client.raw_v1_request(method, path, body).await {
        Ok(v) => app.push_entry(
            TranscriptKind::System,
            title,
            serde_json::to_string_pretty(&v).unwrap_or_default(),
        ),
        Err(e) => app.push_entry(TranscriptKind::Error, title, e.to_string()),
    }
}

/// Interactive ratatui terminal loop.
pub async fn run_interactive(
    app: &mut TuiApp,
    client: &BossClient,
    orchestrator: &mut ToolOrchestrator,
    session_store: &SessionStore,
    tool_ctx: &ToolCtx,
) -> Result<(), TuiError> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let result = loop {
        terminal.draw(|frame| render_app(frame, app))?;
        if app.should_quit {
            break Ok(());
        }
        if event::poll(Duration::from_millis(50))?
            && let Event::Key(key) = event::read()?
        {
            if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
                app.should_quit = true;
                continue;
            }
            if let Some(ref overlay) = app.approval_overlay {
                if let KeyCode::Char(c) = key.code
                    && matches!(overlay.handle_key(c), ApprovalKeyAction::Decided(_))
                {
                    app.approval_overlay = None;
                }
                continue;
            }
            if let Some(ref mut picker) = app.picker {
                match key.code {
                    KeyCode::Esc => app.picker = None,
                    KeyCode::Up => picker.move_up(),
                    KeyCode::Down => picker.move_down(),
                    KeyCode::Enter => {
                        let chosen = picker.current().map(|i| i.id.clone());
                        app.picker = None;
                        if let Some(id) = chosen {
                            app.push_entry(
                                TranscriptKind::System,
                                "Selected",
                                format!("Selected {id}"),
                            );
                        }
                    }
                    KeyCode::Backspace => {
                        picker.filter.pop();
                    }
                    KeyCode::Char(c) => picker.filter.push(c),
                    _ => {}
                }
                continue;
            }
            match key.code {
                KeyCode::Enter => {
                    if let Some(line) = app.take_input() {
                        let cmd = parse_input(&line);
                        handle_command(app, client, orchestrator, session_store, tool_ctx, cmd)
                            .await;
                    }
                }
                KeyCode::Backspace => {
                    app.input.pop();
                }
                KeyCode::Tab => app.tab_complete(),
                KeyCode::Up => app.history_prev(),
                KeyCode::Down => app.history_next(),
                KeyCode::PageUp => {
                    app.scroll_offset = app.scroll_offset.saturating_sub(5);
                }
                KeyCode::PageDown => {
                    app.scroll_offset = app.scroll_offset.saturating_add(5);
                }
                KeyCode::Char(c) => app.input.push(c),
                _ => {}
            }
        }
    };

    let _ = disable_raw_mode();
    let _ = execute!(terminal.backend_mut(), LeaveAlternateScreen);
    let _ = terminal.show_cursor();
    result
}

/// Plain-text non-TTY REPL fallback (`--plain`, pipes, `TERM=dumb`).
pub async fn run_plain(
    app: &mut TuiApp,
    client: &BossClient,
    orchestrator: &mut ToolOrchestrator,
    session_store: &SessionStore,
    tool_ctx: &ToolCtx,
) -> Result<(), TuiError> {
    let stdin = io::stdin();
    let mut stdout = io::stdout();
    let mut reader = stdin.lock();
    let mut line = String::new();

    loop {
        write!(stdout, "metatron> ")?;
        stdout.flush()?;
        line.clear();
        let n = reader.read_line(&mut line)?;
        if n == 0 {
            break;
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let cmd = parse_input(trimmed);
        let before_len = app.transcript.len();
        handle_command(app, client, orchestrator, session_store, tool_ctx, cmd).await;
        for entry in &app.transcript[before_len..] {
            writeln!(stdout, "[{}] {}", entry.title, entry.body)?;
        }
        if app.should_quit {
            break;
        }
    }
    Ok(())
}

// Keep SseEvent referenced cleanly for downstream callers.
#[allow(dead_code)]
fn _assert_sse_reachable(_ev: &SseEvent) {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::approval_overlay::ApprovalOverlay;
    use metatron_core::ApprovalPromptRequest;
    use metatron_protocol::{ApprovalMode, RiskClass};

    #[test]
    fn renders_tui_and_approval_overlay_with_test_backend() {
        let mut app = TuiApp::new(
            "http://127.0.0.1:8000".to_string(),
            "boss-dogfood".to_string(),
            Some("sre-captain".to_string()),
            ApprovalMode::RiskyOnly,
            "msess_render_1".to_string(),
            None,
        );
        app.append_assistant_delta("CPU idle is 94.2% on web-1.");
        app.approval_overlay = Some(ApprovalOverlay::new(ApprovalPromptRequest {
            call_id: "c1".to_string(),
            tool: "ssh_run".to_string(),
            risk_class: RiskClass::Capture,
            argv_redacted: vec!["tcpdump".to_string(), "-i".to_string(), "eth0".to_string()],
            host: Some("web-1".to_string()),
            domain: None,
            reason: "packet capture requires explicit human approval".to_string(),
            rule_id: Some("r-pcap".to_string()),
            hard: true,
            allow_always: false,
            is_escalation: false,
            sandbox_warning: Some(metatron_remote::REMOTE_SANDBOX_WARNING.to_string()),
        }));

        let rendered = render_to_string(&app, 110, 30).expect("render");
        assert!(rendered.contains("boss-dogfood"));
        assert!(rendered.contains("mode:risky-only"));
        assert!(rendered.contains("Hard Policy Gate"));
        assert!(rendered.contains("no 'always allow'"));
        assert!(rendered.contains("does not protect remote hosts"));
    }
}
