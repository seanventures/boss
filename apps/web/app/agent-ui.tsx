"use client";

import { useCallback, useEffect, useState, useTransition } from "react";

import {
  agentChatAction,
  loadAgentWorkspaceAction,
  loadTaskContextAction,
  loadThreadMessagesAction,
} from "./actions";

type Msg = { role: string; content: string; actions?: Array<{ kind: string }>; created_at?: string };
type Task = { id?: string; task_id?: string; title?: string; status?: string };
type Thread = { thread_id: string; external_ref?: string | null; updated_at?: string };
type CodeTask = { id?: string; status?: string; phase?: string; pull_request_url?: string; pull_request_number?: number; task_id?: string };

const GENERAL = "__general__";
const tid = (t: Task): string => String(t.id ?? t.task_id ?? "");

function railBtn(active: boolean) {
  return {
    display: "block", width: "100%", textAlign: "left" as const, padding: "8px 10px",
    marginBottom: 6, borderRadius: 8, cursor: "pointer", border: "1px solid var(--border)",
    background: active ? "var(--accent)" : "transparent",
    color: active ? "var(--accent-foreground, #fff)" : "var(--ink)",
  };
}

/** Hide the machine-addressed context block from the human transcript. */
/**
 * Hide the machine-addressed context block, and NEVER hide the operator's own words.
 *
 * The facts block is prepended to the outgoing message so the tools know which run the
 * conversation is about; it is noise to a reader, so it is stripped for display. But this
 * returned `""` for two real inputs — a facts block with no message after it, and one followed
 * by whitespace — and the console rendered that as an empty "You" bubble. From the operator's
 * side their message had simply disappeared, which is worse than showing them the raw context:
 * a chat that loses what you typed cannot be trusted with anything you type next.
 *
 * A display transform must never be able to erase its input. If stripping leaves nothing, the
 * original is shown instead.
 */
export function stripAppFacts(content: string): string {
  const text = content ?? "";
  if (!text.startsWith("[app context]")) return text;
  const split = text.indexOf("\n\n");
  if (split === -1) return text;
  const stripped = text.slice(split + 2).trimStart();
  return stripped || text;
}

export function AgentChat({ projectId, actor, initialTaskId, appFacts }: {
  projectId: string; actor: string; initialTaskId?: string;
  /** What BOSS already knows about the app this conversation is about. */
  appFacts?: string;
}) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [sel, setSel] = useState<string>(initialTaskId || GENERAL);   // GENERAL or a task_id
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [context, setContext] = useState<{ task?: Record<string, unknown>; code_tasks?: CodeTask[] } | null>(null);
  const [input, setInput] = useState("");
  const [pendingPlan, setPendingPlan] = useState<Record<string, unknown> | null>(null);
  const [loadingThread, setLoadingThread] = useState(false);
  const [busy, startTransition] = useTransition();

  const threadFor = useCallback(
    (id: string, list: Thread[]): Thread | undefined =>
      list.find((t) => (t.external_ref ?? null) === (id === GENERAL ? null : id)),
    [],
  );

  const openThreadAndContext = useCallback((id: string, list: Thread[]) => {
    setSel(id); setMessages([]); setContext(null); setPendingPlan(null); setLoadingThread(true);
    const th = threadFor(id, list);
    setThreadId(th?.thread_id ?? null);
    const jobs: Promise<unknown>[] = [];
    if (th?.thread_id) {
      jobs.push(loadThreadMessagesAction(th.thread_id, projectId, actor).then((r) => {
        if (r.ok && r.data) {
          const d = r.data as Record<string, unknown>;
          setMessages((d.messages as Msg[]) ?? []);
          setPendingPlan((d.pending_confirmation as Record<string, unknown>) ?? null);
        }
      }));
    }
    if (id !== GENERAL) {
      jobs.push(loadTaskContextAction(id, projectId).then((r) => {
        if (r.ok && r.data) setContext(r.data as { task?: Record<string, unknown>; code_tasks?: CodeTask[] });
      }));
    }
    Promise.all(jobs).finally(() => setLoadingThread(false));
  }, [projectId, actor, threadFor]);

  // Load the workspace (tasks + threads, last 10 days) on mount, then open the initial selection.
  useEffect(() => {
    let alive = true;
    loadAgentWorkspaceAction(projectId, actor).then((r) => {
      if (!alive || !r.ok || !r.data) return;
      const ts = ((r.data as Record<string, unknown>).tasks as Task[]) ?? [];
      const ths = ((r.data as Record<string, unknown>).threads as Thread[]) ?? [];
      setTasks(ts); setThreads(ths);
      openThreadAndContext(initialTaskId || GENERAL, ths);
    });
    return () => { alive = false; };
  }, [projectId, actor, initialTaskId, openThreadAndContext]);

  const refreshThreads = useCallback(() => {
    loadAgentWorkspaceAction(projectId, actor).then((w) => {
      if (w.ok && w.data) setThreads(((w.data as Record<string, unknown>).threads as Thread[]) ?? []);
    });
  }, [projectId, actor]);

  const runTurn = useCallback((fd: FormData) => {
    fd.set("project_id", projectId); fd.set("actor", actor);
    // TELL IT WHICH APP.
    //
    // The agent has tools that can read a run, its repo, its preview and its deployment — and
    // no way to know WHICH run the conversation is about, because the thread key is an opaque
    // id. So it answered "tell me what to do" to "what is the PR link", while the answer sat
    // in the row behind the card. The facts go in front of the message, with the run id, so
    // the tools have something to be pointed at.
    // A BLANK TURN IS NOT A QUESTION.
    //
    // Sending an empty message produced an empty "You" bubble and a real (billed) model call
    // that had nothing to answer. Combined with the strip above, the operator saw a blank turn
    // and reasonably concluded their message had been lost.
    const typed = String(fd.get("message") ?? "").trim();
    if (!typed) return;
    if (appFacts) {
      fd.set("message", `${appFacts}\n\n${typed}`);
    } else {
      fd.set("message", typed);
    }
    if (threadId) fd.set("thread_id", threadId);
    if (sel !== GENERAL) fd.set("external_ref", sel);   // scope the turn to this task
    startTransition(async () => {
      const r = await agentChatAction(null, fd);
      if (r.ok && r.data) {
        const d = r.data as Record<string, unknown>;
        if (typeof d.thread_id === "string") setThreadId(d.thread_id);
        // NEVER DROP WHAT THE OPERATOR JUST TYPED.
        //
        // `send` appends the message optimistically and this replaced the whole list with the
        // server's. When the server's copy is missing that turn — a thread_id that moved, a
        // truncated history — the message the operator was looking at disappeared, which is
        // indistinguishable from "it was never sent".
        if (Array.isArray(d.messages)) {
          const server = d.messages as Msg[];
          setMessages((local) => {
            const lastLocal = local[local.length - 1];
            const serverHasIt =
              !lastLocal ||
              lastLocal.role !== "user" ||
              server.some((m) => m.role === "user" && (m.content ?? "").includes(lastLocal.content ?? ""));
            return serverHasIt ? server : [...server, lastLocal];
          });
        }
        setPendingPlan((d.pending_confirmation as Record<string, unknown>) ?? null);
        refreshThreads();
      } else {
        setMessages((m) => [...m, { role: "assistant", content: "⚠ " + (r.error ?? "request failed") }]);
      }
    });
  }, [projectId, actor, threadId, sel, refreshThreads]);

  const send = useCallback(() => {
    const msg = input.trim();
    if (!msg || busy) return;
    setMessages((m) => [...m, { role: "user", content: msg }]);
    setInput("");
    const fd = new FormData(); fd.set("message", msg);
    runTurn(fd);
  }, [input, busy, runTurn]);

  const confirmPlan = useCallback(() => {
    const planId = String(pendingPlan?.plan_id ?? pendingPlan?.id ?? "");
    if (!planId) return;
    const fd = new FormData(); fd.set("message", ""); fd.set("confirm_plan_id", planId);
    setPendingPlan(null);
    runTurn(fd);
  }, [pendingPlan, runTurn]);

  const task = context?.task as Record<string, unknown> | undefined;

  return (
    <div className="agent-chat-shell" style={{ display: "grid", gridTemplateColumns: "clamp(220px,24%,300px) 1fr", gap: 14, minHeight: 540 }}>
      {/* RAIL — General + one entry per task (💬 if it already has a conversation) */}
      <aside style={{ borderRight: "1px solid var(--border)", paddingRight: 12, overflowY: "auto", maxHeight: "72vh" }}>
        <div className="eyebrow" style={{ marginBottom: 8 }}>Conversations · last 10 days</div>
        <button type="button" onClick={() => openThreadAndContext(GENERAL, threads)} style={railBtn(sel === GENERAL)}>
          <span style={{ fontWeight: 700 }}>💬 General chat</span>
          <span className="detail" style={{ display: "block", fontSize: 11 }}>Cross-cutting — not tied to a task</span>
        </button>
        <div className="eyebrow" style={{ margin: "14px 0 6px" }}>Tasks</div>
        {tasks.length === 0 ? (
          <p className="detail">No tasks yet.</p>
        ) : tasks.map((t) => {
          const id = tid(t); const hasChat = !!threadFor(id, threads);
          return (
            <button type="button" key={id} onClick={() => openThreadAndContext(id, threads)} style={railBtn(sel === id)}>
              <span style={{ fontWeight: 600, display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.title || id}</span>
              <span className="detail" style={{ display: "block", fontSize: 11 }}>{t.status ?? "task"}{hasChat ? " · 💬 has chat" : ""}</span>
            </button>
          );
        })}
      </aside>

      {/* MAIN — context header + scoped history + input */}
      <section style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
        {sel === GENERAL ? (
          <div className="agent-chat-intro">
            <span className="eyebrow">BOSS Agent · General</span>
            <h2>Describe the outcome. BOSS picks the governed path.</h2>
            <p>Start code, research, a meeting, a PRD, or a program from one conversation — or pick a task on the left to work on it in context.</p>
          </div>
        ) : (
          <div className="ct-block" style={{ marginBottom: 10 }}>
            <div className="ct-block-title">
              {String(task?.title ?? "Task")} <span className="cap-badge tone-blue">{String(task?.status ?? "")}</span>
            </div>
            {context?.code_tasks?.length ? (
              <div className="detail" style={{ marginTop: 6, display: "flex", gap: 12, flexWrap: "wrap" }}>
                {context.code_tasks.map((c) => (
                  <span key={c.id}>
                    {c.status}/{c.phase}
                    {c.pull_request_url ? <> · <a href={c.pull_request_url} target="_blank" rel="noreferrer">PR #{c.pull_request_number}</a></> : null}
                  </span>
                ))}
              </div>
            ) : <p className="detail" style={{ marginTop: 6 }}>No code tasks yet for this task — ask BOSS to build or improve it.</p>}
          </div>
        )}

        <div className="agent-chat-log" aria-live="polite" aria-label="Agent conversation" style={{ flex: 1, overflowY: "auto", minHeight: 260 }}>
          {loadingThread ? (
            <p className="detail">Loading conversation…</p>
          ) : messages.length === 0 ? (
            <div className="agent-chat-empty">
              <p>{sel === GENERAL ? "Try “Research auth providers, then build login.”" : "No messages yet for this task. Ask BOSS to improve the UI, add a feature, or deploy it — the whole task context is loaded."}</p>
            </div>
          ) : messages.map((m, i) => (
            <article className={`agent-message ${m.role}`} key={i}>
              <span>{m.role === "assistant" ? "BOSS" : m.role === "user" ? "You" : "tool"}</span>
              {/* The app-facts block is addressed to the MODEL, not to the reader. It was
                  prepended to the outgoing message and then echoed back in the transcript, so
                  every question appeared with a wall of run_id / live_preview / "use your
                  tools" above it — context doing its job and ruining the conversation. */}
              <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                {stripAppFacts(m.content)}
              </p>
              {m.actions && m.actions.length > 0 ? (
                <div className="agent-action-row">
                  {m.actions.map((a, j) => <span className="agent-action" key={j}>{a.kind.replaceAll("_", " ")}</span>)}
                </div>
              ) : null}
            </article>
          ))}
          {/* THINKING IS NOT NOTHING.
              A turn takes 5-30 seconds while the agent chains tools, and the only feedback was
              the Send button changing to "…" at the far bottom of the panel. From the reading
              position — the bottom of the log — the operator saw their message and then dead
              air, and reasonably concluded the request had been lost. */}
          {busy ? (
            <article className="agent-message assistant" data-testid="agent-working">
              <span>BOSS</span>
              <p className="detail agent-working">
                <span className="agent-working-dot" aria-hidden />
                Working — reading the system and choosing tools. This usually takes a few
                seconds.
              </p>
            </article>
          ) : null}
        </div>

        {pendingPlan ? (
          <div className="ct-block" style={{ margin: "8px 0" }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>⏸ BOSS drafted a plan — nothing runs until you confirm.</div>
            {typeof pendingPlan.summary === "string" ? (
              <p className="detail" style={{ whiteSpace: "pre-wrap", marginBottom: 8 }}>{pendingPlan.summary as string}</p>
            ) : null}
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <button type="button" className="button" onClick={confirmPlan} disabled={busy}>{busy ? "…" : "✓ Confirm & run"}</button>
              <span className="detail">or type a change below to revise it (e.g. “no, do it as one task”).</span>
            </div>
          </div>
        ) : null}

        {/* A TEXTAREA, not a single-line input. The useful questions here are the long ones —
            "why did this run take 152 minutes and what went wrong" plus the context that makes
            it answerable — and a one-line field that scrolls sideways hides what you typed at
            exactly the moment you want to re-read it. Enter still sends, so the common short
            message is unchanged; Shift+Enter starts a new line. */}
        <form onSubmit={(e) => { e.preventDefault(); send(); }}
              style={{ display: "flex", gap: 8, marginTop: 10, alignItems: "flex-end" }}>
          <textarea className="input agent-input" value={input} disabled={busy}
            aria-label="Message BOSS"
            placeholder={sel === GENERAL ? "Message BOSS (general)…" : "Message BOSS about this task…"}
            rows={1}
            onChange={(e) => {
              setInput(e.target.value);
              // Grow with the content up to a cap, then scroll. Reset first so deleting a
              // line shrinks it back — without that it only ever gets taller.
              const el = e.currentTarget;
              el.style.height = "auto";
              el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
            }}
            style={{ flex: 1, resize: "none", overflowY: "auto", minHeight: 34, maxHeight: 160,
                     lineHeight: 1.4, fontFamily: "inherit" }} />
          <button type="submit" className="button" disabled={busy || !input.trim()}>{busy ? "…" : "Send"}</button>
        </form>
      </section>
    </div>
  );
}
