"use client";

import { useCallback, useEffect, useState, useTransition } from "react";

import { Markdown } from "./markdown";

import {
  askBossAction,
  loadAgentWorkspaceAction,
  loadTaskContextAction,
  loadThreadMessagesAction,
  runAgentActionAction,
} from "./actions";

type Msg = { role: string; content: string; actions?: Array<{ kind: string }>; created_at?: string };
type Task = { id?: string; task_id?: string; title?: string; status?: string };
type Thread = { thread_id: string; external_ref?: string | null; updated_at?: string; title?: string | null; message_count?: number };
type CodeTask = { id?: string; status?: string; phase?: string; pull_request_url?: string; pull_request_number?: number; task_id?: string };

// A turn is normally single-digit seconds; past NORMAL the operator is told it is running
// long, and past STALL they are told what to do instead of being left to guess. STALL is a
// REPORTING bound, not a cancellation: the turn may still land server-side, so the wording
// must not claim the work was stopped.
const AGENT_NORMAL_SECONDS = 20;
const AGENT_STALL_SECONDS = 120;

const GENERAL = "__general__";
const tid = (t: Task): string => String(t.id ?? t.task_id ?? "");

/** A readable name for a conversation in the rail. */
function sessionTitle(t: Thread): string {
  const raw = (t.title ?? "").trim();
  // Task threads carry the machine context block as their title; strip it rather than showing
  // "[app context] You are talking about ONE app in BOSS." as a chat name.
  const cleaned = raw.startsWith("[app context]")
    ? (raw.split("\n\n").slice(1).join(" ").trim() || raw.replace("[app context]", "").trim())
    : raw;
  if (!cleaned) return "New conversation";
  return cleaned.length > 52 ? cleaned.slice(0, 52).trimEnd() + "…" : cleaned;
}

/** "3m ago" / "2h ago" / "5d ago" — a chat list is read by recency, not by timestamp. */
function relativeWhen(iso?: string): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

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
/**
 * Remove the model's context block from a message before a human reads it.
 *
 * The block is prepended at send time as `${facts}\n\n${typed}`, so the boundary is the LAST
 * blank line, not the first. Splitting on the first one left everything after it on screen the
 * moment any fact contained a blank line of its own — which is how a one-line question came to
 * be displayed under twenty lines of run_id, branch, "DEPLOY THIS ONE" and instructions
 * addressed to the model.
 *
 * The last-blank-line rule is only applied when what precedes it still looks like the block
 * (it carries `run_id:`), so a person writing a message with paragraphs in it keeps all of it.
 */
/** Marks where the model's context ends and the person's message begins. */
export const APP_FACTS_END = "[/app context]";

export function stripAppFacts(content: string): string {
  const text = content ?? "";
  if (!text.trimStart().startsWith("[app context]")) return text;

  // Exact, for anything sent since the terminator existed. Multi-paragraph questions survive
  // because the boundary is a marker rather than a guess about blank lines.
  const marked = text.indexOf(APP_FACTS_END);
  if (marked !== -1) {
    return text.slice(marked + APP_FACTS_END.length).trimStart() || text;
  }

  // LEGACY messages, already in the transcript without a terminator. FIRST blank line, not
  // last: a last-blank-line rule strips more of the block, but on a message the operator wrote
  // in several paragraphs it deletes everything except the final one. There is a test called
  // "chat never loses your message" guarding exactly that, and it is the right guarantee —
  // showing some stale context above an old question is a far smaller harm than silently
  // discarding what someone typed. New messages carry the terminator and are exact.
  const split = text.indexOf("\n\n");
  if (split === -1) return text;
  return text.slice(split + 2).trimStart() || text;
}

/** An action BOSS is offering. `tool` + `args` are executed verbatim on click. */
type ProposedAction = {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  label: string;
  why?: string;
};

export function AgentChat({ projectId, actor, initialTaskId }: {
  projectId: string; actor: string; initialTaskId?: string;
  /** What BOSS already knows about the app this conversation is about. */
}) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [sel, setSel] = useState<string>(initialTaskId || GENERAL);   // GENERAL or a task_id
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [context, setContext] = useState<{ task?: Record<string, unknown>; code_tasks?: CodeTask[] } | null>(null);
  const [input, setInput] = useState("");
  // ADDRESSED FROM SOMEWHERE ELSE. The Agents page sends the operator here with the message
  // already opened — "@QA Lead — " — so asking one of them something is a click from the card
  // that named them, rather than navigating here and retyping who you meant.
  useEffect(() => {
    const read = () => {
      const asked = new URLSearchParams(window.location.search).get("ask");
      if (!asked) return;
      setInput((current) => current || asked);
      // Consumed: a one-shot instruction left in the URL is re-delivered by every later
      // navigation, which is how a composer ends up refilling itself behind your typing.
      try {
        const url = new URL(window.location.href);
        url.searchParams.delete("ask");
        window.history.replaceState(null, "", url.toString());
      } catch { /* non-browser — the text is in the box either way */ }
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  const [pendingPlan, setPendingPlan] = useState<Record<string, unknown> | null>(null);
  /** Actions BOSS is OFFERING. Nothing here has run; a click is what executes it. */
  const [proposed, setProposed] = useState<ProposedAction[]>([]);
  const [ranAction, setRanAction] = useState<string | null>(null);
  /**
   * Prompts about THIS app, handed over by the screen that opened the dock.
   *
   * Grounded in recorded facts rather than generated, so a suggestion cannot describe a gap
   * that does not exist. When the dock is opened with no run in context there is nothing to
   * ground them in, and the general set is the honest answer.
   */
  const [suggestions, setSuggestions] = useState<string[]>([
    "What changed today?",
    "Which runs failed and why?",
    "What is this run waiting on?",
  ]);
  useEffect(() => {
    if (!initialTaskId) return;
    try {
      const raw = window.sessionStorage.getItem(`boss.agent.suggests.${initialTaskId}`);
      const parsed = raw ? (JSON.parse(raw) as unknown) : null;
      if (Array.isArray(parsed) && parsed.length && parsed.every((x) => typeof x === "string")) {
        setSuggestions(parsed as string[]);
      }
    } catch { /* private mode: the general prompts still work */ }
  }, [initialTaskId]);

  const [loadingThread, setLoadingThread] = useState(false);
  const [busy, startTransition] = useTransition();
  // HOW LONG, AND WHAT IF IT NEVER ANSWERS.
  // "It says it will monitor and update and I don't see any waiting signal — it just quits and
  // I don't know when it will come back." A spinner with no clock and no end state is why:
  // there was no way to tell 8 seconds of work from a turn that had already died. So the
  // elapsed time is always visible, and a turn that outlives NORMAL/STALL says so in the log
  // rather than leaving the operator watching a dot forever.
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!busy) { setElapsed(0); return; }
    const t0 = Date.now();
    const id = window.setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, [busy]);

  const threadFor = useCallback(
    (id: string, list: Thread[]): Thread | undefined =>
      list.find((t) => (t.external_ref ?? null) === (id === GENERAL ? null : id)),
    [],
  );

  /**
   * Open one conversation by its thread id, or start a fresh one.
   *
   * The rail used to be a list of TASKS with a single "General chat" pinned on top, so the only
   * way back into a past conversation was to remember which task it hung off — and a
   * cross-cutting chat had exactly one slot, permanently reused. Tasks now have their own Ask
   * BOSS panel on the card, which is where task-scoped work belongs; this page is the general
   * one, so it is organised the way a chat app is: sessions, newest first, and a New chat
   * button that does not overwrite the last one.
   */
  const openSession = useCallback((threadId: string | null) => {
    setSel(GENERAL);
    setMessages([]); setContext(null); setPendingPlan(null); setProposed([]);
    setThreadId(threadId);
    if (!threadId) { setLoadingThread(false); return; }   // New chat: nothing to fetch
    setLoadingThread(true);
    loadThreadMessagesAction(threadId, projectId, actor).then((r) => {
      if (r.ok && r.data) {
        const d = r.data as Record<string, unknown>;
        setMessages((d.messages as Msg[]) ?? []);
        setPendingPlan((d.pending_confirmation as Record<string, unknown>) ?? null);
        setProposed(Array.isArray(d.proposed_actions) ? (d.proposed_actions as ProposedAction[]) : []);
      }
    }).finally(() => setLoadingThread(false));
  }, [projectId, actor]);

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
        setProposed(Array.isArray(d.proposed_actions) ? (d.proposed_actions as ProposedAction[]) : []);
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
    // THE MESSAGE IS WHAT THEY TYPED. NOTHING ELSE.
    //
    // This used to prepend a block of run facts to every question, because the agent had no
    // idea which app a thread was about. Two things were wrong with it. The operator could
    // SEE it — their own words buried under a wall of context they never wrote, which no
    // amount of stripping on render reliably hid, because the server echoes the stored
    // message back. And it did not even work: the facts were a summary, so asked to read the
    // PRD or the diff the agent answered that they were "not present in the searchable data"
    // while the PRD sat in the database.
    //
    // Scope now travels in `external_ref` (below) and the server puts it in the SYSTEM prompt,
    // where it is invisible; the agent calls read_spec to fetch the real PRD itself.
    fd.set("message", typed);
    if (threadId) fd.set("thread_id", threadId);
    if (sel !== GENERAL) fd.set("external_ref", sel);   // scope the turn to this task
    startTransition(async () => {
      // Bound the WAIT, not the work. If the server has not answered by STALL we stop pretending
      // it is imminent and say so in the transcript; whatever the turn did still stands, and
      // reopening the thread shows it.
      const stalled = Symbol("stalled");
      const r = await Promise.race([
        // NOT agentChatAction: that one calls revalidatePath("/"), so the transition stays
        // pending while the entire 8 MB console re-renders. The answer painted and the
        // "Working…" bubble kept spinning for another 15 seconds underneath it. Nothing on the
        // page depends on a question having been asked; the thread list is refreshed
        // explicitly below.
        askBossAction(null, fd),
        new Promise<typeof stalled>((res) =>
          window.setTimeout(() => res(stalled), AGENT_STALL_SECONDS * 1000)),
      ]);
      if (r === stalled) {
        setMessages((m) => [...m, { role: "assistant", content:
          `⚠ No answer after ${AGENT_STALL_SECONDS}s. The turn may still be running on the ` +
          "server — nothing was cancelled. Reopen this thread in a moment to see whether it " +
          "landed, or send the message again." }]);
        // NOT refreshThreads() HERE.
        //
        // It fires a server action, and server actions serialise: this one queues behind the
        // turn that has not answered. React keeps the transition pending until everything
        // started inside it settles, so the composer stayed disabled until the original turn
        // finished — 131 seconds against a 120 second bound, and the whole point of the bound
        // is to give control back. Deferred outside the transition instead.
        window.setTimeout(() => refreshThreads(), 0);
        return;
      }
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

  const acceptAction = useCallback((action: ProposedAction) => {
    startTransition(async () => {
      const r = await runAgentActionAction(projectId, actor, action.tool, action.args, threadId ?? undefined);
      const ok = r.ok && (r.data as Record<string, unknown> | undefined)?.ok !== false;
      const detail = ok
        ? `✓ ${action.label}`
        : `⚠ ${action.label} failed: ${
            (r.data as Record<string, unknown> | undefined)?.error ?? r.error ?? "unknown error"
          }`;
      setRanAction(action.id);
      // Report the outcome in the log. A button that changes nothing visible is a button the
      // operator will press twice.
      setMessages((m) => [...m, { role: "assistant", content: detail }]);
      // The offer is spent either way: succeeded, or failed and needs a different approach.
      setProposed((list) => list.filter((a) => a.id !== action.id));
      refreshThreads();
    });
  }, [projectId, actor, threadId, refreshThreads]);

  const confirmPlan = useCallback(() => {
    const planId = String(pendingPlan?.plan_id ?? pendingPlan?.id ?? "");
    if (!planId) return;
    const fd = new FormData(); fd.set("message", ""); fd.set("confirm_plan_id", planId);
    setPendingPlan(null);
    runTurn(fd);
  }, [pendingPlan, runTurn]);

  const task = context?.task as Record<string, unknown> | undefined;

  // Only cross-cutting conversations belong on this page. A thread with an external_ref is a
  // TASK thread and is reachable from that task's own panel; showing it here too is what made
  // the rail read as a duplicate task list.
  const generalThreads = threads
    .filter((t) => !t.external_ref)
    .slice()
    .sort((a, b) => String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? "")));

  return (
    <div className="agent-chat-shell" style={{ display: "grid", gridTemplateColumns: "clamp(220px,24%,300px) 1fr", gap: 14, minHeight: 540 }}>
      {/* RAIL — General + one entry per task (💬 if it already has a conversation) */}
      <aside style={{ borderRight: "1px solid var(--border)", paddingRight: 12, overflowY: "auto", maxHeight: "72vh" }}>
        {/* SESSIONS, NOT TASKS.
            The task rail is gone: every task card now has its own Ask BOSS panel, which is the
            right place for task-scoped work and carries the app context with it. Listing tasks
            here as well split the same conversation across two entry points and left "General
            chat" as a single slot that every cross-cutting question overwrote. */}
        <button type="button" className="agent-newchat" onClick={() => openSession(null)}>
          <span aria-hidden>＋</span> New chat
        </button>
        <div className="eyebrow" style={{ margin: "14px 0 6px" }}>Chats</div>
        {generalThreads.length === 0 ? (
          <p className="detail">No conversations yet. Ask BOSS something to start one.</p>
        ) : generalThreads.map((t) => (
          <button
            type="button"
            key={t.thread_id}
            onClick={() => openSession(t.thread_id)}
            style={railBtn(threadId === t.thread_id)}
            title={sessionTitle(t)}
          >
            <span style={{ fontWeight: 600, display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {sessionTitle(t)}
            </span>
            <span className="detail" style={{ display: "block", fontSize: 11 }}>
              {relativeWhen(t.updated_at)}
            </span>
          </button>
        ))}
      </aside>

      {/* MAIN — context header + scoped history + input */}
      <section style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
        {sel === GENERAL ? (
          <div className="agent-chat-intro">
            <span className="eyebrow">BOSS Agent</span>
            <h2>{threadId ? sessionTitle(threads.find((t) => t.thread_id === threadId) ?? { thread_id: threadId })
                          : "What do you want to do?"}</h2>
            {threadId ? null : (
              <p>Ask anything, or tell BOSS to do it — code, research, a meeting, a PRD, a
                 deployment. Work on one app from its card in Factory, where the app context
                 comes with it.</p>
            )}
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
            ) : <p className="detail" style={{ marginTop: 6 }}>No projects yet for this task — ask BOSS to build or improve it.</p>}
          </div>
        )}

        <div className="agent-chat-log" aria-live="polite" aria-label="Agent conversation" style={{ flex: 1, overflowY: "auto", minHeight: 260 }}>
          {loadingThread ? (
            <p className="detail">Loading conversation…</p>
          ) : messages.length === 0 ? (
            <div className="agent-chat-empty">
              <div className="agent-empty-mark" aria-hidden>◆</div>
              <p className="agent-empty-lead">Ask BOSS about this work.</p>
            </div>
          ) : messages.map((m, i) => (
            <article className={`agent-message ${m.role}`} key={i}>
              <span>{m.role === "assistant" ? "BOSS" : m.role === "user" ? "You" : "tool"}</span>
              {/* The app-facts block is addressed to the MODEL, not to the reader. It was
                  prepended to the outgoing message and then echoed back in the transcript, so
                  every question appeared with a wall of run_id / live_preview / "use your
                  tools" above it — context doing its job and ruining the conversation. */}
              {/* BOSS answers in markdown — "**What I found**", GFM tables of evidence, bullet
                  lists, `run_id` in backticks. Rendered as preformatted text the operator read
                  raw pipes and asterisks, and an evidence table became unreadable exactly when
                  it mattered most. The renderer already existed and three other views used it;
                  the chat was the one place still printing the source.

                  A USER's message stays plain: they typed characters, not markup, and turning
                  their underscores into italics rewrites what they said. */}
              {m.role === "assistant" ? (
                <Markdown className="agent-md" text={stripAppFacts(m.content)} />
              ) : (
                <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                  {stripAppFacts(m.content)}
                </p>
              )}
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
              <p className="detail agent-working" data-slow={elapsed >= AGENT_NORMAL_SECONDS}
                 data-testid="agent-working-line" aria-live="polite">
                <span className="agent-working-dot" aria-hidden />
                <span>
                  {elapsed >= AGENT_NORMAL_SECONDS
                    ? "Still working — this turn is running long. It has not been dropped; BOSS is chaining tools."
                    : "Working — reading the system and choosing tools."}
                </span>
                <span className="agent-working-elapsed" data-testid="agent-elapsed">
                  {elapsed}s{elapsed >= AGENT_NORMAL_SECONDS ? ` / ~${AGENT_STALL_SECONDS}s` : ""}
                </span>
              </p>
            </article>
          ) : null}
        </div>

        {/* OFFERS, NOT DEEDS. Each is a tool + arguments BOSS chose but did not run; the click
            is what executes it, verbatim, with no model in between. Cheapest first, because
            "this link is wrong" does not say whether you want it cleared or the app redeployed —
            and one of those costs minutes and money. */}
        {proposed.length > 0 ? (
          <div className="agent-proposals" data-testid="agent-proposals">
            <p className="detail agent-proposals-head">
              BOSS can do this for you — nothing runs until you choose:
            </p>
            {proposed.map((a) => (
              <div className="agent-proposal" key={a.id}>
                <button
                  type="button"
                  className="button agent-proposal-btn"
                  disabled={busy}
                  data-testid={`agent-proposal-${a.tool}`}
                  onClick={() => acceptAction(a)}
                >
                  {busy && ranAction === a.id ? "…" : a.label}
                </button>
                {a.why ? <span className="detail agent-proposal-why">{a.why}</span> : null}
              </div>
            ))}
          </div>
        ) : null}

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

        {/* ABOUT THIS APP, AND STILL THERE ON TURN TWO.
            These lived inside the empty state, so they existed only until you said anything —
            which is backwards: on the first turn you do not yet know what to ask, and by the
            third you have read an answer and want the obvious follow-up. A conversation is
            exactly when a suggestion is worth most.

            They are derived from what BOSS RECORDED about this run — findings it deferred,
            checks that never ran, tests that missed an injected bug, acceptance criteria with
            no test — so each one names a real gap in this build rather than a generic prompt
            that reads the same whatever you are looking at. */}
        {suggestions.length > 0 ? (
          <div className="agent-suggests agent-suggests-bar" data-testid="agent-suggests">
            {suggestions.map((q) => (
              <button type="button" className="agent-suggest" key={q}
                      title={q}
                      disabled={busy}
                      data-testid={`agent-suggest-${q.slice(0, 12)}`}
                      onClick={() => { setInput(q); }}>
                {q}
              </button>
            ))}
          </div>
        ) : null}

        {/* A TEXTAREA, not a single-line input. The useful questions here are the long ones —
            "why did this run take 152 minutes and what went wrong" plus the context that makes
            it answerable — and a one-line field that scrolls sideways hides what you typed at
            exactly the moment you want to re-read it. Enter still sends, so the common short
            message is unchanged; Shift+Enter starts a new line. */}
        {/* ONE SURFACE. The field and its controls were a bare input with a Send button beside
            it, which reads as a form on a page rather than a place to talk. */}
        <form className="agent-composer" onSubmit={(e) => { e.preventDefault(); send(); }}>
          {/* NOT disabled while BOSS is working. A turn here can run for two minutes, and
              locking the field means you cannot draft the follow-up, correct a typo, or even
              select what you wrote to copy it. Only SEND is held. */}
          <textarea className="agent-input" value={input}
            aria-label="Message BOSS"
            placeholder="Ask BOSS…"
            // Three lines, not one. The useful questions here are the long ones — "why did
            // this run take 152 minutes and what went wrong", plus the context that makes it
            // answerable — and a field one line tall says "type something short".
            rows={3}
            onChange={(e) => {
              setInput(e.target.value);
              // Grow with the content up to a cap, then scroll. Reset first so deleting a
              // line shrinks it back — without that it only ever gets taller.
              const el = e.currentTarget;
              el.style.height = "auto";
              // Never below the resting three lines, never above a screenful.
              el.style.height = `${Math.min(Math.max(el.scrollHeight, 64), 220)}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
            }}
            style={{ resize: "none", overflowY: "auto", minHeight: 64, maxHeight: 220,
                     lineHeight: 1.55, fontFamily: "inherit" }} />
          <div className="agent-composer-bar">
            <span className="agent-composer-hint">
              {busy ? "BOSS is working…" : "Enter to send · Shift+Enter for a new line"}
            </span>
            {/* Circular, and disabled until there is something to send — a live-looking button
                that does nothing is worse than one that says it is not ready. */}
            <button type="submit" className="agent-send" disabled={busy || !input.trim()}
                    aria-label="Send message" data-testid="agent-send">
              {busy ? <span className="agent-send-spin" aria-hidden /> : "↑"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
