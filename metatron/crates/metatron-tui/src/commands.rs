//! Complete slash-command catalog and parser with 100% parity with `scripts/metatron_tui.py`
//! plus MT-2 (`/resume`, `/fork`, `/compact`, `/copy`, `/export`, `/diff`) and
//! MT-7 (`/mode`, `/grants`, `/policy`, `/hosts`) commands.

use metatron_protocol::ApprovalMode;
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SlashCommandSpec {
    pub name: &'static str,
    pub args: &'static str,
    pub description: &'static str,
    pub python_parity: bool,
}

/// Complete command catalog — every Python TUI command has a first-class Rust entry.
pub const SLASH_COMMANDS: &[SlashCommandSpec] = &[
    // --- 27+ Python TUI parity commands (scripts/metatron_tui.py lines 102-132) ---
    SlashCommandSpec {
        name: "/help",
        args: "",
        description: "Commands and keys",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/agent",
        args: "[KEY|auto]",
        description: "Pick the agent you are talking to (Agent Studio)",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/project",
        args: "[ID]",
        description: "Pick or switch the project",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/projects",
        args: "",
        description: "List the projects this key can see",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/model",
        args: "[ENGINE[:MODEL]|auto]",
        description: "Pick the chat model (Setup > jobs > chat)",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/status",
        args: "",
        description: "Connection checklist: API, identity, project, policy, sandbox",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/pick",
        args: "[N]",
        description: "Run one of the last reply's numbered choices",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/confirm",
        args: "",
        description: "Go ahead with the plan waiting on you",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/expand",
        args: "",
        description: "The last reply's tool calls, in full",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/new",
        args: "",
        description: "Start a new conversation",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/thread",
        args: "[ID]",
        description: "Read the conversation from the server",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/tasks",
        args: "[PAGE]",
        description: "Task list, 25 a page",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/task",
        args: "ID | create KEY TITLE",
        description: "Show one task, or create one",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/decisions",
        args: "[open|all] [PAGE]",
        description: "Decision inbox",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/decision",
        args: "ID | approve|reject ID NOTE",
        description: "Show a decision card, or act on it",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/runs",
        args: "[STATUS]",
        description: "Workflow runs",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/run",
        args: "ID",
        description: "One run's steps",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/follow",
        args: "ID",
        description: "Follow a run's steps live",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/alerts",
        args: "[firing|resolved|all]",
        description: "Alerts across apps",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/goals",
        args: "",
        description: "Captain goals",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/watch",
        args: "",
        description: "Captain: one watch tick",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/overwatch",
        args: "",
        description: "Captain: observe, measure, validate, apply-safe, verify",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/drive",
        args: "[auto|go]",
        description: "Captain: propose moves (auto applies validated ones)",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/go",
        args: "",
        description: "Captain: drive auto",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/ops",
        args: "[TERM] [--page N]",
        description: "Search the live API operations",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/describe",
        args: "OPERATION_ID",
        description: "An operation's parameters and body",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/op",
        args: "OPERATION_ID [JSON]",
        description: "Invoke an operation",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/api",
        args: "[TERM] | N | run N [JSON] | METHOD /v1/path [JSON]",
        description: "Browse the API and call an operation",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/coverage",
        args: "",
        description: "Live OpenAPI against the mounted routes",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/clear",
        args: "",
        description: "Clear the screen",
        python_parity: true,
    },
    SlashCommandSpec {
        name: "/quit",
        args: "",
        description: "Leave Metatron",
        python_parity: true,
    },
    // --- New MT-2 / MT-7 / MT-3 Rust commands ---
    SlashCommandSpec {
        name: "/resume",
        args: "[SESSION_ID]",
        description: "Open local session picker or resume a session JSONL",
        python_parity: false,
    },
    SlashCommandSpec {
        name: "/fork",
        args: "",
        description: "Fork the current local session JSONL",
        python_parity: false,
    },
    SlashCommandSpec {
        name: "/compact",
        args: "[KEEP]",
        description: "Compact older session items into a summary",
        python_parity: false,
    },
    SlashCommandSpec {
        name: "/copy",
        args: "",
        description: "Copy the last assistant reply",
        python_parity: false,
    },
    SlashCommandSpec {
        name: "/export",
        args: "[PATH]",
        description: "Export the session transcript as Markdown",
        python_parity: false,
    },
    SlashCommandSpec {
        name: "/diff",
        args: "",
        description: "Show local read-only git diff",
        python_parity: false,
    },
    SlashCommandSpec {
        name: "/mode",
        args: "[read-only|ask|risky-only|act-on-my-behalf]",
        description: "Show or tighten the approval mode",
        python_parity: false,
    },
    SlashCommandSpec {
        name: "/grants",
        args: "[revoke ID]",
        description: "List or revoke act-on-my-behalf TTL grants",
        python_parity: false,
    },
    SlashCommandSpec {
        name: "/policy",
        args: "",
        description: "Show verified BOSS policy bundle status",
        python_parity: false,
    },
    SlashCommandSpec {
        name: "/hosts",
        args: "",
        description: "List BOSS registered hosts and ~/.ssh/config hosts",
        python_parity: false,
    },
];

#[derive(Debug, Clone, PartialEq)]
pub enum ParsedCommand {
    Prompt(String),
    Help,
    Quit,
    Clear,
    Status,
    Agent(Option<String>),
    Project(Option<String>),
    Projects,
    Model(Option<String>),
    Pick(Option<usize>),
    Confirm,
    Expand,
    New,
    Thread(Option<String>),
    Tasks {
        page: usize,
    },
    TaskShow(String),
    TaskCreate {
        dedupe_key: String,
        title: String,
    },
    Decisions {
        scope: String,
        page: usize,
    },
    DecisionShow(String),
    DecisionAct {
        action: String,
        id: String,
        note: String,
    },
    Runs {
        status: Option<String>,
    },
    Run {
        id: String,
        follow: bool,
    },
    Alerts {
        status: String,
    },
    Goals,
    Watch,
    Overwatch,
    Drive {
        auto: bool,
    },
    Ops {
        term: Option<String>,
        page: usize,
    },
    Describe(String),
    Op {
        operation_id: String,
        args: Option<Value>,
    },
    ApiBrowse {
        term: Option<String>,
        page: usize,
    },
    ApiShow(usize),
    ApiRun {
        index: usize,
        args: Option<Value>,
    },
    ApiRaw {
        method: String,
        path: String,
        body: Option<Value>,
    },
    Coverage,
    // MT-2 / MT-7 / MT-3 additions
    Resume(Option<String>),
    Fork,
    Compact {
        keep_recent: usize,
    },
    Copy,
    Export(Option<String>),
    Diff,
    Mode(Option<ApprovalMode>),
    GrantsList,
    GrantRevoke(String),
    PolicyStatus,
    Hosts,
    Error(String),
}

/// Filter slash commands for tab/popup completion.
pub fn complete_slash_commands(prefix: &str) -> Vec<&'static SlashCommandSpec> {
    let p = prefix.trim().to_ascii_lowercase();
    SLASH_COMMANDS
        .iter()
        .filter(|spec| spec.name.starts_with(&p))
        .collect()
}

/// Parse user composer input into a `ParsedCommand` (matches Python TUI semantics).
pub fn parse_input(raw: &str) -> ParsedCommand {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return ParsedCommand::Error("empty input".to_string());
    }

    // Bare positive integer selects a numbered offer (`/pick N`).
    if trimmed.bytes().all(|b| b.is_ascii_digit())
        && let Ok(n) = trimmed.parse::<usize>()
        && n >= 1
    {
        return ParsedCommand::Pick(Some(n));
    }

    let lowered = trimmed.to_ascii_lowercase();
    if lowered == "help" {
        return ParsedCommand::Help;
    }
    if !trimmed.starts_with('/') {
        return ParsedCommand::Prompt(trimmed.to_string());
    }

    let mut split = trimmed.splitn(2, char::is_whitespace);
    let cmd = split.next().unwrap_or("").to_ascii_lowercase();
    let rest = split.next().unwrap_or("").trim();

    match cmd.as_str() {
        "/quit" | "/exit" | "/q" => ParsedCommand::Quit,
        "/help" | "/?" => ParsedCommand::Help,
        "/clear" => ParsedCommand::Clear,
        "/status" => ParsedCommand::Status,
        "/projects" => ParsedCommand::Projects,
        "/project" => ParsedCommand::Project((!rest.is_empty()).then(|| rest.to_string())),
        "/agent" | "/agents" => ParsedCommand::Agent((!rest.is_empty()).then(|| rest.to_string())),
        "/model" | "/models" => ParsedCommand::Model((!rest.is_empty()).then(|| rest.to_string())),
        "/confirm" => ParsedCommand::Confirm,
        "/expand" => ParsedCommand::Expand,
        "/new" => ParsedCommand::New,
        "/thread" => ParsedCommand::Thread((!rest.is_empty()).then(|| rest.to_string())),
        "/pick" => {
            if rest.is_empty() {
                ParsedCommand::Pick(None)
            } else {
                match rest.parse::<usize>() {
                    Ok(n) if n >= 1 => ParsedCommand::Pick(Some(n)),
                    _ => {
                        ParsedCommand::Error("/pick requires a positive choice number".to_string())
                    }
                }
            }
        }
        "/tasks" => {
            let page = rest.parse::<usize>().ok().filter(|&p| p >= 1).unwrap_or(1);
            ParsedCommand::Tasks { page }
        }
        "/task" => {
            let parts: Vec<&str> = rest.split_whitespace().collect();
            if parts.is_empty() {
                ParsedCommand::Error("usage: /task ID | /task create DEDUPE_KEY TITLE".to_string())
            } else if parts[0].eq_ignore_ascii_case("create") {
                if parts.len() < 3 {
                    ParsedCommand::Error("usage: /task create DEDUPE_KEY TITLE".to_string())
                } else {
                    ParsedCommand::TaskCreate {
                        dedupe_key: parts[1].to_string(),
                        title: parts[2..].join(" "),
                    }
                }
            } else {
                ParsedCommand::TaskShow(parts[0].to_string())
            }
        }
        "/decisions" => {
            let parts: Vec<&str> = rest.split_whitespace().collect();
            let mut scope = "open".to_string();
            let mut page = 1usize;
            for p in parts {
                if p.eq_ignore_ascii_case("all") || p.eq_ignore_ascii_case("open") {
                    scope = p.to_ascii_lowercase();
                } else if let Ok(n) = p.parse::<usize>()
                    && n >= 1
                {
                    page = n;
                }
            }
            ParsedCommand::Decisions { scope, page }
        }
        "/decision" => {
            let parts: Vec<&str> = rest.split_whitespace().collect();
            if parts.is_empty() {
                ParsedCommand::Error(
                    "usage: /decision ID | /decision approve|reject ID NOTE".to_string(),
                )
            } else if parts[0].eq_ignore_ascii_case("approve")
                || parts[0].eq_ignore_ascii_case("reject")
            {
                if parts.len() < 3 {
                    ParsedCommand::Error("usage: /decision approve|reject ID NOTE".to_string())
                } else {
                    ParsedCommand::DecisionAct {
                        action: parts[0].to_ascii_lowercase(),
                        id: parts[1].to_string(),
                        note: parts[2..].join(" "),
                    }
                }
            } else {
                ParsedCommand::DecisionShow(parts[0].to_string())
            }
        }
        "/runs" => ParsedCommand::Runs {
            status: (!rest.is_empty()).then(|| rest.to_string()),
        },
        "/run" | "/follow" => {
            if rest.is_empty() {
                ParsedCommand::Error(format!("usage: {cmd} RUN_ID"))
            } else {
                ParsedCommand::Run {
                    id: rest.to_string(),
                    follow: cmd == "/follow",
                }
            }
        }
        "/alerts" => ParsedCommand::Alerts {
            status: if rest.is_empty() {
                "firing".to_string()
            } else {
                rest.to_ascii_lowercase()
            },
        },
        "/goals" => ParsedCommand::Goals,
        "/watch" => ParsedCommand::Watch,
        "/overwatch" | "/ow" => ParsedCommand::Overwatch,
        "/go" => ParsedCommand::Drive { auto: true },
        "/drive" => {
            let auto = rest.eq_ignore_ascii_case("auto") || rest.eq_ignore_ascii_case("go");
            ParsedCommand::Drive { auto }
        }
        "/ops" => {
            let (term, page) = parse_term_and_page(rest);
            ParsedCommand::Ops { term, page }
        }
        "/describe" => {
            if rest.is_empty() {
                ParsedCommand::Error("usage: /describe OPERATION_ID".to_string())
            } else {
                ParsedCommand::Describe(rest.to_string())
            }
        }
        "/op" => parse_op_command(rest),
        "/api" => parse_api_command(rest),
        "/coverage" => ParsedCommand::Coverage,
        // MT-2 / MT-7 / MT-3 additions
        "/resume" => ParsedCommand::Resume((!rest.is_empty()).then(|| rest.to_string())),
        "/fork" => ParsedCommand::Fork,
        "/compact" => {
            let keep = rest.parse::<usize>().ok().filter(|&n| n >= 1).unwrap_or(6);
            ParsedCommand::Compact { keep_recent: keep }
        }
        "/copy" => ParsedCommand::Copy,
        "/export" => ParsedCommand::Export((!rest.is_empty()).then(|| rest.to_string())),
        "/diff" => ParsedCommand::Diff,
        "/mode" => {
            if rest.is_empty() {
                ParsedCommand::Mode(None)
            } else {
                match rest.parse::<ApprovalMode>() {
                    Ok(m) => ParsedCommand::Mode(Some(m)),
                    Err(e) => ParsedCommand::Error(e),
                }
            }
        }
        "/grants" => {
            let parts: Vec<&str> = rest.split_whitespace().collect();
            if parts.is_empty() {
                ParsedCommand::GrantsList
            } else if parts[0].eq_ignore_ascii_case("revoke") && parts.len() == 2 {
                ParsedCommand::GrantRevoke(parts[1].to_string())
            } else {
                ParsedCommand::Error("usage: /grants | /grants revoke GRANT_ID".to_string())
            }
        }
        "/policy" => ParsedCommand::PolicyStatus,
        "/hosts" => ParsedCommand::Hosts,
        other => ParsedCommand::Error(format!(
            "unknown command '{other}' (type /help for available commands)"
        )),
    }
}

fn parse_term_and_page(rest: &str) -> (Option<String>, usize) {
    let mut terms = Vec::new();
    let mut page = 1usize;
    let tokens: Vec<&str> = rest.split_whitespace().collect();
    let mut i = 0;
    while i < tokens.len() {
        if tokens[i] == "--page" {
            if let Some(val) = tokens.get(i + 1).and_then(|v| v.parse::<usize>().ok())
                && val >= 1
            {
                page = val;
            }
            i += 2;
        } else {
            terms.push(tokens[i]);
            i += 1;
        }
    }
    let term = if terms.is_empty() {
        None
    } else {
        Some(terms.join(" "))
    };
    (term, page)
}

fn parse_op_command(rest: &str) -> ParsedCommand {
    if rest.is_empty() {
        return ParsedCommand::Error("usage: /op OPERATION_ID [JSON]".to_string());
    }
    let mut split = rest.splitn(2, char::is_whitespace);
    let op_id = split.next().unwrap_or("").to_string();
    let json_str = split.next().unwrap_or("").trim();
    if json_str.is_empty() {
        return ParsedCommand::Op {
            operation_id: op_id,
            args: None,
        };
    }
    match serde_json::from_str::<Value>(json_str) {
        Ok(v) if v.is_object() => ParsedCommand::Op {
            operation_id: op_id,
            args: Some(v),
        },
        Ok(_) => ParsedCommand::Error("/op arguments must be a JSON object".to_string()),
        Err(e) => ParsedCommand::Error(format!("/op JSON parse error: {e}")),
    }
}

fn parse_api_command(rest: &str) -> ParsedCommand {
    if rest.is_empty() {
        return ParsedCommand::ApiBrowse {
            term: None,
            page: 1,
        };
    }
    let mut parts = rest.splitn(3, char::is_whitespace);
    let first = parts.next().unwrap_or("");
    let second = parts.next().unwrap_or("").trim();
    let third = parts.next().unwrap_or("").trim();

    // `/api NUMBER`
    if second.is_empty() && first.bytes().all(|b| b.is_ascii_digit()) {
        return match first.parse::<usize>() {
            Ok(n) if n >= 1 => ParsedCommand::ApiShow(n),
            _ => ParsedCommand::Error("/api numbers start at 1".to_string()),
        };
    }

    // `/api run NUMBER [JSON]`
    if first.eq_ignore_ascii_case("run") {
        let Ok(n) = second.parse::<usize>() else {
            return ParsedCommand::Error("usage: /api run NUMBER [JSON]".to_string());
        };
        if n == 0 {
            return ParsedCommand::Error("/api numbers start at 1".to_string());
        }
        let args = if third.is_empty() {
            None
        } else {
            match serde_json::from_str::<Value>(third) {
                Ok(v) => Some(v),
                Err(e) => return ParsedCommand::Error(format!("/api run JSON error: {e}")),
            }
        };
        return ParsedCommand::ApiRun { index: n, args };
    }

    // `/api METHOD /v1/path [JSON]`
    let upper = first.to_ascii_uppercase();
    if matches!(upper.as_str(), "GET" | "POST" | "PUT" | "PATCH" | "DELETE") {
        if !second.starts_with("/v1/") {
            return ParsedCommand::Error("/api path must be a local /v1/ route".to_string());
        }
        let body = if third.is_empty() {
            None
        } else {
            match serde_json::from_str::<Value>(third) {
                Ok(v) => Some(v),
                Err(e) => {
                    return ParsedCommand::Error(format!("/api body must be valid JSON: {e}"));
                }
            }
        };
        return ParsedCommand::ApiRaw {
            method: upper,
            path: second.to_string(),
            body,
        };
    }

    let (term, page) = parse_term_and_page(rest);
    ParsedCommand::ApiBrowse { term, page }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_python_slash_command_is_present_in_rust_catalog() {
        let python_commands = [
            "/help",
            "/agent",
            "/project",
            "/projects",
            "/model",
            "/status",
            "/pick",
            "/confirm",
            "/expand",
            "/new",
            "/thread",
            "/tasks",
            "/task",
            "/decisions",
            "/decision",
            "/runs",
            "/run",
            "/follow",
            "/alerts",
            "/goals",
            "/watch",
            "/overwatch",
            "/drive",
            "/go",
            "/ops",
            "/describe",
            "/op",
            "/api",
            "/coverage",
            "/clear",
            "/quit",
        ];
        for cmd in python_commands {
            assert!(
                SLASH_COMMANDS.iter().any(|spec| spec.name == cmd),
                "missing Python parity command: {cmd}"
            );
        }
    }

    #[test]
    fn parses_new_mt2_and_mt7_commands_and_bare_pick_numbers() {
        assert_eq!(parse_input("2"), ParsedCommand::Pick(Some(2)));
        assert_eq!(parse_input("/resume"), ParsedCommand::Resume(None));
        assert_eq!(
            parse_input("/resume msess_123"),
            ParsedCommand::Resume(Some("msess_123".to_string()))
        );
        assert_eq!(parse_input("/fork"), ParsedCommand::Fork);
        assert_eq!(
            parse_input("/compact 4"),
            ParsedCommand::Compact { keep_recent: 4 }
        );
        assert_eq!(parse_input("/copy"), ParsedCommand::Copy);
        assert_eq!(parse_input("/diff"), ParsedCommand::Diff);
        assert_eq!(
            parse_input("/mode risky-only"),
            ParsedCommand::Mode(Some(ApprovalMode::RiskyOnly))
        );
        assert_eq!(parse_input("/grants"), ParsedCommand::GrantsList);
        assert_eq!(
            parse_input("/grants revoke mgrant_1"),
            ParsedCommand::GrantRevoke("mgrant_1".to_string())
        );
    }

    #[test]
    fn parses_op_and_api_forms() {
        assert!(matches!(
            parse_input(r#"/op post_v1_tasks {"body":{"title":"hello"}}"#),
            ParsedCommand::Op { operation_id, args: Some(_) } if operation_id == "post_v1_tasks"
        ));
        assert!(matches!(
            parse_input("/api GET /v1/tasks?project_id=boss-dogfood"),
            ParsedCommand::ApiRaw { method, path, body: None } if method == "GET" && path.starts_with("/v1/tasks")
        ));
        assert!(matches!(
            parse_input("/api GET /admin"),
            ParsedCommand::Error(_)
        ));
    }
}
