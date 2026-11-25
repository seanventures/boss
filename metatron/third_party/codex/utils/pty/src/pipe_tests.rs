// Copyright 2025 OpenAI
// SPDX-License-Identifier: Apache-2.0
// Vendored from openai/codex@05ae1b0fdd5ab412d24248acc76c5d9f6f275440 (codex-rs/utils/pty/src/pipe_tests.rs)

use super::*;
use std::process::Stdio;

#[test]
fn process_fallback_interrupt_terminates_root() -> anyhow::Result<()> {
    let mut child = std::process::Command::new("ping.exe")
        .args(["-n", "60", "127.0.0.1"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    let mut terminator = PipeChildTerminator {
        windows: WindowsChildTerminator::Process(child.id()),
    };

    terminator.signal(ProcessSignal::Interrupt)?;

    assert!(!child.wait()?.success());
    Ok(())
}
