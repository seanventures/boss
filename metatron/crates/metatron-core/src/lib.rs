//! Core Metatron configuration/profiles, repo context assembly (`AGENTS.md` + `@file`),
//! governed tool orchestration (7-step policy/sandbox/escalation/audit), and SSE turn driver.

pub mod config;
pub mod context;
pub mod orchestrator;
pub mod turn;

pub use config::{ConfigError, MetatronConfig, ProfileConfig, ResolvedProfile};
pub use context::{ContextError, collect_agents_md, collect_git_diff, resolve_at_file_mentions};
pub use orchestrator::{
    ApprovalPromptRequest, ApprovalPrompter, BossReviewerClient, HumanApproval, OrchestratorError,
    ReviewerBackend, ShellLocalTool, SshRunTool, Tool, ToolCtx, ToolOrchestrator, ToolOutcome,
};
pub use turn::{TurnDriverOptions, TurnRunResult, drive_turn, validate_json_against_schema};
