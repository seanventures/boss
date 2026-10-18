"use client";

import { HERO_BRIEF_PARAM } from "./url-intent";
import { useCallback, useEffect, useState, useTransition, useRef} from "react";

import { landingAnswerArrived } from "../lib/chat-landing";
import { Markdown } from "./markdown";
import { RateIt } from "./improve/rate-it";
import { notifyChanged } from "./changed";

import {
  askBossAction,
  runAgentActionAction,
} from "./actions";
import { LedgerStrip } from "./memory-ui";
import { apiWrite } from "../lib/api-write";

import { callAction } from "../lib/call-action";
import { askConsent, consentFromBody } from "../lib/identity/consent";
import { GatedButton, useGate } from "../components/gated";
// Agent World (S-runs): the agent's face, its live state, and the work it cites.
import { CitedActs, citedIds, DockAgentHead, DockGutterFace, dockFaceKey, dockRoleKey, useDockSpeech } from "./agents/dock-agent";
import { nameOf } from "./agents/run-face";
import { useIdentityVersion } from "../lib/use-agent-identities";
/** An action as RECORDED, which is not one shape. The router writes `{kind, args, result}`;
 *  `AgentService` writes `{id, tool, ok}`. Typing it as only the first is what let a `.kind`
 *  that is sometimes absent reach the render. */
type RecordedAction = { kind?: string; tool?: string; ok?: boolean };

type Msg = { role: string; content: string; actions?: RecordedAction[]; created_at?: string;
             attachments?: Array<{ name: string; kind: string }>;
             /** A failed turn's technical line, shown only under a collapsed Details. */
             technical?: string };

/** A file the operator attached to the message they are writing. */
type Attached = { name: string; media_type: string; kind: "image" | "text"; data: string };

//: The same bound the API enforces, said before the upload rather than after it. Five files and
//: 12MB: enough for a few screenshots or a long log, small enough that a turn stays a turn.
const MAX_FILES = 5;
const MAX_BYTES = 12 * 1024 * 1024;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);

/** Read one dropped/pasted/picked file into what the turn carries. */
async function readAttachment(file: File): Promise<Attached> {
  const media = (file.type || "").toLowerCase();
  if (IMAGE_TYPES.has(media)) {
    const buffer = await file.arrayBuffer();
    // Chunked rather than String.fromCharCode(...bytes): spreading a megabyte of bytes into
    // arguments overflows the call stack, which is a crash on exactly the large screenshot the
    // operator most wanted to send.
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let i = 0; i < bytes.length; i += 8192) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
    }
    return { name: file.name || "screenshot.png", media_type: media, kind: "image",
             data: btoa(binary) };
  }
  return { name: file.name || "file.txt", media_type: media || "text/plain", kind: "text",
           data: await file.text() };
}
type Task = { id?: string; task_id?: string; title?: string; status?: string };
type Thread = { thread_id: string; external_ref?: string | null; updated_at?: string; title?: string | null; message_count?: number;
                /** The agent the conversation is with, lower-cased as the server stores it. */
                agent_role?: string | null };
type CodeTask = { id?: string; status?: string; phase?: string; pull_request_url?: string; pull_request_number?: number; task_id?: string };

// A turn is normally single-digit seconds; past NORMAL the operator is told it is running
// long, and past STALL they are told what to do instead of being left to guess. STALL is a
// REPORTING bound, not a cancellation: the turn may still land server-side, so the wording
// must not claim the work was stopped.
const AGENT_NORMAL_SECONDS = 20;
// The turn is genuinely longer since the answering agent moved to Opus and its step
// budget went up: a deep diagnostic was measured at 192s doing 27 lookups. 120 gave
// control back in the middle of work that was going fine.
const AGENT_STALL_SECONDS = 240;

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

/**
 * What to call an action in the transcript.
 *
 * THIS CRASHED THE WHOLE CONSOLE. `a.kind.replaceAll(...)` assumed every recorded action has a
 * `kind`, and one of the two writers records `{id, tool, ok}` instead — so a thread containing
 * one of those threw `Cannot read properties of undefined (reading 'replaceAll')` during
 * render, React unmounted the tree, and the operator got "Application error: a client-side
 * exception has occurred" with no console at all. A transcript is a log of what happened; one
 * unfamiliar row in it must never be able to take the page down.
 */
export function actionLabel(action: { kind?: string; tool?: string } | null | undefined): string {
  const name = (action?.kind ?? action?.tool ?? "").trim();
  return name ? name.replaceAll("_", " ") : "action";
}

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
/** One tool BOSS called while answering. The evidence behind the paragraph. */
type ToolCall = { tool?: string; args?: Record<string, unknown>; ok?: boolean };

/**
 * A TURN THE DOCK OFFERS NEXT, under an answer BOSS gave from a record in one call.
 *
 * boss-1, 2026-10-02: "Ask the Qa" on a failed Preview step ran the whole investigating loop for
 * 347 s before answering what the step's own record said. The first answer now comes in one
 * call, and digging deeper is the operator's choice: "Investigate further" or "Do the fix" send
 * `message` with `depth`, which is what runs the full tool loop. See subject_answer.py.
 */
export type NextStep = { id: string; label: string; depth?: string; message: string };

export function nextStepsOf(data: Record<string, unknown> | null | undefined): NextStep[] {
  const raw = data?.next_steps;
  if (!Array.isArray(raw)) return [];
  return raw.filter((s): s is NextStep =>
    !!s && typeof (s as NextStep).label === "string" && typeof (s as NextStep).message === "string");
}

type ProposedAction = {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  label: string;
  why?: string;
};

/**
 * The dock's reads, off the Server Action path.
 *
 * "Loading conversation… is stuck when I click Ask for Engineering Captain." It was not stuck,
 * it was QUEUED: these were Server Actions, Next serialises those against each other and
 * against `router.refresh()`, and the console refreshes itself on a live interval into a
 * re-render of a 6 MB page. A dock opened during that window waited behind it.
 *
 * A route handler does not queue. The timeout is the second half: a read that never answers
 * must not hold the panel in "Loading…" for ever — the panel opens empty and usable, which is
 * a far better failure than a spinner with no end.
 */
async function read<T>(params: Record<string, string>, ms = 12_000): Promise<T | null> {
  const query = new URLSearchParams(params).toString();
  try {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), ms);
    const r = await fetch(`/api/agent?${query}`, {
      cache: "no-store", signal: controller.signal,
    });
    window.clearTimeout(timer);
    if (!r.ok) return null;
    const body = await r.json();
    return (body && typeof body === "object" && "error" in body) ? null : (body as T);
  } catch {
    return null;                      // aborted, offline, or malformed — the caller recovers
  }
}


export function AgentChat({ projectId, actor, initialTaskId, initialMessage, roleName,
                           fresh, roleKey: roleKeyProp, actId }: {
  projectId: string; actor: string; initialTaskId?: string;
  /** Text to seed the composer with, handed in as a PROP rather than read from the URL.
   *
   *  `?ask=` is consumed by whichever AgentChat reads it first, and the console keeps the BOSS
   *  Agent area mounted at all times — so a starter clicked in Infra was swallowed by an
   *  instance nobody was looking at, and the dock opened empty. */
  initialMessage?: string;
  /** Whose conversation this is. The dock is not always "BOSS": in Infra you are talking to
   *  the Deployment Manager, and calling that BOSS hides which role answers for the work. */
  roleName?: string;
  /** Start a NEW conversation rather than reopening the last one.
   *
   *  On mount this panel calls `openThreadAndContext(GENERAL)`, which loads the most recent
   *  general thread — right for a dock you are returning to, wrong for an idea you have just
   *  decided to discuss. Pressing Discuss on a stock-trading site reopened a thread about
   *  starved queues and put the idea underneath it, so the model would have answered the new
   *  question with the old conversation in context. */
  fresh?: boolean;
  /** What BOSS already knows about the app this conversation is about. */
  /** The agent by role key (`?dock_role_key=`): its face, its name, and who the turn is for. */
  roleKey?: string;
  /** The work this conversation is about (`?dock_act=`): sent as `context.act_id`. */
  actId?: string;
}) {
  useIdentityVersion(); // names and faces below are resolved at render: repaint when identities load
  // Pre-greyed for a viewer who may not chat (P1-18): Send, Enter and the proposals do nothing.
  const chat = useGate("assistant:chat");
  /**
   * ONE DURABLE CONVERSATION PER ROLE.
   *
   * Asking the Engineering Captain something, closing the dock and asking again started a NEW
   * thread every time: a role chat had no scope key, so it fell into the general bucket with a
   * null thread id and nothing to find its way back to. You could not refer to what you had
   * just been told, and the role could not either.
   *
   * `external_ref` is the backend's find-or-create key (one thread per project + surface +
   * ref), which is how a TASK's chat stays with that task. A role gets the same treatment under
   * its own namespace, so re-opening the dock resumes the conversation instead of starting a
   * stranger.
   */
  const roleRef = roleName ? `role:${roleName}` : "";
  const [tasks, setTasks] = useState<Task[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [sel, setSel] = useState<string>(initialTaskId || GENERAL);   // GENERAL or a task_id
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [context, setContext] = useState<{ task?: Record<string, unknown>; code_tasks?: CodeTask[] } | null>(null);
  const [input, setInput] = useState(initialMessage ?? "");
  //: Files on the message being written. Cleared when it is sent, so they belong to one turn.
  const [files, setFiles] = useState<Attached[]>([]);
  const [fileError, setFileError] = useState("");
  const picker = useRef<HTMLInputElement>(null);

  /** Take files from the picker, a paste, or a drop — refusing, with a reason, before upload. */
  const attach = useCallback(async (incoming: FileList | File[] | null) => {
    const list = Array.from(incoming ?? []);
    if (!list.length) return;
    setFileError("");
    const next: Attached[] = [];
    for (const file of list) {
      if (files.length + next.length >= MAX_FILES) {
        setFileError(`At most ${MAX_FILES} files on one message.`);
        break;
      }
      try {
        next.push(await readAttachment(file));
      } catch {
        // SAY WHICH ONE. "Upload failed" over three files is a message that cannot be acted on.
        setFileError(`Could not read ${file.name || "that file"}.`);
      }
    }
    const total = [...files, ...next].reduce((sum, f) => sum + f.data.length, 0);
    if (total > MAX_BYTES) {
      // BEFORE THE ROUND TRIP, not after it. The API enforces the same bound; being told by the
      // server means waiting for an upload that was never going to be accepted.
      setFileError(`Those come to ${Math.round(total / 1024)}KB — the limit is `
                   + `${Math.round(MAX_BYTES / 1024)}KB per message.`);
      return;
    }
    if (next.length) setFiles((f) => [...f, ...next]);
  }, [files]);
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
  /** "Investigate further" / "Do the fix" under the last answer — see NextStep. */
  const [nextSteps, setNextSteps] = useState<NextStep[]>([]);
  const [trail, setTrail] = useState<ToolCall[]>([]);
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
  // WHO IS IN THE DOCK, BY KEY, and what they are doing right now. Thinking while a turn is in
  // flight, speaking as its words arrive — from the turn's real state, never on a timer.
  // `dockAgent` is the key the turn sends (given, or resolved — never guessed from a name);
  // `dockFace` is only what the dock draws its face and name from.
  const dockAgent = dockRoleKey(roleKeyProp, roleName);
  const dockFace = dockFaceKey(roleKeyProp, roleName);
  const speech = useDockSpeech(busy);
  const speakChunk = speech.chunk;
  const [aboutAct, setAboutAct] = useState<string | null>(actId ?? null);
  // Work an answer cited, by the answer's index in `messages`, when the reply carried it.
  //
  // CITATIONS BELONG TO THE THREAD THEY CAME IN. They are kept with that thread's id and shown
  // only while it is the open one. Clearing them whenever `threadId` changed wiped the first
  // answer's chips: a new conversation's turn sets the thread id and the citation together.
  const [citations, setCitations] = useState<{ thread: string | null; byIndex: Record<number, string[]> }>(
    { thread: null, byIndex: {} });
  const citedHere = citations.thread === threadId ? citations.byIndex : {};
  const clearAboutAct = useCallback(() => {
    setAboutAct(null);
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete("dock_act");
      window.history.replaceState(window.history.state, "", url.toString());
    } catch { /* non-browser */ }
  }, []);
  //: MESSAGES TYPED WHILE A TURN IS RUNNING.
  //:
  //: Send was disabled while busy, so a second thought had to be held in the operator's head
  //: for the two minutes a turn can take — or typed into the box and forgotten. A queue is the
  //: obvious thing and the panel already has everything it needs for one: the turn ends, the
  //: next message goes.
  //:
  //: Shown in the transcript as pending rather than kept invisible, because a message you
  //: cannot see is a message you cannot tell was sent.
  const [queued, setQueued] = useState<string[]>([]);
  //: A turn the operator stopped waiting for. The work itself is a server action and cannot be
  //: killed from here — so this says what is actually true rather than pretending to cancel.
  const [released, setReleased] = useState(false);
  //: Consecutive failed actions handed back for repair. Reset by any success, so a long
  //: conversation is not permanently one strike from giving up.
  const [repairs, setRepairs] = useState(0);
  // HOW LONG, AND WHAT IF IT NEVER ANSWERS.
  // "It says it will monitor and update and I don't see any waiting signal — it just quits and
  // I don't know when it will come back." A spinner with no clock and no end state is why:
  // there was no way to tell 8 seconds of work from a turn that had already died. So the
  // elapsed time is always visible, and a turn that outlives NORMAL/STALL says so in the log
  // rather than leaving the operator watching a dot forever.
  const [elapsed, setElapsed] = useState(0);
  // WHAT IT IS DOING, WHILE IT IS DOING IT.
  //
  // A turn is one request, so the tool calls are known only when it returns — and the
  // reliability work deliberately makes turns investigate MORE, which makes the wait longer.
  // The server records each step as it starts; this polls for them. "Working — reading the
  // system" for two minutes is indistinguishable from a turn that has died.
  const [steps, setSteps] = useState<string[]>([]);
  // AND WHAT IS STILL WORKING AFTER THE TURN HAS ANSWERED.
  //
  // An investigation is a real debugging session — a shell, the repo, the fleet — and it runs
  // for minutes AFTER the chat turn returns. Polling only while `busy` meant the most
  // informative progress BOSS produces went to a table nobody read: the operator saw a
  // finished answer and no sign that anything was still happening.
  const [probe, setProbe] = useState<{ id: string; question: string } | null>(null);
  //: A turn that outran the reporting bound. The work is still going and its answer lands in
  //: the thread by itself, so the console watches for it instead of asking the operator to.
  const [landing, setLanding] = useState(false);
  useEffect(() => {
    if (!threadId || (!busy && !probe && !landing)) { if (!busy && !landing) setSteps([]); return; }
    let alive = true;
    let cursor = 0;
    const tick = () => {
      read<{ steps?: { id: number; label: string }[]; cursor?: number;
             investigation?: { id: string; question: string } | null }>(
        { view: "progress", project_id: projectId, actor, thread_id: threadId,
          since: String(cursor) }, 4_000).then((d) => {
        if (!alive || !d) return;
        if (Array.isArray(d.steps) && d.steps.length) {
          cursor = Number(d.cursor ?? cursor);
          setSteps((prev) => [...prev, ...d.steps!.map((s) => s.label)].slice(-6));
        }
        const live = d.investigation ?? null;
        setProbe((was) => {
          // It just finished: its findings were posted into this thread as their own message,
          // so reload rather than leaving the operator to wonder how it went.
          if (was && !live) {
            void read<Record<string, unknown>>(
              { view: "messages", project_id: projectId, actor, thread_id: threadId })
              .then((d) => { if (d?.messages) setMessages(d.messages as Msg[]); });
            return null;
          }
          return live ? { id: live.id, question: live.question } : was;
        });
      });
    };
    const id = window.setInterval(tick, 1500);
    tick();
    return () => { alive = false; window.clearInterval(id); };
  }, [busy, threadId, probe, landing]);
  // WATCH FOR THE ANSWER THAT OUTRAN THE BOUND.
  //
  // The turn was never cancelled — it is still running server-side and writes its reply into
  // this thread when it finishes. Polling for that is the difference between "reopen this in a
  // moment and see" and the answer simply appearing, which is what the operator expected when
  // they pressed send.
  // HOW THIS DOCK WAS OPENED. `?dock_mode=discuss` means the operator brought a subject with
  // them from the composer rather than clicking into a run — so the panel starts a new thread
  // and frames itself around the conversation. It no longer selects a different AGENT: one
  // conversation answers both an idea and a question about the factory (_one_conversation_brief).
  const [discussing, setDiscussing] = useState(false);
  useEffect(() => {
    const sync = () => {
      try {
        setDiscussing(new URLSearchParams(window.location.search).get("dock_mode") === "discuss");
      } catch { /* non-browser */ }
    };
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);

  /** A brief the agent wrapped for one-press use, if this message carries one. */
  const briefIn = (text: string): string | null => {
    const m = /<<<BOSS-BRIEF\s*([\s\S]*?)BOSS-BRIEF>>>/.exec(text || "");
    return m ? m[1].trim() : null;
  };
  /** The same message with the wrapped brief removed — it is rendered as its own block. */
  const withoutBrief = (text: string): string =>
    (text || "").replace(/<<<BOSS-BRIEF[\s\S]*?BOSS-BRIEF>>>/g, "").trim();
  /** Hand the brief to the Command Center composer. Still a press away from building. */
  const useBrief = (brief: string): void => {
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("area", "overview");
      // Its own parameter (url-intent HERO_BRIEF_PARAM): `compose=` is the composer's command.
      url.searchParams.set(HERO_BRIEF_PARAM, brief);
      url.searchParams.delete("agent");
      url.searchParams.delete("dock_mode");
      window.history.pushState(null, "", url.toString());
      window.dispatchEvent(new PopStateEvent("popstate"));
    } catch { /* non-browser */ }
  };

  const quiet = useRef(0);
  const landingFor = useRef("");
  const sawWorking = useRef(false);
  useEffect(() => {
    if (!landing || !threadId) return;
    let alive = true;
    quiet.current = 0;
    sawWorking.current = false;
    const started = Date.now();
    const id = window.setInterval(() => {
      if (!alive) return;
      // Twenty minutes: longer than any turn measured, and it stops rather than polling for
      // ever if the turn really did die.
      if (Date.now() - started > 20 * 60_000) { setLanding(false); return; }
      void read<{ messages?: Msg[]; working?: boolean }>(
        { view: "messages", project_id: projectId, actor, thread_id: threadId }).then((d) => {
        if (!alive || !d?.messages) return;
        // THIS TURN, NOT THE PREVIOUS ONE.
        //
        // The user line and the reply commit together. Until then the thread still ends on
        // the last turn's assistant message. Treating that leftover as arrival replaced the
        // transcript and dropped the question the operator had just typed.
        if (landingAnswerArrived(d.messages, landingFor.current)) {
          const server = d.messages;
          setMessages((local) => {
            const lastLocal = local[local.length - 1];
            const serverHasIt =
              !lastLocal ||
              lastLocal.role !== "user" ||
              server.some((m) => m.role === "user" && (m.content ?? "").includes(lastLocal.content ?? ""));
            return serverHasIt ? server : [...server, lastLocal];
          });
          setLanding(false);
          return;
        }
        // NOTHING IS RUNNING, SO STOP SAYING IT IS — but only after this turn has been
        // seen working. A false on the first sample is "no tool has started yet", which
        // is how every turn begins. Two consecutive not-working reads after that, because
        // a turn between steps can briefly show as idle and one sample would call that death.
        if (d.working === true) {
          sawWorking.current = true;
          quiet.current = 0;
        } else if (d.working === false && sawWorking.current) {
          quiet.current += 1;
          if (quiet.current >= 2) {
            setLanding(false);
            setMessages((m) => [...m, { role: "assistant", content:
              "That turn ended without an answer — nothing is running now. Whatever it had "
              + "already done still stands; ask again and it will pick up from the thread." }]);
          }
        }
      });
    }, 4000);
    return () => { alive = false; window.clearInterval(id); };
  }, [landing, threadId, projectId, actor]);
  useEffect(() => {
    if (!busy) { setElapsed(0); return; }
    const t0 = Date.now();
    const id = window.setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, [busy]);

  // ONE THREAD PER (SUBJECT, AGENT) — the key the turn itself uses (agent_service: project,
  // surface, external_ref, agent_role). Matching the subject alone opened whichever agent's
  // conversation about it came first. An API older than `agent_role` on the list matches by
  // subject, as before.
  const roleKey = (roleName ?? "").trim().toLowerCase();
  const threadFor = useCallback(
    (id: string, list: Thread[]): Thread | undefined =>
      list.find((t) => (t.external_ref ?? null) === (id === GENERAL ? null : id)
        && (t.agent_role ?? roleKey) === roleKey),
    [roleKey],
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
    setMessages([]); setContext(null); setPendingPlan(null); setProposed([]); setNextSteps([]);
    setThreadId(threadId);
    if (!threadId) { setLoadingThread(false); return; }   // New chat: nothing to fetch
    setLoadingThread(true);
    read<Record<string, unknown>>(
      { view: "messages", project_id: projectId, actor, thread_id: threadId }).then((d) => {
      if (d) {
        setMessages((d.messages as Msg[]) ?? []);
        setPendingPlan((d.pending_confirmation as Record<string, unknown>) ?? null);
        setProposed(Array.isArray(d.proposed_actions) ? (d.proposed_actions as ProposedAction[]) : []);
      }
    }).finally(() => setLoadingThread(false));
  }, [projectId, actor]);

  const openThreadAndContext = useCallback((id: string, list: Thread[]) => {
    // Proposals are cleared with everything else. Left behind, an offer from the PREVIOUS
    // conversation stayed on screen while a different thread loaded — and it stays clickable,
    // so "delete all containers on hetzner-ci-01" could be pressed from a chat about
    // something else entirely.
    setSel(id); setMessages([]); setContext(null); setPendingPlan(null); setProposed([]); setNextSteps([]);
    setLoadingThread(true);
    const th = threadFor(id, list);
    setThreadId(th?.thread_id ?? null);
    const jobs: Promise<unknown>[] = [];
    if (th?.thread_id) {
      jobs.push(read<Record<string, unknown>>(
        { view: "messages", project_id: projectId, actor, thread_id: th.thread_id }).then((d) => {
        if (d) {
          setMessages((d.messages as Msg[]) ?? []);
          setPendingPlan((d.pending_confirmation as Record<string, unknown>) ?? null);
        setProposed(Array.isArray(d.proposed_actions) ? (d.proposed_actions as ProposedAction[]) : []);
        }
      }));
    }
    if (id !== GENERAL) {
      jobs.push(read<{ task?: Record<string, unknown>; code_tasks?: CodeTask[] }>(
        { view: "task-context", project_id: projectId, actor, task_id: id }).then((d) => {
        if (d) setContext(d);
      }));
    }
    Promise.all(jobs).finally(() => setLoadingThread(false));
  }, [projectId, actor, threadFor]);

  // Load the workspace (tasks + threads, last 10 days) on mount, then open the initial selection.
  //
  // ON MOUNT, NOT ON FIRST MESSAGE. The history has to be on screen when the panel opens: an
  // operator who cannot see what they were told last time re-asks it, and a role that cannot
  // see it answers as if the conversation never happened. The window is the thread list's own —
  // the last week — so a conversation resumes for as long as it is plausibly still yours, and
  // anything older opens fresh rather than silently reviving a month-old argument.
  useEffect(() => {
    let alive = true;
    // A role chat resumes even before the workspace list lands: it is the common case in the
    // dock, and waiting on the task list to paint a role's history makes the panel flash empty.
    if (roleRef) setLoadingThread(true);
    read<{ tasks?: Task[]; threads?: Thread[] }>(
      { view: "workspace", project_id: projectId, actor }).then((d) => {
      if (!alive) return;
      if (!d) { setLoadingThread(false); return; }
      const ts = d.tasks ?? [];
      const ths = d.threads ?? [];
      setTasks(ts); setThreads(ths);
      // A DOOR ABOUT A SUBJECT OPENS THAT SUBJECT'S CONVERSATION WITH THIS AGENT. "Ask Qa about
      // this" mounted with both a subject and a role, and this loaded the role's GENERAL thread
      // — Qa's unrelated history and its still-clickable proposals — above a question about the
      // step; the turn then moved to the (subject, role) thread mid-panel, and an earlier answer
      // about the same step was never shown (review of 374ba07). The subject's thread, matched
      // on the same (subject, role) key the turn uses, wins; the role's general thread is for a
      // door with no subject.
      if (initialTaskId && !fresh) { openThreadAndContext(initialTaskId, ths); return; }
      const mine = roleRef ? ths.find((t) => (t.external_ref ?? null) === roleRef) : undefined;
      if (mine?.thread_id) {
        setThreadId(mine.thread_id);
        read<Record<string, unknown>>(
          { view: "messages", project_id: projectId, actor,
            thread_id: mine.thread_id }).then((d) => {
          if (!alive) return;
          if (d) {
            setMessages((d.messages as Msg[]) ?? []);
            setPendingPlan((d.pending_confirmation as Record<string, unknown>) ?? null);
            setProposed(Array.isArray(d.proposed_actions)
              ? (d.proposed_actions as ProposedAction[]) : []);
          }
        }).finally(() => { if (alive) setLoadingThread(false); });
        return;
      }
      if (roleRef) { setLoadingThread(false); return; }   // this role has no history yet
      // A NEW IDEA IS A NEW CONVERSATION. The thread list still holds the old ones; this only
      // declines to reopen the newest by default.
      if (fresh) { setSel(GENERAL); setLoadingThread(false); return; }
      openThreadAndContext(initialTaskId || GENERAL, ths);
    });
    return () => { alive = false; };
  }, [projectId, actor, initialTaskId, roleRef, fresh, openThreadAndContext]);

  // WHAT TO ASK *THIS* ROLE.
  //
  // Every surface offered the same three: "What changed today?", "Which runs failed and why?",
  // "What is this run waiting on?" — under a banner announcing the Engineering Captain. Those
  // are questions for a dashboard, and a dashboard is what a named expert is not. The server
  // builds them from the role's lens AND its own record here, so they name real apps, this
  // project's last failure, and positions this role actually took.
  // ONE SURFACE, SO THE STARTERS COVER BOTH. "Which runs failed and why?" alone said this was
  // a dashboard; three product questions alone said it could not answer about the factory.
  // Phrased as things the OPERATOR says — a chip sends its text as their message, so "Who is
  // this for?" would have them asking their own question back to themselves.
  useEffect(() => {
    if (!discussing) return;
    setSuggestions([
      "Push back — where is this weak?",
      "What is the smallest version worth building?",
      "What changed today?",
    ]);
  }, [discussing]);

  useEffect(() => {
    if (!roleName) return;
    let alive = true;
    read<{ starters?: unknown }>(
      { view: "starters", project_id: projectId, actor, role: roleName }).then((d) => {
      if (!alive || !d) return;
      const list = d.starters;
      if (Array.isArray(list) && list.length && list.every((x) => typeof x === "string")) {
        setSuggestions(list as string[]);
      }
    });
    return () => { alive = false; };
  }, [projectId, actor, roleName]);

  const refreshThreads = useCallback(() => {
    read<{ threads?: Thread[] }>({ view: "workspace", project_id: projectId, actor })
      .then((d) => { if (d) setThreads(d.threads ?? []); });
  }, [projectId, actor]);

  const runTurn = useCallback((fd: FormData) => {
    fd.set("project_id", projectId); fd.set("actor", actor);
    // The offers belong to the answer they came from; a new turn retires them.
    setNextSteps([]);
    // Every turn, not just the first: the operator asked a named member of the workforce, and
    // the second question is still addressed to them.
    if (roleName) fd.set("role", roleName);
    // The agent by KEY as well, and the work the conversation is about (Agent World): the reply
    // is parented to that work and answers as that agent's own identity.
    if (dockAgent) fd.set("role_key", dockAgent);
    if (aboutAct) fd.set("context_act_id", aboutAct);
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
    // AN IDEA CONVERSATION, when the dock was opened for one. Read from the URL rather than
    // held in state so the link that opens it decides, and reopening the thread keeps it.
    // `mode` IS GONE FROM THE WIRE. The agent classifies the question itself now — see
    // _one_conversation_brief — so the console no longer decides on the operator's behalf
    // which of two heads answers.
    if (threadId) fd.set("thread_id", threadId);
    // WHAT THE TURN IS ABOUT. `sel` is a build id or, from an "Ask about this" door, a typed
    // subject reference (`workflow_step:wfr_…:preview` — see lib/dock-subject.ts) that the
    // server resolves into that thing's own record.
    const scopeRef = sel !== GENERAL ? sel : roleRef;
    if (scopeRef) fd.set("external_ref", scopeRef);
    startTransition(async () => {
      // A THREAD ID BEFORE THE TURN, OR THE PROGRESS LINE IS DEAD FOR THE WHOLE TURN.
      //
      // `agent_progress` is keyed by thread and the poll needs that key. On a NEW chat the
      // console learned the id from the turn's RESPONSE — by which time the turn is over.
      // So the first conversation with BOSS, every time, showed one frozen sentence:
      // "Still working — this turn is running long", for minutes, while a perfectly good
      // trail ("reading the role's remit", "querying the database — SELECT …") accumulated
      // in a table the page could not yet address.
      //
      // Reported as "it displays same message for more than 3 mins — how can user wait and
      // see same message". One cheap call fixes it: the thread exists before we send, so the
      // poll has something to ask about from the first second.
      //
      // AND IT HAS TO BE THE THREAD THE TURN WILL LAND IN. In boss-1 this asked for a thread by
      // project and actor alone; the turn — scoped to a step and addressed to Qa — resolved to a
      // different one, so every poll for 276 seconds read an empty thread. The ensure call now
      // carries the same (scope, role) key the turn does, and runs whenever the turn has a
      // scope, because a thread id held from an earlier conversation may belong to another.
      let live = threadId;
      if (!live || scopeRef) {
        try {
          const r = await apiWrite("/api/agent-thread", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id: projectId, actor,
                                   external_ref: scopeRef || undefined,
                                   role: roleName || undefined }),
          });
          const body = (await r.json()) as { thread_id?: string };
          if (r.ok && typeof body?.thread_id === "string") {
            live = body.thread_id;
            setThreadId(live);            // starts the progress poll immediately
            fd.set("thread_id", live);    // and the turn lands in the same thread
          }
        } catch {
          // An indicator must never cost the operator their message. Without an id the turn
          // still runs exactly as before — the progress line just stays generic, which is the
          // behaviour this replaced.
        }
      }
      // Bound the WAIT, not the work. If the server has not answered by STALL we stop pretending
      // it is imminent and say so in the transcript; whatever the turn did still stands, and
      // reopening the thread shows it.
      const stalled = Symbol("stalled");
      // A TURN THAT FAILS TO START MUST NOT LOOK LIKE ONE THAT IS RUNNING.
      //
      // There was no catch here at all. `askBossAction` is a server action, so the CALL can
      // reject even though the action itself returns errors as data — a dropped round trip, a
      // proxy timeout, or a queue behind another pending action. The rejection escaped the
      // transition, nothing was appended, and the panel sat on "BOSS is working…" forever.
      // Measured on the live system: the operator's message was never recorded — the thread
      // still held four messages — while the console showed a turn in flight.
      let r: Awaited<ReturnType<typeof askBossAction>> | typeof stalled;
      try {
        const turn = () => Promise.race([
        // NOT agentChatAction: that one calls revalidatePath("/"), so the transition stays
        // pending while the entire 8 MB console re-renders. The answer painted and the
        // "Working…" bubble kept spinning for another 15 seconds underneath it. Nothing on the
        // page depends on a question having been asked; the thread list is refreshed
        // explicitly below.
          callAction(askBossAction, undefined, "assistant:chat")(null, fd),
        new Promise<typeof stalled>((res) =>
          window.setTimeout(() => res(stalled), AGENT_STALL_SECONDS * 1000)),
        ]);
        r = await turn();
        // THE AGENT ACTS ON YOUR BEHALF, SO IT ASKS FIRST. A turn that needs a grant the
        // person does not hold yet comes back as a consent request (actions.ts postJsonReturn):
        // open the consent sheet, and once granted send the same turn once more.
        const asked = r !== stalled && !r.ok
          ? consentFromBody((r.data as Record<string, unknown> | undefined)?.consent) : null;
        if (asked && await askConsent(asked)) r = await turn();
      } catch (e) {
        setMessages((m) => [...m, { role: "assistant", content:
          "That message did not reach BOSS — " + String((e as Error)?.message ?? e)
          + ". Nothing was started, so nothing is running. Send it again." }]);
        return;
      }
      if (r === stalled) {
        // "REOPEN THIS THREAD IN A MOMENT" IS WORK THE CONSOLE CAN DO ITSELF.
        //
        // The bound exists to give the composer back, and that part was right. Telling the
        // operator to come back and look was not: the turn IS still running, its steps are
        // still streaming into this very panel, and the answer lands in the thread by itself.
        // Watching for it is one poll.
        landingFor.current = typed;
        setLanding(true);
        setMessages((m) => [...m, { role: "assistant", content:
          `Still working — past ${AGENT_STALL_SECONDS}s. Nothing was cancelled and you can ` +
          "type again; the answer will appear here on its own when it lands." }]);
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
        // ITS WORDS ARRIVED: one chunk (a whole reply today, each token once turns stream).
        speakChunk();
        const cited = citedIds(d.cited_acts);
        if (Array.isArray(d.messages) && cited.length) {
          const list = d.messages as Msg[];
          let last = -1;
          list.forEach((m, n) => { if (m.role === "assistant") last = n; });
          const thread = typeof d.thread_id === "string" ? d.thread_id : live;
          if (last >= 0) {
            setCitations((c) => ({ thread, byIndex: { ...(c.thread === thread ? c.byIndex : {}), [last]: cited } }));
          }
        }
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
        // THE OFFER ARRIVES WITH THE REPLY, AND WAS BEING THROWN AWAY.
        //
        // Both thread-LOADING paths set this; the one that matters most — the turn you just
        // took — did not. So the agent would say "I've offered a button to delete all
        // containers on hetzner-ci-01", the API really did return the proposal, and the
        // console rendered nothing. Asked where the button was, the agent could only repeat
        // that it had offered one. It had.
        //
        // The whole confirm-before-acting design routes through `proposed_actions`: dropping
        // them here does not make anything safer, it makes the action unreachable and the
        // agent look like it is lying.
        setProposed(Array.isArray(d.proposed_actions) ? (d.proposed_actions as ProposedAction[]) : []);
        // THE ANSWER FIRST, THE DIGGING ON REQUEST — see NextStep.
        setNextSteps(nextStepsOf(d));
        // WHAT THE ANSWER IS BASED ON. See ReadTrail: a confident paragraph with nothing
        // behind it is exactly what makes an operator distrust the whole thing.
        setTrail(Array.isArray(d.tool_calls) ? (d.tool_calls as ToolCall[]) : []);
        refreshThreads();
      } else {
        // THE SENTENCE, NOT THE EXCEPTION. This fenced the raw error as code, and on an existing install
        // that was "IntegrityError: (psycopg.errors.UniqueViolation) … [SQL: INSERT INTO …".
        // The API now says a failed turn in one sentence; its technical line sits under a
        // collapsed Details below the message.
        const technical = typeof r.data?.technical === "string" ? r.data.technical : undefined;
        setMessages((m) => [...m, { role: "assistant",
          content: "⚠ " + (r.error ?? "request failed"), technical }]);
      }
    });
  }, [projectId, actor, threadId, sel, roleRef, roleName, refreshThreads, dockAgent, aboutAct, speakChunk]);

  /** Take one of the offered next steps: its message, sent at its depth, as the operator's. */
  const takeNextStep = useCallback((step: NextStep) => {
    if (busy) return;
    setMessages((m) => [...m, { role: "user", content: step.message }]);
    const fd = new FormData();
    fd.set("message", step.message);
    if (step.depth) fd.set("depth", step.depth);
    runTurn(fd);
  }, [busy, runTurn]);

  const send = useCallback(() => {
    const msg = input.trim();
    // A SCREENSHOT ON ITS OWN IS A MESSAGE. "Look at this" is the most natural way to report a
    // broken screen, and requiring words alongside it would be the console insisting on
    // ceremony the operator did not need.
    if (!msg && !files.length) return;
    // QUEUED, NOT REFUSED. An attachment cannot be queued — the file handles are consumed by
    // the turn that sends them — so a message with files still waits for the turn to end.
    if (busy && !files.length) {
      setQueued((q) => [...q, msg]);
      setInput("");
      return;
    }
    if (busy) return;
    const shown = msg || "Look at what I've attached.";
    setMessages((m) => [...m, {
      role: "user", content: shown,
      attachments: files.map((f) => ({ name: f.name, kind: f.kind })),
    }]);
    setInput("");
    const fd = new FormData(); fd.set("message", msg);
    if (files.length) fd.set("attachments", JSON.stringify(files));
    setFiles([]);
    runTurn(fd);
  }, [input, files, busy, runTurn]);

  // THE QUEUE DRAINS ITSELF WHEN THE TURN ENDS.
  //
  // One at a time and in order: sending them together would interleave the answers, and the
  // second question usually depends on the first one's answer. `released` stops the drain —
  // an operator who pressed Stop wanted the queued follow-ups stopped too, since they were
  // written for a turn that is no longer the one running.
  useEffect(() => {
    if (busy || released || !queued.length) return;
    const [next, ...rest] = queued;
    setQueued(rest);
    setMessages((m) => [...m, { role: "user", content: next }]);
    const fd = new FormData();
    fd.set("message", next);
    runTurn(fd);
  }, [busy, released, queued, runTurn]);

  /** Stop waiting for the turn in flight, and drop anything queued behind it.
   *
   *  HONEST ABOUT WHAT IT CANNOT DO. A turn is a server action; there is no cancel endpoint,
   *  so the model keeps going and its answer still lands in this thread. What this stops is
   *  the WAITING — the composer comes back, the queue is cleared, and the transcript says so
   *  rather than implying the work was killed. Calling it "Cancel" would be a lie the next
   *  message exposes.
   */
  const release = useCallback(() => {
    setReleased(true);
    setQueued([]);
    setMessages((m) => [...m, { role: "assistant", content:
      "Stopped waiting. BOSS may still finish this turn — if it does, the answer appears here. "
      + "Anything you had queued was dropped, so send what you actually want now." }]);
  }, []);

  // A new turn is a new wait.
  useEffect(() => { if (busy) setReleased(false); }, [busy]);

  /**
   * Clear THIS conversation, and only this one.
   *
   * Scoped to the thread id the panel is currently showing, so a role's chat cannot reach
   * another role's — there is no wildcard to get wrong. The thread is archived rather than
   * deleted: the messages stay readable by id, and what makes the panel look clear is the
   * server freeing the find-or-create key so the next message opens a fresh thread.
   */
  const [clearing, setClearing] = useState(false);
  const clearChat = useCallback(async () => {
    if (!threadId || clearing) return;
    setClearing(true);
    try {
      const r = await apiWrite("/api/agent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "clear", project_id: projectId, actor,
                               thread_id: threadId }),
      });
      if (!r.ok) return;
      // Local state last, and only on success: clearing the panel before the server agreed
      // would show an empty conversation that comes back on the next load.
      setMessages([]); setProposed([]); setNextSteps([]); setTrail([]); setPendingPlan(null);
      setThreadId(null);
      refreshThreads();
    } catch {
      /* the conversation is still there; the button can be pressed again */
    } finally {
      setClearing(false);
    }
  }, [threadId, clearing, projectId, actor, refreshThreads]);

  const acceptAction = useCallback((action: ProposedAction) => {
    startTransition(async () => {
      const r = await callAction(runAgentActionAction, undefined, "assistant:chat")(
        projectId, actor, action.tool, action.args, threadId ?? undefined,
        action.id, action.label);
      const payload = (r.data ?? {}) as Record<string, unknown>;
      const ok = r.ok && payload?.ok !== false;
      // BOSS CHANGED SOMETHING THE OTHER PANELS ARE SHOWING. Clicking "Create the target" here
      // is the same write the Factory card and the Workflows screen render, and neither had a
      // way to learn it happened — so the operator fixed the problem in the dock and went back
      // to a screen still describing it.
      if (ok) notifyChanged("agent", { tool: action.tool });
      // SHOW WHAT IT DID, NOT JUST THAT IT DID SOMETHING.
      //
      // A tick and a label is fine for "cancel a job". For a command on a live host it is not:
      // after clicking "Delete all containers on hetzner-ci-01" the operator got `✓` and had
      // no way to see the exit code, the output, or what was actually removed — they had to
      // ask someone else to check the box. The tool already returns all of it.
      const body = [
        typeof payload.exit_code === "number" ? `exit ${payload.exit_code}` : "",
        String(payload.stdout ?? "").trim(),
        String(payload.stderr ?? "").trim(),
      ].filter(Boolean).join("\n").slice(0, 4000);
      const detail = ok
        ? `✓ ${action.label}${body ? `\n\n\`\`\`\n${body}\n\`\`\`` : ""}`
        // IN A CODE FENCE, BECAUSE MARKDOWN EATS IDENTIFIERS.
        //
        // Assistant messages are markdown-rendered, so a validator saying
        // "needs 'media_artifact_id'" displays as "needs 'mediaartifactid'" — the underscores
        // read as emphasis. The operator reported exactly that string, and an identifier you
        // cannot copy is an error you cannot act on: it does not appear in the schema, the
        // code, or any search.
        : `⚠ ${action.label} failed:\n\n\`\`\`\n${
            payload?.error ?? r.error ?? "unknown error"
          }${body ? `\n\n${body}` : ""}\n\`\`\``;
      setRanAction(action.id);
      // Report the outcome in the log. A button that changes nothing visible is a button the
      // operator will press twice.
      setMessages((m) => [...m, { role: "assistant", content: detail }]);
      // The offer is spent either way: succeeded, or failed and needs a different approach.
      setProposed((list) => list.filter((a) => a.id !== action.id));
      refreshThreads();

      // A FAILED ACTION IS THE AGENT'S NEXT TURN, NOT A DEAD END.
      //
      // THIS WAS THE WHOLE "IT JUST RUNS A SCRIPT" PROBLEM. The failure was appended to
      // CLIENT state and the offer dropped — so the model was never told. It finished the turn
      // believing it had handed over a working action, and the next time the operator asked it
      // saw a conversation in which nothing had gone wrong, and proposed the same thing again.
      // Ten times, if asked ten times. Not the model refusing to adapt: the harness throwing
      // away the only information it could have adapted to.
      //
      // The validator's message is precise and actionable — "step 'edit' needs
      // 'media_artifact_id', which is not set" is enough to repair the arguments. Handing it
      // back closes the loop, and a loop that carries the error converges where a script
      // cannot.
      if (!ok) {
        const reason = String(payload?.error ?? r.error ?? "unknown error");
        setRepairs((n) => n + 1);
        // BOUNDED. Two attempts, then it stops and says so. An unbounded repair loop burns
        // budget on an error the agent cannot fix and looks, from outside, exactly like the
        // script it replaces.
        if (repairs < 2) {
          const fd = new FormData();
          fd.set("message",
            `The action "${action.label}" FAILED and you have not been told until now.\n\n` +
            `Tool: ${action.tool}\nError: ${reason}\n\n` +
            "Read that error literally, fix the arguments, and try again yourself — do not " +
            "re-offer the same call, do not ask me to confirm, and do not explain the design " +
            "again. If the error names a missing input, supply it or wire it from a step that " +
            "produces it. If this action cannot run here at all, do not ask me to do its work " +
            "by hand until you have checked what BOSS can run instead (a retry, a redeploy, " +
            "another action) and offered it as a button. If you genuinely cannot fix it, say " +
            "in one sentence what is blocking you and what you need from me.");
          runTurn(fd);
        } else {
          setMessages((m) => [...m, { role: "assistant", content:
            "That has failed twice with the same kind of error, so I have stopped retrying "
            + "rather than loop. Tell me how you would like to proceed." }]);
        }
      } else {
        setRepairs(0);
      }
    });
  }, [projectId, actor, threadId, refreshThreads, repairs, runTurn]);

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
    /* LAYOUT LIVES IN CSS, NOT IN INLINE STYLES.
       The grid, the rail width and the log's height were inline, so the dock could only fight
       them with `!important` — and the one that could not be overridden at all was the log's
       `max-height: 52vh`. In a tall panel the transcript stopped at half the viewport and the
       composer sat wherever the content happened to end, with the rest of the panel empty
       below it. A chat's input belongs at the bottom of whatever box it is in, and the
       transcript takes the slack; that is a rule about the shape, not about a pixel count. */
    <div className="agent-chat-shell" data-role={roleName ? "yes" : undefined}>
      {/* RAIL — General + one entry per task (💬 if it already has a conversation) */}
      <aside className="agent-chat-rail">
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
      <section className="agent-chat-main">
        {dockFace ? (
          <DockAgentHead roleKey={dockFace} roleName={roleName} state={speech.state} projectId={projectId}
                         actId={aboutAct} onClearAct={clearAboutAct} />
        ) : null}
        {sel === GENERAL ? (
          /* A ROLE CHAT IS NOT "BOSS AGENT". The panel announced the Engineering Captain in its
             header and then, one line down, "BOSS Agent — What do you want to do?" with a
             description of the general assistant. Two identities in one column reads as a
             template that nobody adapted, which is what it was. */
          <div className="agent-chat-intro">
            {/* WHAT THIS SESSION IS ACTUALLY FOR, read from the ledger rather than from the
                scrollback above it. A long conversation has its early turns compacted away
                and the objective is usually the first thing to go — this is the same record
                the agent re-reads each turn, so operator and agent see one thing. Renders
                nothing when there is no long-horizon work, which is most conversations. */}
            <LedgerStrip threadId={threadId} />
            {dockFace ? null : <span className="eyebrow">{roleName ?? "BOSS Agent"}</span>}
            <h2>{threadId
                   ? (roleName
                        ? `Continuing with ${roleName}`
                        : sessionTitle(threads.find((t) => t.thread_id === threadId)
                                       ?? { thread_id: threadId }))
                   : roleName
                     ? `Ask ${roleName}`
                     : discussing
                       ? "What's on your mind?"
                       : "What do you want to do?"}</h2>
            {threadId ? (
              roleName ? (
                <p className="detail">Picking up where you left off — this conversation is kept
                   for a week.</p>
              ) : null
            ) : roleName ? (
              <p>Answering from this role&rsquo;s own lens and what it has actually argued on
                 this project — not from the general assistant.</p>
            ) : discussing ? (
              /* ONE SURFACE, BOTH KINDS OF QUESTION — so the copy names both rather than
                 promising a discussion and then answering about deploys, or the reverse. */
              <p>An idea, or what the factory is doing — ask either. BOSS will ask what it
                 needs to know, argue the parts that look wrong, and read live state when the
                 question turns on it. Nothing starts until you press Build.</p>
            ) : (
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

        {/* ONE CONTROL, EVERY CONVERSATION.
            This lived inside the general branch above, so a chat opened on a RUN — the dock's
            most common state, reached from a card or a workflow step — had no way to start over
            while the general one did. Same component, same thread machinery, two behaviours
            depending on which branch rendered. `clearChat` was never the limitation: it takes a
            thread_id and has always worked for any of them. */}
        {threadId && messages.length ? (
          <button type="button" className="linklike agent-clear" data-testid="agent-clear"
                  disabled={clearing} onClick={() => void clearChat()}
                  title={`Start ${roleName ?? "this"} conversation again. The messages are `
                         + "archived, not deleted, and no other chat is touched."}>
            {clearing ? "Clearing…" : "Clear this chat"}
          </button>
        ) : null}

        <div className="agent-chat-log" aria-live="polite" aria-label="Agent conversation">
          {loadingThread ? (
            <p className="detail">Loading conversation…</p>
          ) : messages.length === 0 ? (
            <div className="agent-chat-empty">
              <div className="agent-empty-mark" aria-hidden>◆</div>
              <p className="agent-empty-lead">{discussing
                ? "Nothing here yet — ask away."
                : `Ask ${roleName ?? "BOSS"} about this work.`}</p>
            </div>
          ) : messages.map((m, i) => (
            <article className={`agent-message ${m.role}`} key={i}
                     data-testid={`agent-message-${m.role}`}>
              {m.role === "assistant" && dockFace ? (
                <span className="ag-dock-gutter" data-testid="ag-dock-gutter">
                  <DockGutterFace roleKey={dockFace} />{nameOf(dockFace).name}
                </span>
              ) : (
                <span>{m.role === "assistant" ? "BOSS" : m.role === "user" ? "You" : "tool"}</span>
              )}
              {/* WHAT WAS SENT WITH IT. Without this the transcript shows "Look at what I've
                  attached." over nothing, and neither the operator nor a later reader can tell
                  whether the file arrived. */}
              {m.attachments?.length ? (
                <div className="agent-attached" data-testid="agent-message-attached">
                  {m.attachments.map((a, n) => (
                    <span className="agent-attached-chip" key={`${a.name}-${n}`}>
                      <span aria-hidden>{a.kind === "image" ? "▣" : "▤"}</span>
                      <span className="agent-attached-name">{a.name}</span>
                    </span>
                  ))}
                </div>
              ) : null}
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
                <>
                  <Markdown className="agent-md"
                            text={stripAppFacts(withoutBrief(m.content))} />
                  {m.technical ? (
                    <details className="agent-technical" data-testid="agent-message-technical">
                      <summary>Details</summary>
                      <pre>{m.technical}</pre>
                    </details>
                  ) : null}
                  {/* THE CONVERSATION BECOMES THE THING YOU BUILD.
                      A discussion that ends with "now go and type all that into the composer"
                      wastes the part that took the effort. When the agent hands back a brief,
                      it is one press to the composer — and still a press, because a chat that
                      quietly starts a build is the opposite of a surface for thinking. */}
                  <CitedActs ids={[...citedIds((m as { cited_acts?: unknown }).cited_acts), ...(citedHere[i] ?? [])]}
                             projectId={projectId} viewer={actor} speaker={dockAgent} />
                  {/* One-tap thumbs on a finished answer (run scorecards: "Talk to BOSS answer"),
                      keyed by the thread and the answer's place in it. Not on the answer still
                      arriving. */}
                  {threadId && m.content && !(busy && i === messages.length - 1) ? (
                    <RateIt projectId={projectId} actor={actor} subject="answer"
                            id={`chat:${threadId}:${i}`} testId="agent-message-rate" />
                  ) : null}
                  {briefIn(m.content) ? (
                    <div className="agent-brief" data-testid="agent-brief">
                      <pre>{briefIn(m.content)}</pre>
                      <button type="button" className="cap-btn primary"
                              data-testid="agent-brief-use"
                              onClick={() => useBrief(briefIn(m.content)!)}>
                        Use this as the brief →
                      </button>
                      <span className="detail">
                        Drops it into the Command Center. Nothing starts until you press Build.
                      </span>
                    </div>
                  ) : null}
                </>
              ) : (
                <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                  {stripAppFacts(m.content)}
                </p>
              )}
              {m.actions && m.actions.length > 0 ? (
                <div className="agent-action-row">
                  {m.actions.map((a, j) => (
                    <span className="agent-action" key={j}>{actionLabel(a)}</span>
                  ))}
                </div>
              ) : null}
            </article>
          ))}
          {/* THINKING IS NOT NOTHING.
              A turn takes 5-30 seconds while the agent chains tools, and the only feedback was
              the Send button changing to "…" at the far bottom of the panel. From the reading
              position — the bottom of the log — the operator saw their message and then dead
              air, and reasonably concluded the request had been lost. */}
          {busy || probe || landing ? (
            <article className="agent-message assistant" data-testid="agent-working">
              {dockFace ? (
                <span className="ag-dock-gutter">
                  <DockGutterFace roleKey={dockFace} state={speech.state} />{nameOf(dockFace).name}
                </span>
              ) : <span>BOSS</span>}
              <p className="detail agent-working" data-slow={elapsed >= AGENT_NORMAL_SECONDS}
                 data-testid="agent-working-line" aria-live="polite">
                <span className="agent-working-dot" aria-hidden />
                <span>
                  {/* The most recent real step beats any generic sentence: it is evidence the
                      turn is alive, and it tells the operator what is being looked at. */}
                  {steps.length
                    ? steps[steps.length - 1]
                    : probe
                      ? "Debugging — reading the code, the database and the host. This takes a few minutes."
                      : elapsed >= AGENT_NORMAL_SECONDS
                        ? "Still working — this turn is running long. It has not been dropped; the next step it reports will appear here."
                        : "Working — reading the system and choosing tools."}
                </span>
                <span className="agent-working-elapsed" data-testid="agent-elapsed">
                  {/* A stall warning against a 120s budget is wrong for a debugging session,
                      which is EXPECTED to run for minutes. Saying "running long" about work
                      that is going exactly to plan teaches the operator to ignore the warning
                      on the turn that really has died. */}
                  {busy
                    ? `${elapsed}s${elapsed >= AGENT_NORMAL_SECONDS ? ` / ~${AGENT_STALL_SECONDS}s` : ""}`
                    : "investigating"}
                </span>
              </p>
              {/* The trail, oldest first. Six is enough to show it is chaining without the
                  panel scrolling away from the answer when it arrives. */}
              {steps.length > 1 ? (
                <ol className="agent-steps" data-testid="agent-steps">
                  {steps.slice(0, -1).map((s, i) => <li key={`${s}-${i}`}>{s}</li>)}
                </ol>
              ) : null}
            </article>
          ) : null}

          {/* WHAT IS WAITING ITS TURN. Shown as your own words, dimmed, in order — a queued
              message you cannot see is one you cannot tell was accepted, and the operator
              would retype it. */}
          {queued.map((q, i) => (
            <article key={`queued-${i}`} className="agent-message user agent-queued"
                     data-testid="agent-queued">
              <span>You</span>
              <p>{q}</p>
              <span className="detail agent-queued-note">
                queued — sends when this turn finishes
                <button type="button" className="linklike"
                        data-testid={`agent-unqueue-${i}`}
                        onClick={() => setQueued((all) => all.filter((_, n) => n !== i))}>
                  remove
                </button>
              </span>
            </article>
          ))}

          {/* INSIDE THE TRANSCRIPT, NOT UNDER IT.
              These were siblings of the scroll box on a fixed-height shell, so their height came
              OUT of the conversation: five proposals — each a button plus a paragraph of why —
              squeezed the log to about one bubble, and a 3,525-character answer was present,
              correct and invisible. The operator saw their own question, then buttons, and no
              reply. They also belong here on the merits: the evidence and the offers are part of
              the answer they came from, and they should scroll with it. */}
        {/* WHAT THE ANSWER IS BASED ON.
              BOSS would read a dozen files to diagnose a bug and then show a paragraph with
              nothing behind it — `actions` stays empty because a read changes nothing. The
              operator's only options were to believe it or redo the work by hand. Collapsed by
              default: it is evidence, not narrative, and it should be there when you doubt the
              answer without shouting over it when you do not. */}
          {trail.length > 0 ? (
            <details className="agent-trail" data-testid="agent-trail">
              <summary className="detail">
                {`What BOSS read for this answer — ${trail.length} lookup${trail.length === 1 ? "" : "s"}`}
              </summary>
              <ul className="agent-trail-list">
                {trail.map((call, i) => {
                  const args = call.args || {};
                  // The arguments worth showing: what it searched for and where it looked. The
                  // ref matters most — a search of the wrong branch is the failure this whole
                  // trail exists to make visible.
                  const detail = ["pattern", "file", "run_id", "repo", "ref", "service", "url"]
                    .filter((k) => args[k] !== undefined && args[k] !== "")
                    .map((k) => `${k}=${String(args[k]).slice(0, 60)}`)
                    .join("  ");
                  return (
                    <li key={i} className="agent-trail-item">
                      <code>{call.tool}</code>
                      {call.ok === false ? <span className="agent-trail-bad"> failed</span> : null}
                      {detail ? <span className="detail"> {detail}</span> : null}
                    </li>
                  );
                })}
              </ul>
            </details>
          ) : null}

          {/* OFFERS, NOT DEEDS. Each is a tool + arguments BOSS chose but did not run; the click
              is what executes it, verbatim, with no model in between. Cheapest first, because
              "this link is wrong" does not say whether you want it cleared or the app redeployed —
              and one of those costs minutes and money. */}
          {/* THE ANSWER CAME FIRST; GOING DEEPER IS A CHOICE. Under an answer BOSS gave from
              the record in one call (boss-1: 347 s became one call), the full investigation and
              the fix are one press away — and never start on their own. */}
          {nextSteps.length > 0 && !busy ? (
            <div className="agent-next-steps" data-testid="agent-next-steps">
              {nextSteps.map((step) => (
                <button
                  type="button"
                  className="button agent-next-step"
                  key={step.id || step.label}
                  data-testid={`agent-next-${step.id || step.label}`}
                  title={step.message}
                  onClick={() => takeNextStep(step)}
                >
                  {step.label}
                </button>
              ))}
            </div>
          ) : null}

          {proposed.length > 0 ? (
            <div className="agent-proposals" data-testid="agent-proposals">
              <p className="detail agent-proposals-head">
                BOSS can do this for you — nothing runs until you choose:
              </p>
              {proposed.map((a) => (
                <div className="agent-proposal" key={a.id}>
                  <GatedButton
                    permission="assistant:chat"
                    type="button"
                    className="button agent-proposal-btn"
                    disabled={busy}
                    data-testid={`agent-proposal-${a.tool}`}
                    onClick={() => acceptAction(a)}
                  >
                    {busy && ranAction === a.id ? "…" : a.label}
                  </GatedButton>
                  {a.why ? <span className="detail agent-proposal-why">{a.why}</span> : null}
                </div>
              ))}
            </div>
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
        <form className="agent-composer" onSubmit={(e) => { e.preventDefault(); if (chat.allowed) send(); }}>
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
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (chat.allowed) send(); }
            }}
            // CMD+V IS HOW A SCREENSHOT ARRIVES. Every operator already has the image on their
            // clipboard when they decide to report the bug; making them save it to disk first
            // so they can pick it from a dialog is the console adding a step.
            onPaste={(e) => {
              const dropped = Array.from(e.clipboardData?.files ?? []);
              if (dropped.length) { e.preventDefault(); void attach(dropped); }
            }}
            style={{ resize: "none", overflowY: "auto", minHeight: 64, maxHeight: 220,
                     lineHeight: 1.55, fontFamily: "inherit" }} />
          {files.length ? (
            <div className="agent-attached" data-testid="agent-attached">
              {files.map((f, i) => (
                <span className="agent-attached-chip" key={`${f.name}-${i}`}>
                  <span aria-hidden>{f.kind === "image" ? "▣" : "▤"}</span>
                  <span className="agent-attached-name">{f.name}</span>
                  <button type="button" className="agent-attached-drop"
                          aria-label={`Remove ${f.name}`}
                          onClick={() => setFiles((list) => list.filter((_, n) => n !== i))}>
                    ×
                  </button>
                </span>
              ))}
            </div>
          ) : null}
          {fileError ? <p className="agent-attached-error">{fileError}</p> : null}
          <div className="agent-composer-bar">
            <input ref={picker} type="file" multiple hidden
                   data-testid="agent-file-input"
                   accept="image/png,image/jpeg,image/webp,image/gif,text/plain,text/markdown,text/csv,application/json,.log,.txt,.md,.json,.yaml,.yml,.csv"
                   onChange={(e) => { void attach(e.target.files); e.target.value = ""; }} />
            <button type="button" className="agent-attach" data-testid="agent-attach"
                    onClick={() => picker.current?.click()}
                    title="Attach a screenshot or a text file — or just paste an image here">
              📎
            </button>
            <span className="agent-composer-hint">
              {busy
                ? (queued.length
                    ? `BOSS is working — ${queued.length} queued`
                    : "BOSS is working — send anyway to queue")
                : "Enter to send · Shift+Enter for a new line"}
            </span>
            {/* STOP WAITING, not cancel. A turn is a server action and cannot be killed from
                here; this gives the composer back and drops the queue, and the transcript says
                exactly that. A button labelled Cancel would be a lie the next message
                exposes. */}
            {busy ? (
              <button type="button" className="linklike agent-stop"
                      onClick={release} data-testid="agent-stop"
                      title="Stop waiting for this turn. BOSS may still finish it; anything
                             queued behind it is dropped.">
                Stop
              </button>
            ) : null}
            {/* NOT disabled while busy any more: pressing it queues. Still disabled with
                nothing to send — a live-looking button that does nothing is worse than one
                that says it is not ready. An attachment cannot be queued, so that case waits. */}
            <GatedButton permission="assistant:chat" type="submit" className="agent-send"
                    disabled={(!input.trim() && !files.length) || (busy && !!files.length)}
                    aria-label={busy ? "Queue message" : "Send message"}
                    data-testid="agent-send">
              {busy && files.length ? <span className="agent-send-spin" aria-hidden /> : "↑"}
            </GatedButton>
          </div>
        </form>
      </section>
    </div>
  );
}
