// Copyright 2025 OpenAI
// SPDX-License-Identifier: Apache-2.0
// Vendored from openai/codex@05ae1b0fdd5ab412d24248acc76c5d9f6f275440 (codex-rs/utils/pty/src/lib.rs)
// changed by BOSS: added test_support::write_executable to drop codex-utils-cargo-bin dev-dep

mod child;
pub use child::Child;
mod child_command;
pub use child_command::ChildStdin;
pub use child_command::Command;
pub use child_command::DescriptorPolicy;
pub use child_command::ProcessMode;
pub use child_command::SpawnFallback;
#[cfg(target_os = "linux")]
mod linux_fds;
pub mod pipe;
mod process;
pub mod process_group;
pub mod pty;
pub use pty::ChildFds;
#[cfg(test)]
mod tests;
#[cfg(unix)]
mod unix_io;
#[cfg(windows)]
mod win;
#[cfg(windows)]
mod windows_input;

pub const DEFAULT_OUTPUT_BYTES_CAP: usize = 1024 * 1024;

/// Spawn a non-interactive process using regular pipes for stdin/stdout/stderr.
pub use pipe::spawn_process as spawn_pipe_process;
/// Spawn a non-interactive process using regular pipes, but close stdin immediately.
pub use pipe::spawn_process_no_stdin as spawn_pipe_process_no_stdin;
/// Driver-backed process adapter used by integrations with their own process transport.
pub use process::ProcessDriver;
/// Handle for interacting with a spawned process (PTY or pipe).
pub use process::ProcessHandle;
/// Process signal supported by spawned-process handles.
pub use process::ProcessSignal;
/// Bundle of process handles plus split output and exit receivers returned by spawn helpers.
pub use process::SpawnedProcess;
/// Terminal size in character cells used for PTY spawn and resize operations.
pub use process::TerminalSize;
/// Combine stdout/stderr receivers into a single broadcast receiver.
pub use process::combine_output_receivers;
/// Adapt an externally-driven process into the standard spawned-process handle.
pub use process::spawn_from_driver;
/// Backwards-compatible alias for ProcessHandle.
pub type ExecCommandSession = ProcessHandle;
/// Backwards-compatible alias for SpawnedProcess.
pub type SpawnedPty = SpawnedProcess;
/// Report whether ConPTY is available on this platform (Windows only).
pub use pty::conpty_supported;
/// Spawn a process attached to a PTY for interactive use.
pub use pty::spawn_process as spawn_pty_process;
#[cfg(windows)]
pub use win::JobObject;
#[cfg(windows)]
pub use win::PsuedoCon;
#[cfg(windows)]
pub use win::conpty::RawConPty;
#[cfg(windows)]
pub use windows_input::WindowsTtyInputNormalizer;

#[cfg(target_os = "linux")]
mod spawn_helper;
#[cfg(target_os = "linux")]
mod spawn_helper_main;
#[cfg(target_os = "linux")]
pub use spawn_helper::init_spawn_helper;

#[cfg(all(test, target_os = "linux"))]
use tests::spawn_helper_tests;

// changed by BOSS: local test_support::write_executable replaces codex-utils-cargo-bin
#[cfg(test)]
pub(crate) mod test_support {
    use std::path::Path;

    pub(crate) fn write_executable(path: &Path, script: &str) -> std::io::Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(path, script)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(path)?.permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(path, perms)?;
        }
        Ok(())
    }
}
