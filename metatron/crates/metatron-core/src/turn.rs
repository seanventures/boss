//! Streaming turn driver for `metatron exec` and interactive TUI (`MT-2`, `MT-3`).

use std::path::PathBuf;
use std::time::Duration;

use chrono::Utc;
use futures_util::StreamExt;
use metatron_boss_client::{BossClient, BossClientError};
use metatron_protocol::{ApprovalMode, ClientInfo, ExecEvent, SseEvent, TurnContext, TurnRequest};
use metatron_session::{SessionItem, SessionStore};
use serde_json::Value;
use thiserror::Error;
use uuid::Uuid;

use crate::context::{
    DEFAULT_AGENTS_MD_MAX_BYTES, DEFAULT_AT_FILE_MAX_BYTES, collect_agents_md,
    resolve_at_file_mentions,
};
use crate::hooks::HookEventKind;
use crate::orchestrator::{OrchestratorError, ToolCtx, ToolOrchestrator};

#[derive(Debug, Error)]
pub enum TurnDriverError {
    #[error("BOSS client error: {0}")]
    BossClient(#[from] BossClientError),
    #[error("tool orchestration error: {0}")]
    Orchestrator(#[from] OrchestratorError),
    #[error("turn failed ({code}): {message}")]
    TurnFailed {
        code: String,
        message: String,
        retryable: bool,
    },
    #[error("output failed --output-schema validation: {0}")]
    SchemaMismatch(String),
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone)]
pub struct TurnDriverOptions {
    pub project_id: String,
    pub thread_id: Option<String>,
    pub agent: Option<String>,
    pub mode: ApprovalMode,
    pub cwd: PathBuf,
    pub session_id: String,
    pub max_reconnects: usize,
    pub output_file: Option<PathBuf>,
    pub output_schema: Option<Value>,
}

#[derive(Debug, Clone)]
pub struct TurnRunResult {
    pub turn_id: String,
    pub thread_id: String,
    pub final_message: String,
    pub stop_reason: String,
    pub events: Vec<ExecEvent>,
}

/// Execute one complete streaming turn (with automatic `Last-Event-ID` reconnect),
/// emitting `ExecEvent`s to `on_event`, running any server-requested client `tool_call`
/// through `orchestrator`, persisting to `session_store`, and flushing audit events.
pub async fn drive_turn<F>(
    client: &BossClient,
    orchestrator: &ToolOrchestrator,
    session_store: Option<&SessionStore>,
    tool_ctx: &ToolCtx,
    opts: &TurnDriverOptions,
    prompt: &str,
    mut on_event: F,
) -> Result<TurnRunResult, TurnDriverError>
where
    F: FnMut(&ExecEvent),
{
    let agents_md = collect_agents_md(&opts.cwd, DEFAULT_AGENTS_MD_MAX_BYTES);
    let files =
        resolve_at_file_mentions(prompt, &opts.cwd, DEFAULT_AT_FILE_MAX_BYTES).unwrap_or_default();

    let req = TurnRequest {
        project_id: opts.project_id.clone(),
        thread_id: opts.thread_id.clone(),
        agent: opts.agent.clone(),
        message: prompt.to_string(),
        mode: opts.mode,
        client: ClientInfo::current(),
        context: TurnContext {
            cwd_label: Some(opts.cwd.display().to_string()),
            agents_md,
            files,
            attachments: Vec::new(),
        },
        client_tools: orchestrator.visible_tools(),
    };

    if let Some(store) = session_store {
        let _ = store.append(
            &opts.session_id,
            &SessionItem::UserMessage {
                ts: Utc::now(),
                text: prompt.to_string(),
            },
        );
    }

    let idempotency_key = Uuid::new_v4().to_string();
    let mut events: Vec<ExecEvent> = Vec::new();
    let mut turn_id = String::new();
    let mut thread_id = opts.thread_id.clone().unwrap_or_default();
    let mut final_message = String::new();
    let mut stop_reason = "end_turn".to_string();
    let mut failure: Option<(String, String, bool)> = None;
    let mut emitted_thread_started = false;
    let mut msg_item_started = false;

    let mut stream = client.start_turn_stream(&req, &idempotency_key).await?;
    let mut last_event_id: Option<String> = None;
    let mut saw_terminal = false;
    let mut reconnects = 0usize;

    loop {
        let mut stream_err: Option<BossClientError> = None;
        while let Some(item) = stream.next().await {
            let frame = match item {
                Ok(f) => f,
                Err(e) => {
                    stream_err = Some(e);
                    break;
                }
            };
            if let Some(ref id) = frame.id {
                last_event_id = Some(id.clone());
            }

            let mut emit = |ev: ExecEvent| {
                on_event(&ev);
                events.push(ev);
            };

            match frame.event {
                SseEvent::TurnStarted {
                    turn_id: tid,
                    thread_id: thid,
                    model,
                    mode,
                    tools,
                    dropped,
                } => {
                    turn_id = tid.clone();
                    thread_id = thid.clone();
                    // MT-9: record the exact per-step tool list confirmed by the server in the session JSONL.
                    if let Some(store) = session_store {
                        let _ = store.append(
                            &opts.session_id,
                            &SessionItem::TurnTools {
                                ts: Utc::now(),
                                turn_id: tid.clone(),
                                tools: tools.clone(),
                                dropped: dropped.clone(),
                            },
                        );
                    }
                    if !emitted_thread_started {
                        emitted_thread_started = true;
                        emit(ExecEvent::ThreadStarted {
                            thread_id: thid.clone(),
                        });
                    }
                    emit(ExecEvent::TurnStarted {
                        turn_id: tid,
                        thread_id: thid,
                        model,
                        mode,
                        tools,
                        dropped,
                    });
                }
                SseEvent::TextDelta { text } => {
                    if !msg_item_started {
                        msg_item_started = true;
                        emit(ExecEvent::ItemStarted {
                            id: "assistant_message".to_string(),
                            kind: "agent_message".to_string(),
                            name: "assistant".to_string(),
                            summary: "streaming reply".to_string(),
                        });
                    }
                    final_message.push_str(&text);
                    emit(ExecEvent::ItemUpdated {
                        id: "assistant_message".to_string(),
                        delta: text,
                    });
                }
                SseEvent::ServerToolStarted {
                    call_id,
                    name,
                    summary,
                    ..
                } => {
                    emit(ExecEvent::ItemStarted {
                        id: call_id,
                        kind: "server_tool".to_string(),
                        name,
                        summary,
                    });
                }
                SseEvent::ServerToolFinished {
                    call_id,
                    summary,
                    ok,
                    ..
                } => {
                    emit(ExecEvent::ItemCompleted {
                        id: call_id,
                        kind: "server_tool".to_string(),
                        status: if ok { "ok" } else { "error" }.to_string(),
                        text: Some(summary),
                        exit_code: None,
                        truncated: false,
                    });
                }
                SseEvent::ToolCall {
                    call_id,
                    name,
                    arguments,
                    ..
                } => {
                    emit(ExecEvent::ItemStarted {
                        id: call_id.clone(),
                        kind: "client_tool".to_string(),
                        name: name.clone(),
                        summary: format!("executing {name}"),
                    });
                    let submission = orchestrator
                        .execute_tool_call(
                            tool_ctx,
                            (!turn_id.is_empty()).then_some(turn_id.as_str()),
                            &call_id,
                            &name,
                            arguments,
                        )
                        .await?;

                    let text_preview = submission
                        .output
                        .content
                        .iter()
                        .filter_map(|c| match c {
                            metatron_protocol::ToolContentItem::Text { text } => {
                                Some(text.as_str())
                            }
                            metatron_protocol::ToolContentItem::Image { .. } => None,
                        })
                        .collect::<Vec<_>>()
                        .join("\n");
                    let status_str = format!("{:?}", submission.status).to_ascii_lowercase();
                    emit(ExecEvent::ItemCompleted {
                        id: call_id,
                        kind: "client_tool".to_string(),
                        status: status_str,
                        text: Some(text_preview),
                        exit_code: submission.output.exit_code,
                        truncated: submission.output.truncated,
                    });

                    if !turn_id.is_empty() {
                        client.submit_tool_result(&turn_id, &submission).await?;
                    }
                }
                SseEvent::PolicyDenied {
                    call_id,
                    reason,
                    rule_id,
                } => {
                    emit(ExecEvent::ItemCompleted {
                        id: call_id,
                        kind: "policy_denied".to_string(),
                        status: "denied".to_string(),
                        text: Some(format!("{reason} (rule={rule_id})")),
                        exit_code: None,
                        truncated: false,
                    });
                }
                SseEvent::Usage {
                    input_tokens,
                    output_tokens,
                    cost,
                } => {
                    emit(ExecEvent::Usage {
                        input_tokens,
                        output_tokens,
                        cost,
                    });
                }
                SseEvent::TurnCompleted {
                    turn_id: tid,
                    stop_reason: sr,
                } => {
                    saw_terminal = true;
                    turn_id = tid.clone();
                    stop_reason = sr.clone();
                    if msg_item_started {
                        emit(ExecEvent::ItemCompleted {
                            id: "assistant_message".to_string(),
                            kind: "agent_message".to_string(),
                            status: "ok".to_string(),
                            text: Some(final_message.clone()),
                            exit_code: None,
                            truncated: false,
                        });
                    }
                    emit(ExecEvent::TurnCompleted {
                        turn_id: tid,
                        stop_reason: sr,
                        final_message: Some(final_message.clone()),
                    });
                    break;
                }
                SseEvent::TurnFailed {
                    turn_id: tid,
                    code,
                    message,
                    retryable,
                } => {
                    saw_terminal = true;
                    turn_id = tid.clone();
                    failure = Some((code.clone(), message.clone(), retryable));
                    emit(ExecEvent::TurnFailed {
                        turn_id: tid,
                        code,
                        message,
                        retryable,
                    });
                    break;
                }
            }
        }

        if saw_terminal {
            break;
        }
        if reconnects >= opts.max_reconnects || turn_id.is_empty() {
            return Err(TurnDriverError::BossClient(stream_err.unwrap_or_else(
                || BossClientError::SseParse("turn stream ended before terminal event".to_string()),
            )));
        }
        reconnects += 1;
        tokio::time::sleep(Duration::from_millis(50 * reconnects as u64)).await;
        stream = client
            .resume_turn_stream(&turn_id, last_event_id.as_deref())
            .await?;
    }

    // Best-effort flush of buffered audit events after every turn.
    let _ = orchestrator.audit().flush(client).await;

    // MT-5: Fire configured `on_run_complete` hooks with redacted payload.
    if let Some(ref hooks) = tool_ctx.hooks {
        let hook_payload = serde_json::json!({
            "event": "on_run_complete",
            "session_id": opts.session_id,
            "turn_id": turn_id,
            "thread_id": thread_id,
            "stop_reason": stop_reason,
            "ok": failure.is_none(),
        });
        let _ = hooks
            .fire(HookEventKind::OnRunComplete, &hook_payload)
            .await;
    }

    if let Some((code, message, retryable)) = failure {
        return Err(TurnDriverError::TurnFailed {
            code,
            message,
            retryable,
        });
    }

    if let Some(store) = session_store {
        let _ = store.append(
            &opts.session_id,
            &SessionItem::AssistantMessage {
                ts: Utc::now(),
                turn_id: Some(turn_id.clone()),
                text: final_message.clone(),
            },
        );
    }

    if let Some(ref schema) = opts.output_schema {
        let parsed: Value = serde_json::from_str(&final_message).map_err(|e| {
            TurnDriverError::SchemaMismatch(format!("final message is not valid JSON: {e}"))
        })?;
        validate_json_against_schema(&parsed, schema).map_err(TurnDriverError::SchemaMismatch)?;
    }

    if let Some(ref out_path) = opts.output_file {
        if let Some(parent) = out_path.parent()
            && !parent.as_os_str().is_empty()
        {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(out_path, &final_message)?;
    }

    Ok(TurnRunResult {
        turn_id,
        thread_id,
        final_message,
        stop_reason,
        events,
    })
}

/// Lightweight JSON Schema validator supporting `type`, `required`, `properties`, `items`, `enum`.
pub fn validate_json_against_schema(value: &Value, schema: &Value) -> Result<(), String> {
    if let Some(expected_type) = schema.get("type").and_then(Value::as_str) {
        let type_ok = match expected_type {
            "object" => value.is_object(),
            "array" => value.is_array(),
            "string" => value.is_string(),
            "number" => value.is_number(),
            "integer" => value.is_i64() || value.is_u64(),
            "boolean" => value.is_boolean(),
            "null" => value.is_null(),
            _ => true,
        };
        if !type_ok {
            return Err(format!("expected JSON type '{expected_type}'"));
        }
    }
    if let Some(enum_vals) = schema.get("enum").and_then(Value::as_array)
        && !enum_vals.contains(value)
    {
        return Err("value is not in schema enum".to_string());
    }
    if let Some(obj) = value.as_object() {
        if let Some(req) = schema.get("required").and_then(Value::as_array) {
            for k in req {
                if let Some(key) = k.as_str()
                    && !obj.contains_key(key)
                {
                    return Err(format!("missing required property '{key}'"));
                }
            }
        }
        if let Some(props) = schema.get("properties").and_then(Value::as_object) {
            for (k, prop_schema) in props {
                if let Some(v) = obj.get(k) {
                    validate_json_against_schema(v, prop_schema)
                        .map_err(|e| format!("property '{k}': {e}"))?;
                }
            }
        }
    }
    if let Some(arr) = value.as_array()
        && let Some(item_schema) = schema.get("items")
    {
        for (idx, item) in arr.iter().enumerate() {
            validate_json_against_schema(item, item_schema)
                .map_err(|e| format!("item[{idx}]: {e}"))?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn validates_output_schema_constraints() {
        let schema = json!({
            "type": "object",
            "required": ["status", "count"],
            "properties": {
                "status": { "type": "string", "enum": ["ok", "degraded"] },
                "count": { "type": "integer" }
            }
        });
        assert!(
            validate_json_against_schema(&json!({"status": "ok", "count": 2}), &schema).is_ok()
        );
        assert!(validate_json_against_schema(&json!({"status": "ok"}), &schema).is_err());
        assert!(
            validate_json_against_schema(&json!({"status": "bad", "count": 2}), &schema).is_err()
        );
    }
}
