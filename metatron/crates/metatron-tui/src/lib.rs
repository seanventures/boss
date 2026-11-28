//! Ratatui terminal user interface for Metatron (`MT-2b`, `MT-7`, `MT-3`).
//!
//! Achieves 100% slash-command parity with `scripts/metatron_tui.py` (all 27 Python
//! commands plus `/resume`, `/fork`, `/compact`, `/copy`, `/export`, `/diff`, `/mode`,
//! `/grants`, `/policy`, `/hosts`) and adds the governed approval overlay.

pub mod app;
pub mod approval_overlay;
pub mod commands;
pub mod ui;

pub use app::{PickerItem, PickerState, TranscriptEntry, TuiApp};
pub use approval_overlay::{ApprovalKeyAction, ApprovalOverlay};
pub use commands::{
    ParsedCommand, SLASH_COMMANDS, SlashCommandSpec, complete_slash_commands, parse_input,
};
pub use ui::{render_app, render_to_string, run_interactive, run_plain};
