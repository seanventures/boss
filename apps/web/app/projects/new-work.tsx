"use client";

/**
 * NEW WORK, as a modal (wireframe new-project-v2/d-new-work.html; parity e-new-work-parity.html):
 * it replaces the Work tab's one-line composer and opens from the header's New work, from the bar
 * on the Work tab (typing there opens it with your words), from N, and from an Ask-Helm answer's
 * "New work · Edit" (`compose=1&compose_text=&compose_kind=`: prefilled, never sent).
 *
 *   ┌ New work · <project> its branch ────────────────────────────── ⤢ ✕ ┐
 *   │ Feature · Bug · Ticket or link        │ Talk it through            │
 *   │ [ multi-line box                    ] │  Helm: one question, quick │
 *   │ Task (Helm)                           │  replies; you reply freely │
 *   │  title · what changes · area (graph)  │  and the brief updates     │
 *   │  done when · risk   Looks right/Fix/↻ │  Open in Ask Helm          │
 *   │ Picked for you: Workflow · Priority · Estimate · Issue · Tell · Target · More │
 *   │ Plan: who does each step · readiness refusal (Start anyway, ticked)          │
 *   └ ≈ time · ≈ cost                                  Add to Next   ▶ Start ⌘↵ ┘
 *
 * Helm's understanding is the one agent pipeline's turn (POST …/work/understand); the chips are
 * this project's own record (GET …/work/plan?detail=true): the workflow per kind from its
 * installed mappings with its real steps and approvals, the estimate from its runs, its tracker,
 * targets and notification routes. Start goes through THE start door; Add to Next through the
 * plan route. No model, or no time: Start uses the words as written. Esc keeps the draft.
 */
import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { useAreaKeydown } from "../../lib/area-keys";
import { createPortal } from "react-dom";
import {
  ArrowSquareOut, ArrowsIn, ArrowsOut, ArrowsVertical, Bug, CaretDoubleUp, CaretDown, ChatCircleDots, Check, Database, Equals, File as FileIcon,
  FileCode, FilePy, FileTs, FileTsx, GitBranch, GitMerge, GithubLogo, Globe, Graph, Hammer, Link as LinkIcon, ListPlus, MagnifyingGlass,
  PaperPlaneTilt, Paperclip, Play, Plus, PlusCircle, RocketLaunch, Sparkle, Ticket, Trash, UserCircle, X,
} from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { post } from "../workflows/delivery-api";
import { ReadinessPanel, unaccepted, type Readiness } from "../workflow-readiness-ui";
import { startBody, type StartView } from "../../lib/start-project";
import {
  dollarsAbout, isMissingStatus, usePxRead, writePxUrl, type Agent, type PlanNode, type WorkKind,
} from "../../lib/project-experience";
import { FX_WORK_PLAN } from "../../lib/project-experience-fixtures";
import {
  PRIORITIES, PRIORITY_WORDS, addToNext, issueIsBug, issueLinkIn, newDraftId, parsePlanDetail, planDetailPath, readDraft, startNewWork,
  understand, writeDraft, type Brief, type ChatMsg, type IssueChoice, type IssueRead, type PlanDetail, type Priority, type Understanding,
} from "../../lib/new-work";
import { BriefCard, Marked, type BriefRow } from "./brief-card";
import { Face, Flow, Said } from "./kit";
import type { PxCtx } from "./project-page";
import { changeWords, projectIsLocal } from "../../lib/repo-links";
import "./new-work.css";

export type Kind = WorkKind;
/** What a door opens it with; `fromBar`: the first keys typed in the Work tab's bar (a kept
 *  draft still comes back, with them added). */
export type Prefill = { text?: string; kind?: Kind; fromBar?: boolean } | null;

const KINDS: { key: Kind; label: string; icon: typeof Sparkle; ph: string }[] = [
  { key: "feature", label: "Feature", icon: Sparkle, ph: "Describe it — e.g. \"let agents tag tickets by product\"" },
  { key: "bug", label: "Bug", icon: Bug, ph: "What's wrong — e.g. \"SLA timer ignores weekends\"" },
  { key: "ticket", label: "Ticket or link", icon: LinkIcon, ph: "Paste a GitHub or Plane issue link" },
];
/** What a person may ask Helm before it asks anything (they send, Helm answers). */
const SUGGEST: Record<Kind, string[]> = {
  feature: ["What could break?", "Smaller first step?", "Similar work before?"],
  bug: ["How would you reproduce it?", "What else could break?", "Seen this before?"],
  ticket: ["What does the issue miss?", "Smaller first step?", "Similar work before?"],
};
/** The box's limit (the start door's and the old composer's). */
const MAX = 2000;
/** Text files ride in the words (a log, a stack trace); at most this much of each. */
const MAX_FILE_CHARS = 6000;
const TEXT_FILE = /\.(log|txt|md|json|ya?ml|csv|tsv|xml|html?|css|[jt]sx?|py|go|rb|java|kt|rs|sql|sh|toml|ini|env\.example|stack|trace)$/i;

/** Before BOSS answers (or on an engine without the plan route): the strip, said as typical. */
const TYPICAL: PlanNode[] = [
  { key: "plan", label: "Plan", type: "agent", agent: { name: "Juno", role_key: "pm", avatar: "" } },
  { key: "approve", label: "You approve", type: "you", agent: null },
  { key: "build", label: "Build", type: "todo", agent: null }, { key: "review", label: "Review", type: "todo", agent: null },
  { key: "pr", label: "PR", type: "todo", agent: null }, { key: "ship", label: "Staging", type: "todo", agent: null },
];
const NODE_ICON: Record<string, React.ReactNode> = {
  build: <Hammer size={11} aria-hidden />, review: <MagnifyingGlass size={11} aria-hidden />, pr: <GitMerge size={11} aria-hidden />,
  ship: <Globe size={11} aria-hidden />, design: <Sparkle size={11} aria-hidden />, approve: <GitMerge size={11} aria-hidden />,
};

const lines = (t: string) => (t ? t.split("\n").length : 0);
const fileIcon = (path: string) => (/\.py$/.test(path) ? FilePy : /\.tsx$/.test(path) ? FileTsx : /\.ts$/.test(path) ? FileTs
  : /migration|\.sql$/i.test(path) ? Database : /\.(js|jsx|go|rb|java|rs|kt)$/.test(path) ? FileCode : FileIcon);

type Status = "idle" | "reading" | "ready" | "fallback";
type By = "default" | "helm" | "you";

/** THE WORK TAB's bar (the composer's place): click, press N, or start typing — the modal opens
 *  with your words. */
export function NewWorkBar({ ctx, said }: { ctx: PxCtx; said?: { text: string; tone?: "red" | "green" } }) {
  const gate = useGate("workflow_run:start", ctx.projectId);
  return (
    <div className="px-nw-barwrap">
      <label className="px-nw-bar" data-testid="px-nw-bar" title="Opens New work. Typing here opens it with your words." data-can={gate.allowed ? "yes" : "no"}>
        <PlusCircle size={16} aria-hidden style={{ color: "var(--accent-text)", flex: "none" }} />
        <span className="px-sr">New work</span>
        <input value="" placeholder="New work: describe a feature, a bug, or paste a ticket link…" aria-label="New work: describe a feature, a bug, or paste a ticket link"
               data-testid="px-nw-bar-input" readOnly={!gate.allowed} aria-disabled={gate.allowed ? undefined : true} title={gate.allowed ? undefined : gate.reason}
               onClick={() => { if (gate.allowed) ctx.newWork?.(); }}
               onChange={(e) => { if (gate.allowed && e.target.value) ctx.newWork?.({ text: e.target.value, fromBar: true }); }}
               onKeyDown={(e) => { if (gate.allowed && e.key === "Enter") { e.preventDefault(); ctx.newWork?.(); } }} />
        <span className="px-nw-kbd px-hide-phone" aria-hidden>N</span>
      </label>
      {said?.text ? <Said text={said.text} tone={said.tone} /> : null}
    </div>
  );
}

export function NewWorkModal({ ctx, prefill, onClose, onDone }: {
  ctx: PxCtx; prefill: Prefill; onClose: () => void;
  /** Started (run id) or planned: the page shows the row and says so. */
  onDone: (r: { runId?: string; said: string; tab: "work" | "roadmap" | null }) => void;
}) {
  const pid = ctx.projectId;
  // THE DRAFT: an Esc-kept draft comes back; a prefill (Ask Helm's Edit, typing in the bar) wins.
  const restored = useMemo(() => ((prefill?.text || prefill?.kind) && !prefill?.fromBar ? null : readDraft(pid)), [pid, prefill]);
  const [draftId] = useState(() => restored?.id ?? newDraftId());
  const [kind, setKind] = useState<Kind>(prefill?.kind ?? restored?.kind ?? "feature");
  const [text, setText] = useState(prefill?.fromBar && restored ? `${restored.text}${prefill.text ?? ""}` : prefill?.text ?? restored?.text ?? "");
  const [back, setBack] = useState(Boolean(restored));
  const [chat, setChat] = useState<ChatMsg[]>(restored?.chat ?? []);
  const [brief, setBrief] = useState<Brief | null>(restored?.brief ?? null);
  const [status, setStatus] = useState<Status>(restored?.brief ? "ready" : "idle");
  const [confirmed, setConfirmed] = useState(Boolean(restored?.confirmed));
  const [editing, setEditing] = useState(false);
  const [fromChat, setFromChat] = useState<string[]>([]);
  const [issue, setIssue] = useState<IssueRead | null>(null);
  const [helm, setHelm] = useState<Agent | null>(ctx.overview.captain ?? null);
  const [graph, setGraph] = useState<{ indexed: boolean; files: number } | null>(null);
  const [talking, setTalking] = useState(false);
  const [reply, setReply] = useState("");
  const [priority, setPriority] = useState<{ v: Priority; by: By }>({ v: restored?.priority ?? "normal", by: restored?.priority ? "you" : "default" });
  const [notify, setNotify] = useState<{ v: string; by: By }>({ v: restored?.notify ?? "", by: restored?.notify ? "you" : "default" });
  const [issueChoice, setIssueChoice] = useState<IssueChoice>(restored?.issue ?? { mode: "none" });
  const [picked, setPicked] = useState<{ v: string; by: By }>({ v: restored?.workflow ?? "", by: restored?.workflow ? "you" : "default" });
  const [readFirst, setReadFirst] = useState("");
  const [pop, setPop] = useState<string | null>(null);
  const [full, setFull] = useState(false);
  const [editor, setEditor] = useState(false);
  const [wordsOpen, setWordsOpen] = useState(false);
  const [busy, setBusy] = useState<"" | "start" | "next">("");
  const [said, setSaid] = useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const [refusal, setRefusal] = useState<Readiness | null>(null);
  const [startAnyway, setStartAnyway] = useState<string[]>([]);
  const [fileNote, setFileNote] = useState("");
  const box = useRef<HTMLTextAreaElement>(null);
  const sheet = useRef<HTMLDivElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const turn = useRef(0);
  const lastRead = useRef(restored?.brief ? `${restored.kind}|${restored.text}` : "");
  const titleId = useId();

  // ── Helm's plan for this kind (and the picked workflow), with the chips' defaults ──────────
  // A Ticket whose issue is labelled a bug runs the project's bug workflow (its default).
  const planKind: Kind = kind === "ticket" && issueIsBug(issue) ? "bug" : kind;
  const plan = usePxRead<PlanDetail>(planDetailPath(pid, planKind, picked.v), parsePlanDetail,
    { everyMs: 120_000, demo: () => parsePlanDetail({ ...FX_WORK_PLAN, kind: planKind, ...DEMO_DETAIL }) });
  const p = plan.data;
  const k = KINDS.find((x) => x.key === kind)!;

  // ── keep the draft (Esc, ✕, a reload) ─────────────────────────────────────────────────────
  useEffect(() => {
    writeDraft(pid, { id: draftId, kind, text, chat, brief, confirmed, priority: priority.by === "you" ? priority.v : null,
                      notify: notify.by === "you" ? notify.v : "", issue: issueChoice, workflow: picked.by === "you" ? picked.v : "", at: Date.now() });
  }, [pid, draftId, kind, text, chat, brief, confirmed, priority, notify, issueChoice, picked]);

  // ── focus, Esc, ⌘↵ ─────────────────────────────────────────────────────────────────────────
  const startRef = useRef<() => void>(() => {});
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const t = box.current;
    if (t) { t.focus({ preventScroll: true }); t.setSelectionRange(t.value.length, t.value.length); }
    return () => { opener?.focus?.({ preventScroll: true }); };
  }, []);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const layer = useRef({ pop, editor, full });
  layer.current = { pop, editor, full };
  useAreaKeydown((e) => {
    if (e.key === "Escape") {
      e.preventDefault(); e.stopPropagation();
      if (layer.current.pop) setPop(null);
      else if (layer.current.editor) setEditor(false);
      else closeRef.current();
    } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault(); e.stopPropagation();
      startRef.current();
    }
  }, { target: "document", capture: true });
  // The chat scrolls inside its own list (never the modal): the newest line in view.
  useEffect(() => {
    const list = chatEnd.current?.parentElement;
    if (list) list.scrollTop = list.scrollHeight;
  }, [chat.length, talking]);

  // ── Helm's understanding: one turn of the one agent pipeline ─────────────────────────────
  const apply = useCallback((u: Understanding, how: "words" | "chat" | "rewrite", prev: Brief | null) => {
    if (u.agent) setHelm(u.agent);
    setIssue(u.issue);
    setGraph(u.graph);
    if (u.status !== "understood" || !u.brief) {
      if (!prev) setStatus(u.status === "empty" ? "idle" : "fallback");
      if (how !== "words" && u.status !== "empty") {
        setChat((c) => [...c, { from: "helm", text: prev ? "I couldn't answer just now, so the brief stays as it was. Start works as it is." : u.reply || "I couldn't read it just now. Start uses your words as written." }]);
      }
      return;
    }
    const b = u.brief;
    if (how === "chat" && prev) {
      const before = new Set(prev.done_when);
      const fresh = b.done_when.filter((d) => !before.has(d));
      if (fresh.length) setFromChat((f) => [...f, ...fresh.filter((d) => !f.includes(d))]);
    }
    setBrief(b);
    setStatus("ready");
    if (how === "rewrite") setConfirmed(false);
    const firstRead = !chat.some((m) => m.from === "helm");
    const add: ChatMsg[] = [];
    if (how !== "words" || firstRead) {
      if (u.reply) add.push({ from: "helm", text: u.reply });
      if (u.ask) add.push({ from: "helm", text: u.ask.text, ask: true, replies: u.ask.replies });
    }
    if (u.applied.length && how !== "words") add.push({ from: "sys", text: `Brief updated · ${u.applied.join(" · ")}` });
    if (add.length) setChat((c) => [...c, ...add]);
    // What the person asked for in the chat sets the chips — never over a chip they set themselves.
    if (u.settings.priority) setPriority((x) => (x.by === "you" ? x : { v: u.settings.priority!, by: "helm" }));
    if (u.settings.notify) setNotify((x) => (x.by === "you" ? x : { v: u.settings.notify!, by: "helm" }));
    if (u.settings.workflow_key) setPicked((x) => (x.by === "you" ? x : { v: u.settings.workflow_key!, by: "helm" }));
  }, [chat]);

  const read = useCallback(async (how: "words" | "chat" | "rewrite", talk: ChatMsg[] = chat, hint = "") => {
    const words = text.trim();
    if (!words && !talk.length) return;
    const mine = ++turn.current;
    const prev = brief;
    if (how === "words") { lastRead.current = `${kind}|${text}`; if (!prev) setStatus("reading"); }
    else setTalking(true);
    try {
      const u = await understand(pid, { kind, text: words, chat: talk, brief: prev, hint, draft: draftId, workflow_key: picked.v || undefined });
      if (mine !== turn.current && how === "words") return; // a newer read of the words won
      apply(u, how, prev);
    } finally {
      if (how !== "words") setTalking(false);
    }
  }, [apply, brief, chat, draftId, kind, picked.v, pid, text]);

  // Helm reads as you type (a pause, enough words), and again when the kind changes.
  useEffect(() => {
    if (confirmed || editing || talking) return;
    if (text.trim().length < 12 || lastRead.current === `${kind}|${text}`) return;
    const t = setTimeout(() => { void read("words"); }, 1100);
    return () => clearTimeout(t);
  }, [text, kind, confirmed, editing, talking, read]);

  const send = (said0: string) => {
    const t = said0.trim();
    if (!t || talking) return;
    const next: ChatMsg[] = [...chat, { from: "you", text: t }];
    setChat(next);
    setReply("");
    void read("chat", next);
  };

  // ── the brief's rows (the shared card) ─────────────────────────────────────────────────────
  const b = brief;
  const setB = (patch: Partial<Brief>) => setBrief((x) => (x ? { ...x, ...patch } : x));
  const doneMain = (b?.done_when ?? []).filter((d) => !fromChat.includes(d));
  const doneChat = (b?.done_when ?? []).filter((d) => fromChat.includes(d));
  const newFiles = (b?.area ?? []).filter((f) => f.new).length;
  const oldFiles = (b?.area ?? []).length - newFiles;
  const rows: BriefRow[] = b ? [
    { key: "title", label: "Title", value: <span className="px-ub-ttl" data-testid="px-ub-v-title">{b.title}</span>,
      edit: <input className="px-input" value={b.title} maxLength={120} onChange={(e) => setB({ title: e.target.value })} aria-label="Title" data-testid="px-ub-e-title" /> },
    { key: "what", label: "What changes", value: <span><Marked text={b.what_changes} added={b.added} /></span>,
      edit: <textarea className="px-input" rows={3} value={b.what_changes} maxLength={1200} onChange={(e) => setB({ what_changes: e.target.value })} aria-label="What changes" /> },
    { key: "area", label: "Area", sub: "code graph",
      value: b.area.length ? (
        <span className="px-ub-files">{b.area.map((f) => {
          const I = fileIcon(f.path);
          return <span key={f.path} data-new={f.new ? "" : undefined}><I size={12} aria-hidden /><span className="px-mono">{f.new ? `+ ${f.path}` : f.path}</span>{f.why ? <em>{f.why}</em> : null}</span>;
        })}</span>
      ) : <span className="px-faint">{graph && !graph.indexed ? "No code graph for this project yet, so no files are named." : "No file in the code graph matches these words."}</span>,
      edit: b.area.length ? <span className="px-ub-files">{b.area.map((f) => (
        <span key={f.path}><span className="px-mono">{f.path}</span>
          <button type="button" className="px-icon-btn" aria-label={`Remove ${f.path}`} onClick={() => setB({ area: b.area.filter((x) => x.path !== f.path) })}><Trash size={11} aria-hidden /></button></span>
      ))}</span> : undefined },
    { key: "done", label: "Done when",
      value: <ul>{doneMain.map((d, i) => <li key={i}><Marked text={d} added={b.added} /></li>)}</ul>,
      edit: <ListEdit items={b.done_when} onChange={(done_when) => setB({ done_when })} label="Criterion" /> },
    ...doneChat.map((d, i): BriefRow => ({ key: `chat${i}`, label: "+ from chat", fresh: true, value: <span>{d}</span> })),
    { key: "risk", label: "Risk",
      value: <span><span className="px-tag" data-tone={b.risk.level === "high" ? "red" : b.risk.level === "low" ? "green" : "amber"}>{b.risk.level[0].toUpperCase() + b.risk.level.slice(1)}</span>
        {b.risk.why ? <> <Marked text={b.risk.why} added={b.added} /></> : null}</span>,
      edit: <span className="px-row" style={{ gap: 6, flexWrap: "nowrap" }}>
        <select className="px-input" style={{ width: "auto" }} value={b.risk.level} aria-label="Risk" onChange={(e) => setB({ risk: { ...b.risk, level: e.target.value as Brief["risk"]["level"] } })}>
          <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select>
        <input className="px-input" value={b.risk.why} onChange={(e) => setB({ risk: { ...b.risk, why: e.target.value } })} aria-label="Why" /></span> },
    ...(b.open_questions.length ? [{ key: "open", label: "Open", question: true,
      value: <ul>{b.open_questions.map((q, i) => <li key={i}>{q}</li>)}</ul> } as BriefRow] : []),
  ] : [
    { key: "title", label: "Title", skeleton: [70] }, { key: "what", label: "What changes", skeleton: [92, 60] },
    { key: "done", label: "Acceptance", skeleton: [80, 65, 72] }, { key: "risk", label: "Area · risk", skeleton: [50] },
  ].map((r) => ({ ...r, value: null }));
  const foldChips = b ? (
    <>
      <span className="px-nw-chip"><Check size={12} aria-hidden /> Done when <b>{b.done_when.length}</b></span>
      {doneChat.map((d) => <span key={d} className="px-nw-chip" data-tone="green"><Plus size={11} aria-hidden /> {d.length > 48 ? `${d.slice(0, 46)}…` : d}</span>)}
      {b.area.length ? <span className="px-nw-chip"><Graph size={12} aria-hidden /> {oldFiles} file{oldFiles === 1 ? "" : "s"}{newFiles ? ` + ${newFiles} new` : ""}</span> : null}
      <span className="px-tag" data-tone={b.risk.level === "high" ? "red" : b.risk.level === "low" ? "green" : "amber"}>Risk: {b.risk.level}</span>
    </>
  ) : null;
  const briefState = status === "reading" ? "reading" : status === "fallback" ? "fallback" : status === "ready" && b ? (confirmed && !editing ? "confirmed" : "ready") : null;
  const updated = chat.some((m) => m.from === "sys");

  // ── what Start sends ───────────────────────────────────────────────────────────────────────
  const wfKey = picked.v || (planKind !== kind ? p?.workflow?.key ?? "" : "");
  const words = () => {
    const extra = readFirst.trim() ? `\n\nRead first:\n${readFirst.trim().split(/\n+/).map((l) => `- ${l.trim()}`).join("\n")}` : "";
    return `${text.trim()}${extra}`;
  };
  const fields = (anyway: string[]) => ({
    brief: status === "ready" ? brief : null, priority: priority.v, notify: notify.v, issue: issueChoice,
    ...(wfKey ? { workflow_key: wfKey } : {}), ...(anyway.length ? { start_anyway: anyway } : {}),
  });
  const locked = Boolean(refusal) && unaccepted(refusal, startAnyway).length > 0;
  const start = async () => {
    const t = words();
    if (!text.trim() || busy || locked) return;
    setBusy("start"); setSaid({ text: "" });
    try {
      let r = await startNewWork(pid, kind, t, fields(startAnyway));
      // NOT READY (409 from THE start door): the project's checklist, and Start anyway with ticks.
      if (!r.ok && r.readiness) { setRefusal(r.readiness as Readiness); return; }
      // AN ENGINE WITHOUT §3's POST: the project's normal start door.
      if (!r.ok && isMissingStatus(r.status)) {
        const view = await fetch(`/v1/projects/${encodeURIComponent(pid)}/start`, { cache: "no-store" }).then((x) => (x.ok ? x.json() : null)).catch(() => null);
        if (view) {
          const s = await post("start", { ...startBody(view as StartView, t), ...(startAnyway.length ? { start_anyway: startAnyway } : {}) });
          if (!s.ok && "readiness" in s && s.readiness) { setRefusal(s.readiness); return; }
          r = s.ok ? { ok: true, status: 200, body: {}, error: null } : { ok: false, status: 400, body: null, error: typeof s.error === "string" ? s.error : "It didn't start." };
        }
      }
      if (!r.ok) { setSaid({ text: r.error ?? "It didn't start.", tone: "red" }); return; }
      const planner = p?.nodes[0]?.agent?.name ?? p?.planner?.name ?? "Helm";
      const msg = r.demo ? "Sample mode — nothing was sent." : `Started — ${planner} is planning it.`;
      const issueError = typeof r.body?.issue_error === "string" ? ` ${r.body.issue_error}` : "";
      writeDraft(pid, null);
      onDone({ runId: typeof r.body?.run_id === "string" ? r.body.run_id : undefined, said: msg + issueError, tab: "work" });
    } finally { setBusy(""); }
  };
  startRef.current = () => { void start(); };
  const planNext = async () => {
    if (!text.trim() || busy) return;
    setBusy("next"); setSaid({ text: "" });
    try {
      const r = await addToNext(pid, kind, words(), fields([]));
      if (!r.ok) { setSaid({ text: r.error ?? "It wasn't added.", tone: "red" }); return; }
      const at = typeof r.body?.position === "number" ? r.body.position : null;
      writeDraft(pid, null);
      onDone({ said: r.demo ? "Sample mode — nothing was sent." : `Added to Next${at ? ` (#${at})` : ""} — nothing started.`, tab: null });
    } finally { setBusy(""); }
  };

  // ── Open in Ask Helm, with the draft ──────────────────────────────────────────────────────
  const openAsk = () => {
    const summary = [
      `About new work I'm drafting (${k.label.toLowerCase()}):`, text.trim(),
      ...(b ? ["", `Your brief so far: ${b.title}.`, ...(b.done_when.length ? [`Done when: ${b.done_when.join("; ")}.`] : [])] : []),
      ...(chat.filter((m) => m.from === "you").length ? ["", `I said: ${chat.filter((m) => m.from === "you").map((m) => m.text).join(" / ")}`] : []),
    ].join("\n").slice(0, 1800);
    onClose();
    writePxUrl({ drawer: "captain", cap_draft: summary });
  };

  // ── attach: a log or a text file rides in the words ─────────────────────────────────────────
  const attach = async (files: FileList | null) => {
    setFileNote("");
    for (const f of Array.from(files ?? [])) {
      if (!TEXT_FILE.test(f.name) && !/^text\//.test(f.type) && f.type !== "application/json") {
        setFileNote(`${f.name}: only a log or a text file can ride with new work. Paste a link to a screenshot instead.`);
        continue;
      }
      try {
        const body = (await f.text()).slice(0, MAX_FILE_CHARS);
        setText((t) => `${t}${t && !t.endsWith("\n") ? "\n" : ""}\n${f.name}:\n${body}`.slice(0, MAX));
      } catch { setFileNote(`Could not read ${f.name}.`); }
    }
  };

  // ── the chips ────────────────────────────────────────────────────────────────────────────
  const est = p?.estimate ?? null;
  const wfName = p?.workflow ? p.workflow.name : plan.state === "loading" ? "Reading the plan…" : "Minor Feature";
  const steps = p?.steps ?? [];
  const approvals = steps.filter((s) => s.you);
  const nodes = p?.nodes.length ? p.nodes : TYPICAL;
  const d = p?.defaults ?? null;
  const target = d?.targets.find((t) => t.id === d.target) ?? d?.targets.find((t) => !t.prod) ?? null;
  const prod = d?.targets.find((t) => t.prod) ?? null;
  const repo = ctx.overview.project.repos[0];
  const tellWords = [...new Set((d?.tell.routes ?? []).flatMap((r) => r.words.split(/\s*·\s*/)).filter(Boolean))].join(" · ") || "Console";
  const issueWhere = d?.tracker ? `${d.tracker.name}${d.tracker.where ? ` · ${d.tracker.where}` : ""}` : "";
  const linked = issueChoice.mode === "link" ? issueChoice.url : issueLinkIn(text);
  const crew = useMemo(() => {
    const seen = new Map<string, Agent>();
    for (const n of nodes) if (n.agent && !seen.has(n.agent.name)) seen.set(n.agent.name, n.agent);
    for (const s of steps) if (s.agent && !seen.has(s.agent.name)) seen.set(s.agent.name, s.agent);
    return [...seen.values()];
  }, [nodes, steps]);
  const minutesTitle = est?.minutes_basis === "history"
    ? `Median agent time to the pull request over the last ${est.minutes_runs} run${est.minutes_runs === 1 ? "" : "s"} of this workflow here`
    : "The steps' typical time — no run of this workflow here yet";
  const estWords = est ? [est.minutes ? `≈ ${est.minutes} min` : "", est.usd !== null ? `≈ ${dollarsAbout(est.usd)}` : ""].filter(Boolean).join(" · ") : "";

  const kinds = (
    <div className="px-nw-kinds">
      <div className="px-seg" role="tablist" aria-label="Kind of work" title="The kind picks the default workflow">
        {KINDS.map((x) => (
          <button key={x.key} type="button" role="tab" aria-selected={kind === x.key} aria-pressed={kind === x.key} data-testid={`px-compose-${x.key}`}
                  onClick={() => { setKind(x.key); if (picked.by !== "you") setPicked({ v: "", by: "default" }); setPop(null); }}>
            <x.icon size={13} aria-hidden /> {x.label}</button>
        ))}
      </div>
      <span className="px-faint px-hide-phone" style={{ fontSize: ".56rem" }}>A link pasted on any tab is read too</span>
    </div>
  );

  const showWords = !(confirmed && !editing && !wordsOpen) || !b;
  const box_ = (
    <div className="px-nw-in" data-full={editor ? "" : undefined} data-testid="px-nw-in">
      <span className="px-nw-kind" aria-hidden><k.icon size={18} weight="fill" /></span>
      <label className="px-sr" htmlFor="px-compose-text">Describe the work</label>
      <textarea id="px-compose-text" ref={box} className="px-nw-txt" value={text} placeholder={k.ph} maxLength={MAX}
                rows={editor ? 18 : Math.min(12, Math.max(4, lines(text)))} inputMode={kind === "ticket" ? "url" : undefined}
                data-testid="px-compose-text"
                onChange={(e) => { setText(e.target.value); setBack(false); if (confirmed) setConfirmed(false); }}
                onPaste={(e) => { if (e.clipboardData.files.length) { e.preventDefault(); void attach(e.clipboardData.files); } }}
                onDragOver={(e) => e.preventDefault()} onDrop={(e) => { if (e.dataTransfer.files.length) { e.preventDefault(); void attach(e.dataTransfer.files); } }} />
      <div className="px-nw-tools">
        <button type="button" className="px-icon-btn" title={editor ? "Back to the modal" : "Full-screen editor"} aria-label={editor ? "Close the full-screen editor" : "Full-screen editor"}
                aria-pressed={editor} onClick={() => setEditor((v) => !v)} data-testid="px-nw-editor">{editor ? <ArrowsIn size={13} /> : <ArrowsOut size={13} />}</button>
        <button type="button" className="px-icon-btn" title="Attach a log or a text file" aria-label="Attach a log or a text file" onClick={() => picker.current?.click()} data-testid="px-nw-attach">
          <Paperclip size={13} /></button>
        <input ref={picker} type="file" multiple hidden onChange={(e) => { void attach(e.target.files); e.target.value = ""; }} />
      </div>
      <div className="px-nw-in-foot">
        <span><ArrowsVertical size={11} aria-hidden /> {Math.max(1, lines(text))} line{lines(text) === 1 ? "" : "s"} · grows</span><span aria-hidden>·</span>
        <span>⇧↵ new line · ⌘↵ start</span>
        <span className="px-spacer" />
        {linked ? <span className="px-nw-chip" data-tone="blue" data-testid="px-nw-link"><GithubLogo size={11} aria-hidden /> {issue?.read ? `${issue.label} · ${issue.title.slice(0, 40)}` : "issue link · Helm reads it"}</span>
          : <span className="px-hide-phone">{status === "idle" ? "Helm reads as you type" : `${text.length} / ${MAX}`}</span>}
      </div>
      {fileNote ? <p className="px-nw-note" role="alert">{fileNote}</p> : null}
    </div>
  );
  const folded = (
    <button type="button" className="px-nw-in px-nw-in-fold" onClick={() => setWordsOpen(true)} title="Your words, folded once the brief is confirmed · click to open" data-testid="px-nw-words-fold">
      <span className="px-nw-kind" aria-hidden><k.icon size={15} weight="fill" /></span>
      <span className="px-ell" style={{ flex: 1, minWidth: 0 }}>{text.replace(/\s+/g, " ")}</span>
      <span className="px-nw-chip" data-tone="blue"><CaretDown size={11} aria-hidden /> {lines(text)} line{lines(text) === 1 ? "" : "s"}</span>
    </button>
  );

  const hasHelm = chat.some((m) => m.from === "helm");
  const lastAsk = [...chat].reverse().find((m) => m.from !== "sys");
  const replies = lastAsk?.from === "helm" && lastAsk.ask ? lastAsk.replies ?? [] : [];
  const side = (
    <aside className="px-nw-side" aria-label="Talk it through with Helm" data-testid="px-nw-chat">
      <div className="px-row" style={{ gap: 8 }}>
        <Face agent={helm ?? { name: "Helm", role_key: "engineering_captain" }} size={22} state={talking ? "working" : "idle"} />
        <b className="px-nw-h">Talk it through</b>
      </div>
      <div className="px-nw-msgs" role="log" aria-live="polite" data-testid="px-nw-msgs">
        {!chat.length ? (
          <>
            <p className="px-faint" style={{ fontSize: ".6rem", lineHeight: 1.5, margin: 0 }}>
              {helm?.name ?? "Helm"} asks a question or two once it has read your words and the code. Optional: you can start without answering.</p>
            <div className="px-nw-sugs">{SUGGEST[kind].map((q) => (
              <button key={q} type="button" className="px-nw-sug" onClick={() => send(q)} disabled={!text.trim() || talking} data-testid="px-nw-suggest">{q}</button>
            ))}</div>
          </>
        ) : null}
        {chat.map((m, i) => {
          const firstHelm = m.from === "helm" && (i === 0 || chat[i - 1].from !== "helm");
          if (m.from === "sys") return <span key={i} className="px-nw-sys"><Check size={11} weight="bold" aria-hidden /> {m.text}</span>;
          return (
            <React.Fragment key={i}>
              {firstHelm ? <div className="px-nw-who"><Face agent={helm ?? { name: "Helm", role_key: "engineering_captain" }} size={16} />{helm?.name ?? "Helm"}</div> : null}
              <div className="px-nw-msg" data-from={m.from} data-ask={m.ask ? "" : undefined} data-testid={`px-nw-msg-${m.from}`}>{m.text}</div>
            </React.Fragment>
          );
        })}
        {replies.length && !talking ? (
          <div className="px-nw-sugs" data-testid="px-nw-replies">{replies.map((r) => (
            <button key={r} type="button" className="px-nw-sug" data-quick="" onClick={() => send(r)}>{r}</button>
          ))}</div>
        ) : null}
        {talking ? <div className="px-nw-msg px-nw-typing" data-from="helm" aria-label={`${helm?.name ?? "Helm"} is thinking`}><i /><i /><i /></div> : null}
        <div ref={chatEnd} />
      </div>
      <form className="px-nw-composer" onSubmit={(e) => { e.preventDefault(); send(reply); }}>
        <ChatCircleDots size={14} aria-hidden />
        <input value={reply} onChange={(e) => setReply(e.target.value)} maxLength={1000} aria-label={`Reply to ${helm?.name ?? "Helm"}`}
               placeholder={confirmed ? `Ask ${helm?.name ?? "Helm"}…` : `Reply to ${helm?.name ?? "Helm"}, or ask anything…`} data-testid="px-nw-reply"
               disabled={!text.trim() && !hasHelm} />
        <button type="submit" className="px-icon-btn" aria-label="Send" disabled={!reply.trim() || talking} data-testid="px-nw-send"><PaperPlaneTilt size={14} /></button>
      </form>
      <button type="button" className="px-linkbtn px-nw-ask" onClick={openAsk} title="Continue in Ask Helm, with this draft carried along" data-testid="px-nw-open-ask">
        <ArrowSquareOut size={11} aria-hidden /> Open in Ask {helm?.name ?? "Helm"}</button>
    </aside>
  );

  const chip = (key: string, props: { label: React.ReactNode; value: React.ReactNode; sub?: React.ReactNode; wide?: boolean; tone?: "you"; dashed?: boolean;
                                       title?: string; testid?: string; popover?: React.ReactNode; noPop?: boolean }) => {
    // The Workflow chip's popover has two pages: the workflows, and the chosen one's Steps.
    const isOpen = pop === key || (key === "workflow" && pop === "steps");
    return (
    <div className="px-nw-def" data-wide={props.wide ? "" : undefined} data-t={props.tone} data-dashed={props.dashed ? "" : undefined} data-open={isOpen ? "" : undefined}>
      <button type="button" className="px-nw-def-b" title={props.title} aria-haspopup={props.noPop ? undefined : "dialog"} aria-expanded={props.noPop ? undefined : isOpen}
              onClick={props.noPop ? undefined : () => setPop(isOpen ? null : key)} data-testid={props.testid ?? `px-nw-chip-${key}`} tabIndex={props.noPop ? -1 : 0}>
        <span className="px-lbl">{props.label}{props.noPop ? null : <CaretDown size={10} aria-hidden className="px-nw-caret" />}</span>
        <span className="px-nw-v">{props.value}</span>
        {props.sub ? <span className="px-nw-s">{props.sub}</span> : null}
      </button>
      {isOpen && props.popover ? <div className="px-nw-pop" role="dialog" aria-label={typeof props.label === "string" ? props.label : key}>{props.popover}</div> : null}
    </div>
    );
  };

  const workflowPop = p ? (
    <>
      <div className="px-nw-pop-h"><b>Workflow for this work</b><span className="px-faint">this project&apos;s workflows</span></div>
      <div className="px-nw-wfs" role="listbox" aria-label="Workflow for this work" data-testid="px-compose-workflows">
        {p.options.map((o) => {
          const od = p.optionDetail[o.key];
          return (
            <button key={o.key} type="button" role="option" aria-selected={p.workflow?.key === o.key} className="px-nw-wf" data-testid={`px-compose-wf-${o.key}`} title={o.detail || undefined}
                    onClick={() => { setPicked({ v: o.key, by: "you" }); setPop(null); }}>
              <span className="px-row" style={{ gap: 6 }}>
                {p.workflow?.key === o.key ? <Check size={11} weight="bold" aria-hidden /> : <span style={{ width: 11 }} aria-hidden />}
                <b>{o.name}</b>{o.fits ? <span className="px-tag" data-tone="green">fits a {planKind === "bug" ? "bug" : "feature"}</span> : null}
                {od?.steps ? <span className="px-nw-mini" aria-hidden>{Array.from({ length: Math.min(od.steps, 14) }, (_, i) => <i key={i} data-y={i < od.approvals.length ? "" : undefined} />)}</span> : null}
              </span>
              <small>{[od?.steps ? `${od.steps} steps` : "", od?.approvals.length ? `you approve ${od.approvals.join(", ")}` : "",
                       od?.minutes ? `≈ ${od.minutes} min` : "", od && od.usd !== null ? `≈ ${dollarsAbout(od.usd)}` : "", od?.was ? `was “${od.was}”` : ""].filter(Boolean).join(" · ")}</small>
            </button>
          );
        })}
      </div>
      <div className="px-nw-pop-f">
        {picked.v ? <button type="button" className="px-linkbtn" data-testid="px-compose-wf-default" onClick={() => { setPicked({ v: "", by: "default" }); setPop(null); }}>Back to the project&apos;s default</button> : <span />}
        <span className="px-spacer" />
        <button type="button" className="px-linkbtn" onClick={() => setPop("steps")} data-testid="px-nw-steps-open">Steps</button>
      </div>
    </>
  ) : <p className="px-faint">Reading this project&apos;s workflows…</p>;
  const stepsPop = (
    <>
      <div className="px-nw-pop-h"><b>{wfName} · steps</b><span className="px-faint">who does each</span></div>
      <ol className="px-nw-steps" data-testid="px-nw-steps">
        {steps.map((s, i) => (
          <li key={`${s.id}-${i}`} data-you={s.you ? "" : undefined}>
            <span className="px-nw-stn" data-s={s.you ? "you" : undefined}>{s.you ? "✋" : i + 1}</span>
            <span className="px-ell">{s.label}</span>
            {s.you ? <span className="px-nw-you">you approve{s.approve ? ` ${s.approve}` : ""}</span> : s.agent ? <span className="px-row" style={{ gap: 4 }}><Face agent={s.agent} size={16} />{s.agent.name}</span> : <span />}
          </li>
        ))}
      </ol>
      <div className="px-nw-pop-f"><button type="button" className="px-linkbtn" onClick={() => setPop("workflow")}>Back to workflows</button></div>
    </>
  );

  const defs = (
    <div className="px-nw-defs" data-testid="px-compose-plan" data-state={p ? "ready" : plan.state}>
      {chip("workflow", {
        wide: true, tone: approvals.length ? "you" : undefined, testid: "px-compose-change",
        title: `${picked.v ? "Picked" : `Default for a ${planKind === "bug" ? "bug" : kind === "ticket" ? "ticket" : "feature"}`} · change picks another of this project's workflows`,
        label: <>Workflow{!picked.v ? <span className="px-nw-auto" title="Picked for you from the kind">AUTO</span> : picked.by === "helm" ? <span className="px-nw-auto">HELM</span> : null}</>,
        value: <span data-testid="px-compose-workflow" className="px-ell"><span title={p?.workflow?.why ? `Start runs ${p.workflow.name}: ${p.workflow.why}` : undefined}>{wfName}</span>
          {p?.workflow?.why ? <span className="px-faint" style={{ fontWeight: 400, fontSize: ".54rem" }}> · {p.workflow.why}</span> : null}</span>,
        sub: steps.length ? (
          <span className="px-nw-strip">
            <span className="px-nw-st" aria-hidden>{steps.slice(0, 14).map((s, i) => (
              <React.Fragment key={`${s.id}-${i}`}>{i ? <span className="px-nw-stw" /> : null}<span className="px-nw-stn" data-s={s.you ? "you" : undefined} title={`${s.label}${s.you ? ` · you approve ${s.approve || "it"}` : ""}`}>{s.you ? "✋" : i + 1}</span></React.Fragment>
            ))}</span>
            <span>{steps.length} steps{approvals.length ? <> · <b className="px-nw-amber">{approvals.length} you approve: {approveWords(approvals)}</b></> : " · no approvals"}</span>
          </span>
        ) : undefined,
        popover: pop === "steps" ? stepsPop : workflowPop,
      })}
      {chip("priority", {
        label: "Priority", title: "Where it lands in Planned, and how Marshal ranks it",
        value: <>{priority.v === "high" || priority.v === "urgent" ? <CaretDoubleUp size={13} weight="bold" style={{ color: "var(--amber)" }} aria-hidden /> : <Equals size={13} style={{ color: "var(--faint)" }} aria-hidden />}
          <span data-testid="px-nw-priority">{PRIORITY_WORDS[priority.v]}</span></>,
        sub: priority.by === "helm" ? "set in chat" : priority.by === "you" ? "you set it" : "Marshal ranks it",
        popover: (
          <div role="radiogroup" aria-label="Priority" className="px-nw-radios">
            {PRIORITIES.map((x) => (
              <button key={x} type="button" role="radio" aria-checked={priority.v === x} data-testid={`px-nw-prio-${x}`}
                      onClick={() => { setPriority({ v: x, by: "you" }); setPop(null); }}>{priority.v === x ? <Check size={11} weight="bold" aria-hidden /> : <span style={{ width: 11 }} />}{PRIORITY_WORDS[x]}</button>
            ))}
          </div>
        ),
      })}
      {chip("estimate", {
        label: "Estimate", noPop: true, title: est ? minutesTitle : "BOSS prices a workflow from its own runs in this project",
        value: est ? (
          <span className="px-mono" style={{ fontSize: ".6rem" }}>
            {est.minutes ? <span data-testid="px-compose-est-min" title={minutesTitle}>≈ {est.minutes} min</span> : null}
            {est.minutes && est.usd !== null ? " · " : null}
            {est.usd !== null ? <span data-testid="px-compose-est-usd" title={`Median cost of the last ${est.usd_runs} run${est.usd_runs === 1 ? "" : "s"} of this workflow here`}>≈ {dollarsAbout(est.usd)}</span> : null}
          </span>
        ) : <span className="px-faint">—</span>,
        sub: est ? (est.usd === null ? <span data-testid="px-compose-est-usd-none" title="BOSS prices a workflow from its own runs in this project">cost after its first run</span>
          : est.minutes_basis === "history" ? `from ${est.minutes_runs} run${est.minutes_runs === 1 ? "" : "s"} here` : `from ${est.usd_runs} run${est.usd_runs === 1 ? "" : "s"} here`) : undefined,
      })}
      {chip("issue", {
        label: "Issue", dashed: issueChoice.mode === "none" && !linked, title: "Link a GitHub or Plane issue, or have BOSS create one",
        value: issueChoice.mode === "create" ? <><GithubLogo size={13} aria-hidden /> creates one</> : linked ? <><Ticket size={13} aria-hidden /> {issue?.label || "linked"}</>
          : <span className="px-faint" style={{ fontWeight: 500 }}>+ link or create</span>,
        sub: issueWhere || "no tracker yet",
        popover: <IssuePop where={issueWhere} canCreate={Boolean(d?.tracker)} choice={issueChoice} linkedFromWords={issueLinkIn(text)}
                           onChoose={(c) => { setIssueChoice(c); setPop(null); }} />,
      })}
      {chip("tell", {
        label: "Tell", title: "Who hears when it needs someone or finishes",
        value: <><UserCircle size={14} aria-hidden /> <span className="px-ell" data-testid="px-nw-tell">{notify.v ? `${tellWords} + ${notify.v}` : tellWords}</span></>,
        sub: notify.by === "helm" ? "set in chat" : "on merge, on stuck",
        popover: <TellPop routes={d?.tell.routes ?? []} value={notify.v} onChange={(v) => setNotify({ v, by: "you" })} projectId={pid} />,
      })}
      {chip("target", {
        label: "Target", title: "Where it is deployed after merge",
        value: target ? <span className="px-ell">{target.label.toLowerCase()}{target.port ? ` · :${target.port}` : ""}</span> : <span className="px-faint" style={{ fontWeight: 500 }}>none yet</span>,
        sub: prod ? "prod only with you" : target ? "after you merge" : "set one in Deployments",
        popover: (
          <>
            <div className="px-nw-pop-h"><b>Where it deploys</b></div>
            {d?.targets.length ? <ul className="px-nw-list">{d.targets.map((t) => (
              <li key={t.id}><Globe size={12} aria-hidden /> <b>{t.label}</b> <span className="px-faint px-ell">{t.app}{t.url ? ` · ${t.url.replace(/^https?:\/\//, "")}` : ""}</span>
                {t.prod ? <span className="px-nw-you">only with you</span> : t.id === target?.id ? <span className="px-tag" data-tone="green">after merge</span> : null}</li>
            ))}</ul> : <p className="px-faint" style={{ margin: 0 }}>This project has no deploy target yet; the run stops before deploying.</p>}
            <div className="px-nw-pop-f"><button type="button" className="px-linkbtn" onClick={() => { onClose(); ctx.tab("deployments"); }}>Open Deployments</button></div>
          </>
        ),
      })}
      {chip("more", {
        label: "More", dashed: true, title: "Crew · connector · branch · read first",
        value: <span className="px-faint" style={{ fontWeight: 500 }}>{readFirst ? "read first · " : ""}crew · connector · branch</span>,
        popover: (
          <>
            <div className="px-nw-pop-h"><b>More</b></div>
            <div className="px-nw-more">
              <span className="px-lbl">Crew</span>
              <span className="px-row" style={{ gap: "4px 10px", flexWrap: "wrap" }}>{crew.length ? crew.map((a) => <span key={a.name} className="px-row" style={{ gap: 4, flex: "none" }}><Face agent={a} size={18} />{a.name}</span>) : <span className="px-faint">from the workflow&apos;s steps</span>}
                <button type="button" className="px-linkbtn" onClick={() => { onClose(); ctx.tab("settings", { section: "team" }); }}>Change</button></span>
              <span className="px-lbl">Branch</span>
              <span className="px-row" style={{ gap: 6, alignItems: "flex-start" }}><GitBranch size={12} aria-hidden style={{ flex: "none", marginTop: 2 }} />{repo ? <span className="px-mono" style={{ overflowWrap: "anywhere" }}>{repo.full_name} · onto {repo.branch || "main"}</span> : <span className="px-faint">no repository yet</span>}</span>
              <span className="px-lbl">Connector</span>
              <span><a className="px-linkbtn" href={`?project_id=${encodeURIComponent(pid)}&area=skills`}>Add a connector</a> <span className="px-faint">the build uses this project&apos;s connected ones</span></span>
              <label className="px-lbl" htmlFor="px-nw-read">Read first</label>
              <textarea id="px-nw-read" className="px-input" rows={2} value={readFirst} onChange={(e) => setReadFirst(e.target.value)} placeholder="A doc, a file or a link the crew reads first (one per line)" data-testid="px-nw-readfirst" />
            </div>
          </>
        ),
      })}
    </div>
  );

  const showPlan = Boolean(b) || Boolean(p);
  const planBox = showPlan ? (
    <div className="px-nw-plan">
      <div className="px-row"><span className="px-lbl">Plan</span><span className="px-spacer" /><span className="px-faint" style={{ fontSize: ".54rem" }}>Nothing reaches prod without you</span></div>
      <Flow testid="px-compose-strip" nodes={nodes.map((n) => ({
        label: n.type === "you" && /approve/i.test(n.label) && n.key !== "approve" ? n.label : n.key === "pr" ? changeWords(projectIsLocal(ctx.overview?.project?.repos)).station : n.label,
        type: n.type === "you" ? "waiting" : n.type === "agent" ? "agent" : "todo",
        icon: n.type === "agent" && n.agent ? <Face agent={n.agent} size={16} /> : NODE_ICON[n.key] ?? (n.key === "ship" ? <RocketLaunch size={11} aria-hidden /> : null),
      }))} />
    </div>
  ) : null;

  const gate = useGate("workflow_run:start", pid);
  const n = lines(text);
  const modal = (
    <div className="px px-nw-portal">
    <div className="px-sheet-wrap px-nw-wrap" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={sheet} className="px-sheet px-nw" data-full={full ? "" : undefined} data-editor={editor ? "" : undefined} role="dialog" aria-modal="true" aria-labelledby={titleId} data-testid="px-composer">
        <div className="px-sheet-h px-nw-head">
          <span className="px-ico" data-c="teal" aria-hidden><PlusCircle size={16} /></span>
          <h2 id={titleId}>New work</h2>
          <span className="px-tag px-hide-phone" title="Builds onto this project's branch"><GitBranch size={11} aria-hidden /> {ctx.overview.project.name} · its branch</span>
          {back ? <span className="px-faint px-hide-phone" style={{ fontSize: ".56rem" }} data-testid="px-nw-back">Your draft is back ·{" "}
            <button type="button" className="px-linkbtn" onClick={() => { setText(""); setChat([]); setBrief(null); setStatus("idle"); setConfirmed(false); setFromChat([]); setBack(false); lastRead.current = ""; writeDraft(pid, null); }}>Clear</button></span> : null}
          <span className="px-spacer" />
          <button type="button" className="px-icon-btn px-hide-phone" onClick={() => setFull((v) => !v)} aria-pressed={full} title={full ? "Back to the modal" : "Full screen"} aria-label="Full screen" data-testid="px-nw-full">
            {full ? <ArrowsIn size={14} /> : <ArrowsOut size={14} />}</button>
          <button type="button" className="px-icon-btn" onClick={onClose} title="Close (Esc) · your words are kept as a draft" aria-label="Close" data-testid="px-nw-close"><X size={15} /></button>
        </div>
        <div className="px-nw-body">
          <div className="px-nw-main px-nw-top">
            {kinds}
            {showWords || wordsOpen ? box_ : folded}
            {briefState ? (
              <BriefCard state={briefState} agent={helm} heading="Task" rows={rows} editing={editing} confirmed={confirmed}
                         title={b?.title ?? ""} chips={foldChips}
                         reading={<>Reading your words and the code graph{graph?.indexed ? "" : "…"}</>}
                         sub={briefState === "fallback" ? `${helm?.name ?? "Helm"} couldn't read it just now — no model answered in time.`
                           : confirmed ? (updated ? "Brief confirmed · updated from your chat with Helm" : "Brief confirmed")
                           : `Your ${n} line${n === 1 ? "" : "s"} as a change · ${graph?.indexed === false ? "no code graph yet" : "checked against the code graph"} · click any line to edit`}
                         onConfirm={() => { setConfirmed(true); setEditing(false); setWordsOpen(false); }}
                         onFix={() => setEditing((v) => !v)}
                         onRewrite={(hint) => { void read("rewrite", chat, hint || "Rewrite it more clearly"); }}
                         onOpen={() => setEditing(false)}
                         onRetry={() => { lastRead.current = ""; void read("words"); }}
                         foot={confirmed ? "This becomes the work item’s spec and the PR’s checklist." : undefined} />
            ) : null}
          </div>
          {side}
          <div className="px-nw-main px-nw-bot">
            <div className="px-row"><span className="px-lbl">Picked for you</span><span className="px-faint" style={{ fontSize: ".54rem" }}>from the kind and the brief · click to change</span></div>
            {defs}
            {planBox}
            {refusal ? (
              <div className="px-nw-refused" data-testid="px-compose-readiness">
                <ReadinessPanel readiness={refusal} accepted={startAnyway} onAccept={setStartAnyway} projectId={pid} />
              </div>
            ) : null}
          </div>
        </div>
        <div className="px-nw-foot">
          {estWords ? <span className="px-tag" data-tone="cost" data-testid="px-nw-foot-est">{estWords}</span> : est ? <span className="px-faint" style={{ fontSize: ".56rem" }}>cost after its first run</span> : null}
          {status === "idle" || status === "reading" ? <span className="px-faint px-hide-phone" style={{ fontSize: ".56rem" }}>Start uses the defaults; {helm?.name ?? "Helm"}&apos;s read takes a few seconds</span> : null}
          <Said text={said.text} tone={said.tone} />
          <span className="px-spacer" />
          <GatedButton permission="workflow_run:start" projectId={pid} className="px-btn" data-testid="px-nw-next" title="Adds it to Planned, ranked by Marshal, without starting"
                       disabled={!text.trim() || Boolean(busy)} onClick={() => void planNext()}>
            <ListPlus size={13} aria-hidden /> {busy === "next" ? "Adding…" : "Add to Next"}</GatedButton>
          <GatedButton permission="workflow_run:start" projectId={pid} className="px-btn px-nw-start" data-kind="primary" data-testid="px-compose-start"
                       title={refusal ? (locked ? "Tick Start anyway above to accept what is missing" : "Starts it, accepting what is missing") : "Starts it with the brief and defaults shown (also confirms the brief)"}
                       disabled={!text.trim() || Boolean(busy) || locked} onClick={() => void start()}>
            <Play size={12} weight="fill" aria-hidden /> {busy === "start" ? "Starting…" : refusal ? "Start anyway" : "Start"}{gate.allowed ? <span className="px-nw-kbd" aria-hidden>⌘↵</span> : null}</GatedButton>
        </div>
      </div>
    </div>
    </div>
  );
  // Above the whole console (the rail too), like every sheet: a portal on the body.
  return typeof document === "undefined" ? modal : createPortal(modal, document.body);
}

/** "the PRD, the deploy ×2": what a person approves, each once, with how often. */
function approveWords(steps: { approve: string; label: string }[]): string {
  const n = new Map<string, number>();
  for (const s of steps) { const w = s.approve || s.label.toLowerCase(); n.set(w, (n.get(w) ?? 0) + 1); }
  return [...n].map(([w, c]) => (c > 1 ? `${w} ×${c}` : w)).join(", ");
}

/** Done when, line by line: edit, add, delete. */
function ListEdit({ items, onChange, label }: { items: string[]; onChange: (v: string[]) => void; label: string }) {
  return (
    <span className="px-nw-listedit">
      {items.map((it, i) => (
        <span key={i} className="px-row" style={{ gap: 4, flexWrap: "nowrap" }}>
          <input className="px-input" value={it} aria-label={`${label} ${i + 1}`} onChange={(e) => onChange(items.map((x, j) => (j === i ? e.target.value : x)))} data-testid={`px-ub-e-done-${i}`} />
          <button type="button" className="px-icon-btn" aria-label={`Delete ${label.toLowerCase()} ${i + 1}`} onClick={() => onChange(items.filter((_, j) => j !== i))}><Trash size={11} aria-hidden /></button>
        </span>
      ))}
      <button type="button" className="px-linkbtn" onClick={() => onChange([...items, ""])} data-testid="px-ub-e-done-add"><Plus size={11} aria-hidden /> Add a criterion</button>
    </span>
  );
}

function IssuePop({ where, canCreate, choice, linkedFromWords, onChoose }: {
  where: string; canCreate: boolean; choice: IssueChoice; linkedFromWords: string; onChoose: (c: IssueChoice) => void;
}) {
  const [url, setUrl] = useState(choice.mode === "link" ? choice.url : "");
  const ok = /^https?:\/\/\S+$/.test(url.trim());
  return (
    <>
      <div className="px-nw-pop-h"><b>Issue</b><span className="px-faint">{where || "no tracker connected"}</span></div>
      {linkedFromWords ? <p className="px-faint" style={{ margin: 0, fontSize: ".56rem" }}>Your words carry <span className="px-mono">{linkedFromWords.replace(/^https?:\/\//, "")}</span>; it is linked.</p> : null}
      <form className="px-row" style={{ gap: 6, flexWrap: "nowrap" }} onSubmit={(e) => { e.preventDefault(); if (ok) onChoose({ mode: "link", url: url.trim() }); }}>
        <input className="px-input" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="Paste a GitHub or Plane issue link" aria-label="Issue link" data-testid="px-nw-issue-url" />
        <button type="submit" className="px-btn" data-size="s" disabled={!ok}>Link</button>
      </form>
      <button type="button" className="px-nw-opt" aria-pressed={choice.mode === "create"} disabled={!canCreate} onClick={() => onChoose({ mode: "create" })} data-testid="px-nw-issue-create">
        <Plus size={12} aria-hidden /> Create one{where ? ` in ${where}` : ""}<small>{canCreate ? "filed when you start, with the brief as its text" : "connect a tracker or a repository first"}</small></button>
      {choice.mode !== "none" ? <button type="button" className="px-linkbtn" onClick={() => onChoose({ mode: "none" })}>No issue</button> : null}
    </>
  );
}

function TellPop({ routes, value, onChange, projectId }: { routes: { event: string; label: string; words: string }[]; value: string; onChange: (v: string) => void; projectId: string }) {
  return (
    <>
      <div className="px-nw-pop-h"><b>Who hears about it</b></div>
      <ul className="px-nw-list">{(routes.length ? routes : [{ event: "needs_you", label: "Needs you", words: "Console" }, { event: "pr_ready", label: "PR ready", words: "Console" }]).map((r) => (
        <li key={r.event}><span className="px-faint" style={{ minWidth: 70 }}>{r.event === "needs_you" ? "When stuck" : r.event === "pr_ready" ? "On merge" : r.label}</span><b>{r.words}</b></li>
      ))}</ul>
      <label className="px-lbl" htmlFor="px-nw-tell">Also tell</label>
      <input id="px-nw-tell" className="px-input" value={value} onChange={(e) => onChange(e.target.value)} placeholder="email:lead@example.com or #channel" data-testid="px-nw-tell-input" maxLength={200} />
      <div className="px-nw-pop-f"><a className="px-linkbtn" href={`?project_id=${encodeURIComponent(projectId)}&area=project&tab=settings&section=notifications`}>Project notifications</a></div>
    </>
  );
}

/** Sample mode: the chips' defaults as the wireframe draws them. */
const DEMO_DETAIL = {
  steps: [{ id: "meeting", label: "Plan", you: false }, { id: "build", label: "Build", you: false }, { id: "tests", label: "Unit tests", you: false },
          { id: "review", label: "Review", you: false }, { id: "merge", label: "Merge PR", you: true, approve: "merge PR" }, { id: "deploy", label: "Staging", you: false },
          { id: "smoke", label: "Smoke", you: false }],
  defaults: { tracker: { provider: "github_issues", name: "GitHub", where: "paramg/helpdesk-ai-demo", connected: true },
              targets: [{ id: "dt_s", label: "Staging", environment: "staging", app: "helpdesk", url: "http://10.0.0.5:8004", port: 8004, prod: false },
                        { id: "dt_p", label: "Production", environment: "production", app: "helpdesk", url: "", port: null, prod: true }],
              target: "dt_s", tell: { routes: [{ event: "needs_you", label: "Needs you", words: "Email" }, { event: "pr_ready", label: "PR ready", words: "Email" }] }, priority: "normal" },
};
