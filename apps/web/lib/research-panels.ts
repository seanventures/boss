/**
 * The words and shapes the Research (dossiers) and Panels (meetings) screens show, as pure
 * functions of the API's records — so the full pages, the cards and the compact views inside a
 * workflow run's step panel cannot disagree about what a dossier is called, what its verdict
 * is, or which stage a panel has reached.
 */
import type { DiscoveryListItem, DiscoveryRun, Meeting, MeetingTurnGist } from "@boss/api-client";

import { consoleHref } from "../app/url-intent";

export type Tone = "good" | "warn" | "bad" | "neutral" | "live";
export type StageState = "done" | "now" | "todo" | "stopped" | "skipped";
export type Stage = { key: string; label: string; state: StageState };

// ── Titles ──────────────────────────────────────────────────────────────────────────────────

/** A scaffold header BOSS writes into a panel's prompt: "--- RESEARCH ALREADY DONE FOR THIS ---". */
const SCAFFOLD = /^[ \t]*-{2,}[ \t]*([A-Z][A-Z0-9 '’/&,-]{3,}?)[ \t]*-{2,}[ \t]*$/m;
const PROMOTE_LEAD = "Produce a PRD for this evidence-backed opportunity";

/**
 * What the person asked, never the prompt BOSS built around it — the ONE reader of a brief for
 * every hero, card, title and drawer. The API's `brief` (engine meeting_brief.split_brief) when
 * it sent one; else the same cut made here, so an older API cannot leak "--- RESEARCH ALREADY
 * DONE FOR THIS --- … ## What was researched {" onto a screen (shared-server: 4 of 22 panels).
 */
export function humanBrief(raw: string | null | undefined, apiBrief?: string | null): string {
  const tidy = (t: string) => t.replace(/[ \t]*\[boss:[^\]]*\]/gi, "").replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n").trim();
  if (apiBrief && apiBrief.trim()) return tidy(apiBrief);
  let text = String(raw ?? "").replace(/\r\n/g, "\n");
  if (text.trimStart().startsWith(PROMOTE_LEAD)) {
    const field = (k: string) => new RegExp(`^${k}:\\s*(.*)$`, "m").exec(text)?.[1]?.trim() ?? "";
    const pilot = field("TARGET PILOT");
    return tidy(field("OUTCOME TO BUILD") + (pilot && pilot !== "(n/a)" ? `\nWho it is for: ${pilot}` : ""));
  }
  const ask = /^[ \t]*-{2,}[ \t]*WHAT YOU ARE BEING ASKED ABOUT[ \t]*-{2,}[ \t]*$/m.exec(text);
  if (ask) text = text.slice(ask.index + ask[0].length);
  else if (/^\s*-{2,}\s*WHAT THIS PANEL IS FOR/.test(text)) return "";
  const next = SCAFFOLD.exec(text);
  if (next) text = text.slice(0, next.index);
  return tidy(text);
}

/** What a card or a hero can carry: the first sentence of a brief, without "Build a" and
 *  without the "Who it is for" block a workflow brief continues with. A question keeps its "?". */
export function shortTitle(raw: string | null | undefined, max = 80): string {
  // The kind is the eyebrow's to say: "Go/No-Go:", "Standup:" are not part of a title.
  let t = humanBrief(String(raw ?? "").replace(/^(?:PRD meeting|Go\/No-Go|Go \/ no-go|Stand-?up|Simulated meeting):\s*/i, ""))
    .replace(/\s+/g, " ").trim();
  t = t.replace(/^(?:build|make|create)\s+(?:a|an|the)\s+/i, "");
  t = t.split(/\s(?:Who it is for|What it does|What it must do|Who it's for)\b/i)[0];
  const sentence = /^(.*?[.!?])(?:\s|$)/.exec(t);
  if (sentence && sentence[1].length > 3) t = sentence[1].endsWith("?") ? sentence[1] : sentence[1].slice(0, -1);
  t = t.replace(/[.:;,\s]+$/, "").trim();
  if (!t) return "Untitled";
  if (t.length > max) {
    const cut = t.lastIndexOf(" ", max - 1);
    t = `${t.slice(0, cut > max / 2 ? cut : max - 1)}…`;
  }
  return t[0].toUpperCase() + t.slice(1);
}

/** A dossier's or a panel's own words for the hero line: "For <who>. <what it does>" from a
 *  workflow brief, else the rest of the brief after its first sentence. */
export function briefLine(raw: string | null | undefined, max = 220, apiBrief?: string | null): string {
  const text = humanBrief(raw, apiBrief);
  const who = /Who it(?: is|'s) for:\s*([^\n]+)/i.exec(text)?.[1]?.trim();
  const what = /What it does:\s*([^\n]+)/i.exec(text)?.[1]?.trim();
  let line = "";
  if (who || what) {
    const w = who ? `For ${who.replace(/^(A|An|The)\b/, (a) => a.toLowerCase())}` : "";
    line = [w && (/[.!?]$/.test(w) ? w : `${w}.`), what ? capital(what) : ""].filter(Boolean).join(" ");
  } else {
    const flat = text.replace(/\s+/g, " ").trim();
    const stop = flat.search(/[.?!]\s/);
    line = stop > 0 ? flat.slice(stop + 1).trim() : "";
  }
  if (line.length > max) {
    const cut = line.lastIndexOf(" ", max - 1);
    line = `${line.slice(0, cut > max / 2 ? cut : max - 1)}…`;
  }
  return line;
}

function capital(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** The dossier's title: the run's own when it is a name, else the seed's first sentence. */
export function dossierTitle(run: Pick<DiscoveryListItem, "title" | "seed">): string {
  const title = (run.title ?? "").trim();
  const seed = (run.seed ?? "").trim();
  if (title && title.length <= 70 && title !== seed && !/^discovery$/i.test(title)) return shortTitle(title);
  return shortTitle(seed || title);
}

// ── Money and time ─────────────────────────────────────────────────────────────────────────

export function usd(micros: number | null | undefined, digits = 2): string {
  if (micros == null || !Number.isFinite(micros)) return "";
  return `$${(micros / 1_000_000).toFixed(digits)}`;
}

export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, (now - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** How long something took, from start to finish ("32s", "4m 20s"). */
export function took(start?: string | null, end?: string | null): string {
  if (!start || !end) return "";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  const secs = Math.round(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  if (m < 60) return secs % 60 ? `${m}m ${secs % 60}s` : `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function domainOf(url: string | null | undefined): string {
  const raw = String(url ?? "").trim();
  if (!raw) return "";
  try {
    return new URL(raw).hostname.replace(/^www\./, "");
  } catch {
    return raw.replace(/^https?:\/\/(www\.)?/, "").split("/")[0];
  }
}

export function pct(x: number | null | undefined): number {
  return Math.max(0, Math.min(100, Math.round((Number(x) || 0) * 100)));
}

// ── Research ───────────────────────────────────────────────────────────────────────────────

export const RESEARCH_STAGES: Array<{ key: string; label: string; doing: string }> = [
  { key: "signal_sweep", label: "Signal sweep", doing: "Sweeping the web for signals" },
  { key: "synthesize", label: "Synthesize", doing: "Synthesizing what was found" },
  { key: "vet", label: "Critic", doing: "A critic is trying to refute it" },
  { key: "shape", label: "Shape outcomes", doing: "Shaping outcomes you can pilot" },
  { key: "gate", label: "Evidence gate", doing: "The evidence gate is deciding" },
];

export function researchStages(run: Pick<DiscoveryListItem, "status" | "phase">): Stage[] {
  const at = RESEARCH_STAGES.findIndex((s) => s.key === run.phase);
  const finished = run.status === "completed" || run.phase === "done";
  return RESEARCH_STAGES.map((s, i) => {
    let state: StageState = "todo";
    if (finished) state = "done";
    else if (run.status === "failed") state = i < at ? "done" : i === at ? "stopped" : "todo";
    else if (at >= 0) state = i < at ? "done" : i === at ? "now" : "todo";
    else if (run.phase === "queued" && i === 0) state = "now";
    return { key: s.key, label: s.label, state };
  });
}

export function researchDoing(phase: string | null | undefined): string {
  if (phase === "queued") return "Queued — waiting for a worker";
  return RESEARCH_STAGES.find((s) => s.key === phase)?.doing ?? "Researching";
}

export type VerdictFilter = "all" | "promising" | "needs_more" | "not_worth";

/** A dossier's verdict as the screens say it, and which filter it belongs to. */
export function dossierVerdict(run: { status: string; summary?: { verdict?: string | null } | null },
                               verdict?: string | null): { label: string; tone: Tone; filter: VerdictFilter | null } {
  if (run.status === "failed") return { label: "Failed", tone: "bad", filter: null };
  if (run.status === "running" || run.status === "queued") return { label: "Researching", tone: "live", filter: null };
  const v = verdict ?? run.summary?.verdict ?? null;
  if (v === "pursue") return { label: "Pursue", tone: "good", filter: "promising" };
  if (v === "promising") return { label: "Promising", tone: "good", filter: "promising" };
  if (v === "needs_more_research") return { label: "Needs more research", tone: "warn", filter: "needs_more" };
  if (v === "not_promising" || v === "kill" || v === "not_worth_it") return { label: "Not worth it", tone: "bad", filter: "not_worth" };
  return { label: "Done", tone: "neutral", filter: null };
}

export const GATE_CHECK_LABEL: Record<string, string> = {
  has_outcome: "Has an outcome",
  confidence_strong: "Strong confidence",
  outcome_has_pilot: "Pilot named",
  enough_cited_sources: "Enough sources",
  disconfirming_considered: "Counter-evidence weighed",
};

export function checkLabel(key: string): string {
  return GATE_CHECK_LABEL[key] ?? capital(key.replace(/_/g, " "));
}

/** Can a panel be convened from this dossier: the gate said it is worth building. */
export function buildable(run: Pick<DiscoveryRun, "state">): boolean {
  const v = run.state?.gate?.verdict;
  return v === "pursue" || v === "promising";
}

/** The brief "Build it" hands the New project composer, from one outcome of a dossier. */
export function outcomeBrief(title: string, o: { outcome?: string; target_pilot?: string;
  first_prototype_slice?: string; validation_plan?: string }): string {
  return [
    (o.outcome ?? title).trim(),
    o.target_pilot ? `Who it is for: ${o.target_pilot.trim()}` : "",
    o.first_prototype_slice ? `What it does first: ${o.first_prototype_slice.trim()}` : "",
    o.validation_plan ? `How we will know: ${o.validation_plan.trim()}` : "",
  ].filter(Boolean).join("\n");
}

// ── Panels ─────────────────────────────────────────────────────────────────────────────────

export type MeetingKind = "prd_panel" | "go_no_go" | "standup";

export function kindOf(m: Pick<Meeting, "meeting_type">): MeetingKind {
  const t = String(m.meeting_type ?? "prd_panel");
  return t === "go_no_go" || t === "standup" ? t : "prd_panel";
}

/** Each kind's stages, and which engine phases each stage covers. */
const STAGES: Record<MeetingKind, Array<{ key: string; label: string; phases: string[]; doing: string }>> = {
  prd_panel: [
    { key: "views", label: "Opening views", phases: ["research", "round1"], doing: "Each seat is framing it" },
    { key: "debate", label: "Debate", phases: ["round2"], doing: "The seats are debating" },
    { key: "synthesis", label: "Synthesis", phases: ["synthesis"], doing: "The facilitator is reconciling" },
    { key: "prd", label: "PRD", phases: ["draft", "finalize"], doing: "Drafting the PRD" },
    { key: "build", label: "Build", phases: [], doing: "" },
  ],
  go_no_go: [
    { key: "views", label: "Evidence", phases: ["research", "round1"], doing: "Each seat is weighing the evidence" },
    { key: "debate", label: "Debate", phases: ["round2"], doing: "The seats are arguing it out" },
    { key: "verdict", label: "Verdict", phases: ["synthesis", "draft", "finalize"], doing: "The facilitator is calling it" },
  ],
  standup: [
    { key: "views", label: "Check-ins", phases: ["research", "round1"], doing: "Each seat is checking in" },
    { key: "debate", label: "Blockers", phases: ["round2"], doing: "Calling out blockers" },
    { key: "recap", label: "Recap", phases: ["synthesis", "draft", "finalize"], doing: "The facilitator is recapping" },
  ],
};
export const PANEL_STAGES = STAGES.prd_panel;

/** What the rounds are called for this kind of meeting. */
export function roundNames(m: Pick<Meeting, "meeting_type">): [string, string, string, string] {
  const k = kindOf(m);
  if (k === "standup") return ["Check-ins", "Where each seat stands", "Blockers", "What is in the way"];
  if (k === "go_no_go") return ["Evidence", "Each seat weighs the case", "Debate", "For, against and the risk"];
  return ["Opening views", "Each seat frames it through its own lens", "Debate",
          "They answer each other — agreements, gaps, risks"];
}

export function meetingLive(m: Pick<Meeting, "status">): boolean {
  return m.status === "running" || m.status === "queued";
}

/** The Build stage from the build the PRD fed (or the workflow's build step, when one is given). */
function buildStage(m: Pick<Meeting, "build" | "pull_request_url" | "prd_state">,
                    override?: StageState | "skipped"): StageState | "skipped" {
  if (override) return override;
  const b = m.build?.status;
  if (b === "running" || b === "queued") return "now";
  if (b === "completed" || b === "succeeded") return "done";
  if (b === "failed" || b === "blocked") return "stopped";
  // Not started: the same "not yet" the workflow's step view shows, never "now".
  return "todo";
}

export function panelStages(m: Pick<Meeting, "status" | "phase" | "pull_request_url" | "prd_markdown"
  | "meeting_type" | "build" | "prd_state">, build?: StageState | "skipped"): Stage[] {
  const stages = STAGES[kindOf(m)];
  const live = meetingLive(m);
  const at = stages.findIndex((s) => s.phases.includes(m.phase ?? ""));
  const done = m.status === "completed" || m.status === "simulated" || m.phase === "done";
  return stages.map((s, i) => {
    let state: StageState | "skipped" = "todo";
    if (s.key === "build" && done) state = buildStage(m, build);
    // The PRD is done only when there is one; a panel that ended without writing it skipped it.
    else if (s.key === "prd" && done) state = m.prd_markdown ? "done" : "skipped";
    else if (done) state = "done";
    else if (m.status === "failed") state = at < 0 ? (i === 0 ? "stopped" : "todo") : i < at ? "done" : i === at ? "stopped" : "todo";
    else if (live) state = at < 0 ? (i === 0 ? "now" : "todo") : i < at ? "done" : i === at ? "now" : "todo";
    return { key: s.key, label: s.label, state: state as StageState };
  });
}

export function panelDoing(m: Pick<Meeting, "phase" | "url" | "meeting_type">): string {
  if (m.phase === "research") return m.url ? `Studying ${domainOf(m.url)}` : "Gathering the brief";
  return STAGES[kindOf(m)].find((s) => s.phases.includes(m.phase ?? ""))?.doing ?? "In session";
}

export type Outcome = {
  pill: string; tone: Tone; headline: string;
  /** Where the PRD stands past the panel: what the outcome card offers next. */
  stage: "live" | "failed" | "verdict" | "recorded" | "simulated" | "building" | "built" | "published"
    | "approved" | "awaiting" | "declined" | "minutes";
};

/** Where a panel ended up, from the PRD's real decision and build — the card pill, the hero's
 *  outcome card and the step panel all read it. A fixture says so before anything else. */
export function meetingOutcome(m: Pick<Meeting, "status" | "pull_request_url" | "pull_request_number"
  | "prd_label" | "prd_state" | "prd_markdown" | "meeting_type" | "verdict" | "build"
  | "action_items">): Outcome {
  const k = kindOf(m);
  if (meetingLive(m)) return { pill: "In session", tone: "live", headline: "The panel is in session", stage: "live" };
  if (m.status === "failed") return { pill: "Stopped", tone: "bad", headline: "The panel stopped", stage: "failed" };
  if (k === "go_no_go") {
    const v = m.verdict;
    return { pill: v === "go" ? "GO" : v === "no_go" ? "NO-GO" : "No verdict", tone: v === "go" ? "good" : v === "no_go" ? "bad" : "neutral",
             headline: v === "go" ? "Go" : v === "no_go" ? "No-go" : "No verdict recorded", stage: "verdict" };
  }
  if (k === "standup") {
    const n = m.action_items?.length ?? 0;
    return { pill: "Recorded", tone: "neutral", headline: n ? `${n} action${n === 1 ? "" : "s"} recorded` : "Stand-up recorded",
             stage: "recorded" };
  }
  if (m.status === "simulated") return { pill: "Simulated", tone: "neutral", headline: "No-spend fixture", stage: "simulated" };
  const b = m.build?.status;
  if (b === "running" || b === "queued") return { pill: "Building", tone: "live", headline: "Being built from the PRD", stage: "building" };
  if (b === "completed") return { pill: "Built", tone: "good", headline: "Built from the PRD", stage: "built" };
  if (m.pull_request_url) {
    return { pill: "Agreed · PRD ready", tone: "good",
      headline: `PRD in pull request #${m.pull_request_number ?? m.pull_request_url.split("/").pop()}`, stage: "published" };
  }
  const st = m.prd_state ?? "";
  if (st === "approved" || st === "published") return { pill: "Agreed · PRD approved", tone: "good", headline: "PRD approved", stage: "approved" };
  if (st === "awaiting_approval") return { pill: "Agreed · PRD drafted", tone: "warn", headline: "PRD awaiting your approval", stage: "awaiting" };
  if (st === "rejected" || st === "revision_requested" || st === "expired") {
    return { pill: capital(m.prd_label ?? st.replace(/_/g, " ")), tone: "warn", headline: capital(m.prd_label ?? "PRD not approved"), stage: "declined" };
  }
  if (m.prd_markdown) return { pill: "Agreed · PRD drafted", tone: "good", headline: "PRD drafted", stage: "awaiting" };
  return { pill: "Minutes", tone: "neutral", headline: "Minutes recorded", stage: "minutes" };
}

/** Approvals, open on the decision this PRD waits on. */
export function decisionHref(search: string, decisionId?: string | null): string {
  return consoleHref(search, { area: "decisions", ...(decisionId ? { decision: decisionId } : {}) });
}

export type PrdParts = { summary: string[]; goals: string[]; nonGoals: string[]; requirements: string[] };
type Role = keyof PrdParts | "other" | "stop";

/** A heading's ROLE, by its words — "Goals & Non-Goals" opens goals, "Non-Goals (out of scope)"
 *  is never a goal, "Non-Functional Requirements" is never a requirement to build. */
function roleOf(heading: string): Role {
  const h = heading.toLowerCase().replace(/[‐‑‒–—]/g, "-").replace(/^[\d.\s]+/, "").replace(/[*_`#:]/g, "").trim();
  if (/^non-?\s*goals?\b|out of scope|^deferred/.test(h)) return "nonGoals";
  if (/\bgoals?\b/.test(h)) return "goals";
  // A section of its own, even as a sub-heading: never part of the goals or what to build.
  if (/non-?\s*functional|risk|user|persona|solution|architecture|acceptance|rollout|question|metric|timeline|wirefram|sketch/.test(h)) return "stop";
  if (/^(?:functional\s+)?requirements?\b|^functional\b|must-?ship|^scope\b/.test(h)) return "requirements";
  if (/^(summary|overview|executive summary|tl;?dr)\b/.test(h)) return "summary";
  return "other";
}

const plain = (t: string) => t.replace(/[*_`>]/g, "").replace(/\s+/g, " ").trim();
const isId = (t: string) => /^[A-Z]{1,4}[-‐‑]?\d+(?:\.\d+)?$/.test(plain(t));

/**
 * A PRD's parts by the ROLE of each section, reading bullets, numbered lines and TABLES (row by
 * row, the column that names the thing). shared-server's PRDs put Goals in a table and Non-Goals in
 * bullets under a bold line, so a keyword match on "goal" handed the composer the Non-Goals.
 */
export function prdParts(md: string | null | undefined): PrdParts {
  const out: PrdParts = { summary: [], goals: [], nonGoals: [], requirements: [] };
  let role: Role = "other";
  let level = 0;
  let table: { col: number } | null = null;
  for (const raw of String(md ?? "").split("\n")) {
    const line = raw.trim();
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    // A bold line standing alone is a heading too: "**Non‑Goals** (out of scope for MVP)".
    const bold = /^(?:\*\*|__)([^*_]+?)(?:\*\*|__)\s*(?:\([^)]*\))?\s*:?\s*$/.exec(line);
    if (h || (bold && !["other", "stop"].includes(roleOf(bold[1])))) {
      const named = roleOf(h ? h[2] : bold![1]);
      const lv = h ? h[1].length : level + 1;
      // A sub-heading inside a role keeps it unless it names a section of its own.
      if (named === "stop") { role = "other"; level = lv; }
      else if (named !== "other" || !h || lv <= level || role === "other") { role = named; level = lv; }
      table = null;
      continue;
    }
    if (!line || /^-{3,}$/.test(line)) { table = null; continue; }
    if (role === "other") continue;
    if (line.startsWith("|")) {
      const cells = line.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;
      if (!table) {
        // The header row: which column names the thing.
        const named = cells.findIndex((c) => /goal|requirement|feature|capability|item|what/i.test(c));
        table = { col: named >= 0 ? named : 0 };
        continue;
      }
      let cell = cells[table.col] ?? "";
      if (isId(cell)) cell = cells.find((c, i) => i !== table!.col && c && !isId(c)) ?? cell;
      const text = plain(cell);
      if (text) out[role].push(text);
      continue;
    }
    table = null;
    const item = /^(?:[-*+•]|\d+[.)])\s+(.*)$/.exec(line);
    const text = plain(item ? item[1] : line);
    if (!text) continue;
    if (role === "summary" || item) out[role].push(text);
  }
  return out;
}

/**
 * The brief "Build it" hands the composer from a panel: the AGREED PRD — its title, summary,
 * goals and what it must do, and a link to read all of it — never the raw directive, and never
 * a Non-Goal as a goal.
 */
export function prdDigest(md: string | null | undefined, title: string, prUrl?: string | null): string {
  const p = prdParts(md);
  const summary = p.summary.slice(0, 2).join(" ");
  const goals = p.goals.slice(0, 4);
  const reqs = p.requirements.slice(0, 6);
  return [
    title,
    summary,
    goals.length ? `Goals:\n${goals.map((g) => `- ${g}`).join("\n")}` : "",
    reqs.length ? `What it must do:\n${reqs.map((r) => `- ${r}`).join("\n")}` : "",
    prUrl ? `Read the full PRD: ${prUrl}` : "",
  ].filter(Boolean).join("\n\n");
}

/** A recorded vote, as a sentence: "Approved · 3 for, 1 against, 1 abstain · quorum met". */
export function voteSentence(d: Record<string, unknown> | null | undefined): string {
  if (!d) return "";
  const n = (k: string) => Number(d[k] ?? 0) || 0;
  const outcome = String(d.outcome ?? (d.consensus_reached ? "approved" : "recorded")).replace(/_/g, " ");
  const label = outcome === "no quorum" ? "No quorum" : capital(outcome);
  const quorum = d.quorum_met === false
    ? `quorum not met (${n("votes_cast")} of ${n("quorum")} needed)` : "quorum met";
  return `${label} · ${n("approve")} for, ${n("reject")} against, ${n("abstain")} abstain · ${quorum}`;
}

export function repoOf(prUrl: string | null | undefined): string {
  const m = /github\.com\/([^/]+\/[^/]+)\/pull\//.exec(String(prUrl ?? ""));
  return m ? m[1] : "";
}

/** The PRD's section list: its `##` headings, numbering stripped. */
export function prdSections(md: string | null | undefined, max = 8): string[] {
  const out: string[] = [];
  for (const line of String(md ?? "").split("\n")) {
    if (!/^##\s+/.test(line)) continue;
    const h = line.replace(/^##\s+/, "").replace(/[*_`#]/g, "").replace(/^\d+[.)]\s*/, "").trim();
    if (h) out.push(h);
    if (out.length >= max) break;
  }
  return out;
}

/** A seat's gist for a round, with the client-side fallback for an API that sends none. */
export function seatGist(m: Pick<Meeting, "round1" | "round2" | "round1_gists" | "round2_gists">,
                         round: 1 | 2, seat: string): MeetingTurnGist | null {
  const gists = round === 1 ? m.round1_gists : m.round2_gists;
  const text = (round === 1 ? m.round1 : m.round2)?.[seat];
  if (!text) return null;
  const g = gists?.[seat];
  if (g && g.headline) return g;
  return fallbackGist(text);
}

/** The engine's deterministic gist (meeting_gist.extract_gist), for an older API. */
export function fallbackGist(md: string, points = 3): MeetingTurnGist {
  const lines = md.replace(/```[\s\S]*?```/g, "\n").split("\n")
    .filter((l) => l.trim() && !(l.trim().startsWith("|") || (l.match(/\|/g) ?? []).length >= 2))
    .map((l) => l.replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/^\s{0,3}#{1,6}\s+/, "")
      .replace(/^\s*(?:[-*+•]|\d+[.)])\s+/, "").replace(/[*_`>#~]/g, "").replace(/\s+/g, " ")
      .trim().replace(/^[-–—:•\s]+|[-–—:•\s]+$/g, ""))
    .filter((l) => l.length > 3);
  const clip = (s: string, n: number) => (s.length <= n ? s : `${s.slice(0, s.lastIndexOf(" ", n - 1) > n / 2 ? s.lastIndexOf(" ", n - 1) : n - 1)}…`);
  return { headline: clip(lines[0] ?? "", 120), points: lines.slice(1).filter((l) => l.length >= 8).slice(0, points).map((l) => clip(l, 140)) };
}

export const SEAT_ORDER = [
  "Product Manager", "Engineer / Architect", "UX Designer", "Privacy & Safety",
  "Finance / FinOps", "Growth / Marketing", "QA / Test", "Customer Advocate", "Security", "Data / Analytics",
];

export function orderedSeats(map: Record<string, unknown> | null | undefined): string[] {
  const keys = Object.keys(map ?? {});
  return [...SEAT_ORDER.filter((r) => keys.includes(r)), ...keys.filter((r) => !SEAT_ORDER.includes(r))];
}

/** The seats of a panel, facilitator excluded — it is drawn as the BOSS mark. */
export function seatsOf(m: Pick<Meeting, "participants">): string[] {
  return (m.participants ?? []).filter((p) => !/facilitator/i.test(p));
}

/** Which meeting a Panels URL names: `meeting=`, a run id in `run=`, the task it is linked to
 *  (`task=`, what a dossier's "Convene a panel" sends), or a `#id` hash. */
export function meetingFromUrl(search: string, hash: string, meetings: Array<Pick<Meeting, "id" | "task_id">>): string | null {
  const q = new URLSearchParams(search);
  const named = q.get("meeting") || (/^(mrun|mtg)_/.test(q.get("run") ?? "") ? q.get("run") : null);
  if (named) return named;
  const task = q.get("task");
  if (task) {
    const hit = meetings.find((m) => m.task_id === task);
    if (hit) return hit.id;
  }
  const h = decodeURIComponent((hash || "").replace(/^#/, ""));
  if (/^(mrun|mtg)_/.test(h)) return h;
  return null;
}

// ── Where "Build it" goes ──────────────────────────────────────────────────────────────────

/**
 * The New project composer, prefilled. `compose=new` is the composer's deep link (and `new=1`
 * the one this build of Workflows reads); `brief` and `title` carry what to build. One place
 * builds this link so a change to the composer's contract is a one-line change.
 */
export function composerHref(search: string, o: { title?: string; brief: string }): string {
  return consoleHref(search, {
    area: "delivery_workflows", tab: "runs", compose: "new", new: "1",
    title: o.title?.trim() || undefined, brief: o.brief.trim().slice(0, 4000),
  });
}

/** Read — and so consume — a composer prefill from the URL. */
export function readComposerPrefill(search: string): { title?: string; directive?: string } | null {
  const q = new URLSearchParams(search);
  const brief = q.get("brief");
  if (!brief) return null;
  return { title: q.get("title") ?? undefined, directive: brief };
}

/** The dossier a URL names: `discovery=`, or `run=` when it holds a research run (drun_). A
 *  meeting run (mrun_) or a code task (crun_) in `run=` belongs to another area. */
export function researchRunParam(discovery?: string | null, run?: string | null): string | undefined {
  if (discovery) return discovery;
  return run && !/^(mrun|mtg|crun|wfr)_/.test(run) ? run : undefined;
}

/** One dissent record in words. A named seat leads; the panel's count and the quorum note speak
 *  for themselves; a machine id ("r0", "a1") is never shown as a voter. */
export function dissentLine(r: { role?: string | null; reason?: string | null }): string {
  const role = String(r.role ?? "").trim();
  const reason = String(r.reason ?? "").trim();
  if (!role || ["panel", "quorum", "participant"].includes(role) || /^[a-z]\d+$/i.test(role)) {
    return reason ? reason[0].toUpperCase() + reason.slice(1) : "";
  }
  return `${role}: ${reason}`;
}
