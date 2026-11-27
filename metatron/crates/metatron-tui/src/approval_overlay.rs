//! Approval overlay widget for `metatron-tui` (`MT-7`, `MT-3`).
//!
//! Enforces the hard-prompt UI contract:
//! - Hard rules (`hard: true` or `capture`/`secret-use`/`app-control`/`remote-write`)
//!   NEVER offer `[a] Always allow` and ignore the `'a'` key.
//! - Remote host executions prominently warn that the local OS sandbox does not protect
//!   remote hosts.

use metatron_core::{ApprovalPromptRequest, HumanApproval};
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ApprovalKeyAction {
    Pending,
    Decided(HumanApproval),
}

#[derive(Debug, Clone)]
pub struct ApprovalOverlay {
    pub request: ApprovalPromptRequest,
}

impl ApprovalOverlay {
    pub fn new(request: ApprovalPromptRequest) -> Self {
        Self { request }
    }

    /// True if session-wide `[a] Always allow` may be offered to the user.
    pub fn offers_always_allow(&self) -> bool {
        self.request.allow_always && !self.request.hard && !self.request.is_escalation
    }

    /// Process a single key press in the approval overlay.
    pub fn handle_key(&self, ch: char) -> ApprovalKeyAction {
        match ch.to_ascii_lowercase() {
            'y' | '\n' => ApprovalKeyAction::Decided(HumanApproval::AllowOnce),
            'n' | '\u{1b}' => ApprovalKeyAction::Decided(HumanApproval::Deny),
            'a' if self.offers_always_allow() => {
                ApprovalKeyAction::Decided(HumanApproval::AllowSession)
            }
            _ => ApprovalKeyAction::Pending,
        }
    }

    pub fn render(&self, frame: &mut Frame<'_>, area: Rect) {
        let popup = centered_rect(82, 65, area);
        frame.render_widget(Clear, popup);

        let title = if self.request.is_escalation {
            " Approval Required — Sandbox Escalation (Fresh Decision) "
        } else if self.request.hard {
            " Approval Required — Hard Policy Gate "
        } else {
            " Approval Required "
        };

        let mut lines = vec![
            Line::from(vec![
                Span::styled("Tool: ", Style::default().add_modifier(Modifier::BOLD)),
                Span::raw(self.request.tool.clone()),
                Span::raw("   "),
                Span::styled("Risk: ", Style::default().add_modifier(Modifier::BOLD)),
                Span::styled(
                    self.request.risk_class.as_str(),
                    Style::default().fg(if self.request.hard {
                        Color::Red
                    } else {
                        Color::Yellow
                    }),
                ),
            ]),
            Line::from(vec![
                Span::styled("Command: ", Style::default().add_modifier(Modifier::BOLD)),
                Span::raw(self.request.argv_redacted.join(" ")),
            ]),
        ];

        if let Some(ref host) = self.request.host {
            lines.push(Line::from(vec![
                Span::styled("Host: ", Style::default().add_modifier(Modifier::BOLD)),
                Span::raw(host.clone()),
            ]));
        }
        if let Some(ref domain) = self.request.domain {
            lines.push(Line::from(vec![
                Span::styled("Domain: ", Style::default().add_modifier(Modifier::BOLD)),
                Span::raw(domain.clone()),
            ]));
        }

        lines.push(Line::from(vec![
            Span::styled("Reason: ", Style::default().add_modifier(Modifier::BOLD)),
            Span::raw(self.request.reason.clone()),
        ]));

        if let Some(ref warn) = self.request.sandbox_warning {
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                format!("WARNING: {warn}"),
                Style::default()
                    .fg(Color::Yellow)
                    .add_modifier(Modifier::BOLD),
            )));
        }

        lines.push(Line::from(""));
        let choices = if self.offers_always_allow() {
            "[y] Allow once   [a] Always allow (this session)   [n] Deny"
        } else {
            "[y] Allow once   [n] Deny   (hard/escalation gate: no 'always allow')"
        };
        lines.push(Line::from(Span::styled(
            choices,
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        )));

        let block = Block::default()
            .title(title)
            .borders(Borders::ALL)
            .border_style(Style::default().fg(Color::Yellow));
        let paragraph = Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false });
        frame.render_widget(paragraph, popup);
    }
}

fn centered_rect(percent_x: u16, percent_y: u16, area: Rect) -> Rect {
    let width = area.width.saturating_mul(percent_x) / 100;
    let height = area.height.saturating_mul(percent_y) / 100;
    let x = area.x + area.width.saturating_sub(width) / 2;
    let y = area.y + area.height.saturating_sub(height) / 2;
    Rect::new(
        x,
        y,
        width.max(20).min(area.width),
        height.max(8).min(area.height),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use metatron_protocol::RiskClass;

    #[test]
    fn hard_prompt_never_offers_or_accepts_always_allow() {
        let req = ApprovalPromptRequest {
            call_id: "c1".to_string(),
            tool: "ssh_run".to_string(),
            risk_class: RiskClass::Capture,
            argv_redacted: vec!["tcpdump".to_string(), "-i".to_string(), "eth0".to_string()],
            host: Some("web-1".to_string()),
            domain: None,
            reason: "packet capture".to_string(),
            rule_id: Some("r-pcap".to_string()),
            hard: true,
            allow_always: false,
            is_escalation: false,
            sandbox_warning: Some("remote host".to_string()),
        };
        let overlay = ApprovalOverlay::new(req);
        assert!(!overlay.offers_always_allow());
        assert_eq!(overlay.handle_key('a'), ApprovalKeyAction::Pending);
        assert_eq!(
            overlay.handle_key('y'),
            ApprovalKeyAction::Decided(HumanApproval::AllowOnce)
        );
        assert_eq!(
            overlay.handle_key('n'),
            ApprovalKeyAction::Decided(HumanApproval::Deny)
        );
    }

    #[test]
    fn non_hard_prompt_accepts_session_allow() {
        let req = ApprovalPromptRequest {
            call_id: "c2".to_string(),
            tool: "shell_local".to_string(),
            risk_class: RiskClass::WriteScratch,
            argv_redacted: vec!["python3".to_string(), "plot.py".to_string()],
            host: None,
            domain: None,
            reason: "run analysis script".to_string(),
            rule_id: None,
            hard: false,
            allow_always: true,
            is_escalation: false,
            sandbox_warning: None,
        };
        let overlay = ApprovalOverlay::new(req);
        assert!(overlay.offers_always_allow());
        assert_eq!(
            overlay.handle_key('a'),
            ApprovalKeyAction::Decided(HumanApproval::AllowSession)
        );
    }
}
