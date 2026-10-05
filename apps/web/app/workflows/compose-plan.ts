/**
 * The New project composer's plan, as pure functions: which paths it offers in each mode, the
 * timeline a path draws, who does each step, what a start sends, and the words around them.
 *
 * Kept apart from the component so every rule the screen shows can be tested without a browser
 * (tests/new-project-compose.test.ts), and so the two modes — a new app, and a feature added to
 * an app that already exists — read their differences from one table rather than from two
 * screens that drift.
 */

/** The composer's two jobs. Adding to an app that exists is the everyday one. */
export type ComposeMode = "new" | "add";

/** A path card. `key` is a workflow key, or `@quick` / `@spec` for the two change routes that
 *  are not workflows (the feedback proposal and the spec-first panel). */
export type PathCard = { key: string; short: string; tip: string; recommended?: boolean };

/** A new app: the five catalogue paths, smallest to largest. Standard is the one to pick. */
export const NEW_PATHS: PathCard[] = [
  { key: "fast_fix", short: "Fast fix", tip: "A small, known fix. No panel." },
  { key: "minor_feature", short: "Small", tip: "A small addition. Skips research." },
  { key: "standard_product_delivery", short: "Standard", tip: "Research, a panel, a spec you approve, then the build.",
    recommended: true },
  { key: "major_feature", short: "Major", tip: "A big change. More review, more gates." },
  { key: "research_led_discovery", short: "Discovery", tip: "Find out first. Ends in a decision, not code." },
];

export const QUICK = "@quick";
export const SPEC = "@spec";

/** Adding to an app: every one of these builds ONTO the app's branch, never a fresh scaffold. */
export const ADD_PATHS: PathCard[] = [
  { key: QUICK, short: "Quick change", tip: "An agent scopes it; you confirm the proposal before anything runs." },
  { key: "minor_feature", short: "Small", tip: "A panel scopes it, then it is built on the app's branch.",
    recommended: true },
  { key: SPEC, short: "Spec it first", tip: "A panel writes a spec for this feature, then it is built onto the app." },
  { key: "major_feature", short: "Major", tip: "A big change, fully reviewed, on the app's branch." },
];

/** The default path for a mode: the recommended one when this project has it installed. */
export function defaultPath(mode: ComposeMode, installed: readonly string[]): string {
  const rec = (mode === "new" ? NEW_PATHS : ADD_PATHS).find((p) => p.recommended)!;
  if (rec.key.startsWith("@") || installed.includes(rec.key)) return rec.key;
  return mode === "add" ? QUICK : "";
}

/** The cards a mode draws: its table, less any workflow this project does not have. */
export function pathCards(mode: ComposeMode, installed: readonly string[]): PathCard[] {
  return (mode === "new" ? NEW_PATHS : ADD_PATHS)
    .filter((p) => p.key.startsWith("@") || installed.includes(p.key));
}

/** A step as the installed list carries it (delivery-workflows-ui `Installed.steps`). */
export type InstalledStep = { id: string; label?: string; kind?: string; approval?: string;
                              captain?: string; agent_work?: boolean };

/** Who does a step: an agent (its captain), BOSS's own machinery, or you at a gate. */
export type Who = { kind: "agent"; role: string } | { kind: "boss" } | { kind: "you" };

export type TimelineStep = { id: string; name: string; line: string; who: Who;
                             /** The regression check every change to an app passes. */
                             keep?: boolean };

/** Plain names and one-line descriptions, by step id. The mockup's words. */
const WORDS: Record<string, [string, string]> = {
  research: ["Research", "Looks at what exists and what users need"],
  meeting: ["Panel", "Agents debate it and write the spec"],
  spec_review: ["Approve the spec", "You read the spec before any code"],
  "spec_review@auto": ["Spec sign-off", "The panel's decision approves it"],
  build: ["Build", "Branch, code and tests"],
  preview: ["Preview", "A running copy you can click"],
  plrr: ["Launch check", "Production-readiness review"],
  report: ["Report", "What was built, what it cost"],
  deploy: ["Deploy", "Ships only when you say yes"],
  monitor: ["Watch", "Keeps an eye on it after launch"],
  notify: ["Notify", "Tells the channel it is live"],
  ux_review: ["UX sign-off", "You use the preview and sign off"],
  document: ["Write it up", "A decision document, not code"],
  review: ["Review", "Agents review the change"],
  test: ["Test", "The suite runs"],
};

/** The same steps, said about a change to an app that already exists. */
function addWords(app: string): Record<string, [string, string]> {
  return {
    meeting: ["Scope the change", "The panel reads the app and agrees what changes"],
    build: ["Build on its branch", `Adds to ${app || "the app"} — no re-scaffold`],
    preview: ["Preview", "The app with the new feature, running"],
    report: ["Report", "What changed, what it cost"],
  };
}

export const KEEP_STEP: TimelineStep = {
  id: "keep", name: "Keep what works", line: "Its existing tests must still pass before the PR",
  who: { kind: "agent", role: "qa" }, keep: true,
};

/** Whether a step stops for a person: the catalogue's own gate list when it has one (the PRD
 *  approval is answered by the panel and is not one), else the step's effective approval. */
export function stopsForYou(step: InstalledStep, gates?: readonly string[] | null,
                            automatic?: readonly string[] | null): boolean {
  if (gates) return gates.includes(step.id);
  if (automatic?.includes(step.id)) return false;
  return String(step.approval ?? "") === "manual";
}

/** Who answers for a step that is not a gate: its captain, else BOSS. */
function whoDoes(step: InstalledStep): Who {
  return step.captain ? { kind: "agent", role: step.captain } : { kind: "boss" };
}

/** A workflow's steps as the timeline draws them. In add mode the change is said as a change,
 *  and "Keep what works" follows the build: the app's existing tests gate the PR. */
export function timelineOf(o: { steps: readonly InstalledStep[]; gates?: readonly string[] | null;
                                automatic?: readonly string[] | null; mode: ComposeMode; app?: string }
): TimelineStep[] {
  const words = o.mode === "add" ? { ...WORDS, ...addWords(o.app ?? "") } : WORDS;
  const out: TimelineStep[] = [];
  for (const s of o.steps) {
    const gate = stopsForYou(s, o.gates, o.automatic);
    // A PRD approval the panel answers is not yours: said as what it is, not "Approve the spec".
    const [name, line] = (!gate && words[`${s.id}@auto`]) || words[s.id] || [s.label || s.id, ""];
    out.push({ id: s.id, name: s.label && !words[s.id] ? s.label : name, line,
               who: gate ? { kind: "you" } : whoDoes(s) });
    if (o.mode === "add" && s.id === "build") out.push(KEEP_STEP);
  }
  return out;
}

/** The two change routes that are not workflows, drawn the same way. */
export function routeTimeline(key: string, app: string): TimelineStep[] {
  const build: TimelineStep = { id: "build", name: "Build on its branch",
                                line: `Adds to ${app || "the app"} — no re-scaffold`,
                                who: { kind: "agent", role: "engineering_captain" } };
  if (key === QUICK) {
    return [
      { id: "scope", name: "Scope it", line: "An agent proposes the change", who: { kind: "agent", role: "pm" } },
      { id: "confirm", name: "Confirm it", line: "Nothing runs until you say so", who: { kind: "you" } },
      build, KEEP_STEP,
      { id: "preview", name: "Preview", line: "The app with the change, running", who: { kind: "agent", role: "qa" } },
    ];
  }
  return [
    { id: "meeting", name: "Spec the feature", line: "A panel writes the spec for this feature",
      who: { kind: "agent", role: "pm" } },
    { id: "spec_review", name: "Approve the spec", line: "You read it before any code", who: { kind: "you" } },
    build, KEEP_STEP,
    { id: "preview", name: "Preview", line: "The app with the feature, running", who: { kind: "agent", role: "qa" } },
  ];
}

/** "N steps · you approve K times", counted from what the timeline draws (Keep what works is
 *  part of the build, not a step of its own). */
export function counts(steps: readonly TimelineStep[]): { steps: number; approvals: number } {
  return { steps: steps.filter((s) => !s.keep).length,
           approvals: steps.filter((s) => s.who.kind === "you").length };
}

export function approvalsWord(n: number): string {
  return n === 0 ? "0 approvals" : n === 1 ? "1 approval" : `${n} approvals`;
}

export function youApproveWord(n: number): string {
  return n === 0 ? "no approvals" : n === 1 ? "you approve once" : `you approve ${n} times`;
}

/** What one workflow has cost before, from GET /v1/delivery-workflows/costs. */
export type CostHistory = { runs: number; low_usd: number; high_usd: number; metered?: boolean };

function dollars(n: number): string {
  if (n < 1) return n > 0 ? `$${n.toFixed(2)}` : "$0";
  return `$${Math.round(n)}`;
}

/** "$4–$7", or "$3" when the range is one number; null without history (the line is hidden). */
export function costRange(c: CostHistory | null | undefined): string | null {
  if (!c || !c.runs || !(c.high_usd > 0)) return null;
  const lo = dollars(c.low_usd);
  const hi = dollars(c.high_usd);
  return lo === hi ? hi : `${lo}–${hi}`;
}

/** The history a path's cost line reads: a workflow's own, or quick changes' (`@change`). */
export function costKey(path: string): string {
  return path === QUICK ? "@change" : path === SPEC ? "minor_feature" : path;
}

// ── Apps ──────────────────────────────────────────────────────────────────────────────────

const letters = (s: string) => s.toLowerCase().replace(/[^a-z]/g, "");

/** The template idea an app was built from, matched by name (Lead-Triage Inbox ↔ Lead triage
 *  inbox); null when none matches and the card draws a monogram instead. */
export function matchIdea<T extends { title: string }>(app: string, ideas: readonly T[]): T | null {
  const n = letters(app);
  if (n.length < 4) return null;
  for (const idea of ideas) {
    const k = letters(idea.title);
    if (k.slice(0, 10) === n.slice(0, 10) || (k.length > 5 && n.includes(k)) || (n.length > 5 && k.includes(n))) {
      return idea;
    }
  }
  return null;
}

/** An app's monogram: two initials and a hue fixed by its name. */
export function monogram(app: string): { initials: string; hue: number } {
  const words = app.match(/[A-Za-z0-9]+/g) ?? [];
  const initials = words.slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
  const hue = ([...app].reduce((n, ch) => n + ch.charCodeAt(0), 0) * 37) % 360;
  return { initials, hue };
}

/** The app card's second line: its PR and how many builds. */
export function appLine(a: { prNumber?: number; prState?: string; word: string; builds: { length: number } }): string {
  const n = a.builds.length;
  const pr = a.prNumber ? `PR #${a.prNumber} ${a.prState ?? "open"}` : a.word;
  return `${pr} · ${n} build${n === 1 ? "" : "s"}`;
}

/** Does this app match the search box? Name, brief or repository. */
export function appMatches(a: { app: string; brief?: string; repo?: string }, q: string): boolean {
  const t = q.trim().toLowerCase();
  return !t || a.app.toLowerCase().includes(t) || (a.brief ?? "").toLowerCase().includes(t)
    || (a.repo ?? "").toLowerCase().includes(t);
}

/** The app a deep link names: by its key, or by any of its builds. */
export function findApp<T extends { key: string; builds: { runId: string }[] }>(apps: readonly T[], id: string | null | undefined): T | null {
  if (!id) return null;
  return apps.find((a) => a.key === id) ?? apps.find((a) => a.builds.some((b) => b.runId === id)) ?? null;
}

// ── What a start sends in add mode ──────────────────────────────────────────────────────────

/** The check every change starts with. */
export const KEEPS_WORKING = "Everything that worked before still works";

/** The chips' kinds, toggled: each one's expected outcome is a Done-when check. */
export function toggleKind(picked: readonly string[], key: string): string[] {
  return picked.includes(key) ? picked.filter((k) => k !== key) : [...picked, key];
}

/** The checks with a chip's outcome added or taken away, keeping what the operator typed. */
export function checksWithKind(checks: readonly string[], expected: string, on: boolean): string[] {
  const outcome = expected.charAt(0).toUpperCase() + expected.slice(1);
  const rest = checks.filter((c) => c !== outcome);
  return on ? [...rest.filter((c) => c !== KEEPS_WORKING), outcome,
               ...(rest.includes(KEEPS_WORKING) ? [KEEPS_WORKING] : [])] : rest;
}

/** A title for the change: the app's name and the first words of what it should gain. */
export function changeTitle(app: string, gain: string): string {
  const first = gain.trim().split(/[.\n]/)[0].trim();
  const short = first.length > 60 ? `${first.slice(0, 57)}…` : first;
  return short ? `${app}: ${short}` : app;
}

/** What a workflow run started on an app is given. `extends_run_id` is the contract with the
 *  engine: the build step builds onto that run's branch (create_code_task → start_followup_task). */
export function featureInput(o: { app: string; runId: string; gain: string; kinds: readonly string[];
                                  checks: readonly string[]; links: readonly string[] }) {
  const directive = [o.gain.trim(), o.kinds.length ? `Kind of change: ${o.kinds.join(", ")}` : ""]
    .filter(Boolean).join("\n");
  const criteria = o.checks.map((c) => c.trim()).filter(Boolean);
  const links = o.links.map((l) => l.trim()).filter((l) => /^https?:\/\//i.test(l));
  return {
    title: changeTitle(o.app, o.gain), directive, extends_run_id: o.runId,
    ...(criteria.length ? { acceptance_criteria: criteria } : {}),
    ...(links.length ? { reference_links: links } : {}),
  };
}

/** The quick change's message and its Done when, as the feedback proposal reads them. */
export function quickChange(o: { gain: string; kinds: readonly string[]; checks: readonly string[];
                                 links: readonly string[] }): { message: string; expected: string } {
  const links = o.links.map((l) => l.trim()).filter(Boolean);
  const message = [o.gain.trim(), o.kinds.length ? `Kind of change: ${o.kinds.join(", ")}` : "",
                   links.length ? `Read first: ${links.join(" ")}` : ""].filter(Boolean).join("\n");
  return { message, expected: o.checks.map((c) => c.trim()).filter(Boolean).join("; ") };
}

// ── Remembering the mode ────────────────────────────────────────────────────────────────────

const MODE_KEY = "boss.compose.mode";

/** The mode last used, per browser. A convenience: anything unreadable is "new". */
export function rememberedMode(): ComposeMode {
  try {
    return window.localStorage.getItem(MODE_KEY) === "add" ? "add" : "new";
  } catch {
    return "new";
  }
}

export function rememberMode(mode: ComposeMode): void {
  try { window.localStorage.setItem(MODE_KEY, mode); } catch { /* private mode */ }
}

/** A readiness item as the plan's dot row says it: two words, the full label in its tooltip. */
const READY_WORDS: Record<string, [string, string]> = {
  model: ["Models ready", "No model"],
  repository: ["Repo connected", "No repo"],
  deployment_target: ["Deploy target", "No deploy target"],
  budget: ["Budget OK", "Over budget"],
  email: ["Email ready", "No email"],
  notify_channel: ["Channel ready", "No channel"],
};

export function readyWord(key: string, label: string, missing: boolean): string {
  const w = READY_WORDS[key];
  if (w) return missing ? w[1] : w[0];
  if (key.startsWith("role:")) return missing ? "No approver" : "Approver set";
  if (key.startsWith("channel:")) return missing ? "No channel" : "Channel ready";
  return label;
}
