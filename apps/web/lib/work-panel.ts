/**
 * THE WORK PANEL, AS DATA (the approved wireframe ~/Downloads/work-panel-wireframe.html, 2026-11-19).
 *
 * One piece of work's landing page reads only what the console already holds — the Work row
 * (engine work.py), its story (work_story.py: stations in words, the explanation, the review's
 * comments) and the project's deployments — and turns it into what the panel draws:
 *
 *   flowNodes        Spec → Build → Review & tests → Merge → Staging → Production, each with its
 *                    agent, status, duration and the one inline note ("BOSS is fixing…" / "Your move")
 *   stateSentence    the header's state word and one sentence
 *   helmSuggestions  three questions fixed per state
 *   costPoints       cumulative cost of the attempts in time order (the sparkline)
 *
 * Pure functions: the component draws, these decide. No model call anywhere.
 */
import type { Agent, Attempt, Deployments, StepState, WorkItem, WorkStage, WorkStory } from "./project-experience";
import type { NextStep } from "./work-next-step";
import { mergeWaitOf, mustHaveWords } from "./work-next-step";
import { changeRef } from "./repo-links";

export type FlowState = "done" | "live" | "wait" | "todo" | "failed" | "skipped";
export type FlowNote = { tone: "boss" | "you"; title: string; text: string; action?: { label: string; href: string } | null };
export type FlowNode = {
  key: "spec" | "build" | "review" | "merge" | "staging" | "production";
  name: string; state: FlowState;
  /** The face on the ring: an agent, or the person (`you`). */
  agent: Agent | null; you: boolean;
  /** The mono line under the name ("PRD agreed · 6 m") and the dim line ("Juno + panel of 5"). */
  sub: string; who: string;
  note: FlowNote | null;
};

/** "6 m", "1 h 6 m", "45 s" — a duration in the panel's mono words. */
export function durationWords(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} m`;
  const h = Math.floor(m / 60);
  const r = m % 60;
  if (h < 24) return r ? `${h} h ${r} m` : `${h} h`;
  const d = Math.floor(h / 24);
  return `${d} d ${h % 24} h`;
}

/** Seconds between two ISO times (or to now). */
export function secondsBetween(from: string | null | undefined, to?: string | null, nowMs = Date.now()): number | null {
  const a = from ? Date.parse(from) : NaN;
  const b = to ? Date.parse(to) : nowMs;
  return Number.isFinite(a) && Number.isFinite(b) && b > a ? (b - a) / 1000 : null;
}

const FLOW_STATE: Record<StepState, FlowState> = {
  done: "done", now: "live", waiting: "wait", todo: "todo", failed: "failed", skipped: "skipped",
} as Record<StepState, FlowState>;
const toFlow = (s: StepState | undefined): FlowState => (s ? FLOW_STATE[s] ?? "todo" : "todo");

/** Who stands at a station when the engine named nobody: the crew's agent for that role. */
const ROLE_FOR: Record<string, string[]> = {
  spec: ["product_manager", "pm", "product_owner"],
  build: ["software_engineer", "engineering_captain", "engineer"],
  review: ["qa_engineer", "adversarial_reviewer", "reviewer", "qa"],
  staging: ["devops_engineer", "release_manager", "deploy_engineer", "sre"],
  production: ["sre", "site_reliability_engineer", "devops_engineer"],
};
const FALLBACK: Record<string, Agent> = {
  spec: { name: "Juno", role_key: "product_manager", avatar: "" },
  build: { name: "Forge", role_key: "software_engineer", avatar: "" },
  review: { name: "Sentry", role_key: "qa_engineer", avatar: "" },
  staging: { name: "Harbor", role_key: "devops_engineer", avatar: "" },
  production: { name: "Beacon", role_key: "sre", avatar: "" },
};
export function crewAgent(key: string, crew: Agent[]): Agent | null {
  const roles = ROLE_FOR[key];
  if (!roles) return null;
  for (const r of roles) {
    const hit = crew.find((a) => a.role_key === r);
    if (hit) return hit;
  }
  return FALLBACK[key] ?? null;
}

type Ctx = {
  w: WorkItem; story: WorkStory | null; next: NextStep | null; deployments?: Deployments | null;
  crew?: Agent[]; nowMs?: number; hhmm?: (iso: string) => string;
};

function stage(w: WorkItem, key: string): WorkStage | undefined {
  return w.stages.find((s) => s.key === key);
}
function storyStage(story: WorkStory | null, key: string) {
  return story?.stages.find((s) => s.key === key);
}

/** The six nodes of "Who did what". */
export function flowNodes({ w, story, next, deployments, crew = [], nowMs = Date.now(), hhmm = (x) => x }: Ctx): FlowNode[] {
  const mp = mergeWaitOf(w);
  const fix = mp?.fixing ?? null;
  const built = story?.explained?.built ?? null;
  const said = story?.explained?.discussed ?? null;
  const gate = story?.review_comments?.items.find((c) => c.severity === "gate") ?? null;
  const plan = stage(w, "plan"); const design = stage(w, "design");
  const ref = mp ? changeRef(mp.external_url, mp.number, true) : w.links.pr ? changeRef(w.links.pr.external_url, w.links.pr.number, true) : "";

  // ── Spec: the panel's plan, and the person's approval of it ──
  const specState: FlowState = [plan, design].some((s) => s?.state === "failed") ? "failed"
    : design?.state === "waiting" ? "wait"
    : [plan, design].some((s) => s?.state === "now") ? "live"
    : [plan, design].some((s) => s?.state === "done") ? "done"
    : [plan, design].every((s) => !s || s.state === "skipped") ? "skipped" : "todo";
  const specAgent = plan?.agent ?? crewAgent("spec", crew);
  const approved = storyStage(story, "design");
  const specSecs = (plan?.duration_s ?? 0) + (design?.duration_s ?? 0);
  const spec: FlowNode = {
    key: "spec", name: "Spec", state: specState, agent: specAgent, you: false,
    sub: [storyStage(story, "plan")?.sub || (specState === "done" ? "Planned" : ""), durationWords(specSecs)].filter(Boolean).join(" · "),
    who: [specAgent ? `${specAgent.name}${said?.seats ? ` + panel of ${said.seats}` : ""}` : "",
          approved?.at ? `${approved.sub ? `${approved.sub} approved` : "you approved"} ${hhmm(approved.at)}` : ""].filter(Boolean).join(" · "),
    note: null,
  };

  // ── Build ──
  const b = stage(w, "build");
  const buildAgent = b?.agent ?? crewAgent("build", crew);
  const files = built?.filesChanged || 0;
  const build: FlowNode = {
    key: "build", name: "Build", state: toFlow(b?.state), agent: buildAgent, you: false,
    sub: [files ? `${files} file${files === 1 ? "" : "s"}` : "", durationWords(b?.duration_s)].filter(Boolean).join(" · "),
    who: [buildAgent?.name ?? "", w.attempt_count > 1 ? `${w.attempt_count} tries` : ""].filter(Boolean).join(" · "),
    note: null,
  };

  // ── Review & tests: a follow-up still fixing the change is BOSS working here ──
  const r = stage(w, "review");
  const reviewAgent = r?.agent ?? crewAgent("review", crew);
  const tests = built?.tests;
  const testsWords = tests?.passed === true ? (tests.count ? `${tests.count}/${tests.count} pass` : "tests pass")
    : tests?.passed === false ? "tests failing" : "";
  const rounds = built?.review.rounds ?? 0;
  let reviewState = toFlow(r?.state);
  let reviewNote: FlowNote | null = null;
  if (fix) {
    reviewState = "live";
    const n = fix.findings;
    reviewNote = {
      tone: "boss", title: "BOSS is fixing",
      text: [`${n ? `${n} review finding${n === 1 ? "" : "s"}` : "The review's findings"}${gate ? ` + gate “${gateName(gate.text)}”` : ""}`,
             fix.label, durationWords(secondsBetween(fix.since, null, nowMs)) ? `${durationWords(secondsBetween(fix.since, null, nowMs))} in` : ""]
        .filter(Boolean).join(" · "),
    };
  } else if (r?.state === "failed" && w.why) {
    reviewNote = { tone: w.why.who === "you" ? "you" : "boss", title: w.why.who === "you" ? "Your move" : "BOSS is fixing", text: w.why.words };
  }
  const review: FlowNode = {
    key: "review", name: "Review & tests", state: reviewState, agent: reviewAgent, you: false,
    sub: [testsWords, rounds ? `round ${rounds + (fix ? 1 : 0)}` : ""].filter(Boolean).join(" · "),
    who: [reviewAgent?.name ?? "", reviewState === "live" ? "now" : ""].filter(Boolean).join(" · "),
    note: reviewNote,
  };

  // ── Merge: a person's ──
  const p = stage(w, "pr");
  const mergeStory = storyStage(story, "pr");
  const waitingFor = durationWords(secondsBetween(w.updated_at || w.created_at, null, nowMs));
  let mergeState = toFlow(p?.state);
  if (mp && w.state === "needs_you") mergeState = "wait";
  const merge: FlowNode = {
    key: "merge", name: "Merge", state: mergeState, agent: null, you: true,
    sub: mergeState === "wait" ? [ref, waitingFor ? `waiting ${waitingFor}` : ""].filter(Boolean).join(" · ")
      : mergeStory?.sub || (mergeState === "done" ? "Merged" : ref),
    who: mergeState === "done" && mp?.follow_up_of ? `Follow-up fix after #${mp.follow_up_of}` : mergeState === "done" ? "" : "Only a person merges",
    note: mergeState === "wait" && next?.kind === "merge" ? {
      tone: "you", title: "Your move",
      text: fix ? "Merge now, or wait for the follow-up to land its fixes"
        : mp?.follow_up_of ? `Follow-up fix after #${mp.follow_up_of} — ${next.words}` : next.words,
    } : null,
  };

  // ── Staging: what ships after the merge ──
  const s = stage(w, "ship");
  const envs = deployments?.environments ?? [];
  const stg = envs.find((e) => e.key === "staging") ?? null;
  const stagingAgent = s?.agent ?? crewAgent("staging", crew);
  const shipStory = storyStage(story, "ship");
  const staging: FlowNode = {
    key: "staging", name: "Staging", state: toFlow(s?.state), agent: stagingAgent, you: false,
    sub: shipStory?.sub ? shipStory.sub.toLowerCase() : s?.state === "done" ? "live" : "auto after merge",
    who: [stagingAgent?.name ?? "", stg?.host || stg?.address || ""].filter(Boolean).join(" · "),
    note: s?.state === "waiting" && next?.kind === "decide" ? { tone: "you", title: "Your move", text: next.words } : null,
  };

  // ── Production: the deployments read (no host yet → its action is the person's) ──
  const prodEnv = envs.find((e) => e.key === "production") ?? null;
  const prod = deployments?.production;
  const prodAgent = crewAgent("production", crew);
  const noHost = !prodEnv && (prod?.state === "none" || prod?.state === "needs_host" || !prod);
  const production: FlowNode = {
    key: "production", name: "Production", state: prodEnv ? (prodEnv.health === "healthy" ? "done" : "todo") : "todo", agent: prodAgent, you: false,
    sub: prodEnv ? (prodEnv.last_deploy?.pr && mp && prodEnv.last_deploy.pr !== mp.number ? `runs change #${prodEnv.last_deploy.pr}` : prodEnv.health) : "no host yet",
    who: prodAgent?.name ?? "",
    note: noHost && prod?.action ? { tone: "you", title: "Your move", text: "Pick a prod host", action: prod.action } : null,
  };

  return [spec, build, review, merge, staging, production];
}

/** The connector after node i: green once both ends moved, flowing into a live node, else dotted. */
export function connectorState(nodes: FlowNode[], i: number): "done" | "live" | "todo" {
  const a = nodes[i]; const b = nodes[i + 1];
  if (!a || !b) return "todo";
  if (b.state === "live" || b.state === "wait") return a.state === "done" || a.state === "live" ? "live" : "todo";
  if (a.state === "done" && (b.state === "done" || b.state === "failed")) return "done";
  return "todo";
}

/** "“Package honest”" from a gate comment's words. */
export function gateName(text: string): string {
  const m = /[“"]([^”"]+)[”"]/.exec(text);
  return m ? m[1] : text.split(" check")[0];
}

/** The header's state word and one sentence (never "Ready to merge" over a fix still running
 *  or over no must-have met: the words come from workNextStep). */
export function stateSentence(w: WorkItem, next: NextStep | null): { lead: string; tone: "amber" | "mint" | "red" | "green" | "dim"; rest: string } {
  const mp = mergeWaitOf(w);
  if (w.state === "needs_you" && next) {
    if (next.kind === "merge") {
      const ref = mp ? changeRef(mp.external_url, mp.number, true) : "the change";
      const Ref = ref[0].toUpperCase() + ref.slice(1);
      if (mp?.fixing) {
        const n = mp.fixing.findings;
        return { lead: "Merge now, or wait.", tone: "amber",
                 rest: `${Ref} is built and reviewed; BOSS is still fixing ${n ? `${n} review finding${n === 1 ? "" : "s"}` : "it"} in ${mp.fixing.label}.` };
      }
      if (mp?.acceptance && mp.acceptance.total && mp.acceptance.met === 0) {
        return { lead: "Merge waiting.", tone: "amber", rest: `${Ref} is open, but ${mustHaveWords(mp.acceptance)} — read what is partial before merging.` };
      }
      if (mp?.failed) return { lead: "Merge failed.", tone: "red", rest: mp.failed };
      return { lead: "Ready to merge.", tone: "amber", rest: `${Ref} is built, tested and reviewed.${mp?.followups?.words ? ` ${mp.followups.words}.` : ""}` };
    }
    return { lead: "Needs you.", tone: "amber", rest: next.words };
  }
  if (w.state === "running") return { lead: "BOSS is working.", tone: "mint", rest: w.why?.words || "" };
  if (w.state === "stalled") return { lead: "Stalled.", tone: "amber", rest: w.why?.words || "Nothing is working on it." };
  if (w.state === "failed") return { lead: "Failed.", tone: "red", rest: w.detail };
  if (w.state === "done") {
    const md = w.links.merged_pr;
    return { lead: "Done.", tone: "green", rest: md ? `Merged as ${changeRef(md.external_url, md.number, true)}.` : "" };
  }
  return { lead: "Planned.", tone: "dim", rest: "" };
}

/** Three questions for Helm, fixed per state (spec §5). */
export function helmSuggestions(w: WorkItem, opts: { noProdHost?: boolean } = {}): string[] {
  const out: string[] = [];
  const mp = mergeWaitOf(w);
  if (w.state === "needs_you" && mp) out.push("Is it safe to merge now?");
  if (w.state === "running") out.push("What is BOSS doing right now?");
  if (w.state === "failed" || w.state === "stalled") out.push("Why did it stop?");
  if (w.attempt_count > 1) out.push("Why did the build retry?");
  if (opts.noProdHost) out.push("What's left for production?");
  out.push("What was built?", "What did the review find?");
  return Array.from(new Set(out)).slice(0, 3);
}

/** The attempts oldest first, each with its running total (list price when the money is $0). */
export function costPoints(w: Pick<WorkItem, "attempts" | "cost_usd" | "list_usd">): { label: string; usd: number; total: number; state: Attempt["state"] }[] {
  const list = [...w.attempts].sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
  let total = 0;
  return list.map((t) => {
    const usd = (t.cost_usd && t.cost_usd > 0 ? t.cost_usd : t.list_usd) ?? 0;
    total += usd;
    return { label: t.label, usd, total, state: t.state };
  });
}

/** "build → retry → follow-up (running)" — the attempts on one line. */
export function attemptsLine(w: Pick<WorkItem, "attempts">): string {
  const list = [...w.attempts].sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
  return list.map((t) => {
    const word = t.kind === "followup" ? "follow-up" : t.kind === "retry" ? "retry" : "build";
    return t.state === "running" || t.state === "stalled" ? `${word} (${t.state})` : word;
  }).join(" → ");
}
