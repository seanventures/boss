/**
 * WORK PANEL V2, AS DATA (approved wireframe 2026-11-22, scratchpad workpanel-v2/wireframe.html).
 *
 * The rc10.32 panel drew six fixed stations for all code work. v2 draws FOUR STAGES that group
 * the workflow's own steps by their kind (engine work_stages.step_list puts a stage on each):
 *
 *   Plan   spec / PRD, panel meeting, discovery, the input file
 *   Build  code, fixes, tests, checkers, review, and Merge as its last step
 *   Ship   preview, staging, deploy, contract / smoke tests
 *   Live   production, health
 *
 * A stage the workflow lacks is grey "Not in this workflow". Each stage has one status, one plain
 * reason and at most one link, to the screen that stage's work lives on; a stage that has not
 * started has no link. One "what next" line carries only the buttons that apply, amber only when
 * it is the person's move. Pure functions: the component draws, these decide.
 */
import { isHydrated } from "./local-time";
import type { StageKey, StepState, WorkItem, WorkStep } from "./project-experience";
import { changeHref, changeRef, isLocalChangeUrl } from "./repo-links";
import { mergeWaitOf, workNextStep, type NextStep } from "./work-next-step";
import { rootBuildOf, workChangeOf, workOpenHref } from "./work-links";
import { durationWords, secondsBetween, stateSentence } from "./work-panel";

export const STAGE_KEYS: StageKey[] = ["plan", "build", "ship", "live"];
export const STAGE_NAME: Record<StageKey, string> = { plan: "Plan", build: "Build", ship: "Ship", live: "Live" };

/** done ✓ · live (BOSS on it) · you (a person's move) · fail · todo (not started) · absent. */
export type StageState = "done" | "live" | "you" | "fail" | "todo" | "absent";
export type StageLink = { label: string; href: string; screen: "meeting" | "build" | "workflow" | "deploy" | "observability" };
export type PanelStep = { name: string; state: StepState; you?: boolean };
export type Stage = { key: StageKey; name: string; state: StageState; steps: PanelStep[]; reason: string; link: StageLink | null };

const clip = (t: string, n = 90) => {
  const x = (t || "").replace(/\s+/g, " ").trim();
  return x.length <= n ? x : `${x.slice(0, n).replace(/\s+\S*$/, "")}…`;
};

/** The project a console link names (`?project_id=…`). */
export function projectOf(w: Pick<WorkItem, "links" | "href">): string {
  for (const h of [w.links.workflow_run, w.links.build, w.href]) {
    const p = new URLSearchParams((h || "").split("?")[1] ?? "").get("project_id");
    if (p) return p;
  }
  return "";
}

/** The screen each stage's work lives on (real console routes; ids only from the work's own
 *  records). Merge, approvals and steps with no screen of their own open the Workflow run. */
export const SCREENS = {
  workflow: (w: WorkItem): StageLink | null => {
    const href = w.links.workflow_run ? workOpenHref(w) : "";
    return href ? { label: "Workflow", href, screen: "workflow" } : null;
  },
  build: (w: WorkItem, steps: WorkStep[] = []): StageLink | null => {
    const own = [...steps].reverse().find((s) => s.refs.build)?.refs.build;
    const href = own || rootBuildOf(w)?.href || w.links.build || "";
    return href ? { label: "Build and test", href, screen: "build" } : null;
  },
  ship: (w: WorkItem, steps: WorkStep[] = []): StageLink | null => {
    const own = [...steps].reverse().find((s) => s.refs.deploy)?.refs.deploy;
    if (own || w.links.deploy) return { label: "Deployment", href: own || w.links.deploy, screen: "deploy" };
    const p = projectOf(w);
    return p ? { label: "Deployments", href: `?${new URLSearchParams({ project_id: p, area: "project", tab: "deployments" })}`, screen: "deploy" } : null;
  },
  live: (w: WorkItem): StageLink | null => {
    const p = projectOf(w);
    return p ? { label: "Observability", href: `?${new URLSearchParams({ project_id: p, area: "observability" })}`, screen: "observability" } : null;
  },
};

/** A build with no workflow (or an older engine): the station strip, folded into the stages. */
function legacySteps(w: WorkItem): WorkStep[] {
  const STAGE: Record<string, StageKey> = { plan: "plan", design: "plan", build: "build", review: "build", pr: "build", ship: "ship" };
  const NAME: Record<string, string> = { plan: "Spec", design: "Approve plan", build: "Build", review: "Review", pr: "Merge", ship: "Staging" };
  return w.stages.filter((st) => STAGE[st.key] && st.state !== "skipped").map((st) => ({
    id: st.key, name: NAME[st.key] ?? st.label, kind: st.key === "design" ? "decision" : "capability",
    capability: st.key === "pr" ? "merge" : st.key, stage: STAGE[st.key], state: st.state,
    started_at: st.started_at || "", completed_at: "", sub: "", refs: {},
  }));
}

/** The work's steps, each with its stage: the engine's, else the station strip. */
export function stepsOf(w: WorkItem): WorkStep[] {
  return w.steps && w.steps.length ? w.steps : legacySteps(w);
}

/** A PERSON'S MOVE: the work waits on them, or a running run holds at a gate only a person
 *  opens (a deploy approval) — never "BOSS working · nothing needed from you". */
export function waitsOnPerson(w: WorkItem): boolean {
  return w.state === "needs_you" || (w.state === "running" && stepsOf(w).some((s) => s.state === "waiting"));
}

const isMergeOrGate = (s: WorkStep) => s.capability === "merge" || s.kind === "decision" || s.state === "waiting";

/** What the engine's station strip says of a stage (its builds, its change, its deploy): the
 *  truth when the run's own steps never moved — a build started outside the run's Build step, a
 *  change merged by hand, a run cancelled once its change merged. */
function stationState(w: WorkItem, key: StageKey): StageState | null {
  const KEYS: Record<StageKey, string[]> = { plan: ["plan", "design"], build: ["build", "review", "pr"], ship: ["ship"], live: [] };
  const got = w.stages.filter((st) => KEYS[key].includes(st.key) && st.state !== "skipped").map((st) => st.state);
  if (!got.length) return null;
  if (got.includes("failed")) return "fail";
  if (got.includes("waiting")) return w.state === "needs_you" ? "you" : "live";
  if (got.includes("now")) return "live";
  if (got.every((x) => x === "done")) return "done";
  return got.includes("done") ? "live" : "todo";
}

type Ctx = {
  /** The change's checker line ("gwlint 9 → 0 open"), when it has a validation record. */
  checks?: string[];
  nowMs?: number;
};

/** THE FOUR STAGES of one piece of work. */
export function panelStages(w: WorkItem, ctx: Ctx = {}): Stage[] {
  const all = stepsOf(w);
  const next = workNextStep(w);
  const mp = mergeWaitOf(w);
  const ch = workChangeOf(w);
  const ref = ch?.number ? changeRef(ch.external_url, ch.number) : "";
  const finished = w.state === "done" || w.state === "failed";
  // WHERE THE PERSON'S MOVE IS. A merge is Build's last step, whatever step waits for it (a
  // deploy that holds for the merge is Ship's, and Ship then starts after the merge); else the
  // open gate, else the step that is on.
  const mergeMove = w.state === "needs_you" && next?.kind === "merge";
  const gateStep = all.find((s) => s.state === "waiting") ?? all.find((s) => s.state === "now");
  const youKey: StageKey | null = !waitsOnPerson(w) ? null : mergeMove ? "build" : gateStep?.stage ?? null;
  const youStep = mergeMove ? all.find((s) => s.capability === "merge") : youKey ? gateStep : undefined;
  const out: Stage[] = [];
  for (const key of STAGE_KEYS) {
    const after = mergeMove && STAGE_KEYS.indexOf(key) > STAGE_KEYS.indexOf("build");
    // After the merge the person must make, a step that waits for it has not started.
    const steps = all.filter((s) => s.stage === key).map((s) => (after && s.state === "waiting" ? { ...s, state: "todo" as StepState } : s));
    // The input file the run started from is Plan's when the workflow has no plan step.
    const input = key === "plan" && !steps.length && w.input_file ? w.input_file : "";
    const shown: PanelStep[] = steps.map((s) => ({ name: s.name, state: s.state, ...(s.id === youStep?.id ? { you: true } : {}) }));
    if (input) shown.push({ name: "Input file", state: "done" });
    if (key === youKey && mergeMove && !youStep) shown.push({ name: "Merge", state: "waiting", you: true });
    let state: StageState;
    if (key === youKey) state = "you";
    else if (!shown.length) state = "absent";
    else if (steps.some((s) => s.state === "failed")) state = "fail";
    else if (steps.some((s) => s.state === "now" || s.state === "waiting")) state = "live";
    else if (input || (steps.some((s) => s.state === "done") && steps.every((s) => s.state === "done" || s.state === "skipped"))) state = "done";
    // Part done and nothing on now: done once the work ended, else not running (grey), its
    // progress said and its screen still linked.
    else if (steps.some((s) => s.state === "done")) state = finished ? "done" : w.state === "running" && !after ? "live" : "todo";
    else state = "todo";
    // The run's steps never moved but the work did (the station strip): say what happened.
    const station = state === "todo" ? stationState(w, key) : null;
    const byStation = !!station && station !== "todo";
    if (byStation) state = station!;
    // A merged piece of work: a step that failed on the way is history, never a red stage.
    const merged = w.state === "done" && !/cancel|closed|abandon|rejected|dismissed/i.test(w.status ?? "");
    const wentOn = merged && state === "fail";
    if (wentOn) state = "done";
    const prev = out[out.length - 1];
    // A report or notify step only says something about its neighbours: never the stage's reason.
    const says = (s: WorkStep) => s.capability !== "report" && s.capability !== "notify";
    const lead = state === "fail" || wentOn ? steps.find((s) => s.state === "failed")
      : state === "live" ? steps.find((s) => s.state === "now" || s.state === "waiting")
      : state === "you" ? youStep ?? gateStep
      : [...steps].reverse().find((s) => s.state === "done" && says(s)) ?? [...steps].reverse().find((s) => s.state === "done");
    // ── the one reason ──
    let reason = "";
    if (wentOn) reason = `${lead?.name ?? STAGE_NAME[key]} failed; the work went on`;
    else if (byStation && state !== "fail") {
      reason = key === "build" && w.links.merged_pr?.number ? `Merged as ${changeRef(w.links.merged_pr.external_url, w.links.merged_pr.number)}`
        : state === "done" ? `${STAGE_NAME[key]} done` : state === "you" ? (next?.words || "Waits on you") : `${STAGE_NAME[key]} running`;
    } else if (state === "absent") reason = "Not in this workflow";
    else if (state === "todo" && steps.some((s) => s.state === "done") && !after) {
      reason = `${steps.filter((s) => s.state === "done").length} of ${steps.length} steps done`;
    } else if (state === "todo") {
      reason = finished ? "Not reached"
        : prev && prev.state === "you" && prev.key === "build" && mp ? "Starts after the merge"
        : prev && prev.state !== "done" && prev.state !== "absent" ? `Waits for ${prev.name}` : "Next";
    } else if (state === "you") {
      reason = next?.kind === "merge"
        ? (mp?.fixing || mp?.checks_failing || mp?.failed ? next.words : ctx.checks?.length ? ctx.checks.join(" · ") : `${ref ? `${ref} ` : ""}ready to merge`)
        : next?.words || `${w.action_label || "A decision"} waits on you`;
    } else if (state === "fail") {
      reason = lead?.sub || (key === "build" && mp?.checks_failing ? mp.checks_failing.words : "") || w.why?.words || `${lead?.name ?? STAGE_NAME[key]} failed`;
    } else if (state === "live") {
      reason = (key === "build" && mp?.fixing ? `BOSS is fixing ${mp.fixing.findings ? `${mp.fixing.findings} finding${mp.fixing.findings === 1 ? "" : "s"}` : "it"}` : "")
        || lead?.sub || (lead ? `${lead.name} running` : `${steps.filter((s) => s.state === "done").length} of ${steps.length} steps done`);
    } else {
      reason = input ? `${input} taken in`
        : key === "build" && w.links.merged_pr?.number ? `Merged as ${changeRef(w.links.merged_pr.external_url, w.links.merged_pr.number)}`
        : key === "build" && ctx.checks?.length ? ctx.checks.join(" · ")
        : lead?.sub || `${lead?.name ?? STAGE_NAME[key]} done`;
    }
    // ── the one link: only once the stage started ──
    let link: StageLink | null = null;
    if (state !== "absent" && (state !== "todo" || (steps.some((s) => s.state === "done") && !after))) {
      if (key === "plan") {
        const meeting = steps.find((s) => s.refs.meeting)?.refs.meeting;
        link = meeting ? { label: "Panel meeting", href: meeting, screen: "meeting" } : SCREENS.workflow(w);
      } else if (key === "build") {
        link = lead && isMergeOrGate(lead) && (state === "you" || state === "live") ? SCREENS.workflow(w) ?? SCREENS.build(w, steps) : SCREENS.build(w, steps) ?? SCREENS.workflow(w);
      } else if (key === "ship") {
        link = lead && lead.kind === "decision" && state === "you" ? SCREENS.workflow(w) : SCREENS.ship(w, steps);
      } else link = SCREENS.live(w);
    }
    out.push({ key, name: STAGE_NAME[key], state, steps: shown, reason: clip(reason), link });
  }
  return out;
}

/** WATCH LIVE: the screen of the stage BOSS is on now (a failing stage it is still retrying
 *  counts). Null when nothing is running. */
export function watchLive(w: WorkItem, stages: Stage[]): StageLink | null {
  if (w.state !== "running" || waitsOnPerson(w)) return null;
  const on = stages.find((s) => s.state === "live") ?? stages.find((s) => s.state === "fail");
  return on?.link ?? SCREENS.workflow(w);
}

export type Chip = { word: string; tone: "amber" | "blue" | "red" | "green" | "idle" };
/** The panel's one status chip. */
export function panelChip(w: WorkItem, stages: Stage[]): Chip {
  if (waitsOnPerson(w)) return { word: "Needs you", tone: "amber" };
  if (w.state === "running") {
    const bad = stages.find((s) => s.state === "fail");
    return bad ? { word: `${bad.name} failing`, tone: "red" } : { word: "BOSS working", tone: "blue" };
  }
  if (w.state === "stalled") return { word: "Stalled", tone: "idle" };
  if (w.state === "failed") return w.replaced_by ? { word: "Replaced", tone: "idle" } : { word: "Failed", tone: "red" };
  if (w.state === "done") {
    const st = (w.status ?? "").toLowerCase();
    if (/cancel/.test(st)) return { word: "Cancelled", tone: "idle" };
    if (/closed|abandon|rejected|dismissed/.test(st)) return { word: "Closed", tone: "idle" };
    return { word: w.links.merged_pr ? "Merged" : "Done", tone: "green" };
  }
  return { word: "Planned", tone: "idle" };
}

/** When the work became ready for the person: the last step that finished, else its last move. */
export function readyAt(w: WorkItem): string {
  const ends = stepsOf(w).map((s) => s.completed_at).filter(Boolean).sort();
  return ends[ends.length - 1] || w.updated_at || "";
}

export type Times = { kicked: string; ready: string; finished: string; elapsed: string; waiting: string; cost: string; costTitle: string; attempt: number };
/** The times line: Kicked off · Ready or Elapsed or Finished · Waiting on you · Cost · Attempt. */
export function panelTimes(w: WorkItem, attempt: number, nowMs?: number): Times {
  const refMs = nowMs ?? (isHydrated() ? Date.now() : 0);
  const live = w.state === "running" || w.state === "stalled";
  const ready = w.state === "needs_you" ? readyAt(w) : "";
  const fin = w.state === "done" || w.state === "failed" ? w.updated_at || "" : "";
  const paid = (w.cost_usd ?? 0) > 0;
  const usd = paid ? w.cost_usd! : w.list_usd ?? 0;
  return {
    kicked: w.created_at, ready, finished: fin,
    elapsed: live && refMs > 0 ? durationWords(secondsBetween(w.created_at, null, refMs)) : "",
    waiting: ready && refMs > 0 ? durationWords(secondsBetween(ready, null, refMs)) : "",
    cost: usd > 0 ? `$${usd.toFixed(2)}` : "", costTitle: paid ? "Spent" : "List price of the calls your plan includes",
    attempt,
  };
}

export type NextButton = { label: string; href: string; kind: "primary" | "amber" | "plain"; external: boolean; testid: string };
export type NextLine = { say: string; sub: string; tone: "you" | "boss" | "fail" | "done" | "idle"; buttons: NextButton[] };

/** THE ONE "WHAT NEXT" LINE: what happens now, and only the buttons that apply. Amber only
 *  when it is the person's move. */
export function nextLine(w: WorkItem, stages: Stage[], opts: { nowMs?: number; hhmm?: (iso: string) => string; checksHref?: string } = {}): NextLine {
  const hhmm = opts.hhmm ?? ((x: string) => x);
  const next: NextStep | null = workNextStep(w);
  const btn = (label: string, href: string, kind: NextButton["kind"], testid: string): NextButton =>
    ({ label, href, kind, external: /^https?:\/\//.test(href), testid });
  if (w.state === "needs_you" && next) {
    const said = stateSentence(w, next);
    const buttons: NextButton[] = [];
    if (next.wait?.href) buttons.push(btn(next.wait.label, next.wait.href, "primary", "wait"));
    if (next.kind === "merge" && opts.checksHref) buttons.push(btn("See Checks", opts.checksHref, "plain", "checks"));
    const href = next.kind === "merge" ? (w.links.decision || next.href) : (w.links.decision || next.href || w.links.workflow_run);
    if (href) buttons.push(btn(next.kind === "merge" && w.links.decision ? "Approve and merge" : next.label, href, "amber", "move"));
    const say = next.kind === "merge" && said.lead === "Ready to merge." ? "Ready to merge, your call" : said.lead.replace(/\.$/, "");
    return { say, sub: said.rest, tone: "you", buttons };
  }
  if (w.state === "running" && waitsOnPerson(w)) {
    const gate = stepsOf(w).find((s) => s.state === "waiting");
    const href = w.links.decision || (stages.find((s) => s.state === "you")?.link?.href ?? "") || (SCREENS.workflow(w)?.href ?? "");
    return { say: `${gate?.name ?? "A step"} waits on you`, sub: clip(gate?.sub || w.why?.words || "Only a person can let it go on.", 140), tone: "you",
             buttons: href ? [btn(w.links.decision ? "Decide" : "Open", href, "amber", "move")] : [] };
  }
  if (w.state === "needs_you") {
    const open = SCREENS.workflow(w);
    return { say: "Needs you", sub: clip(w.why?.words || w.detail, 140), tone: "you",
             buttons: w.links.decision ? [btn(w.action_label || "Decide", w.links.decision, "amber", "move")] : open ? [btn("Open workflow", open.href, "amber", "move")] : [] };
  }
  if (w.state === "running") {
    const watch = watchLive(w, stages);
    const bad = stages.find((s) => s.state === "fail");
    const on = stages.find((s) => s.state === "live") ?? bad;
    const n = w.attempt_count > 1 ? w.attempt_count : 0;
    const say = bad ? `BOSS is retrying ${bad.name.toLowerCase()}${n ? ` (attempt ${n})` : ""}` : `BOSS is on ${on ? on.name : "it"}`;
    const who = w.agents[0]?.name ?? "";
    const began = stepsOf(w).find((s) => s.state === "now" && s.started_at)?.started_at || w.created_at;
    const since = began ? hhmm(began) : "";
    const sub = [who, since ? `since ${since}` : "", "Nothing needed from you yet."].filter(Boolean).join(" · ");
    return { say, sub, tone: "boss", buttons: watch ? [btn("Watch live", watch.href, "primary", "watch")] : [] };
  }
  if (w.state === "stalled") {
    const open = SCREENS.build(w) ?? SCREENS.workflow(w);
    return { say: "Stalled", sub: w.why?.words || "Nothing has moved. Open it to retry or stop it.", tone: "you",
             buttons: open ? [btn(open.screen === "build" ? "Open build" : "Open workflow", open.href, "amber", "open")] : [] };
  }
  if (w.state === "failed") {
    if (w.replaced_by) return { say: "Replaced by a later attempt", sub: w.replaced_by.label, tone: "idle", buttons: [] };
    const bad = stages.find((s) => s.state === "fail");
    const open = bad?.link ?? SCREENS.workflow(w);
    return { say: "BOSS stopped", sub: clip(w.why?.words || bad?.reason || w.detail, 140), tone: "fail",
             buttons: open ? [btn("See what happened", open.href, "amber", "open")] : [] };
  }
  if (w.state === "done") {
    const said = stateSentence(w, null);
    const chip = panelChip(w, stages);
    return { say: chip.word, sub: said.rest, tone: chip.tone === "green" ? "done" : "idle", buttons: [] };
  }
  return { say: "Not started", sub: "", tone: "idle", buttons: [] };
}

/** "Change #24 › Checks": the change's Checks tab, for a change held in BOSS. */
export function checksHrefOf(w: WorkItem): string {
  const ch = workChangeOf(w);
  const h = ch ? changeHref(ch.external_url) || "" : "";
  if (!h || !ch || !isLocalChangeUrl(ch.external_url)) return "";
  return `${h}${h.includes("?") ? "&" : "?"}tab=checks`;
}
