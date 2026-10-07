"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import { GatedButton, useGate } from "../gated";
import React, { useCallback, useEffect, useRef, useState } from "react";

import type { V2Agent } from "../../lib/v2/workforce";
import { apiWrite } from "../../lib/api-write";

/**
 * Talk to one agent.
 *
 * THE REPLY MUST SURVIVE ARRIVING
 * -------------------------------
 * v1's chat lost its answer the instant it came back: the action called revalidatePath("/"),
 * which re-rendered the console tree underneath the panel and snapped it to its empty state.
 * Question sent, "3s" ticking, then the openers again — as though nothing had been asked.
 *
 * So this holds the transcript in component state and talks to a route handler, which returns
 * JSON and nothing else. A chat that loses the reply it just received is worse than a slow one.
 *
 * ORDER IS EXPLICIT
 * -----------------
 * Messages are ordered by arrival here, not by a timestamp. A question and its answer can share
 * a created_at — Postgres now() is transaction start — and sorting on that alone put four of
 * five answers ABOVE their own question in the v1 transcript.
 */

/** `technical` is the failure's own line, shown only under a collapsed Details. */
type Turn = { role: "you" | "agent" | "error"; text: string; technical?: string };

export function AgentChat({
  agent,
  projectId,
  actor,
  onClose,
}: {
  agent: V2Agent;
  projectId: string;
  actor: string;
  onClose: () => void;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [threadId, setThreadId] = useState<string | undefined>(undefined);
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, busy]);

  const chatGate = useGate("assistant:chat");
  const send = useCallback(async () => {
    const message = draft.trim();
    if (!message || busy) return;
    setDraft("");
    // Appended in order, before the request. The operator's own words must never be the thing
    // that goes missing while they wait.
    setTurns((t) => [...t, { role: "you", text: message }]);
    setBusy(true);
    try {
      const res = await apiWrite("/api/agent-chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: projectId,
          actor,
          message,
          agent_id: agent.id,
          thread_id: threadId,
        }),
      });
      const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
      if (!res.ok) {
        const technical = typeof data.technical === "string" ? data.technical : undefined;
        setTurns((t) => [...t, { role: "error", text: String(data.error ?? `HTTP ${res.status}`),
                                 technical }]);
        return;
      }
      if (typeof data.thread_id === "string") setThreadId(data.thread_id);
      const reply = String(data.reply ?? data.message ?? data.text ?? "").trim();
      setTurns((t) => [
        ...t,
        reply
          ? { role: "agent", text: reply }
          // A blank reply is a failure, not a silence. Rendering nothing would look like the
          // agent chose not to answer.
          : { role: "error", text: "The agent returned an empty reply." },
      ]);
    } catch (e) {
      setTurns((t) => [...t, { role: "error", text: String((e as Error)?.message ?? e) }]);
    } finally {
      setBusy(false);
    }
  }, [draft, busy, projectId, actor, agent.id, threadId]);

  return (
    <aside className="v2-chat" data-testid="v2-chat" aria-label={`Chat with ${agent.name}`}>
      <header className="v2-chat-head">
        <span className="v2-card-name">{agent.name}</span>
        <span className="v2-card-role">{agent.role}</span>
        <button
          type="button"
          className="v2-chat-close"
          onClick={onClose}
          aria-label="Close chat"
          data-testid="v2-chat-close"
        >
          ×
        </button>
      </header>

      {/* What it is doing right now, so a question can be about the actual work. */}
      <p className="v2-chat-context" data-testid="v2-chat-context">
        {agent.task}
      </p>

      <div className="v2-chat-log" data-testid="v2-chat-log">
        {turns.length === 0 ? (
          <p className="v2-chat-empty" data-testid="v2-chat-empty">
            Ask about this agent&apos;s work. It answers from live state, not from a summary.
          </p>
        ) : (
          turns.map((t, i) => (
            <p
              key={i}
              className="v2-chat-turn"
              data-role={t.role}
              data-testid={`v2-chat-turn-${i}`}
            >
              <span className="v2-chat-who">{t.role === "you" ? "you" : t.role === "agent" ? agent.name : "error"}</span>
              {t.text}
              {t.technical ? (
                <details className="v2-chat-technical" data-testid={`v2-chat-technical-${i}`}>
                  <summary>Details</summary>
                  {t.technical}
                </details>
              ) : null}
            </p>
          ))
        )}
        {/* A waiting signal. "It says it will monitor and update and I don't see any waiting
            signal, it just quits" was a real complaint about this system. */}
        {busy ? (
          <p className="v2-chat-turn" data-role="pending" data-testid="v2-chat-pending">
            <span className="v2-chat-who">{agent.name}</span>
            thinking…
          </p>
        ) : null}
        <div ref={endRef} />
      </div>

      <div className="v2-chat-composer">
        <textarea
          className="v2-chat-input"
          value={draft}
          rows={2}
          disabled={busy}
          placeholder={`Ask ${agent.name} about its work…`}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends; Shift+Enter is a newline. A chat box that needs a mouse to send is
            // not a chat box.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (chatGate.allowed) void send();
            }
          }}
          data-testid="v2-chat-input"
        />
        <GatedButton permission="assistant:chat"
          type="button"
          className="v2-btn"
          disabled={busy || !draft.trim()}
          onClick={() => void send()}
          data-testid="v2-chat-send"
        >
          {busy ? "Sending…" : "Send"}
        </GatedButton>
      </div>
    </aside>
  );
}
