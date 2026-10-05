/**
 * The words and shapes the Research (dossiers) and Panels (meetings) screens show, as pure
 * functions of the API's records — so the full pages, the cards and the compact views inside a
 * workflow run's step panel cannot disagree about what a dossier is called, what its verdict
 * is, or which stage a panel has reached.
 */
import type { DiscoveryListItem, DiscoveryRun, Meeting, MeetingTurnGist } from "@boss/api-client";

import { consoleHref } from "../app/url-intent";

export type Tone = "good" | "warn" | "bad" | "neutral" | "live";
export type StageState = "done" | "now" | "todo" | "stopped";
export type Stage = { key: string; label: string; state: StageState };

// ── Titles ──────────────────────────────────────────────────────────────────────────────────

/** What a card or a hero can carry: the first sentence of a brief, without "Build a" and
 *  without the "Who it is for" block a workflow brief continues with. */
export function shortTitle(raw: string | null | undefined, max = 80): string {
  let t = String(raw ?? "").replace(/^PRD meeting:\s*/i, "").replace(/\s+/g, " ").trim();
  t = t.replace(/^(?:build|make|create)\s+(?:a|an|the)\s+/i, "");
  t = t.split(/\s(?:Who it is for|What it does|Who it's for)\b/i)[0];
  const stop = t.search(/[.?!](?:\s|$)/);
  if (stop > 0) t = t.slice(0, stop);
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
export function briefLine(raw: string | null | undefined, max = 220): string {
  const text = String(raw ?? "").replace(/\r\n/g, "\n");
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

export const PANEL_STAGES: Array<{ key: string; label: string; phases: string[]; doing: string }> = [
  { key: "views", label: "Opening views", phases: ["research", "round1"], doing: "Each seat is framing it" },
  { key: "debate", label: "Debate", phases: ["round2"], doing: "The seats are debating" },
  { key: "synthesis", label: "Synthesis", phases: ["synthesis"], doing: "The facilitator is reconciling" },
  { key: "prd", label: "PRD", phases: ["draft", "finalize"], doing: "Drafting the PRD" },
  { key: "build", label: "Build", phases: [], doing: "" },
];

export function meetingLive(m: Pick<Meeting, "status">): boolean {
  return m.status === "running" || m.status === "queued";
}

export function panelStages(m: Pick<Meeting, "status" | "phase" | "pull_request_url" | "prd_markdown">): Stage[] {
  const live = meetingLive(m);
  const at = PANEL_STAGES.findIndex((s) => s.phases.includes(m.phase ?? ""));
  const done = m.status === "completed" || m.status === "simulated" || m.phase === "done";
  return PANEL_STAGES.map((s, i) => {
    let state: StageState = "todo";
    if (done) state = s.key === "build" ? "now" : "done";
    else if (m.status === "failed") state = at < 0 ? (i === 0 ? "stopped" : "todo") : i < at ? "done" : i === at ? "stopped" : "todo";
    else if (live) state = at < 0 ? (i === 0 ? "now" : "todo") : i < at ? "done" : i === at ? "now" : "todo";
    return { key: s.key, label: s.label, state };
  });
}

export function panelDoing(m: Pick<Meeting, "phase" | "url">): string {
  if (m.phase === "research") return m.url ? `Studying ${domainOf(m.url)}` : "Gathering the brief";
  return PANEL_STAGES.find((s) => s.phases.includes(m.phase ?? ""))?.doing ?? "In session";
}

/** Where a panel ended up, as its card pill and its hero's outcome card say it. */
export function meetingOutcome(m: Pick<Meeting, "status" | "pull_request_url" | "pull_request_number"
  | "prd_label" | "prd_state" | "prd_markdown">): { pill: string; tone: Tone; headline: string } {
  if (meetingLive(m)) return { pill: "In session", tone: "live", headline: "The panel is in session" };
  if (m.status === "failed") return { pill: "Failed", tone: "bad", headline: "The panel stopped" };
  if (m.status === "simulated") return { pill: "Simulated", tone: "neutral", headline: "No-spend fixture" };
  if (m.pull_request_url) {
    return { pill: "Agreed", tone: "good",
      headline: `PRD in pull request #${m.pull_request_number ?? m.pull_request_url.split("/").pop()}` };
  }
  if (m.prd_markdown) return { pill: "Agreed", tone: "good", headline: m.prd_label ? capital(m.prd_label) : "PRD drafted" };
  return { pill: "Minutes", tone: "neutral", headline: m.prd_label ? capital(m.prd_label) : "Minutes recorded" };
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
