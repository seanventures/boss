//! Process execution, PTY/pipe spawning, process-group cleanup, head+tail output
//! capping, timeouts, and yield-and-poll background sessions (`MT-3`).
//!
//! Built directly on the vendored `codex-utils-pty` and `codex-utils-head-tail-buffer` crates.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, Instant};

use codex_utils_head_tail_buffer::HeadTailBuffer;
use codex_utils_pty::{
    ChildFds, ProcessHandle, TerminalSize, combine_output_receivers, spawn_pipe_process_no_stdin,
    spawn_pty_process,
};
use thiserror::Error;
use tokio::sync::{broadcast, oneshot, watch};
use uuid::Uuid;

/// Default inline model output cap (64 KiB, matching MT-10 `tool_result_inline_bytes`).
pub const MODEL_OUTPUT_CAP_BYTES: usize = 65_536;
/// Standard timeout exit code (matching Codex `exec.rs` and GNU `timeout`).
pub const TIMEOUT_EXIT_CODE: i32 = 124;

#[derive(Debug, Error)]
pub enum ExecError {
    #[error("failed to spawn command '{program}': {detail}")]
    Spawn { program: String, detail: String },
    #[error("exec session '{0}' not found")]
    SessionNotFound(String),
    #[error("failed to write stdin to session '{0}': {1}")]
    StdinError(String, String),
}

/// Request to execute a local command (directly or wrapped in a sandbox).
#[derive(Debug, Clone)]
pub struct ExecRequest {
    pub program: String,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub env: HashMap<String, String>,
    pub timeout: Duration,
    pub use_pty: bool,
    pub yield_time_ms: Option<u64>,
}

impl ExecRequest {
    pub fn new(program: impl Into<String>, args: Vec<String>, cwd: PathBuf) -> Self {
        Self {
            program: program.into(),
            args,
            cwd,
            env: std::env::vars().collect(),
            timeout: Duration::from_secs(60),
            use_pty: false,
            yield_time_ms: None,
        }
    }
}

/// Result of a completed command execution.
#[derive(Debug, Clone)]
pub struct ExecOutput {
    pub exit_code: i32,
    pub timed_out: bool,
    pub cancelled: bool,
    /// Head+tail capped output suitable for sending to BOSS / the model.
    pub inline_output: String,
    pub truncated: bool,
    pub omitted_bytes: usize,
    /// Full un-truncated output for local session JSONL storage.
    pub full_output: String,
    pub total_bytes: usize,
    pub duration_ms: u64,
}

/// Result of starting a command that may either finish quickly or yield a session ID.
#[derive(Debug, Clone)]
pub enum ExecStartOutcome {
    Completed(ExecOutput),
    Yielded {
        session_id: String,
        partial_output: String,
    },
}

/// Result of polling a yielded session.
#[derive(Debug, Clone)]
pub enum ExecPollOutcome {
    Running {
        session_id: String,
        delta_output: String,
    },
    Completed(ExecOutput),
}

/// Run a command to completion (or timeout/cancellation), capping inline output with
/// `HeadTailBuffer<MODEL_OUTPUT_CAP_BYTES>` while preserving full output.
pub async fn run_command(
    req: ExecRequest,
    cancel_rx: Option<watch::Receiver<bool>>,
) -> Result<ExecOutput, ExecError> {
    let start = Instant::now();
    let spawned = spawn_underlying(&req).await?;
    let handle = spawned.session;
    let mut output_rx = combine_output_receivers(spawned.stdout_rx, spawned.stderr_rx);
    let mut exit_rx = spawned.exit_rx;

    let mut capped = HeadTailBuffer::<MODEL_OUTPUT_CAP_BYTES>::default();
    let mut full = Vec::new();

    let mut cancel_rx = cancel_rx;
    let deadline = tokio::time::sleep(req.timeout);
    tokio::pin!(deadline);

    let mut exit_code: Option<i32> = None;
    let mut timed_out = false;
    let mut cancelled = false;

    loop {
        tokio::select! {
            biased;
            _ = async {
                if let Some(ref mut rx) = cancel_rx {
                    while !*rx.borrow() {
                        if rx.changed().await.is_err() {
                            std::future::pending::<()>().await;
                        }
                    }
                } else {
                    std::future::pending::<()>().await;
                }
            } => {
                cancelled = true;
                handle.terminate();
                break;
            }
            _ = &mut deadline => {
                timed_out = true;
                handle.terminate();
                break;
            }
            res = &mut exit_rx => {
                exit_code = Some(res.unwrap_or(-1));
                break;
            }
            chunk = output_rx.recv() => {
                match chunk {
                    Ok(bytes) => {
                        capped.push_chunk(&bytes);
                        full.extend_from_slice(&bytes);
                    }
                    Err(broadcast::error::RecvError::Lagged(_)) => {}
                    Err(broadcast::error::RecvError::Closed) => {
                        if exit_code.is_none() {
                            exit_code = tokio::time::timeout(Duration::from_millis(500), &mut exit_rx)
                                .await
                                .ok()
                                .and_then(Result::ok);
                        }
                        break;
                    }
                }
            }
        }
    }

    // Drain any buffered output briefly after normal exit.
    if !cancelled && !timed_out {
        let drain_deadline = tokio::time::sleep(Duration::from_millis(250));
        tokio::pin!(drain_deadline);
        loop {
            tokio::select! {
                _ = &mut drain_deadline => break,
                chunk = output_rx.recv() => {
                    match chunk {
                        Ok(bytes) => {
                            capped.push_chunk(&bytes);
                            full.extend_from_slice(&bytes);
                        }
                        Err(broadcast::error::RecvError::Lagged(_)) => {}
                        Err(broadcast::error::RecvError::Closed) => break,
                    }
                }
            }
        }
    }

    // Always ensure process group is terminated before returning.
    if timed_out || cancelled {
        handle.terminate();
    }

    let final_code = if timed_out {
        TIMEOUT_EXIT_CODE
    } else if cancelled {
        130
    } else {
        exit_code.unwrap_or(-1)
    };

    let inline_bytes = capped.to_bytes_with_omission_marker();
    Ok(ExecOutput {
        exit_code: final_code,
        timed_out,
        cancelled,
        inline_output: String::from_utf8_lossy(&inline_bytes).into_owned(),
        truncated: capped.omitted_bytes() > 0,
        omitted_bytes: capped.omitted_bytes(),
        full_output: String::from_utf8_lossy(&full).into_owned(),
        total_bytes: full.len(),
        duration_ms: start.elapsed().as_millis() as u64,
    })
}

async fn spawn_underlying(req: &ExecRequest) -> Result<codex_utils_pty::SpawnedProcess, ExecError> {
    if req.use_pty {
        spawn_pty_process(
            &req.program,
            &req.args,
            &req.cwd,
            &req.env,
            &None,
            TerminalSize::default(),
            ChildFds::Inherited(&[]),
        )
        .await
        .map_err(|e| ExecError::Spawn {
            program: req.program.clone(),
            detail: e.to_string(),
        })
    } else {
        spawn_pipe_process_no_stdin(&req.program, &req.args, &req.cwd, &req.env, &None, &[])
            .await
            .map_err(|e| ExecError::Spawn {
                program: req.program.clone(),
                detail: e.to_string(),
            })
    }
}

struct LiveSession {
    handle: ProcessHandle,
    output_rx: broadcast::Receiver<Vec<u8>>,
    exit_rx: Option<oneshot::Receiver<i32>>,
    capped: HeadTailBuffer<MODEL_OUTPUT_CAP_BYTES>,
    full: Vec<u8>,
    started_at: Instant,
    timeout: Duration,
}

/// Manages yield-and-poll sessions for long-running commands (`ARCHITECTURE.md` §1.3).
#[derive(Default)]
pub struct ExecSessionManager {
    sessions: Arc<Mutex<HashMap<String, Arc<tokio::sync::Mutex<LiveSession>>>>>,
}

impl ExecSessionManager {
    pub fn new() -> Self {
        Self::default()
    }

    /// Start a command; if it completes within `yield_time_ms`, returns `Completed`,
    /// otherwise yields a `session_id` that callers can poll or terminate.
    pub async fn start(&self, req: ExecRequest) -> Result<ExecStartOutcome, ExecError> {
        let yield_ms = req.yield_time_ms.unwrap_or(10_000);
        let spawned = spawn_underlying(&req).await?;
        let session_id = format!("exec_{}", Uuid::new_v4().simple());
        let session = Arc::new(tokio::sync::Mutex::new(LiveSession {
            handle: spawned.session,
            output_rx: combine_output_receivers(spawned.stdout_rx, spawned.stderr_rx),
            exit_rx: Some(spawned.exit_rx),
            capped: HeadTailBuffer::<MODEL_OUTPUT_CAP_BYTES>::default(),
            full: Vec::new(),
            started_at: Instant::now(),
            timeout: req.timeout,
        }));
        {
            let mut guard = self.sessions.lock().unwrap_or_else(PoisonError::into_inner);
            guard.insert(session_id.clone(), Arc::clone(&session));
        }

        match self
            .poll(&session_id, Duration::from_millis(yield_ms))
            .await?
        {
            ExecPollOutcome::Completed(out) => Ok(ExecStartOutcome::Completed(out)),
            ExecPollOutcome::Running { delta_output, .. } => Ok(ExecStartOutcome::Yielded {
                session_id,
                partial_output: delta_output,
            }),
        }
    }

    pub async fn poll(
        &self,
        session_id: &str,
        wait: Duration,
    ) -> Result<ExecPollOutcome, ExecError> {
        let session = {
            let guard = self.sessions.lock().unwrap_or_else(PoisonError::into_inner);
            guard
                .get(session_id)
                .cloned()
                .ok_or_else(|| ExecError::SessionNotFound(session_id.to_string()))?
        };

        let mut delta = Vec::new();
        let mut finished_code: Option<i32> = None;
        let mut timed_out = false;

        {
            let mut live = session.lock().await;
            let remaining_timeout = live.timeout.saturating_sub(live.started_at.elapsed());
            let effective_wait = wait.min(remaining_timeout);
            let sleep = tokio::time::sleep(effective_wait);
            tokio::pin!(sleep);

            let mut exit_rx = live.exit_rx.take();
            loop {
                tokio::select! {
                    biased;
                    _ = &mut sleep => {
                        if live.started_at.elapsed() >= live.timeout {
                            timed_out = true;
                            live.handle.terminate();
                        }
                        break;
                    }
                    res = async {
                        if let Some(ref mut rx) = exit_rx {
                            rx.await
                        } else {
                            std::future::pending().await
                        }
                    } => {
                        finished_code = Some(res.unwrap_or(-1));
                        exit_rx = None;
                        break;
                    }
                    chunk = live.output_rx.recv() => {
                        match chunk {
                            Ok(bytes) => {
                                live.capped.push_chunk(&bytes);
                                live.full.extend_from_slice(&bytes);
                                delta.extend_from_slice(&bytes);
                            }
                            Err(broadcast::error::RecvError::Lagged(_)) => {}
                            Err(broadcast::error::RecvError::Closed) => {
                                if let Some(ref mut rx) = exit_rx {
                                    finished_code = tokio::time::timeout(Duration::from_millis(200), rx)
                                        .await
                                        .ok()
                                        .and_then(Result::ok);
                                    exit_rx = None;
                                }
                                break;
                            }
                        }
                    }
                }
            }
            live.exit_rx = exit_rx;

            if finished_code.is_some() || timed_out {
                // Drain remaining buffered chunks quickly.
                while let Ok(bytes) = live.output_rx.try_recv() {
                    live.capped.push_chunk(&bytes);
                    live.full.extend_from_slice(&bytes);
                }
                let inline = live.capped.to_bytes_with_omission_marker();
                let out = ExecOutput {
                    exit_code: if timed_out {
                        TIMEOUT_EXIT_CODE
                    } else {
                        finished_code.unwrap_or(-1)
                    },
                    timed_out,
                    cancelled: false,
                    inline_output: String::from_utf8_lossy(&inline).into_owned(),
                    truncated: live.capped.omitted_bytes() > 0,
                    omitted_bytes: live.capped.omitted_bytes(),
                    full_output: String::from_utf8_lossy(&live.full).into_owned(),
                    total_bytes: live.full.len(),
                    duration_ms: live.started_at.elapsed().as_millis() as u64,
                };
                drop(live);
                let mut guard = self.sessions.lock().unwrap_or_else(PoisonError::into_inner);
                guard.remove(session_id);
                return Ok(ExecPollOutcome::Completed(out));
            }
        }

        Ok(ExecPollOutcome::Running {
            session_id: session_id.to_string(),
            delta_output: String::from_utf8_lossy(&delta).into_owned(),
        })
    }

    pub async fn write_stdin(&self, session_id: &str, bytes: &[u8]) -> Result<(), ExecError> {
        let session = {
            let guard = self.sessions.lock().unwrap_or_else(PoisonError::into_inner);
            guard
                .get(session_id)
                .cloned()
                .ok_or_else(|| ExecError::SessionNotFound(session_id.to_string()))?
        };
        let writer = {
            let live = session.lock().await;
            live.handle.writer_sender()
        };
        writer
            .send(bytes.to_vec())
            .await
            .map_err(|e| ExecError::StdinError(session_id.to_string(), e.to_string()))
    }

    pub async fn terminate(&self, session_id: &str) -> Result<(), ExecError> {
        let session = {
            let mut guard = self.sessions.lock().unwrap_or_else(PoisonError::into_inner);
            guard
                .remove(session_id)
                .ok_or_else(|| ExecError::SessionNotFound(session_id.to_string()))?
        };
        let live = session.lock().await;
        live.handle.terminate();
        Ok(())
    }

    pub async fn terminate_all(&self) {
        let all: Vec<_> = {
            let mut guard = self.sessions.lock().unwrap_or_else(PoisonError::into_inner);
            guard.drain().map(|(_, v)| v).collect()
        };
        for sess in all {
            let live = sess.lock().await;
            live.handle.terminate();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[tokio::test]
    async fn large_output_is_head_tail_capped_while_full_output_preserved() {
        let tmp = TempDir::new().expect("tmp");
        let mut req = ExecRequest::new(
            "/bin/sh",
            vec![
                "-c".to_string(),
                "printf 'HEAD_MARKER\\n'; dd if=/dev/zero bs=1024 count=90 2>/dev/null | tr '\\0' 'A'; printf '\\nTAIL_MARKER\\n'".to_string(),
            ],
            tmp.path().to_path_buf(),
        );
        req.timeout = Duration::from_secs(10);
        let out = run_command(req, None).await.expect("run");
        assert_eq!(out.exit_code, 0);
        assert!(out.truncated, "expected head+tail truncation");
        assert!(out.omitted_bytes > 0);
        assert!(out.inline_output.contains("HEAD_MARKER"));
        assert!(out.inline_output.contains("TAIL_MARKER"));
        assert!(out.inline_output.contains("bytes omitted"));
        assert!(out.total_bytes > MODEL_OUTPUT_CAP_BYTES);
        assert_eq!(out.full_output.len(), out.total_bytes);
    }

    #[tokio::test]
    async fn timeout_kills_process_and_returns_124() {
        let tmp = TempDir::new().expect("tmp");
        let mut req = ExecRequest::new(
            "/bin/sleep",
            vec!["30".to_string()],
            tmp.path().to_path_buf(),
        );
        req.timeout = Duration::from_millis(200);
        let out = run_command(req, None).await.expect("run");
        assert!(out.timed_out);
        assert_eq!(out.exit_code, TIMEOUT_EXIT_CODE);
    }

    #[tokio::test]
    async fn cancel_kills_entire_process_group_leaving_no_orphan() {
        let tmp = TempDir::new().expect("tmp");
        let pid_file = tmp.path().join("child.pid");
        let script = format!("sleep 60 & echo $! > '{}'; wait", pid_file.display());
        let mut req = ExecRequest::new(
            "/bin/sh",
            vec!["-c".to_string(), script],
            tmp.path().to_path_buf(),
        );
        req.timeout = Duration::from_secs(30);

        let (tx, rx) = watch::channel(false);
        let pid_file_wait = pid_file.clone();
        tokio::spawn(async move {
            for _ in 0..50 {
                if pid_file_wait.exists()
                    && std::fs::read_to_string(&pid_file_wait)
                        .ok()
                        .is_some_and(|s| !s.trim().is_empty())
                {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
            let _ = tx.send(true);
        });

        let out = run_command(req, Some(rx)).await.expect("run");
        assert!(out.cancelled);

        tokio::time::sleep(Duration::from_millis(150)).await;
        if let Ok(pid_str) = std::fs::read_to_string(&pid_file)
            && let Ok(pid) = pid_str.trim().parse::<i32>()
        {
            // kill(pid, 0) must fail (ESRCH) proving the grandchild was killed with the process group.
            #[cfg(unix)]
            {
                let alive = unsafe { libc::kill(pid, 0) } == 0;
                assert!(
                    !alive,
                    "grandchild process {pid} survived process-group cancel!"
                );
            }
        }
    }

    #[tokio::test]
    async fn yield_and_poll_handles_long_running_command() {
        let tmp = TempDir::new().expect("tmp");
        let mgr = ExecSessionManager::new();
        let mut req = ExecRequest::new(
            "/bin/sh",
            vec![
                "-c".to_string(),
                "echo start; sleep 0.3; echo done".to_string(),
            ],
            tmp.path().to_path_buf(),
        );
        req.use_pty = true;
        req.yield_time_ms = Some(50);

        let started = mgr.start(req).await.expect("start");
        let session_id = match started {
            ExecStartOutcome::Yielded { session_id, .. } => session_id,
            ExecStartOutcome::Completed(out) => {
                assert!(out.full_output.contains("done"));
                return;
            }
        };

        let mut completed = None;
        for _ in 0..20 {
            match mgr
                .poll(&session_id, Duration::from_millis(100))
                .await
                .expect("poll")
            {
                ExecPollOutcome::Completed(out) => {
                    completed = Some(out);
                    break;
                }
                ExecPollOutcome::Running { .. } => {}
            }
        }
        let out = completed.expect("command should finish");
        assert_eq!(out.exit_code, 0);
        assert!(out.full_output.contains("done"));
    }
}
