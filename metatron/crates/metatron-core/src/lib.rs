//! Core Metatron configuration/profiles, repo context assembly (`AGENTS.md` + `@file`),
//! governed tool orchestration (7-step policy/sandbox/escalation/audit), tool registry/router,
//! shared BOSS + local skills loader, notification hooks, and SSE turn driver.

pub mod config;
pub mod context;
pub mod hooks;
pub mod orchestrator;
pub mod registry;
pub mod skills;
pub mod turn;

pub use config::{ConfigError, MetatronConfig, ProfileConfig, ResolvedProfile};
pub use context::{ContextError, collect_agents_md, collect_git_diff, resolve_at_file_mentions};
pub use hooks::{HookConfig, HookEventKind, HookOutcome, HookRunner};
pub use orchestrator::{
    ApprovalPromptRequest, ApprovalPrompter, BossReviewerClient, HttpAuditMeta, HumanApproval,
    OrchestratorError, ReviewerBackend, ShellLocalTool, SshRunTool, Tool, ToolCtx,
    ToolOrchestrator, ToolOutcome,
};
pub use registry::{McpProxyTool, ToolRegistry, ToolSearchTool};
pub use skills::{
    SkillError, SkillMetadata, SkillRegistry, SkillSource, parse_skill_md_frontmatter,
};
pub use turn::{TurnDriverOptions, TurnRunResult, drive_turn, validate_json_against_schema};
