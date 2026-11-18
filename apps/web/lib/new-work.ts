/**
 * THE NEW WORK MODAL's data (wireframe new-project-v2/d-new-work.html, parity e-new-work-parity.html).
 *
 *   GET  /v1/projects/{id}/work/plan?kind=&workflow_key=&detail=true   Helm's plan + the chips' defaults
 *   POST /v1/projects/{id}/work/understand                            Helm's understanding (one turn)
 *   POST /v1/projects/{id}/work                                       Start (THE start door)
 *   POST /v1/projects/{id}/plan                                       Add to Next (nothing starts)
 *
 * HELM'S UNDERSTANDING is one shape for every box a person types work into (Add a project reads
 * the same card, `BriefCard`): your words stay, Helm's rewrite sits below them with its additions
 * marked. The draft (words, kind, chat, brief, chips) is kept per project on Esc.
 */
import { apiWrite } from "./api-write";
import {
  PX_PATHS, a, isPxDemo, n, o, parseAgent, parseWorkPlan, pxWrite, s, startWork, toWorkKind,
  type Agent, type PxWrite, type WorkKind, type WorkPlan,
} from "./project-experience";
import { changeRef } from "./repo-links";

type R = Record<string, unknown>;

// ── Helm's understanding ─────────────────────────────────────────────────────────────────────
export type AreaFile = { path: string; why: string; new: boolean };
export type Risk = "low" | "medium" | "high";
export type Brief = {
  title: string; what_changes: string; area: AreaFile[]; done_when: string[];
  risk: { level: Risk; why: string }; added: string[]; open_questions: string[];
};
export type IssueRead = { url: string; label: string; title: string; labels: string[]; state: string; read: boolean; provider: string };
export type Ask = { text: string; replies: string[] };
export type Understanding = {
  status: "understood" | "no_model" | "empty"; brief: Brief | null; reply: string; ask: Ask | null;
  applied: string[]; settings: { priority?: Priority; notify?: string; workflow_key?: string };
  issue: IssueRead | null; graph: { indexed: boolean; files: number }; agent: Agent | null; why: string;
};
export type Priority = "low" | "normal" | "high" | "urgent";
export const PRIORITIES: Priority[] = ["low", "normal", "high", "urgent"];
export const PRIORITY_WORDS: Record<Priority, string> = { low: "Low", normal: "Normal", high: "High", urgent: "Urgent" };

const RISKS: Risk[] = ["low", "medium", "high"];
const strs = (v: unknown) => a(v).map((x) => s(x).trim()).filter(Boolean);

export const parseBrief = (v: unknown): Brief | null => {
  const x = o(v);
  if (!s(x.title).trim()) return null;
  const risk = o(x.risk);
  return {
    title: s(x.title), what_changes: s(x.what_changes),
    area: a(x.area).map((f) => ({ path: s(o(f).path), why: s(o(f).why), new: o(f).new === true })).filter((f) => f.path),
    done_when: strs(x.done_when),
    risk: { level: (RISKS as string[]).includes(s(risk.level)) ? (s(risk.level) as Risk) : "medium", why: s(risk.why) },
    added: strs(x.added), open_questions: strs(x.open_questions),
  };
};

export const parseUnderstanding = (v: unknown): Understanding => {
  const x = o(v);
  const st = s(x.status);
  const ask = o(x.ask);
  const set = o(x.settings);
  const issue = x.issue ? o(x.issue) : null;
  return {
    status: st === "understood" || st === "empty" ? st : "no_model",
    brief: parseBrief(x.brief), reply: s(x.reply),
    ask: s(ask.text) ? { text: s(ask.text), replies: strs(ask.replies).slice(0, 4) } : null,
    applied: strs(x.applied),
    settings: {
      ...((PRIORITIES as string[]).includes(s(set.priority)) ? { priority: s(set.priority) as Priority } : {}),
      ...(s(set.notify) ? { notify: s(set.notify) } : {}),
      ...(s(set.workflow_key) ? { workflow_key: s(set.workflow_key) } : {}),
    },
    issue: issue ? { url: s(issue.url), label: s(issue.label, "Issue"), title: s(issue.title), labels: strs(issue.labels), state: s(issue.state),
                     read: issue.read === true, provider: s(issue.provider) } : null,
    graph: { indexed: o(x.graph).indexed === true, files: n(o(x.graph).files) },
    agent: parseAgent(x.agent), why: s(x.why),
  };
};

/** An issue labelled as a bug starts on the project's bug workflow (the Ticket default). */
export const issueIsBug = (issue: IssueRead | null) => Boolean(issue?.labels.some((l) => /bug/i.test(l)));

/** The words' first GitHub or Plane issue link: read on any tab. */
const ISSUE_URL = /https?:\/\/(?:github\.com\/[\w.-]+\/[\w.-]+\/(?:issues|pull)\/\d+|\S*plane\S*\/(?:issues|work-items|browse)\/[A-Za-z0-9_-]+)\S*/i;
export const issueLinkIn = (words: string): string => (ISSUE_URL.exec(words || "")?.[0] ?? "").replace(/[).,;]+$/, "");

// ── the conversation ─────────────────────────────────────────────────────────────────────────
/** One line of "Talk it through": the person, Helm, or a quiet system note ("Brief updated"). */
export type ChatMsg = { from: "you" | "helm" | "sys"; text: string; ask?: boolean; replies?: string[] };

export type UnderstandBody = { kind: WorkKind; text: string; chat: ChatMsg[]; brief: Brief | null; hint?: string; draft: string; workflow_key?: string };

/**
 * HELM'S READ IS A REAL MODEL TURN (recipe-box walk 2026-11-18): the brief is ~1,000 written
 * tokens, so on a subscription model it takes 15-40 s, bounded by the engine's turn deadline
 * (understanding.TURN_SECONDS, 45 s) and `understand`'s own 55 s. "Reading your words and the
 * code graph…" with nothing moving read as hung. The card says how long it has been reading,
 * and past READ_PATIENCE_S that Start does not need it.
 */
export const READ_PATIENCE_S = 12;
export function readingWords(seconds: number, opts: { starting?: boolean } = {}): string {
  if (opts.starting) return "Starting with your words as written; the brief is not needed to start.";
  if (seconds < 4) return "Reading your words and the code graph…";
  if (seconds < READ_PATIENCE_S) return `Writing the brief from your words and the code graph · ${seconds}s`;
  return `Still writing the brief · ${seconds}s. Start works now: your words go as written.`;
}

/** One turn of Helm's understanding. A slow or missing model is an answer (`no_model`), never a throw. */
export async function understand(projectId: string, body: UnderstandBody, ms = 55_000): Promise<Understanding> {
  const chat = body.chat.filter((m) => m.from !== "sys").map((m) => ({ from: m.from, text: m.text, ...(m.ask ? { ask: true } : {}) }));
  const payload = { kind: body.kind, text: body.text, chat, brief: body.brief, hint: body.hint ?? "", draft: body.draft,
                   ...(body.workflow_key ? { workflow_key: body.workflow_key } : {}) } as R;
  if (isPxDemo()) return parseUnderstanding(DEMO_UNDERSTANDING(body));
  // NOT A PRESS: Helm reads as you type, so this is a background write. Sent as an operator's
  // press ("Ask Helm") it held the press scope for the whole 15-40 s turn, and the next read of
  // the changed words was REFUSED ("Ask Helm was not sent — the previous Ask Helm is still in
  // progress") while the older answer was then dropped as stale. Without a press only an
  // identical read joins the running one (lib/api-write.ts runPress).
  const send = apiWrite(`/v1/projects/${encodeURIComponent(projectId)}/work/understand`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), cache: "no-store",
  }).then(async (res): Promise<PxWrite> => {
    let parsed: unknown = null;
    try { parsed = await res.json(); } catch { /* empty */ }
    return res.ok ? { ok: true, status: res.status, body: o(parsed), error: null }
                  : { ok: false, status: res.status, body: null, error: s(o(parsed).message) || s(o(o(parsed).detail).message) || `The API answered ${res.status}.` };
  // A write that throws (a dropped connection) is an answer too: the card never stays "reading"
  // on a promise that rejected.
  }).catch((e: unknown): PxWrite => ({ ok: false, status: 0, body: null, error: e instanceof Error ? e.message : "no answer" }));
  const late = new Promise<PxWrite>((resolve) => setTimeout(() => resolve({ ok: false, status: 0, body: null, error: "Helm took too long" }), ms));
  const r = await Promise.race([send, late]);
  if (r.demo) return parseUnderstanding(DEMO_UNDERSTANDING(body));
  if (!r.ok || !r.body) return { ...parseUnderstanding({}), why: r.error ?? "no answer" };
  return parseUnderstanding(r.body);
}

// ── Helm's plan, in detail (the Workflow chip, the other chips' defaults) ──────────────────────
/** A workflow step; `approve` is what a person approves there ("the PRD", "the deploy"). */
export type Step = { id: string; label: string; you: boolean; approve: string; agent: Agent | null };
export type OptionDetail = { key: string; steps: number; approvals: string[]; minutes: number | null; usd: number | null; was: string };
export type Target = { id: string; label: string; environment: string; app: string; url: string; port: number | null; prod: boolean };
export type NwDefaults = {
  tracker: { provider: string; name: string; where: string; connected: boolean } | null;
  targets: Target[]; target: string | null;
  tell: { routes: { event: string; label: string; words: string }[] };
  priority: Priority;
};
/** THE APP NEW WORK BUILDS ONTO (AR-20), as the engine resolved the door's `app=`: the build the
 *  change continues, the app's name, its lineage branch and its change / PR. */
export type Anchor = {
  runId: string; app: string; branch: string; prNumber: number | null; prUrl: string | null;
  /** The lineage's change: open (continued), merged (a NEW change from `branch`, the default),
   *  or none yet. A closed-unmerged change never anchors (the engine refuses it). */
  prState: "open" | "merged" | null; mode: "continue" | "new_change";
};
export type PlanDetail = WorkPlan & {
  steps: Step[]; optionDetail: Record<string, OptionDetail>; defaults: NwDefaults | null;
  /** Only when an app was asked for: the anchor, or why that app is not one of this project's. */
  anchor?: Anchor | null; anchorError?: string;
};

/** THE BRIEF'S LIMIT (AR-21): the engine's MAX_DIRECTIVE (services/project_experience/add_project.py)
 *  and POST …/work's `text` max_length — one number; a test reads the Python and keeps them
 *  equal. The box never cuts at it: a counter shows near it, and Start says why it is held. */
export const MAX_BRIEF_CHARS = 20000;
/** Where the counter starts showing (90% of the limit). */
export const BRIEF_COUNTER_FROM = Math.floor(MAX_BRIEF_CHARS * 0.9);
/** What the box says about its length: nothing well below the limit, a count near it, a refusal past it. */
export function briefLength(text: string): { count: string | null; over: boolean; message: string } {
  const n = text.length;
  const over = n > MAX_BRIEF_CHARS;
  const fmt = (v: number) => v.toLocaleString("en-US");
  return {
    count: n >= BRIEF_COUNTER_FROM ? `${fmt(n)} / ${fmt(MAX_BRIEF_CHARS)}` : null,
    over,
    message: over ? `This is ${fmt(n - MAX_BRIEF_CHARS)} characters over the ${fmt(MAX_BRIEF_CHARS)} limit. Shorten it, or attach the rest as a file link, before starting.` : "",
  };
}

/** The modal's words for the app it builds onto: "building on Northgate Gateway · change #4"
 *  (a local change set), "· PR #4" on GitHub, or its branch while it has no change yet. */
export function anchorWords(anchor: Anchor): string {
  const name = anchor.app || "this app";
  if (anchor.mode === "new_change") {
    // ITS CHANGE WAS MERGED: this is a new change from the default branch, said so.
    const ref = changeRef(anchor.prUrl, anchor.prNumber, false);
    return `${name} · ${ref} merged; this starts a new change from ${anchor.branch}`;
  }
  return anchor.prNumber ? `building on ${name} · ${changeRef(anchor.prUrl, anchor.prNumber, true)}` : `building on ${name} · ${anchor.branch}`;
}

const parseAnchor = (v: unknown): Anchor | null => {
  const x = o(v);
  if (!s(x.run_id)) return null;
  const pr = typeof x.pr_number === "number" && Number.isFinite(x.pr_number) ? x.pr_number : null;
  return { runId: s(x.run_id), app: s(x.app), branch: s(x.branch), prNumber: pr, prUrl: s(x.pr_url) || null,
           prState: x.pr_state === "open" || x.pr_state === "merged" ? x.pr_state : null,
           mode: x.mode === "new_change" ? "new_change" : "continue" };
};

const nnum = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export const parsePlanDetail = (v: unknown): PlanDetail => {
  const x = o(v);
  const base = parseWorkPlan(v);
  const optionDetail: Record<string, OptionDetail> = {};
  for (const z of a(x.options)) {
    const y = o(z);
    if (!s(y.key)) continue;
    optionDetail[s(y.key)] = { key: s(y.key), steps: n(y.steps), approvals: strs(y.approvals), minutes: nnum(y.minutes), usd: nnum(y.usd), was: s(y.was) };
  }
  const d = x.defaults ? o(x.defaults) : null;
  const tracker = d && d.tracker ? o(d.tracker) : null;
  return {
    ...base,
    ...("anchor" in x ? { anchor: parseAnchor(x.anchor) } : {}),
    ...(s(x.anchor_error) ? { anchorError: s(x.anchor_error) } : {}),
    steps: a(x.steps).map((z) => ({ id: s(o(z).id), label: s(o(z).label), you: o(z).you === true, approve: s(o(z).approve), agent: parseAgent(o(z).agent) })).filter((z) => z.label),
    optionDetail,
    defaults: d ? {
      tracker: tracker && s(tracker.provider) ? { provider: s(tracker.provider), name: s(tracker.name), where: s(tracker.where), connected: tracker.connected === true } : null,
      targets: a(d.targets).map((t) => ({ id: s(o(t).id), label: s(o(t).label), environment: s(o(t).environment), app: s(o(t).app), url: s(o(t).url),
                                           port: nnum(o(t).port), prod: o(t).prod === true })).filter((t) => t.id),
      target: s(d.target) || null,
      tell: { routes: a(o(d.tell).routes).map((r) => ({ event: s(o(r).event), label: s(o(r).label), words: s(o(r).words, "Console") })) },
      priority: (PRIORITIES as string[]).includes(s(d.priority)) ? (s(d.priority) as Priority) : "normal",
    } : null,
  };
};

export const planDetailPath = (projectId: string, kind: string, workflowKey = "", app = "") =>
  `${PX_PATHS.workPlan(projectId, kind, workflowKey)}&detail=true${app ? `&app=${encodeURIComponent(app)}` : ""}`;

// ── Start and Add to Next ────────────────────────────────────────────────────────────────────
export type IssueChoice = { mode: "none" } | { mode: "link"; url: string } | { mode: "create" };
export type StartFields = {
  brief: Brief | null; priority: Priority; notify: string; issue: IssueChoice;
  workflow_key?: string; start_anyway?: string[];
  /** The app it builds onto (AR-20): the door's `app`, sent as is; the engine resolves it. */
  app?: string;
  /** The profile it runs on (BOSSAPP-1334): lean | balanced | strict. */
  profile?: string;
};

/** The body Start and Add to Next send: the words, the confirmed brief, the chips. */
export function workBody(kind: WorkKind, text: string, f: StartFields): R {
  return {
    ...(f.brief ? { brief: f.brief } : {}),
    ...(f.priority !== "normal" ? { priority: f.priority } : {}),
    ...(f.notify.trim() ? { notify_channel: f.notify.trim() } : {}),
    ...(f.issue.mode === "link" && f.issue.url ? { source_url: f.issue.url } : {}),
    ...(f.issue.mode === "create" ? { issue: { mode: "create" } } : {}),
    ...(f.workflow_key ? { workflow_key: f.workflow_key } : {}),
    ...(f.start_anyway?.length ? { start_anyway: f.start_anyway } : {}),
    ...(f.app ? { app: f.app } : {}),
    ...(f.profile ? { profile: f.profile } : {}),
  };
}

export const startNewWork = (projectId: string, kind: WorkKind, text: string, f: StartFields) =>
  startWork(projectId, kind, text, workBody(kind, text, f));

export const addToNext = (projectId: string, kind: WorkKind, text: string, f: StartFields) =>
  pxWrite(PX_PATHS.plan(projectId), { kind, text, ...workBody(kind, text, f) }, "Add to Next");

// ── the draft, kept on Esc ───────────────────────────────────────────────────────────────────
export type Draft = {
  id: string; kind: WorkKind; text: string; chat: ChatMsg[]; brief: Brief | null; confirmed: boolean;
  priority: Priority | null; notify: string; issue: IssueChoice; workflow: string; at: number;
};
const DRAFT_KEY = (projectId: string) => `boss.newWork.draft.${projectId}`;
export const newDraftId = () => `nw${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;

export function readDraft(projectId: string): Draft | null {
  try {
    const raw = window.localStorage.getItem(DRAFT_KEY(projectId));
    if (!raw) return null;
    const x = o(JSON.parse(raw));
    if (!s(x.text).trim() && !a(x.chat).length) return null;
    return {
      id: s(x.id) || newDraftId(), kind: toWorkKind(x.kind), text: s(x.text),
      chat: a(x.chat).map((m) => ({ from: (["you", "helm", "sys"].includes(s(o(m).from)) ? s(o(m).from) : "sys") as ChatMsg["from"], text: s(o(m).text),
                                     ...(o(m).ask ? { ask: true } : {}), ...(a(o(m).replies).length ? { replies: strs(o(m).replies) } : {}) })),
      brief: parseBrief(x.brief), confirmed: x.confirmed === true,
      priority: (PRIORITIES as string[]).includes(s(x.priority)) ? (s(x.priority) as Priority) : null,
      notify: s(x.notify), issue: parseIssueChoice(x.issue), workflow: s(x.workflow), at: n(x.at),
    };
  } catch { return null; }
}
const parseIssueChoice = (v: unknown): IssueChoice => {
  const x = o(v);
  if (x.mode === "create") return { mode: "create" };
  if (x.mode === "link" && s(x.url)) return { mode: "link", url: s(x.url) };
  return { mode: "none" };
};
export function writeDraft(projectId: string, d: Draft | null): void {
  try {
    if (!d || (!d.text.trim() && !d.chat.length)) window.localStorage.removeItem(DRAFT_KEY(projectId));
    else window.localStorage.setItem(DRAFT_KEY(projectId), JSON.stringify(d));
  } catch { /* private window: the draft lives as long as the page */ }
}

// ── sample mode (px_demo=1): Helm's answer, from the words, so the walk shows every state ───────
function DEMO_UNDERSTANDING(body: UnderstandBody): R {
  const said = body.chat.filter((m) => m.from === "you").map((m) => m.text).join(" ");
  const retag = /re-?tag|all once|existing/i.test(said);
  const high = /high|urgent/i.test(said);
  const tell = /#[\w-]+/.exec(said)?.[0];
  return {
    status: "understood", agent: { name: "Helm", role_key: "engineering_captain" },
    brief: {
      title: "Auto-tag new tickets by product",
      what_changes: "Each new ticket gets a product tag picked from the email text and the customer’s plan, with a confidence. Staff can change it in the inbox; the lead can filter by product.",
      area: [{ path: "api/tickets/intake.py", why: "writes tickets" }, { path: "api/tags/models.py", why: "tag table exists" },
             { path: "web/src/inbox/TicketRow.tsx", why: "shows tags" }, { path: "migrations/tickets_product.py", why: "tickets.product", new: true }],
      done_when: ["A new ticket has a product tag within 10 s", "The tag shows its confidence; under 60% it says “check”",
                  "Staff change it in one click; the change is logged", "The inbox filters by product",
                  ...(retag ? ["Re-tags the 12,000 existing tickets once, in the background"] : [])],
      risk: { level: "medium", why: "touches intake, which every email passes through · adds a column to a 12k-row table" },
      added: ["with a confidence", "The tag shows its confidence; under 60% it says “check”", "adds a column to a 12k-row table"],
      open_questions: [],
    },
    reply: said ? (retag ? "Added it as a background job (Done when +1). That adds about 4 min and $0.20. Anything else?" : high || tell ? "Set both below. Ready to start." : "Noted.")
      : "There’s already a tags table, but nothing writes product tags yet. One question:",
    ask: said ? null : { text: "Should the 12,000 existing tickets be tagged too, or only new ones?", replies: ["Only new", "Re-tag all once", "Last 30 days"] },
    applied: retag ? ["Done when +1", "estimate updated"] : [], settings: { ...(high ? { priority: "high" } : {}), ...(tell ? { notify: tell } : {}) },
    graph: { indexed: true, files: 4 },
  };
}

/** A NEW WORK DOOR'S WORDS, KEPT WHOLE (AR-21): written as this project's New work draft, which
 *  the modal brings back. Every door whose words may be too long for a link calls this before it
 *  navigates (lib/compose-links WORDS_IN_LINK). */
export function keepNewWorkDraft(projectId: string, text: string, kind: WorkKind = "feature",
                                 ask: (question: string) => boolean = (q) => (typeof window === "undefined" ? false : window.confirm(q))): boolean {
  // NEVER OVER A PERSON'S UNSENT DRAFT without asking (PR #156 review B3): an Edit replaced it.
  const had = readDraft(projectId);
  if (had && had.text.trim() && had.text !== text
      && !ask("You have an unsent New work draft in this project. Replace it with these words?")) return false;
  writeDraft(projectId, {
    id: newDraftId(), kind, text, chat: [], brief: null, confirmed: false, priority: null,
    notify: "", issue: { mode: "none" }, workflow: "", at: Date.now(),
  });
  return true;
}
