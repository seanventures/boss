"use client";

/**
 * Ask BOSS — the first thing on the Command Center.
 *
 * What was here was a "Status Query" form that posted to Metatron: three inputs, a GET
 * submit, and a read-only paragraph back. It sat below the fold, it could only answer, and
 * two of its three fields asked the operator for ids they would have to go and look up.
 *
 * The agent can already do all of this and act as well, so the entry point is one line of
 * plain language at the top of the page. Scope is inferred, not typed — an operator who has
 * to find a task id before asking a question will go and read the table instead, which is
 * exactly the behaviour this is meant to replace.
 *
 * The animation earns its place by carrying state rather than decorating: the aura is calm
 * when idle, active while a turn runs, and amber once a turn passes what is normal. There is
 * no purely ornamental motion, and all of it collapses under prefers-reduced-motion.
 */

import { useCallback, useEffect, useRef, useState, useTransition } from "react";

import { Markdown } from "./markdown";
import { askBossAction, runAgentActionAction } from "./actions";

const NORMAL_SECONDS = 20;
const STALL_SECONDS = 120;

type ProposedAction = {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  label: string;
  why?: string;
};

/** Openers that demonstrate the range: read, diagnose, act. Each is a real request. */
const OPENERS = [
  { icon: "◈", text: "What changed, what is blocked, and what should I do next?" },
  { icon: "◆", text: "Which runs failed today, and why?" },
  { icon: "▲", text: "Is anything starving or stuck in a queue?" },
  { icon: "●", text: "What is my spend so far this month?" },
];

export function AskBossHero({
  projectId,
  actor,
  orgId,
}: {
  projectId: string;
  actor: string;
  orgId?: string;
}) {
  const [input, setInput] = useState("");
  const [reply, setReply] = useState("");
  const [proposed, setProposed] = useState<ProposedAction[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [ranAction, setRanAction] = useState<string | null>(null);
  const [busy, startTransition] = useTransition();
  const boxRef = useRef<HTMLTextAreaElement>(null);

  // SURVIVE A RE-RENDER OF THE PAGE UNDERNEATH.
  //
  // Server actions that revalidate ("/") re-render the whole console tree, and this panel's
  // answer went with it -- observed live: the reply arrived and vanished in the same frame.
  // askBossAction no longer revalidates, but runAgentActionAction still must (a button click
  // really does change the board), and any future action might. Rather than audit every call
  // site forever, the panel restores its own last answer. sessionStorage, not local: an answer
  // about live state should not outlive the tab that asked.
  const SESSION_KEY = `boss.ask.${projectId}`;
  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(SESSION_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw) as {
        reply?: string; proposed?: ProposedAction[]; threadId?: string | null;
      };
      if (saved.reply) setReply(saved.reply);
      if (Array.isArray(saved.proposed)) setProposed(saved.proposed);
      if (saved.threadId) setThreadId(saved.threadId);
    } catch {}
    // Restore once, on mount, for this project.
  }, [SESSION_KEY]);

  useEffect(() => {
    try {
      if (!reply && proposed.length === 0) window.sessionStorage.removeItem(SESSION_KEY);
      else window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ reply, proposed, threadId }));
    } catch {}
  }, [SESSION_KEY, reply, proposed, threadId]);

  useEffect(() => {
    if (!busy) { setElapsed(0); return; }
    const t0 = Date.now();
    const id = window.setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, [busy]);

  const ask = useCallback((text: string) => {
    const message = text.trim();
    if (!message || busy) return;
    setError("");
    setReply("");
    setProposed([]);
    const fd = new FormData();
    fd.set("message", message);
    fd.set("project_id", projectId);
    fd.set("actor", actor);
    if (threadId) fd.set("thread_id", threadId);
    startTransition(async () => {
      // Bound the WAIT, not the work: a turn that outlives STALL is reported, never presented
      // as cancelled, because it may still land server-side.
      const stalled = Symbol("stalled");
      const r = await Promise.race([
        askBossAction(null, fd),
        new Promise<typeof stalled>((res) =>
          window.setTimeout(() => res(stalled), STALL_SECONDS * 1000)),
      ]);
      if (r === stalled) {
        setError(
          `No answer after ${STALL_SECONDS}s. The turn may still be running — nothing was ` +
          "cancelled. Ask again in a moment, or open the full agent panel to see if it landed.",
        );
        return;
      }
      if (!r.ok || !r.data) { setError(r.error ?? "request failed"); return; }
      const d = r.data as Record<string, unknown>;
      if (typeof d.thread_id === "string") setThreadId(d.thread_id);
      setProposed(Array.isArray(d.proposed_actions) ? (d.proposed_actions as ProposedAction[]) : []);
      const msgs = Array.isArray(d.messages) ? (d.messages as Array<Record<string, unknown>>) : [];
      const last = [...msgs].reverse().find((m) => m.role === "assistant");
      setReply(String((d.reply as string) ?? (last?.content as string) ?? ""));
    });
  }, [busy, projectId, actor, threadId]);

  const accept = useCallback((a: ProposedAction) => {
    setRanAction(a.id);
    startTransition(async () => {
      const r = await runAgentActionAction(projectId, actor, a.tool, a.args, threadId ?? undefined);
      const data = r.data as Record<string, unknown> | undefined;
      const ok = r.ok && data?.ok !== false;
      setReply((prev) =>
        `${prev}\n\n${ok ? "✓" : "⚠"} **${a.label}**${ok ? " — done." : ` failed: ${data?.error ?? r.error ?? "unknown error"}`}`);
      // An offer is spent once taken: leaving it on screen invites a second, duplicate run.
      setProposed((p) => p.filter((x) => x.id !== a.id));
      setRanAction(null);
    });
  }, [projectId, actor, threadId]);

  const slow = elapsed >= NORMAL_SECONDS;
  const state = busy ? (slow ? "slow" : "thinking") : reply || error ? "answered" : "idle";

  return (
    <section className="askboss" data-state={state} aria-label="Ask BOSS">
      <div className="askboss-aura" aria-hidden />
      <div className="askboss-inner">
        <header className="askboss-head">
          <span className="askboss-mark" aria-hidden>
            <span className="askboss-mark-core" />
          </span>
          <div>
            <h2 className="askboss-title">Ask BOSS</h2>
            <p className="askboss-sub">
              Ask anything about your work — or tell BOSS to do it. It reads live
              control-plane state and acts with your approval.
            </p>
          </div>
        </header>

        <form
          className="askboss-form"
          onSubmit={(e) => { e.preventDefault(); ask(input); }}
        >
          <div className="askboss-box">
            <textarea
              ref={boxRef}
              className="askboss-input"
              value={input}
              rows={1}
              disabled={busy}
              placeholder="Ask BOSS anything, or tell it what to do…"
              onChange={(e) => setInput(e.target.value)}
              // Enter sends; Shift+Enter is a newline. A chat box that needs a mouse to send
              // is not a chat box.
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input); }
              }}
              data-testid="askboss-input"
            />
            <button
              className="askboss-send"
              type="submit"
              disabled={busy || !input.trim()}
              data-testid="askboss-send"
              aria-label="Ask BOSS"
            >
              {busy ? <span className="askboss-spinner" aria-hidden /> : "Ask"}
            </button>
          </div>

          {!reply && !busy ? (
            <div className="askboss-openers">
              {OPENERS.map((o) => (
                <button
                  key={o.text}
                  type="button"
                  className="askboss-opener"
                  onClick={() => { setInput(o.text); ask(o.text); }}
                >
                  <span className="askboss-opener-icon" aria-hidden>{o.icon}</span>
                  {o.text}
                </button>
              ))}
            </div>
          ) : null}
        </form>

        {busy ? (
          <p className="askboss-status" data-slow={slow} aria-live="polite" data-testid="askboss-status">
            <span className="askboss-status-dot" aria-hidden />
            {slow
              ? "Still working — this is running long. It has not been dropped."
              : "Reading the system and choosing tools…"}
            <span className="askboss-elapsed">
              {elapsed}s{slow ? ` / ~${STALL_SECONDS}s` : ""}
            </span>
          </p>
        ) : null}

        {error ? (
          <p className="askboss-error" data-testid="askboss-error">⚠ {error}</p>
        ) : null}

        {reply ? (
          <div className="askboss-answer" data-testid="askboss-answer">
            <Markdown text={reply} />
          </div>
        ) : null}

        {proposed.length > 0 ? (
          <div className="askboss-actions" data-testid="askboss-actions">
            <p className="askboss-actions-head">BOSS can do this for you — nothing runs until you choose:</p>
            <div className="askboss-actions-row">
              {proposed.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className="button agent-proposal-btn"
                  disabled={busy}
                  data-running={ranAction === a.id}
                  title={a.why}
                  onClick={() => accept(a)}
                  data-testid={`askboss-action-${a.tool}`}
                >
                  {ranAction === a.id ? "…" : a.label}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {reply || error ? (
          <div className="askboss-foot">
            <button type="button" className="askboss-reset" onClick={() => {
              setReply(""); setProposed([]); setError(""); setInput("");
              boxRef.current?.focus();
            }}>
              Ask something else
            </button>
            <a className="askboss-reset" href={`/?area=agent${orgId ? `&org_id=${orgId}` : ""}`}>
              Open the full agent panel →
            </a>
          </div>
        ) : null}
      </div>
    </section>
  );
}
