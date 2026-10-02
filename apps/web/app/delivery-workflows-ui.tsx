"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { appIdentity, identityKey, type AppRun } from "./app-identity";
import { formatDateTime } from "../lib/datetime";
import { notifyChanged, onChanged } from "./changed";
import { Markdown } from "./markdown";
import { AgentChip } from "./agent-chip";
import { AgentsOnThis } from "./agents-on-this";
import { uniqueWorkflowName } from "./workflow-names";
import { consoleHref, readNewProjectIntent } from "./url-intent";
import { StepIcon, hasStepIcon } from "./step-icons";
import { isLoopbackPreviewUrl, operatorPreviewOpenHref } from "../lib/code-tasks";
import { previewRecordFromStepOutput } from "../lib/previews";
import { openDockAbout, type DockSubject } from "../lib/dock-subject";
import { inputsWithTheDirective, missingRequired, suppliedInputs, workflowInputFields }
  from "../lib/workflow-inputs";
import { HostAdviceBanner, useHostAdvice } from "./host-advice";
import { RunReportPanel } from "./run-report-ui";
import { HelpTip } from "./help-tip";
import { plannedDeploymentFrom, type PlannedDeployment } from "./deploy-ui";
import { WriteInFlightError, apiWrite, runPress } from "../lib/api-write";

/** Where the API lives, for the proxy path a preview link resolves to. */
const PREVIEW_API = process.env.NEXT_PUBLIC_BOSS_API_URL ?? "";

/**
 * Delivery workflows: choose one, watch it run, build your own.
 *
 * Three things an operator does here, in the order they do them:
 *   1. Pick a workflow from the twelve BOSS ships, and apply it to a class of work.
 *   2. Watch what is running — which step, waiting on whom, and what each step produced.
 *   3. Fork one and change it, by dragging steps around.
 *
 * The screen never invents a capability, a risk tier or an approval rule. Everything it draws
 * comes from the engine's registry, so the palette, the validation messages and the gates in
 * the plan are the same ones the runner will enforce. A builder that disagreed with the runtime
 * would let a customer save a workflow that cannot run.
 */

type Capability = {
  key: string;
  label: string;
  summary: string;
  risk_tier: number;
  required_inputs: string[];
  optional_inputs: string[];
  produces: string[];
  requires_project: string[];
  skills: string[];
  typical_minutes: number;
  is_gate: boolean;
  approval_forced_manual: boolean;
  /** The registered role that answers for this step. Sent by the engine so the console does
   *  not keep a second copy of the mapping to drift from it. */
  captain?: string;
};

/** A step this customer defined, bound to one of their own skills or MCP tools. The risk
 *  tier is RESOLVED from whatever it binds — it is never something the customer typed. */
type CustomStep = {
  step_key: string;
  label: string;
  summary: string;
  binding_kind: "skill" | "mcp_tool";
  skill_id: string | null;
  mcp_tool_name: string | null;
  inputs: { name: string; required: boolean; description: string }[];
  produces: string[];
  /** The operator's own house rules for this step, handed to whatever it runs. Served by the
   *  API all along and never typed here, so the console could not show it. */
  knowledge?: string | null;
  enabled: boolean;
  risk_tier: number;
  risk_source: string | null;
  trusted: boolean;
  usable: boolean;
  unusable_reason: string | null;
  approval_forced_manual: boolean;
};

type Bindable = {
  skills: { skill_id: string; risk_tier: number; trusted: boolean; unclassified: boolean }[];
  mcp_servers: {
    id: string; name: string; risk_tier: number; trusted: boolean; reachable: boolean;
    error?: string;
    tools: { name: string; description: string; risk_tier: number }[];
  }[];
};

type CatalogueEntry = {
  key: string;
  name: string;
  description: string;
  work_class: string;
  step_count: number;
  steps: { id: string; capability: string }[];
  gates: string[];
  requires_project: string[];
  estimated_minutes: number;
};

type Installed = {
  id: string;
  workflow_key: string;
  name: string;
  is_catalogue: boolean;
  forked_from_version_id: string | null;
  namespace: string;
  description?: string | null;
  /** Enough to DRAW an operator's own workflow rather than only count it. Optional because a
   *  definition with no published version has neither. */
  step_count?: number;
  /** WHICH steps, not just how many. "Steps 4" says how big a workflow is and nothing about
   *  what it does, so the only way to find out was to open the editor. */
  steps?: { id: string; label: string; kind: string; mine: boolean; approval: string;
            /** WHO RUNS IT. Empty means different things by kind — see the card. */
            captain?: string; agent_work?: boolean }[];
  active_version?: number | null;
  /** What starting it will ask for. Absent on a workflow that declares nothing, which is
   *  every catalogue workflow today. */
  input_schema?: unknown;
};

/** When a workflow runs without anybody asking. `describes` is rendered by the API so the
 *  console cannot word a schedule differently from the thing that fires it. */
type Schedule = {
  id: string;
  workflow_key: string;
  name: string;
  describes: string;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_error: string | null;
  /** Present on every schedule the API returns. Optional on the type so an older
   *  payload still renders the sentence and the pause control. */
  cadence?: string;
  minute?: number;
  hour?: number;
  weekday?: number | null;
  timezone?: string;
  input?: Record<string, unknown>;
};

type ScheduleDraft = {
  workflowKey: string;
  name: string;
  scheduleId: string | null;
  timezone: string;
  values: Record<string, string>;
};

function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function scheduleFormValues(input: Record<string, unknown> | undefined): Record<string, string> {
  const values: Record<string, string> = {};
  for (const [key, value] of Object.entries(input ?? {})) {
    if (value == null) continue;
    values[key] = typeof value === "string" ? value : String(value);
  }
  return values;
}

export type RunStep = {
  step_id: string;
  kind: string;
  status: string;
  capability: string | null;
  output: Record<string, unknown>;
  /** The captain this WORKFLOW named for this step, overriding the capability's default.
   *  Absent on every step nobody overrode, which is almost all of them. */
  captain?: string | null;
  evidence: Record<string, unknown>[];
  error: string | null;
  approval: Record<string, unknown>;
  due_at: string | null;
  timeout_applied: string | null;
  attempt: number;
  // The API has returned these since the runs endpoint was written; the type omitted them, so
  // the one question every operator asks of a pipeline — how long did this take — could not be
  // answered from data already on the page.
  started_at: string | null;
  completed_at: string | null;
  // The gate's decision, which is what makes Approve/Deny answerable here rather than only in
  // the Approvals area.
  decision_id: string | null;
  /** The gate's OWN state. The step's status does not carry it: a step stays `waiting` until a
   *  worker picks the answer up, so an approved gate looked identical to an unanswered one. */
  decision_status?: string | null;
  decision_approved_at?: string | null;
  /** Whether this step's RECORDED blocker still holds, re-tested when the run was read. */
  blocker?: { cleared?: boolean; checkable?: boolean; reason?: string; missing?: string } | null;
  /** The preview running RIGHT NOW for this run's build, resolved when the runs are read.
   *  The one recorded in `output` was live when the step ran, and previews expire. The whole
   *  record, because turning one into an openable link is a rule the console already owns. */
  live_preview?: {
    id: string; status: string; url: string | null;
    /** The build actually serving it, and whether that is THIS step's build. A live preview
     *  of a later build of the same app is worth linking to — but only if we say so. */
    build_id?: string; same_build?: boolean;
  } | null;
};

type Run = {
  id: string;
  status: string;
  workflow_key: string;
  workflow_name: string;
  version: number;
  steps: RunStep[];
  step_count: number;
  steps_done: number;
  current_step: string | null;
  waiting_on: string | null;
  blocked_reason: string | null;
  error: string | null;
  updated_at: string | null;
  /** What the run was started with — `title` is the project it is delivering. */
  input?: Record<string, unknown> | null;
  created_at?: string | null;
  /** What the operator may do to this run, decided by the SERVER from its real state — see
   *  `RunAction`. Absent only from an API older than the field, which then offers nothing. */
  actions?: RunAction[];
  /** The run is over (the engine's FINISHED_RUN_STATES), so what it was assigned is history.
   *  Sent by the server: this file's own list of "terminal" states left out `compensated` and
   *  named `deadlettered` beside the real ones, a private copy that had already drifted. */
  finished?: boolean;
};

/** One thing the operator may do to a run, as the engine decided it
 *  (boss_engine/services/workflow_run_actions.py).
 *
 *  THE CONSOLE DOES NOT DECIDE WHICH BUTTONS A RUN HAS. A cancelled run whose research FAILED
 *  offered "Build on this research →" because this file kept its own copy of the rule ("a
 *  succeeded step with a discovery id") and an old research step that had failed was recorded
 *  as exactly that. Stop, Run again, Remove, Re-check, Try again and Skip were each decided the
 *  same way — private conditions on `run.status` that disagreed with what the engine would
 *  actually do. Now the API sends the list and this renders it: present means offered, absent
 *  means it does not apply, and `enabled: false` carries the sentence saying why not. */
export type RunAction = {
  key: RunActionKey;
  label: string;
  enabled: boolean;
  /** Why it cannot be taken right now — shown as the button's help. Null when enabled. */
  reason: string | null;
  /** Set on a per-step action (Try again, Skip and proceed): the step it acts on. */
  step_id?: string;
};

/** Every key the engine can send. A test holds this equal to the engine's own list. */
export const RUN_ACTION_KEYS = [
  "recheck", "stop", "stop_and_rerun", "rerun", "remove", "build_on_research",
] as const;
export const STEP_ACTION_KEYS = ["retry_step", "skip_step"] as const;
export type RunActionKey = typeof RUN_ACTION_KEYS[number] | typeof STEP_ACTION_KEYS[number];

/** The run-level actions, in the order the engine sent them. */
export function runLevelActions(run: Pick<Run, "actions">): RunAction[] {
  return (run.actions ?? []).filter((a) => !a.step_id);
}

/** The actions on ONE step of a run, keyed so a caller asks for the one it renders. */
export function stepActions(run: Pick<Run, "actions">, stepId: string):
    { retry?: RunAction; skip?: RunAction } {
  const mine = (run.actions ?? []).filter((a) => a.step_id === stepId);
  return {
    retry: mine.find((a) => a.key === "retry_step"),
    skip: mine.find((a) => a.key === "skip_step"),
  };
}

/** The sentence's second half on a stopped run's card: the answers the server OFFERS for the
 *  step, and only those. It said "Try it again, or skip it and carry on." whatever the run
 *  offered — Skip included on an approval, where the server sends none. */
export function stoppedAnswers(actions: { retry?: RunAction; skip?: RunAction }): string {
  const retry = actions.retry?.enabled;
  const skip = actions.skip?.enabled;
  if (retry && skip) return " Try it again, or skip it and carry on.";
  if (retry) return " Try it again.";
  if (skip) return " Skip it and carry on.";
  return "";
}

/** The project this run is delivering. Five runs of "Standard Product Delivery" are five
 *  identical rows unless the thing being delivered is on the row. */
function runSubject(run: Run): string | null {
  // THROUGH THE NAMER, WHATEVER THE SOURCE. `input.title` is usually the directive already
  // truncated for display — "Build an incident timeline builder.\nWho it is for: an on-call
  // enginee…", ellipsis and all — so showing it verbatim put a broken sentence at the top of
  // the run while the report below it, which does run the namer, called the same thing
  // "Incident Timeline Builder". Two names for one app on one screen.
  const title = (run.input ?? {})["title"];
  const raw = typeof title === "string" && title.trim() ? title.trim() : null;
  if (!raw) return null;
  return appIdentity(run.input as AppRun).name || raw;
}

const LIVE = ["running", "waiting", "queued", "validating", "leased"];

/** What a step observed from the build, rather than started itself: the review seats and the
 *  quality gates the governed pipeline actually recorded. Without this, `review` and `test` were
 *  two rows that said only "succeeded" about the checks an operator most wants to read. */
function observedGates(step: RunStep, runAppName = ""): string | null {
  const out = (step.output ?? {}) as {
    status?: string; gates_passed?: unknown; targets?: unknown; findings?: unknown;
  };
  if (out.status !== "observed") return null;
  // The monitoring step reports targets rather than gates: which apps are under health probing
  // and whether a probe has run yet. "2 watched · app (production)" answers the question the
  // step exists to answer.
  // Findings the build shipped with. Not a failure — the pipeline's own policy said surface
  // and continue — but not a clean pass either, and the two must not look the same.
  if (Array.isArray((out as { findings?: unknown }).findings)) {
    const found = ((out as { findings: unknown[] }).findings).map(String);
    if (found.length) return `findings: ${found.join(" · ")}`;
  }
  if (Array.isArray(out.targets)) {
    const names = targetsForThisApp(out.targets, runAppName)
      .map((t) => [t.app_name, t.environment].filter(Boolean).join(" · "))
      .filter(Boolean);
    return names.length ? `watching ${names.join(", ")}` : null;
  }
  const gates = Array.isArray(out.gates_passed) ? out.gates_passed.map(String) : [];
  return gates.length ? gates.join(" · ") : null;
}

/** A step whose work FAILED and whose run carried on regardless.
 *
 *  HISTORY ONLY. The runner no longer does this — a failed step now stops its run until the
 *  operator retries or skips it (the run's `actions`). Runs recorded before that change still carry
 *  `failed_continued` outputs, and they must keep reading as failed rather than as green. */
function failedButContinued(step: RunStep): boolean {
  return (step.output as { status?: string } | undefined)?.status === "failed_continued";
}

/** A step the runtime completed by recording that its phase never happened. */
/** Why a step did not apply — the sentence the capability recorded when it stood aside. */
function skipReason(step: RunStep): string {
  const out = (step.output ?? {}) as { note?: unknown; reason?: unknown };
  return String(out.note ?? out.reason ?? "").slice(0, 220);
}

/** The step states a run STOPS at for the operator — the engine's `STOPPED_STEP_STATES`, which
 *  a test holds this list to. */
export const STOPPED_STEP_STATUSES: readonly string[] = ["failed", "deadlettered"];

/** The step a run has STOPPED AT and is waiting for the operator to decide about: its work
 *  failed (or was dead-lettered) and nobody has retried or skipped it yet.
 *
 *  A STOPPED RUN NEEDS SOMEBODY. When a failed step started stopping its run (boss-1,
 *  2026-10-02), the run landed under "Stopped" beside runs that were cancelled on purpose, the
 *  page did not open on it, and the card said nothing about why — the Try again and Skip
 *  buttons were two clicks deep. Read that way it is "something broke"; it is actually "research
 *  failed, decide: try again or skip". So a stopped step counts as needing the operator
 *  everywhere the console triages runs (`runNeedsYou`), exactly as an open approval does.
 *
 *  Not a refused approval gate: that one is a person's no, already answered, and the card says
 *  so in its own words (see `DENIED`). */
export function stoppedForYou(step: RunStep): boolean {
  if (!STOPPED_STEP_STATUSES.includes(step.status)) return false;
  if (step.kind === "decision") return false;
  return !step.decision_id;
}

/** The first step this run stopped at for the operator, or null. */
export function stoppedStep(run: { status: string; steps?: RunStep[] | null }): RunStep | null {
  // A run somebody stopped, or one that finished, is history — nothing there waits on anybody.
  if (["cancelled", "completed", "compensated"].includes(run.status)) return null;
  return (run.steps ?? []).find(stoppedForYou) ?? null;
}

/** Does this run need a person right now — an open approval, or a step it stopped at? The ONE
 *  rule the bucket, the page's opening run and the run card all read, so they cannot disagree. */
export function runNeedsYou(run: { status: string; steps?: RunStep[] | null }): boolean {
  return (run.steps ?? []).some(needsYou) || stoppedStep(run) !== null;
}

/** Which heading a run is listed under. Exported so the rule is tested, not only rendered. */
export function bucketOf(run: { status: string; steps?: RunStep[] | null }):
    "needs_you" | "running" | "blocked" | "done" {
  if (runNeedsYou(run)) return "needs_you";
  if (["failed", "deadlettered", "blocked", "cancelled"].includes(run.status)) return "blocked";
  return LIVE.includes(run.status) ? "running" : "done";
}

/** Who skipped a step and why, when an OPERATOR skipped it — "" for a branch never taken.
 *  The two read the same on the rail otherwise, and they mean opposite things: one is a
 *  decision somebody made about this step, the other is a path the run did not go down. */
export function skippedBy(step: RunStep): string {
  const out = (step.output ?? {}) as {
    skipped_by_operator?: unknown; skipped_by?: unknown; skip_reason?: unknown };
  if (step.status !== "skipped" || !out.skipped_by_operator) return "";
  const who = String(out.skipped_by ?? "").trim() || "an operator";
  const why = String(out.skip_reason ?? "").trim();
  return why ? `skipped by ${who}: ${why}` : `skipped by ${who}`;
}

function notApplicable(step: RunStep): boolean {
  return (step.output as { status?: string } | undefined)?.status === "not_applicable";
}

/** Live runs first, newest first within each group. Sorting by `updated_at` alone floats a
 *  blocked run from last week above the project that started a minute ago, purely because
 *  something touched it more recently — which is how the page came to show an expired run
 *  while a build was in flight. */
/** "2 Sep 03:42Z" — cut straight from the ISO string rather than through `toLocaleString` or a
 *  "3 hours ago". Both of those read the clock or the timezone, which differ between the server
 *  render and the browser one, and React replaces the whole subtree when they disagree. This is
 *  the same characters everywhere, which is what a timestamp on a row needs to be. */
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function startedLabel(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso ?? "");
  if (!m) return "";
  const month = MONTHS[Number(m[2]) - 1] ?? m[2];
  return `${Number(m[3])} ${month} ${m[4]}:${m[5]}Z`;
}

function byLiveness(a: Run, b: Run): number {
  const live = (r: Run) => (LIVE.includes(r.status) ? 0 : 1);
  if (live(a) !== live(b)) return live(a) - live(b);
  return (b.created_at ?? b.updated_at ?? "").localeCompare(a.created_at ?? a.updated_at ?? "");
}

/** A step on the canvas. `kind` is "capability" for the work steps and a control kind
 *  ("timer", "decision", "subworkflow") for the blocks the runtime has always supported. */
type Draft = {
  id: string;
  kind: string;
  capability?: string;
  /** Set instead of `capability` when the step is one the customer defined. */
  customStep?: string;
  /** HOW THIS STEP'S INPUTS ARE WIRED, exactly as the saved spec has them.
   *
   *  Dropped on load and rebuilt on save, every input became `{from_input: <name>}` — so
   *  opening a workflow whose steps feed each other and pressing Save replaced
   *  `{from_step: "fetch-link", field: "media_artifact_id"}` with a demand that the operator
   *  type a media artifact id by hand. The chain was destroyed by viewing and saving, with
   *  nothing on screen having been changed. */
  inputs?: Record<string, unknown>;
  /** WHICH AGENT RUNS THIS STEP. A capability step's captain has always become the actor; a
   *  custom step ignored it, so a customer's workflow had no agent in it anywhere. */
  captain?: string;
  /** THIS STEP'S WHOLE SPECIFICATION, when it is an agent step.
   *
   *  Every other kind of step binds a skill and BOSS decides what happens. This one says what is
   *  WANTED and the agent works out how, using the skills it holds. That is what stops the
   *  product needing a release per use case: a new kind of request needs the agent to hold a
   *  skill, not BOSS to ship a step.
   *
   *  Either a run input the operator fills in at the start ({from_input}), or a literal pinned
   *  here for a workflow that always does the same thing. */
  agentWork?: boolean;
  instructions?: unknown;
  approval?: string;
  config?: Record<string, unknown>;
};

type Block = {
  kind: string;
  label: string;
  summary: string;
  canvas_required: boolean;
  defaults?: Record<string, unknown>;
  configure?: { field: string; label: string; type: string }[];
};

/** What a workflow WILL DO — the plan, not the graph. Built by `workflow_explain` and shared
 *  with the chat agent's `describe_workflow`, so the console and the agent cannot drift into
 *  describing the same workflow two different ways. */
type PlanStep = {
  position: number;
  id: string;
  label: string;
  /** The capability's own sentence. A step called "Document" says nothing on its own. */
  does: string;
  capability: string | null;
  /** The skill or MCP tool this actually executes. */
  runs: string | null;
  /** The agent that answers for this step. */
  agent: string | null;
  approval: string;
  approval_is_forced: boolean;
  approval_note: string;
  risk_tier: number | null;
  typical_minutes: number | null;
  inputs: { name: string; source: string; ready: boolean }[];
  produces: string[];
};

type WorkflowPlan = {
  name: string;
  version: number | null;
  published: boolean;
  summary: string;
  steps: PlanStep[];
  you_must_supply: string[];
  results: string[];
  agents: string[];
  gates: number;
  typical_minutes?: number;
  can_start?: boolean;
  blocked_because?: string[];
};

type WorkflowSpec = {
  definition_id: string;
  workflow_key: string;
  name: string;
  is_catalogue: boolean;
  version: number;
  spec: { entry_step?: string; steps: Record<string, unknown>[] };
  forked_from: { workflow_key: string; name: string; version: number;
                 spec: { steps: Record<string, unknown>[] } } | null;
};

//: THE FOUR THINGS AN OPERATOR DOES HERE, IN THE ORDER THEY DO THEM.
//:
//: "Build your Own, Design one with BOSS all should come in one page and it's hard to find.
//:  Workflow step should be separate page."
//:
//: `design` holds BOTH ways to author a workflow, because they are the same intent arrived at
//: differently — describe it and have BOSS draft it, or draw it yourself. They were split
//: across a tab and a button that sat IN the tab bar without being one, which is why the agent
//: was hard to find: it looked like a tab, behaved like an action, and was neither.
//:
//: `steps` is management of the customer's own steps — defining and retiring them. It was
//: buried in the builder's palette, below however many steps already existed, so the way to
//: make one was the thing you had to scroll to find. The palette still LISTS them, because
//: that is where you place one; it no longer tries to be where you author one too.
const TABS = ["running", "yours", "catalogue", "design", "steps"] as const;
type Tab = (typeof TABS)[number];

/**
 * The builder's glyphs.
 *
 * Same approach as the nav's: inline SVG on a 16px grid, `currentColor` throughout, so an icon
 * dims and brightens with the label beside it instead of needing a second set of active rules.
 * A whole icon package for eleven glyphs is a lot of bytes for eleven glyphs.
 *
 * Each says what the control DOES rather than decorating it — import is an arrow landing in a
 * tray, reset is an arrow going back, create is a check. The words stay: an icon-only control
 * in a builder is a guessing game.
 */
const WF_ICONS: Record<string, React.ReactNode> = {
  // Editing — a pencil on a line.
  edit: <><path d="M11.3 2.2a1.7 1.7 0 0 1 2.4 2.4L6 12.3l-3.2.9.9-3.2 7.6-7.8Z" /><path d="M10.2 3.4l2.4 2.4" /></>,
  // New — a plus, boxed, because it makes a new thing rather than adding to this one.
  plus: <><rect x="1.8" y="1.8" width="12.4" height="12.4" rx="3" /><path d="M8 5.2v5.6M5.2 8h5.6" /></>,
  // Import — steps landing on the canvas.
  importIn: <><path d="M8 1.8v7.4" /><path d="M5.2 6.6 8 9.4l2.8-2.8" /><path d="M2.4 10.6v1.8a1.8 1.8 0 0 0 1.8 1.8h7.6a1.8 1.8 0 0 0 1.8-1.8v-1.8" /></>,
  // Import again — the same arrow, going round.
  again: <><path d="M13.6 8a5.6 5.6 0 1 1-1.7-4" /><path d="M13.8 1.9v3.4h-3.4" /></>,
  // Create — it exists now.
  check: <><path d="M2.6 8.4 6.2 12l7.2-8" /></>,
  // Cancel.
  x: <><path d="M4 4l8 8M12 4l-8 8" /></>,
  // Save a new version — a document with a tick.
  save: <><path d="M3.2 2.4h6.2l3.4 3.4v7.8a1 1 0 0 1-1 1H3.2a1 1 0 0 1-1-1V3.4a1 1 0 0 1 1-1Z" /><path d="M9.2 2.4v3.6h3.6" /><path d="M5.4 10.2 7 11.8l3-3.4" /></>,
  // Reset — back to how it was.
  reset: <><path d="M2.4 8a5.6 5.6 0 1 0 1.7-4" /><path d="M2.2 1.9v3.4h3.4" /></>,
  // A fact, not a warning.
  info: <><circle cx="8" cy="8" r="6.2" /><path d="M8 7.4v3.4M8 5.2h.01" /></>,
  // Steps — the shape the canvas draws.
  steps: <><rect x="2.2" y="2.2" width="11.6" height="3.4" rx="1.2" /><rect x="2.2" y="10.4" width="11.6" height="3.4" rx="1.2" /><path d="M8 5.6v4.8" /></>,
  // An empty canvas.
  empty: <><rect x="1.8" y="3" width="12.4" height="10" rx="2" strokeDasharray="2.6 2.2" /><path d="M8 6.6v2.8M6.6 8h2.8" /></>,
};

function WfIcon({ id }: { id: keyof typeof WF_ICONS | string }) {
  const d = WF_ICONS[id];
  // No placeholder box for an unknown id: an empty square beside a label reads as a broken
  // image, while the label on its own reads as a control without an icon, which is what it is.
  if (!d) return null;
  return (
    <svg className="dw-icon" viewBox="0 0 16 16" width="15" height="15" fill="none"
         stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"
         aria-hidden focusable="false">
      {d}
    </svg>
  );
}

/** One value from a step's output, short enough for a single line. */
function summarise(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value.slice(0, 60);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? "" : "s"}`;
  if (typeof value === "object") {
    const keys = Object.keys(value as Record<string, unknown>);
    // Prefer an id or a url if the object carries one — that is the thing an operator wants
    // to click, and it is buried one level down often enough to be worth looking for.
    const useful = keys.find((k) => /(^|_)(id|url)$/.test(k));
    if (useful) return String((value as Record<string, unknown>)[useful]).slice(0, 60);
    // THE CONTENT, NOT THE SHAPE. This printed the KEY NAMES — a deploy's result showed as
    // "{health, status, applied}" under a heading reading DETAIL, which tells an operator
    // that three facts exist and none of what they say. Scalars are what fit on a line, so
    // show those as key=value and keep the key list only for an object made entirely of
    // nested objects, where there is genuinely nothing short to say.
    const scalars = keys
      .filter((k) => {
        const v = (value as Record<string, unknown>)[k];
        return v !== null && v !== undefined && typeof v !== "object";
      })
      .slice(0, 3)
      .map((k) => `${k}=${String((value as Record<string, unknown>)[k]).slice(0, 24)}`);
    if (scalars.length) return scalars.join(" · ").slice(0, 90);
    return keys.length ? `{${keys.slice(0, 3).join(", ")}}` : "{}";
  }
  return String(value).slice(0, 60);
}

/** Colour AND a word. A status column read only by hue does not survive a screenshot. */
function tone(status: string): string {
  if (status === "succeeded" || status === "completed") return "ok";
  if (status === "failed" || status === "deadlettered") return "bad";
  if (status === "waiting" || status === "blocked") return "wait";
  if (status === "running" || status === "leased" || status === "ready") return "live";
  return "idle";
}

async function get<T>(view: string, projectId: string): Promise<T | { error: string }> {
  const params = new URLSearchParams({ view });
  if (projectId) params.set("project_id", projectId);
  try {
    const r = await fetch(`/api/delivery-workflows?${params}`, { cache: "no-store" });
    const body = await r.json();
    return r.ok ? body : { error: body?.error ?? `API returned ${r.status}` };
  } catch (e) {
    return { error: String((e as Error)?.message ?? e) };
  }
}

/** The operator's presses on this panel: one at a time, the one running named by `busy`.
 *  ONLY presses. The automatic recheck below is not one — when it shared this scope, of several
 *  runs ready to continue only the first was put back, and a Retry pressed during it vanished. */
const PANEL_PRESSES = "delivery-workflows-panel";

/** POST one run action. `key` is the press's Idempotency-Key when a press sends it; without
 *  one (a background recheck) apiWrite mints its own and takes no press scope. */
async function post(action: string, payload: Record<string, unknown>, key?: string) {
  try {
    const r = await apiWrite("/api/delivery-workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...payload }),
    }, { key });
    const body = await r.json();
    return r.ok ? { ok: true as const, data: body } : { ok: false as const, error: body?.error };
  } catch (e) {
    return { ok: false as const, error: String((e as Error)?.message ?? e) };
  }
}

export function DeliveryWorkflows({ projectId, actor }: { projectId: string; actor: string }) {
  const [tab, setTab] = useState<Tab>("running");
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [riskFloor, setRiskFloor] = useState(2);
  /** The agents a step can be given to. Taken from the capabilities payload rather than the
   *  agent directory, because THIS list is the one the publish-time captain check validates
   *  against — offering a role that would then be refused is worse than offering none. */
  const [stepRoles, setStepRoles] = useState<string[]>([]);
  const [catalogue, setCatalogue] = useState<CatalogueEntry[]>([]);
  const [installed, setInstalled] = useState<Installed[]>([]);
  const [customSteps, setCustomSteps] = useState<CustomStep[]>([]);
  const [bindable, setBindable] = useState<Bindable>({ skills: [], mcp_servers: [] });
  const [runs, setRuns] = useState<Run[]>([]);
  // WHERE THIS PROJECT CAN SHIP. Fetched once for the release gate: choosing a target is part
  // of the deploy decision, and a gate that only says yes/no cannot express "yes, to staging".
  const [targets, setTargets] = useState<DeployTarget[]>([]);
  const [hosts, setHosts] = useState<DeployHost[]>([]);
  // EVERY AGENT THIS PROJECT HAS, so "assign" can offer them by name rather than by role_key.
  // One fetch for the whole screen: a picker per run would be one request per run.
  const [roles, setRoles] = useState<Array<{ role_key: string; display_name?: string | null }>>([]);
  /** WHICH AGENT HOLDS WHICH SKILL, `role_key` → skill ids.
   *
   *  Needed because naming an agent on a step is only half the decision. The other half is
   *  whether that agent is allowed to do the thing — `skill_runtime` checks `role_skills` at run
   *  time and refuses if the grant is missing. Without this the builder happily let someone put
   *  an agent on a tier-3 publish step, save it, start it, wait for the approval, and only then
   *  be told the agent could not call it. One fetch; the whole project's assignments. */
  const [holdings, setHoldings] = useState<Record<string, string[]>>({});
  const loadHoldings = useCallback(async () => {
    try {
      const r = await fetch(
        `/api/skills?view=assignments&project_id=${encodeURIComponent(projectId)}`,
        { cache: "no-store" });
      if (!r.ok) return;
      const body = await r.json();
      const next: Record<string, string[]> = {};
      for (const a of (body?.assignments ?? []) as Array<{ role_key: string; skill_id: string }>) {
        (next[a.role_key] ??= []).push(a.skill_id);
      }
      setHoldings(next);
    } catch { /* the builder still works; it just cannot warn about a missing grant */ }
  }, [projectId]);
  useEffect(() => { void loadHoldings(); }, [loadHoldings]);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await fetch(`/api/agent?view=roles&project_id=${encodeURIComponent(projectId)}`,
                              { cache: "no-store" });
        if (!r.ok) return;
        const body = await r.json();
        if (alive) setRoles((body.items ?? body.roles ?? []) as typeof roles);
      } catch { /* the panel still lists who is assigned; it just cannot offer new ones */ }
    })();
    return () => { alive = false; };
  }, [projectId]);

  /* RE-READ AFTER BINDING — the reason "Set target" looked like it had done nothing.
   *
   * This list was fetched once per project and never again. So the operator pressed Set target,
   * the target really was created (deptgt_ee5dae79, 'Onboarding Path Builder.', active), and
   * the step went on rendering the same empty form, because the only evidence that a target now
   * exists lives in this array. Their reasonable conclusion was that the button had not worked,
   * and the reasonable next action is to press it again — which is exactly the "he may click
   * set target multiple times" the operator described. */
  const reloadTargets = useCallback(async () => {
    try {
      const res = await fetch(
        `/api/deployment-targets?project_id=${encodeURIComponent(projectId)}`,
        { cache: "no-store" });
      if (!res.ok) return;
      const body = await res.json();
      setTargets((body.items ?? []).filter(
        (t: DeployTarget) => (t.status ?? "active") === "active"));
    } catch { /* the gate still works; it just cannot offer a target */ }
  }, [projectId]);

  useEffect(() => {
    let alive = true;
    // TWO INDEPENDENT LOADS, AND THEY MUST STAY INDEPENDENT.
    //
    // These were two try/catch blocks in ONE async function, each ending `if (!res.ok) return;`
    // — and `return` does not skip the rest of a block, it leaves the whole function. So when
    // /api/deployment-targets answered 503, the hosts fetch NEVER RAN. `hosts` stayed empty,
    // and the deploy step told the operator "this project has no verified host. Add one under
    // Infra and Models" while Infra showed hetzner-ci-01 VERIFIED and READY TO DEPLOY. They
    // reported it perhaps ten times; every fix went to the message, the picker and the wire
    // format, and the request that would have populated it was never being made.
    //
    // Separate functions, started separately: one endpoint being down cannot silence the
    // other. The catch blocks always intended this; the early `return` quietly undid it.
    const loadTargets = async () => {
      try {
        const res = await fetch(
          `/api/deployment-targets?project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (!res.ok) return;
        const body = await res.json();
        if (alive) setTargets((body.items ?? []).filter(
          (t: DeployTarget) => (t.status ?? "active") === "active"));
      } catch { /* the gate still works; it just cannot offer a target */ }
    };
    const loadHosts = async () => {
      try {
        const res = await fetch(
          `/api/deployment-hosts?project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (!res.ok) return;
        const body = await res.json();
        if (alive) setHosts(verifiedHostsFrom(body));
      } catch { /* no hosts offered; the operator can still deny */ }
    };
    void loadTargets();
    void loadHosts();
    return () => { alive = false; };
  }, [projectId]);
  // Loading, failed and empty are three different things. A screen that renders nothing while
  // it loads and nothing when the API is down says "you have no workflows" in both cases.
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // A PREVIEW IS ASKED FOR IN A SECOND AND ARRIVES IN A MINUTE.
  //
  // `act` clears `busy` when the POST returns, and start_preview returns "starting" straight
  // away — so the button flashed "Starting…" for about a second, went back to normal, and the
  // notice said "Preview — done." while nothing was serving yet. From the operator's side the
  // click did nothing, which is an invitation to click it again and boot a second copy.
  //
  // This holds the build's id from the click until a preview URL actually shows up (or the
  // wait is plainly too long), and the poll below refreshes so it appears without another click.
  const [previewStarting, setPreviewStarting] = useState<{ runId: string; at: number } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [openRun, setOpenRun] = useState<string | null>(null);
  // ONCE, on the first load that has runs. `useRef` rather than a dependency on `openRun`,
  // because the list re-polls every few seconds: re-deciding each time would reopen a run the
  // operator had just closed, which is a page that argues with you.
  const openedOnce = useRef(false);
  // START A PROJECT IS THE SMALLER JOB ON THIS PAGE, and it was the whole top of it — a
  // composer above the fold, pushing what is actually running below it. Collapsed to one line
  // unless there is nothing to look at yet, in which case an empty page with a closed box is
  // just a dead end.
  const [composerOpen, setComposerOpen] = useState(false);
  /** The workflow "Start one" chose, so the composer opens on it rather than on a blank path
   *  question the operator has already answered by pressing a button on that workflow's card. */
  const [startWith, setStartWith] = useState<string | undefined>(undefined);
  // Lifted out of the builder so forking can land the operator on the thing they just made.
  const [builderTarget, setBuilderTarget] = useState<string>("");
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [scheduleDraft, setScheduleDraft] = useState<ScheduleDraft | null>(null);
  // WHAT WAS JUST MADE, so the builder can SAY it was made.
  //
  // Import worked and looked like it had not: the form closed, the toolbar came back looking
  // exactly as before, and the only confirmation was a sticky line 104px up the page that is
  // easy to miss entirely. Returning to a toolbar that looks unchanged reads as "cancelled",
  // which is how a working feature got reported as broken.
  const [justMade, setJustMade] = useState<{ id: string; name: string; from?: string } | null>(null);

  const load = useCallback(async () => {
    const [caps, cat, inst, live, mine, sched] = await Promise.all([
      get<{ capabilities: Capability[]; blocks: Block[]; manual_approval_risk_floor: number }>(
        "capabilities", projectId),
      get<{ items: CatalogueEntry[] }>("catalogue", projectId),
      get<{ items: Installed[] }>("installed", projectId),
      get<{ runs: Run[] }>("runs", projectId),
      get<{ steps: CustomStep[]; bindable: Bindable }>("custom_steps", projectId),
      get<{ schedules: Schedule[] }>("schedules", projectId),
    ]);
    // SCHEDULES ARE NOT LOAD-BEARING. An install whose schedule table predates this feature
    // must still show its workflows, so a failure here empties the list rather than failing
    // the screen — unlike the five above it, without which there is nothing to draw.
    const failure = [caps, cat, inst, live, mine].find((r) => "error" in r) as { error: string } | undefined;
    if (failure) {
      setState("failed");
      setError(failure.error);
      return;
    }
    setSchedules(("error" in (sched as object))
      ? [] : ((sched as { schedules?: Schedule[] }).schedules ?? []));
    setCapabilities((caps as { capabilities: Capability[] }).capabilities ?? []);
    setBlocks((caps as { blocks?: Block[] }).blocks ?? []);
    setRiskFloor((caps as { manual_approval_risk_floor: number }).manual_approval_risk_floor ?? 2);
    setStepRoles((caps as { roles?: string[] }).roles ?? []);
    setCatalogue((cat as { items: CatalogueEntry[] }).items ?? []);
    setInstalled((inst as { items: Installed[] }).items ?? []);
    setRuns((live as { runs: Run[] }).runs ?? []);
    setCustomSteps((mine as { steps?: CustomStep[] }).steps ?? []);
    setBindable((mine as { bindable?: Bindable }).bindable
                ?? { skills: [], mcp_servers: [] });
    setState("ready");
    setError(null);
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);
  // A target set on a Factory card, or an action BOSS ran in the dock, changes what this screen
  // is showing. Reload rather than wait for the next poll.
  useEffect(() => onChanged(() => { void load(); }, "workflows"), [load]);

  // ARRIVING AT THIS SCREEN RE-TESTS WHAT STOPPED THINGS.
  //
  // A failed step's error is a record of a past attempt, and this page rendered it as a
  // statement about now — so after the missing deployment target was created, the Deploy step
  // went on saying in red that the app had nowhere to ship to, on a run marked dead-lettered.
  // The operator's words: "not sure why the workflow is not updated with the latest... it
  // should automatically refresh when we get to the workflow screen again".
  //
  // The read path already reports, per step, whether the recorded blocker still holds. When it
  // does not, this puts the step back without being asked. Safe to be automatic: `recheck`
  // re-queues ONLY steps whose blocker has actually cleared, and re-queuing an already-queued
  // step is a no-op.
  const rechecked = useRef<Set<string>>(new Set());
  useEffect(() => {
    const stale = runs.filter((r) => (r.steps ?? []).some(
      (st) => (st as RunStep & { blocker?: { cleared?: boolean } }).blocker?.cleared));
    const todo = stale.filter((r) => !rechecked.current.has(r.id));
    if (!todo.length) return;
    // Once per run per mount. Without the guard the reload below re-triggers this effect and
    // the console re-checks in a loop for as long as it is open.
    todo.forEach((r) => rechecked.current.add(r.id));
    void (async () => {
      // ALL AT ONCE, AND NOT A PRESS. These are background writes: no press scope, so one run's
      // recheck cannot refuse another's, and none can refuse a button the operator presses.
      const results = await Promise.all(todo.map((r) =>
        post("recheck", { project_id: projectId, actor, run_id: r.id })));
      // A recheck that did not get through is tried again on the next poll, not written off for
      // as long as the page is open.
      results.forEach((r, i) => { if (!r.ok) rechecked.current.delete(todo[i].id); });
      const moved = results.reduce((n, r) => n + (
        (r.ok ? ((r.data as { requeued?: unknown[] })?.requeued ?? []).length : 0)), 0);
      if (moved) {
        setNotice(`${moved} step${moved === 1 ? "" : "s"} put back — what stopped ` +
                  `${moved === 1 ? "it" : "them"} has been dealt with since.`);
        await load();
      }
    })();
  }, [runs, projectId, actor, load]);

  // KEEP ASKING, EVEN WHEN NOTHING IS RUNNING.
  //
  // This used to stop polling when no run was live — "an idle console is idle". But a console
  // cannot know that work has STARTED without asking: a project kicked off from the Command
  // Center, from the Factory form, or by approving a PRD creates its run somewhere else
  // entirely. Anyone who had this page open when the only run was blocked saw a frozen list
  // and no sign of the project they had just started, because the page never asked again.
  //
  // Fast while something is in flight, slower when idle — idle still means "watch for work
  // appearing", not "stop looking".
  const anyLive = runs.some((r) => LIVE.includes(r.status));
  useEffect(() => {
    const t = window.setInterval(() => { void load(); }, anyLive ? 8000 : 15000);
    return () => window.clearInterval(t);
  }, [anyLive, load]);

  // A RUN THAT IS ON ITS WAY. Arriving from Build, the run may not be in the list yet — it is
  // created by the press, and this page had loaded before it. Poll fast until it shows rather
  // than presenting an empty list, which reads as "nothing happened".
  const awaited = openRun && !runs.some((r) => r.id === openRun) ? openRun : null;
  useEffect(() => {
    if (!awaited) return;
    const t = window.setInterval(() => { void load(); }, 2000);
    return () => window.clearInterval(t);
  }, [awaited, load]);

  // SENT HERE TO START SOMETHING. `?new=1` is a one-shot command, consumed on read so a later
  // navigation cannot re-deliver it — the same rule the Factory form learned the hard way.
  useEffect(() => {
    const read = () => {
      const intent = readNewProjectIntent(window.location.search);
      if (!intent.open) return;
      setTab("running");
      window.setTimeout(() => {
        document.querySelector<HTMLTextAreaElement>('[data-testid="dw-new-directive"]')?.focus();
      }, 0);
      try {
        window.history.replaceState(null, "",
          `${window.location.pathname}${intent.nextSearch}${window.location.hash}`);
      } catch { /* non-browser — the composer is on screen either way */ }
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  // OPEN THE RUN THE OPERATOR WAS SENT HERE TO SEE. Creating a project redirects here with
  // `?wfr=<id>`; expanding it on arrival is the difference between "your project is somewhere
  // in this list" and showing them the steps.
  useEffect(() => {
    const read = () => {
      const wanted = new URLSearchParams(window.location.search).get("wfr");
      if (wanted) setOpenRun(wanted);
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  // Poll while a preview is coming up, so the URL appears on its own. Bounded: after three
  // minutes we stop claiming it is on its way and let the step's own state speak.
  useEffect(() => {
    if (!previewStarting) return;
    const timer = window.setInterval(() => {
      if (Date.now() - previewStarting.at > 180_000) { setPreviewStarting(null); return; }
      void load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [previewStarting, load]);

  // ONE PRESS AT A TIME, AND A REFUSED PRESS IS SAID OUT LOUD.
  //
  // `busy` disables the buttons only after React re-renders, so a quick second click still
  // fired: "Stop and run again" pressed twice started two workflows. Every press on this panel
  // goes through `pressOn`, which holds the panel's press scope in lib/api-write's one registry
  // (registered before any await) and is the ONLY thing that sets `busy`. The same press again
  // joins the running one; a different press is refused by the helper, which says so in the
  // operator's words ("Retry was not sent — Stop is still in progress") instead of the old
  // `return { error: "already running" }` that every button threw away.
  type PressResult<T> = T | { ok: false; error: string; refused: true };
  const pressOn = useCallback(<T,>(label: string, what: unknown,
                                   run: (key: string) => Promise<T>): Promise<PressResult<T>> =>
    runPress<T>({ press: { scope: PANEL_PRESSES, label }, fingerprint: JSON.stringify([label, what]) },
      async (key) => {
        setBusy(label);
        try {
          return await run(key);
        } finally {
          // In a finally so a throw cannot leave the console stuck showing "Advancing…" forever.
          setBusy(null);
        }
      }).catch((error: unknown) => {
        if (error instanceof WriteInFlightError) {
          return { ok: false as const, error: error.message, refused: true as const };
        }
        throw error;
      }), []);

  const act = useCallback((label: string, action: string, payload: Record<string, unknown>) =>
    pressOn(label, [action, payload], async (key) => {
      setNotice(null);
      const result = await post(action, { project_id: projectId, actor, ...payload }, key);
      // The engine's own sentence, not "something went wrong". "step 'd' is risk tier 2 and
      // always requires a person" is the whole value of the validation.
      setNotice(result.ok ? `${label} — done.` : `${label} — ${result.error}`);
      // The Factory card and the deployment panels render the same rows this just changed.
      if (result.ok) notifyChanged("workflows", { action });
      // RELOAD WHILE STILL BUSY. `busy` was cleared BEFORE this, so every action ended the
      // same way: the button snapped back to its normal label and the console then spent
      // seconds re-fetching with nothing on screen to say so. From the operator's side that
      // is a click that did nothing — reported as "I clicked and there's no indicator".
      if (result.ok) await load();
      return result;
    }), [pressOn, projectId, actor, load]);

  // OPEN THE ONE THAT WANTS YOU. A page whose every run is collapsed makes an operator click
  // to find out whether anything needs them, which is the question the page exists to answer.
  useEffect(() => {
    if (openedOnce.current || !runs.length) return;
    openedOnce.current = true;
    setOpenRun((current) => current ?? runToOpen(runs));
  }, [runs]);

  if (state === "loading") {
    return <p className="detail" data-testid="dw-loading">Loading delivery workflows…</p>;
  }
  if (state === "failed") {
    return (
      <p className="detail" style={{ color: "#f85149" }} data-testid="dw-error">
        Could not load delivery workflows — {error}
      </p>
    );
  }

  return (
    <div className="dw" data-testid="delivery-workflows">
      <div className="dw-tabs" role="tablist" aria-label="Delivery workflows">
        {TABS.map((t) => (
          <button key={t} type="button" role="tab" aria-selected={tab === t}
                  className="dw-tab" data-active={tab === t}
                  onClick={() => setTab(t)} data-testid={`dw-tab-${t}`}>
            {t === "running" ? `Running (${runs.length})`
              : t === "yours" ? `Yours (${ownWorkflows(installed).length})`
              // The catalogue counts what BOSS ships, and only that. It used to add the
              // customer's own to the total, so the number answered no question anybody had.
              : t === "catalogue" ? `Catalogue (${catalogue.length})`
              : t === "design" ? "Design a workflow"
              : `Your steps (${customSteps.length})`}
          </button>
        ))}
      </div>

      {/* WHAT IS HAPPENING RIGHT NOW. Per-button labels are easy to forget on the next
          button added, and they say nothing during the reload that follows the request. This
          is one indicator every action gets for free, for as long as the action is running. */}
      {busy ? (
        <p className="dw-notice dw-working" data-testid="dw-working" role="status" aria-live="polite">
          <span className="dw-spin" aria-hidden />
          {busy}… <span className="detail">working — the list refreshes when it finishes</span>
        </p>
      ) : notice ? (
        <p className="dw-notice" data-testid="dw-notice">{notice}</p>
      ) : null}

      {tab === "running" ? (
        composerOpen || runs.length === 0 ? (
          <div className="dw-composer-open">
            {runs.length ? (
              <button type="button" className="linklike dw-composer-hide"
                      data-testid="dw-composer-hide"
                      onClick={() => { setComposerOpen(false); setStartWith(undefined); }}>Hide</button>
            ) : null}
            <NewProjectComposer
              projectId={projectId} actor={actor} workflows={installed} catalogue={catalogue}
              startWith={startWith}
              // KEYED ON THE CHOICE, so pressing "Start one" on a different workflow rebuilds
              // the composer rather than leaving it on the first one's inputs.
              key={startWith ?? "blank"}
              onStarted={(runId) => {
                if (runId) setOpenRun(runId);
                setComposerOpen(false);
                setStartWith(undefined);
                setNotice(runId ? `Started ${runId}.` : "Started.");
                void load();
              }}
            />
          </div>
        ) : (
          <button type="button" className="dw-composer-trigger" data-testid="dw-composer-open"
                  onClick={() => setComposerOpen(true)}>
            <span className="dw-composer-plus" aria-hidden>+</span>
            Start a project
            <span className="detail">
              describe an outcome, choose a workflow, and follow its progress
            </span>
          </button>
        )
      ) : null}
      {tab === "running" && awaited ? (
        <p className="dw-awaiting detail" data-testid="dw-awaiting">
          Starting <code>{awaited}</code>… the run appears here the moment it is created.
        </p>
      ) : null}
      {tab === "running" ? (
        <RunsView
          projectId={projectId}
          runs={runs} openRun={openRun} setOpenRun={setOpenRun} busy={busy}
          capabilities={capabilities} targets={targets} hosts={hosts}
          actor={actor} roles={roles}
          // SAY WHAT IT DID. "Advance now" reported success while advancing nothing — a run
          // waiting on a person cannot be ticked forward — and a button that always says
          // "done" is indistinguishable from a button that does nothing.
          onAdvance={() => void pressOn("Advance", null, async (key) => {
            const r = await post("advance", { project_id: projectId, actor }, key);
            const moved = ((r.ok ? (r.data as { performed?: unknown[] })?.performed : []) ?? []);
            setNotice(!r.ok ? `Advance — ${r.error}`
              : moved.length ? `Advanced ${moved.length} step${moved.length === 1 ? "" : "s"}.`
              : "Nothing to advance — every run is finished or waiting on a person.");
            if (r.ok) await load();
          })}
          onRetryStep={(runId, stepId) =>
            act("Retry", "retry_step", { run_id: runId, step_id: stepId })}
          // SKIP AND PROCEED. A failed step stops the run; going on without it is a decision,
          // so the step asks why (inline, beside the button) and the reason is kept on the step
          // and in the run's history.
          onSkipStep={(runId, stepId, reason) =>
            act("Skip", "skip_step", { run_id: runId, step_id: stepId, reason })}
          onSetTarget={(runId, targetId) =>
            act("Set target", "set_input", { run_id: runId, values: { target_id: targetId } })}
          onPrepareTarget={(runId, hostId) =>
            act("Prepare target", "prepare_target",
                { run_id: runId, host_id: hostId, environment: "staging" })}
          onRunPreview={(buildRunId) => {
            setPreviewStarting({ runId: buildRunId, at: Date.now() });
            // "Starting a preview" — not "done". The old label claimed the work had finished
            // when only the request had been accepted.
            return act("Starting a preview", "start_preview", { run_id: buildRunId });
          }}
          previewStarting={previewStarting?.runId ?? ""}
          // Both: the run's input changes AND the target list gains a row. Reloading only the
          // runs left the step believing there was still nowhere to ship.
          onBound={() => { void load(); void reloadTargets(); }}
          onRecheck={(run) => act("Re-check", "recheck", { run_id: run.id })}
          onStop={(run) => act("Stop", "stop", {
            run_id: run.id, reason: "stopped by the operator from the console",
          })}
          onDelete={async (run) => {
            // ASKED, BECAUSE IT CANNOT BE UNDONE. The only irreversible thing on this screen.
            if (!window.confirm(
              `Remove this ${run.workflow_name} run for good?\n\n`
              + "Its steps and history go with it. Anything it produced — files, issues, "
              + "anything published — is untouched. This cannot be undone.")) return;
            await pressOn("Remove", run.id, async (key) => {
              setNotice(null);
              const query = new URLSearchParams({
                project_id: projectId, actor, run_id: run.id,
              });
              const response = await apiWrite(`/api/delivery-workflows?${query}`,
                                              { method: "DELETE" }, { key });
              const body = await response.json().catch(() => ({}));
              setNotice(response.ok
                ? "Removed."
                : `Remove — ${(body as { error?: string }).error ?? response.status}`);
              if (response.ok) { setOpenRun(null); await load(); }
            });
          }}
          onRerun={(run) => act("Re-run", "start", {
            workflow_key: run.workflow_key, input: inputForRerun(run.input),
            // One fresh run per press; a retried or doubled request returns the same run.
            dedupe_key: `again:${run.id}:${crypto.randomUUID()}`,
          })}
          // STOP, THEN START — in that order and as ONE press.
          //
          // "Run this workflow again does not stop the current run and start from the
          // beginning." It did not, by design: re-running while a run is live makes a SECOND
          // workflow for the same project, which is how three ended up going at once. The
          // screen's own advice was "cancel this run first", for a control it did not offer.
          // Now it offers it, and this does both — the stop has to land before the start, or
          // the new run joins the old one instead of replacing it.
          onStopAndRerun={async (run) => {
            const stopped = await act("Stop", "stop", {
              run_id: run.id, reason: "replaced by a fresh run from the console",
            });
            // ONLY START IF THE STOP LANDED. Starting after a failed cancel is the exact
            // outcome this button exists to prevent: two live runs on one project.
            if (!stopped?.ok) return;
            await act("Re-run", "start", {
              workflow_key: run.workflow_key, input: inputForRerun(run.input),
              // A RUN IS REPLACED ONCE. However often this is pressed for run X, the server
              // returns the same replacement — the second press no longer starts a second one.
              dedupe_key: `replace:${run.id}`,
            });
          }}
          // BUILD ON WHAT THIS ALREADY FOUND. The server resolves the dossier from the run and
          // sets the adopt key itself — a client may not assert that work has happened. What
          // travels is the evidence, not the panel: a Research-Led Discovery panel answered a
          // go/no-go, so the delivery run holds its own and argues FROM the findings.
          onBuildOnThis={(run) => act("Build on this research", "start", {
            // NAME THE WORKFLOW, NOT THE CLASS. `project.new` is shared by Research-Led
            // Discovery (once applied as the default), Backend Service Setup, and Standard
            // Product Delivery. Sending only the class either restarts discovery — the thing
            // this button exists to leave behind — or 409s when nothing is mapped yet, which
            // is every project that started research by key. The label promises Standard
            // Product Delivery; the payload has to.
            workflow_key: "standard_product_delivery",
            work_class: "project.new", from_run: run.id,
            input: inputForRerun(run.input),
          })}
        />
      ) : tab === "yours" ? (
        <YoursView
          installed={installed} schedules={schedules} busy={busy}
          onStart={(key, workClass) => {
            // A WORKFLOW THAT DECLARES INPUTS CANNOT BE STARTED BLIND.
            //
            // `sample: true` is "surprise me": the API picks a sample directive for the
            // workflow's WORK CLASS, which exists for the catalogue's software-building paths.
            // On a workflow that asks for a video link and editing instructions it is refused
            // outright (409, no sample for this class) or — worse — injects a software brief
            // and the run dies on its first step with no video_url, having told the operator
            // nothing about why. Either way the operator never got to say what they wanted.
            //
            // So: if the workflow declared what it needs, open the form on the step that
            // collects it, with this workflow already chosen. Only a workflow that declares
            // nothing still starts on a sample, which is what that button was built for.
            const chosen = installed.find((w) => w.workflow_key === key);
            if (workflowInputFields(chosen?.input_schema).length) {
              setStartWith(key);
              setComposerOpen(true);
              setTab("running");
              return;
            }
            void act(`Start ${key}`, "start", { workflow_key: key, work_class: workClass,
                                                sample: true });
          }}
          onEditMine={(definitionId) => { setBuilderTarget(definitionId); setTab("design"); }}
          scheduleDraft={scheduleDraft}
          onOpenSchedule={(item, existing) => {
            // ASKED, NOT ASSUMED. Monday 08:00 is the common case and the zone is not
            // guessable — a report arriving at 3am because the browser's zone was used and
            // nobody said so is the failure this avoids. Required inputs are asked here too:
            // the schedule runs them unattended, so a button that stores only a time zone
            // cannot start a newsletter.
            setScheduleDraft({
              workflowKey: item.workflow_key,
              name: existing?.name ?? item.name,
              scheduleId: existing?.id ?? null,
              timezone: existing?.timezone || browserTimeZone(),
              values: scheduleFormValues(existing?.input),
            });
          }}
          onCancelSchedule={() => setScheduleDraft(null)}
          onChangeSchedule={(next) => setScheduleDraft(next)}
          onSubmitSchedule={async (draft) => {
            const chosen = installed.find((w) => w.workflow_key === draft.workflowKey);
            const fields = workflowInputFields(chosen?.input_schema);
            // REQUIRED INPUTS ARE STORED WITH THE SCHEDULE. Blank required fields stay
            // on the form; the API would refuse them, and a click that fails after the
            // operator thought it was saved is the gap this replaces.
            if (missingRequired(fields, draft.values).length || !draft.timezone.trim()) return;
            const existing = schedules.find((s) => s.id === draft.scheduleId);
            const result = await act(
              draft.scheduleId ? `Update schedule ${draft.name}` : `Schedule ${draft.name}`,
              "schedule",
              {
                workflow_key: draft.workflowKey,
                name: existing?.name ?? `${draft.name} — weekly`,
                cadence: existing?.cadence ?? "weekly",
                weekday: existing?.weekday ?? 0,
                hour: existing?.hour ?? 8,
                minute: existing?.minute ?? 0,
                timezone: draft.timezone.trim(),
                input: suppliedInputs(fields, draft.values),
                ...(draft.scheduleId ? { schedule_id: draft.scheduleId } : {}),
              });
            if (result.ok) setScheduleDraft(null);
          }}
          onToggleSchedule={async (id, enabled) => {
            await pressOn(enabled ? "Resume schedule" : "Pause schedule", id, async (key) => {
              const r = await apiWrite("/api/delivery-workflows", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "schedule_enabled", schedule_id: id,
                                       enabled, project_id: projectId, actor }),
              }, { key });
              const body = await r.json();
              if (!r.ok) { setError(body?.error ?? `API returned ${r.status}`); return; }
              await load();
            });
          }}
          onDeleteSchedule={async (id) => {
            await pressOn("Remove schedule", id, async (key) => {
              const r = await apiWrite(
                `/api/delivery-workflows?view=schedules&schedule_id=${encodeURIComponent(id)}`
                  + `&project_id=${encodeURIComponent(projectId)}`
                  + `&actor=${encodeURIComponent(actor)}`,
                { method: "DELETE" }, { key });
              const body = await r.json();
              if (!r.ok) { setError(body?.error ?? `API returned ${r.status}`); return; }
              await load();
            });
          }}
        />
      ) : tab === "catalogue" ? (
        <CatalogueView
          catalogue={catalogue} installed={installed} busy={busy}
          onInstall={() => act("Install the catalogue", "install", {})}
          onApply={(key, workClass) =>
            act(`Apply ${key}`, "apply", { workflow_key: key, work_class: workClass })}
          onStart={(key, workClass) =>
            // NOT A PLACEHOLDER. This sent {"directive": "Describe the work here"}, so the
            // panel debated that phrase, the spec review approved it and the builder tried to
            // build it — every step ran and none did anything. The API picks a real piece of
            // work for this workflow's CLASS, and refuses with a reason when the project
            // cannot supply one, which is a better demonstration than a run that does nothing.
            act(`Start ${key}`, "start", {
              workflow_key: key, work_class: workClass, sample: true,
            })}
          onFork={async (key) => {
            const entry = catalogue.find((c) => c.key === key);
            // A UNIQUE KEY PER FORK.
            //
            // `${key}_custom` collided the second time anyone forked the same workflow, and a
            // 409 with the notice scrolled off the top of a long page is indistinguishable
            // from a button that does nothing — which is exactly how this was reported.
            const taken = new Set(installed.map((i) => i.workflow_key));
            let candidate = `${key}_custom`;
            for (let n = 2; taken.has(candidate); n += 1) candidate = `${key}_custom_${n}`;
            const result = await act(`Fork ${entry?.name ?? key}`, "fork", {
              workflow_key: key,
              new_key: candidate,
              // Called what it is a copy OF. "(yours)" said nothing the row does not already
              // show — the console knows which workflows are catalogue templates and which are
              // the customer's own.
              new_name: uniqueWorkflowName(entry?.name ?? key, installed.map((i) => i.name)),
            });
            // IT IS CALLED "FORK TO EDIT", SO GO AND EDIT IT.
            //
            // On success the only visible change was a line of text at the top of the page:
            // the new workflow appears in the BUILDER's dropdown, on a tab the operator is not
            // looking at. Landing them in the editor with it selected is what the button says
            // it does.
            if (result.ok) {
              const created = (result.data as { definition?: { id: string } })?.definition;
              if (created?.id) setBuilderTarget(created.id);
              setTab("design");
            }
          }}
          // Same landing as a fork: select it in the builder and switch to that tab, so the
          // card and the "Editing" picker are two doors into one thing rather than two lists
          // that disagree about what exists.
          onEditMine={(definitionId) => { setBuilderTarget(definitionId); setTab("design"); }}
        />
      ) : tab === "steps" ? (
        <StepsView
          customSteps={customSteps} bindable={bindable} riskFloor={riskFloor}
          projectId={projectId} busy={busy}
          onDefineStep={async (fields) => {
            // A step_key means the form was editing one that exists. Different verb, different
            // route: the key lives in the PATH for an edit, which is what keeps it out of the
            // fields an edit can change — a renamed step must not orphan the workflows placing
            // it.
            if (fields.step_key) {
              return pressOn(`Save ${fields.label}`, fields, async (key) => {
                try {
                  const r = await apiWrite("/api/delivery-workflows", {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ ...fields, actor }),
                  }, { key });
                  const body = await r.json();
                  if (!r.ok) {
                    return { ok: false, error: String(body?.error ?? `API returned ${r.status}`) };
                  }
                  setNotice(`Saved ${fields.label}`);
                  await load();
                  return { ok: true };
                } catch (e) {
                  return { ok: false, error: String((e as Error)?.message ?? e) };
                }
              });
            }
            const result = await act(`Define ${fields.label}`, "define_step", fields);
            if (result.ok) await load();
            return result;
          }}
          onRemoveStep={async (stepKey) => {
            await pressOn(`Remove ${stepKey}`, stepKey, async (key) => {
              const r = await apiWrite(
                `/api/delivery-workflows?project_id=${encodeURIComponent(projectId)}`
                  + `&step_key=${encodeURIComponent(stepKey)}`
                  + `&actor=${encodeURIComponent(actor)}`,
                { method: "DELETE" }, { key },
              );
              const body = await r.json();
              if (!r.ok) { setError(body?.error ?? `API returned ${r.status}`); return; }
              setNotice(`Removed ${stepKey}`);
              await load();
            });
          }}
        />
      ) : (
        <>
        {/* TWO WAYS TO AUTHOR THE SAME THING, SIDE BY SIDE.
            Describing it is the faster path for most people and the one that reads as least
            intimidating, so it is first — but it is offered AS A CHOICE rather than as the
            answer, because an operator who knows the shape they want should not have to
            negotiate it in prose. */}
        <div className="dw-design-ways" data-testid="dw-design-ways">
          <article className="dw-design-way">
            <h3>Describe it</h3>
            <p className="detail">
              Tell BOSS the path you want in your own words. It drafts the workflow, names the
              steps, and offers it back to create in one press — and it can edit or retire an
              existing one the same way.
            </p>
            <button type="button" className="cap-btn primary" data-testid="dw-workflow-agent"
                    onClick={() => askBossAbout(
                      null,
                      "I want to work on this project's workflows. You can draft a new one from"
                      + " a described sequence of steps, change an existing one, define a custom"
                      + " step backed by a skill, or retire one. Start by telling me which"
                      + " workflows this project has and what each is for.",
                      "program_manager")}>
              Design one with BOSS
            </button>
          </article>
          <article className="dw-design-way">
            <h3>Draw it</h3>
            <p className="detail">
              Place steps on the canvas below yourself. Start from nothing, or fork one of the
              catalogue workflows and change what you need.
            </p>
          </article>
        </div>
        <BuilderView
          capabilities={capabilities} blocks={blocks} riskFloor={riskFloor}
          installed={installed} projectId={projectId}
          customSteps={customSteps} bindable={bindable}
          justMade={justMade} onDismissJustMade={() => setJustMade(null)}
          onManageSteps={() => setTab("steps")}
          busy={busy} target={builderTarget} setTarget={setBuilderTarget}
          stepRoles={stepRoles} holdings={holdings} onGranted={loadHoldings}
          onSave={(definitionId, spec) => act("Save", "save", { definition_id: definitionId, spec })}
          onCreate={async (name, spec, fromKey) => {
            // TWO CALLS, AND THE ORDER MATTERS.
            //
            // When the steps came from somewhere, `fork` is what records where — which is the
            // only thing that makes "Reset to the standard" mean anything later. Fork copies
            // the SOURCE's steps though, so whatever the operator changed on the canvas is
            // written straight after as the next version. Starting from nothing needs no
            // lineage, so it is a plain create.
            const result = fromKey
              ? await act(`Create ${name}`, "fork", { workflow_key: fromKey, new_name: name })
              : await act(`Create ${name}`, "create", { name });
            if (!result.ok) return;
            const created = (result.data as { definition?: { id: string } })?.definition;
            if (!created?.id) return;

            const steps = (spec as { steps?: unknown[] })?.steps ?? [];
            if (steps.length) {
              // Silent on a no-op: forking already stored these exact steps, and the runtime
              // answers an identical spec with "unchanged" rather than a new version.
              await act(`Save ${name}`, "save", { definition_id: created.id, spec });
            }
            setBuilderTarget(created.id);
            setJustMade({
              id: created.id, name,
              from: fromKey
                ? installed.find((i) => i.workflow_key === fromKey)?.name
                : undefined,
            });
          }}
        />
        </>
      )}
    </div>
  );
}

/* ── Running ─────────────────────────────────────────────────────────────────── */

/** How long a step took, or has been going. The single most asked question of a pipeline. */
function stepDuration(step: { started_at?: string | null; completed_at?: string | null }): string {
  if (!step.started_at) return "";
  const start = Date.parse(step.started_at);
  const end = step.completed_at ? Date.parse(step.completed_at) : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
  const secs = Math.round((end - start) / 1000);
  const text = secs < 60 ? `${secs}s`
    : secs < 3600 ? `${Math.floor(secs / 60)}m ${secs % 60}s`
    : `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
  return step.completed_at ? text : `${text}…`;
}

/**
 * Where the thing a step produced actually lives.
 *
 * A run view that shows only a status makes an operator open five other screens to find the PR
 * it opened or the panel it convened. The ids are already in the step's output; these turn them
 * into somewhere to go.
 */
/** The targets a step reports that belong to THIS run's app.
 *
 * The capability now scopes itself, but a run that already completed has its output frozen —
 * the Meeting cost meter run on screen lists all three of the project's apps and always will.
 * So the display filters too, which also means an older run stops claiming to watch its
 * neighbours the moment this ships rather than only after it is re-run.
 *
 * Unfiltered when the run does not name an app: showing everything beats showing nothing.
 */
export function targetsForThisApp(
  targets: unknown[], runAppName: string,
): Array<{ app_name?: string; environment?: string }> {
  const rows = targets.map((t) => t as { app_name?: string; environment?: string });
  const wanted = identityKey(runAppName || "");
  if (!wanted) return rows;
  const mine = rows.filter((t) => identityKey(String(t.app_name ?? "")) === wanted);
  // A run whose app matches nothing in the list is the case where the filter would blank a
  // step that does have something to say — an adopted run, a renamed app. Better to show what
  // was recorded than to hide it on a guess.
  return mine.length ? mine : rows;
}

function stepLinks(output: Record<string, unknown>, runAppName = ""): Array<{ label: string; href: string; external?: boolean }> {
  const out: Array<{ label: string; href: string; external?: boolean }> = [];
  // KEEP WHO YOU ARE. These hrefs were built from scratch — `?area=code_tasks&run=…` — which
  // dropped `?org=`, and the console gates on that: no org means the organisation picker. So
  // every one of these links threw the operator onto "Select your organization" and read as
  // the session expiring mid-run.
  const here = typeof window !== "undefined" ? window.location.search : "";
  const link = (params: Record<string, string | null | undefined>) => consoleHref(here, params);
  const str = (k: string) => (typeof output[k] === "string" ? (output[k] as string) : "");
  const awaited = (output.await as { kind?: string; id?: string } | undefined) ?? {};

  const codeTask = str("code_task_run_id") || (awaited.kind === "code_task" ? awaited.id ?? "" : "")
    || (output.capability === "build" ? str("run_id") : "");
  if (codeTask) {
    out.push({ label: "open the project", href: link({ area: "code_tasks", run: codeTask }) });
    // Debugging a step means reading what it actually did, filtered to this work.
    out.push({ label: "activity log", href: link({ area: "activity", run: codeTask }) });
    // NOT "Ask BOSS" AGAIN. Every step already carries an "Ask BOSS about this step" button,
    // which opens the dock with the step's own state prefilled — strictly more than this link
    // did. Two controls with the same name, one worse, is a choice nobody should be asked to
    // make.
  }
  // THE PANEL, not the list of panels. This linked to `?area=meetings` — the landing page —
  // so "open the panel" meant "go and find it", on a screen listing every meeting the project
  // has ever held. The meeting room reads `?meeting=`, and the step knows which one it adopted.
  const meeting = str("meeting_run_id") || (awaited.kind === "meeting" ? awaited.id ?? "" : "");
  if (meeting) {
    out.push({ label: "open the panel", href: link({ area: "meetings", meeting }) });
  }
  // THE APP IT IS WATCHING, not the monitoring landing page. "Set up monitoring" said
  // "watching Meeting cost meter · staging" and offered nowhere to go — the operator had to
  // find the Monitoring area and then find that app in it, for a step whose whole subject is
  // that one app. `?watch=` names it, and the monitoring panel scrolls to it and marks it.
  if (Array.isArray(output.targets)) {
    for (const t of targetsForThisApp(output.targets as unknown[], runAppName)) {
      const app = String(t.app_name ?? "").trim();
      if (!app) continue;
      const env = String(t.environment ?? "").trim();
      out.push({
        label: `monitor ${app}`,
        href: link({ area: "monitoring", watch: env ? `${app}|${env}` : app }),
      });
    }
  }
  const discovery = str("discovery_id") || (awaited.kind === "discovery" ? awaited.id ?? "" : "");
  if (discovery) {
    out.push({ label: "open the research", href: link({ area: "discovery", run: discovery }) });
  }
  const pr = str("pull_request_url");
  if (pr) {
    // Numbered, not a 60-character URL truncated mid-path. The number is what a person says
    // out loud, and the link still goes to the same place.
    const number = String(output.pull_request_number ?? "").trim() || pr.split("/").pop();
    out.push({ label: `pull request #${number}`, href: pr, external: true });
  }
  // START_PREVIEW NESTS ITS RESULT. The tool returns `{preview: {url, id, ...}}`, and this
  // looked only for a flat `preview_url` — so the one link an operator most wants, to the app
  // they just built, was the one that never appeared.
  //
  // THE LINK FACTORY WOULD BUILD, not a second attempt at one.
  //
  // Three times this was wrong, three different ways: no link at all, then a link to a preview
  // that had expired, then a link to 127.0.0.1:42543 — where the app really is listening, on
  // the host, which is not reachable from a browser anywhere else. `operatorPreviewOpenHref`
  // already encodes the whole rule (refuse expired, refuse loopback, else the API's own
  // /v1/previews/<id>/web/ proxy) and Factory has used it all along. Using it here is the fix;
  // writing a fourth version of it would not be.
  // THE STATUS IS IN THE RECORD. DO NOT INVENT ONE.
  //
  // This built the nested shape inline and hardcoded `status: "running"`, which defeated the
  // one rule that matters: operatorPreviewOpenHref refuses expired/stopped/FAILED and was
  // handed a fabricated "running" every time. start_preview records `{preview: {status:
  // "failed", failure_kind: "port_timeout", ...}}` for a preview that never served, and the
  // step still rendered a confident "Open preview" button pointing at a dead
  // /v1/previews/<id>/web/ — the only thing on screen claiming to know how the preview went.
  const record = previewRecordFromStepOutput(output);
  const href = record ? operatorPreviewOpenHref(record) : null;
  if (href) {
    out.push({ label: "open the preview", external: true,
               href: href.startsWith("http") ? href : `${PREVIEW_API}${href}` });
  }
  return out;
}

/** A repository this project can build in, from its registry. */
type Repo = { id: string; full_name: string; role?: string | null; default_branch?: string };

/** What the wizard has collected so far. One object so a Back button costs nothing. */
export type NewProjectDraft = {
  title: string;
  directive: string;
  audience: string;
  criteria: string[];
  workflowKey: string;
  repository: string;
  links: string[];
  /** Who will be on it. Empty means "whatever BOSS seeds" — see the wizard's agents step. */
  agents: string[];
  /** What THIS workflow declared it needs — a video to fetch, a clip to trim, a channel to
   *  publish to. Keyed by field name; only filled-in values are sent. */
  inputs: Record<string, string>;
};

const EMPTY_DRAFT: NewProjectDraft = {
  title: "", directive: "", audience: "", criteria: [""],
  workflowKey: "", repository: "", links: [], agents: [], inputs: {},
};

const STEPS = ["What you are building", "How it gets built", "Where it lives", "Review"] as const;

/** The brief, assembled the way the pipeline reads best.
 *
 * Not decoration. A one-line directive produces a panel arguing about one line: BOSS's own
 * sample briefs are written as who-it-is-for plus what-it-does plus criteria, because that is
 * what a spec review can actually review. The wizard collects the parts and joins them here,
 * so the operator writes prose and the pipeline receives structure.
 */
export function briefFrom(draft: NewProjectDraft): string {
  const parts = [draft.directive.trim()];
  if (draft.audience.trim()) parts.push(`Who it is for: ${draft.audience.trim()}`);
  return parts.filter(Boolean).join("\n");
}

/** Criteria worth sending: blank rows are UI, not requirements. */
export function criteriaFrom(draft: NewProjectDraft): string[] {
  return draft.criteria.map((c) => c.trim()).filter(Boolean);
}

/** Links worth sending, and only ones an agent could actually open. */
export function linksFrom(draft: NewProjectDraft): string[] {
  return draft.links
    .map((l) => l.trim())
    .filter((l) => /^https?:\/\//i.test(l));
}

/** What stops the operator moving on, said as the thing to do rather than as an error. */
export function blockingReason(
  step: number, draft: NewProjectDraft, declaresInputs = false,
): string {
  if (step === 0) {
    // A WORKFLOW THAT DECLARES ITS OWN INPUTS DOES NOT NEED A BRIEF.
    //
    // This demand exists because the catalogue's paths convene a PANEL that argues from the
    // text, and a thin brief produces a thin spec. A workflow that asks for a video link and
    // editing instructions convenes nothing: its steps read the fields it declared, and the
    // real instruction is one of them. Demanding twenty characters of prose as well is asking
    // the operator to write a brief nobody reads, to satisfy a check meant for a different
    // kind of work.
    if (declaresInputs) return "";
    if (draft.directive.trim().length < 20) {
      return "Describe what you want built — a sentence or two, not a title. "
           + "The panel argues from this text, so a thin brief produces a thin spec.";
    }
  }
  return "";
}

/**
 * STARTING WORK, WHERE THE WORK IS WATCHED.
 *
 * "New project" lived in Factory because Factory was where projects were listed. Now the
 * workflow drives — the press starts a run, and the run convenes the panel, waits for the
 * approval, builds, previews and asks before it deploys — so the page that shows those steps
 * is the page that should start them.
 *
 * AND IT IS A WIZARD, NOT A BOX.
 *
 * It was one textarea and a workflow dropdown. Everything else a build needs — which repository
 * the code goes in, what "done" means, anything the builder should read first — was either
 * defaulted silently or not asked at all, and the first chance to correct any of it was the
 * spec review, after a panel had already been convened on a guess. Starting a project is the
 * one moment where a minute of the operator's attention is worth more than any later
 * intervention, and it was the moment the console asked for the least.
 *
 * Four steps, each of which can be skipped by pressing on: nothing here is required except the
 * description, because a wizard that demands ten answers is a wizard people route around.
 */
function NewProjectComposer({ projectId, actor, workflows, catalogue, onStarted, startWith }: {
  projectId: string; actor: string;
  workflows: Installed[];
  catalogue: CatalogueEntry[];
  onStarted: (runId: string | null) => void;
  /** A workflow the operator asked to start, chosen already. Opens on the step that collects
   *  what that workflow declared it needs, because they have already made the path decision. */
  startWith?: string;
}) {
  const [step, setStep] = useState(startWith ? 1 : 0);
  const [draft, setDraft] = useState<NewProjectDraft>(
    startWith ? { ...EMPTY_DRAFT, workflowKey: startWith } : EMPTY_DRAFT);
  // WHO WILL BE ON IT. Read from the API rather than guessed here: start_run seeds from
  // STAGE_ROLES plus the chosen workflow's approver roles, and a second copy of that rule in
  // the console would drift from the one that actually runs.
  const [defaultAgents, setDefaultAgents] = useState<string[]>([]);
  const [allRoles, setAllRoles] = useState<Array<{ role_key: string;
                                                   display_name?: string | null }>>([]);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await fetch(
          `/api/project-agents?project_id=${encodeURIComponent(projectId)}&defaults=1`
          + (draft.workflowKey ? `&workflow_key=${encodeURIComponent(draft.workflowKey)}` : ""),
          { cache: "no-store" });
        if (r.ok && alive) setDefaultAgents(((await r.json())?.default_roles ?? []) as string[]);
      } catch { /* the wizard still starts the run; BOSS seeds them either way */ }
    })();
    return () => { alive = false; };
  }, [projectId, draft.workflowKey]);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await fetch(`/api/agent?view=roles&project_id=${encodeURIComponent(projectId)}`,
                              { cache: "no-store" });
        if (r.ok && alive) setAllRoles(((await r.json())?.items ?? []) as typeof allRoles);
      } catch { /* the picker is empty; the defaults still show */ }
    })();
    return () => { alive = false; };
  }, [projectId]);
  const [repos, setRepos] = useState<Repo[]>([]);
  const [suggested, setSuggested] = useState<{ name: string; source: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof NewProjectDraft>(key: K, value: NewProjectDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  // WHERE THE CODE GOES, ANSWERED BEFORE IT IS ASKED.
  //
  // The project's registry knows its repositories and which one is primary; nothing over HTTP
  // could read it, so this form could not offer them and every build went to the deployment's
  // global default. Loaded on mount so the field arrives filled in — the operator confirms a
  // real answer instead of composing one.
  useEffect(() => {
    let alive = true;
    void (async () => {
      const r = await get<{ repositories: Repo[]; suggested: string; suggested_source: string }>(
        "repositories", projectId);
      // A project with no registry is a normal state, not an error to surface: the build
      // resolves to the platform default and the step says so.
      if (!alive || !r || "error" in r) return;
      setRepos(r.repositories ?? []);
      if (r.suggested) setSuggested({ name: r.suggested, source: r.suggested_source });
    })();
    return () => { alive = false; };
  }, [projectId]);

  /** The catalogue entry behind an installed workflow, for its description and gates. */
  const describe = (key: string): CatalogueEntry | undefined =>
    catalogue.find((c) => c.key === key);

  // WHAT THE CHOSEN WORKFLOW WILL ASK FOR. Read from the installed definition rather than
  // hardcoded: a custom workflow declares its own, and the form has to draw whatever that is.
  const selectedWorkflowSchema = workflows.find(
    (w) => w.workflow_key === draft.workflowKey)?.input_schema;
  const declaredInputs = workflowInputFields(selectedWorkflowSchema);

  // WHAT WILL ACTUALLY BE SENT, worked out once and used for the guard, the review and the
  // start — three places that disagreed. The operator's directive stands in for a single blank
  // prose input rather than being asked for twice; see `inputsWithTheDirective`.
  const inputsToSend = inputsWithTheDirective(declaredInputs, draft.inputs, draft.directive);
  const stillBlank = missingRequired(declaredInputs, inputsToSend);

  // `??` WAS WRONG HERE AND IT COST A RUN. `blockingReason` returns "" for "nothing is
  // blocking", and "" is neither null nor undefined, so the right-hand side never ran and this
  // whole check was dead code: a required input left blank sailed past the form and was refused
  // by the runtime instead, as `WorkflowValidationError: this workflow needs 1 input(s) it was
  // not given: instructions` under a button that had looked perfectly ready to press.
  const blocked = blockingReason(step, draft, declaredInputs.length > 0)
    // A required input still blank blocks the start, and says which one — the run would be
    // refused by the runtime anyway, and being told at the form is the difference between a
    // correction and a failed run.
    || (stillBlank.length ? `Needs ${stillBlank.join(", ")}` : "");

  const start = async () => {
    // WHAT THIS RUN IS CALLED, when the operator wrote no brief.
    //
    // A workflow that declares its own inputs is started from those, and `briefFrom` returns
    // "" — which made the button silently do nothing, the worst possible response to a press.
    // The run still needs something readable in the runs list, so it is named after the
    // workflow and the instruction the operator actually gave.
    const chosen = workflows.find((w) => w.workflow_key === draft.workflowKey);
    const declared = declaredInputs.length > 0;
    const supplied = String(draft.inputs.instructions ?? "").trim();
    const text = briefFrom(draft).trim()
      || (declared
          ? [chosen?.name ?? draft.workflowKey, supplied].filter(Boolean).join(": ")
          : "");
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    try {
      const title = draft.title.trim()
        || (text.length > 72 ? `${text.slice(0, 69)}…` : text);
      const criteria = criteriaFrom(draft);
      const links = linksFrom(draft);
      const r = await post("start", {
        project_id: projectId, actor,
        ...(draft.workflowKey
            ? { workflow_key: draft.workflowKey }
            : { work_class: "project.new" }),
        input: {
          title, directive: text,
          // Sent only when the operator gave one. An empty string here would read as a
          // deliberate choice of "" further down and beat the project's own default.
          ...(criteria.length ? { acceptance_criteria: criteria } : {}),
          ...(links.length ? { reference_links: links } : {}),
          ...(draft.repository ? { repository: draft.repository } : {}),
          // WHAT THIS WORKFLOW DECLARED IT NEEDS. from_input resolves any field name, so a
          // step binding `video_url` reads it straight from here. Only filled-in values are
          // sent: an empty string would read further down as a deliberate choice of empty.
          ...inputsToSend,
        },
      });
      if (!r.ok) { setError(r.error ?? "Could not start it."); return; }
      const run = (r.data as { run?: { id?: string }; id?: string } | undefined);
      // WHO IS ON IT, recorded as a CHOICE — but only when it differs from what start_run
      // already seeded. Writing an identical list back as `operator` would claim a decision
      // nobody made, and the panel distinguishes the two on purpose.
      const chosen = draft.agents;
      const sameAsDefault = chosen.length === defaultAgents.length
        && chosen.every((a) => defaultAgents.includes(a));
      if (chosen.length && !sameAsDefault) {
        try {
          await apiWrite("/api/project-agents", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id: projectId, app: title, actor,
                                   role_keys: chosen }),
          });
          // Anyone the operator took OFF the default list has to come off for real, or the
          // wizard would show a choice the run does not honour.
          for (const gone of defaultAgents.filter((d) => !chosen.includes(d))) {
            await apiWrite("/api/project-agents", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ project_id: projectId, app: title, actor,
                                     action: "unassign", role_key: gone }),
            });
          }
        } catch { /* the run is started and seeded; the assignment can be fixed on the run */ }
      }
      setDraft(EMPTY_DRAFT);
      setStep(0);
      onStarted(String(run?.run?.id ?? run?.id ?? "") || null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="dw-new dw-wizard" data-testid="dw-new-project">
      <div className="dw-new-head">
        <h3>New project</h3>
        <p className="detail">
          A panel specs it, you approve, then it builds, previews, and asks before it deploys.
        </p>
      </div>

      {/* WHERE YOU ARE AND WHAT IS LEFT. Four unlabelled dots would be decoration; the names
          are what let somebody decide whether to keep going or press Start now. */}
      <ol className="dw-wiz-rail" data-testid="dw-wiz-rail">
        {STEPS.map((label, i) => (
          <li key={label} data-state={i === step ? "current" : i < step ? "done" : "todo"}>
            <button type="button" className="dw-wiz-crumb"
                    data-testid={`dw-wiz-crumb-${i}`}
                    // Backwards freely; forwards only over ground already covered, so the
                    // rail cannot be used to skip the one question that is required.
                    disabled={i > step}
                    onClick={() => setStep(i)}>
              <span className="dw-wiz-num" aria-hidden>{i < step ? "✓" : i + 1}</span>
              <span>{label}</span>
            </button>
          </li>
        ))}
      </ol>

      {step === 0 ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-0">
          <div className="dw-field">
            <span className="dw-field-label"><label htmlFor="dw-new-directive">What should it do?</label> <HelpTip
              label="Project description"
              text="The panel and builder use your words. Describe what the product does and what a person can accomplish with it in two or three sentences." /></span>
            <textarea
              id="dw-new-directive"
              className="dw-new-input" rows={5} value={draft.directive}
              data-testid="dw-new-directive"
              placeholder={"A shift-swap board where staff can offer a shift they cannot work "
                + "and colleagues can claim it, with the manager approving the swap."}
              onChange={(e) => set("directive", e.target.value)}
              onKeyDown={(e) => {
                // ⌘/Ctrl+Enter starts with the defaults, as it did before the wizard and as
                // every other composer in this console does. The remaining steps all have a
                // defensible default, so the shortcut is not a way of skipping a decision —
                // it is the same "Start with defaults" the footer offers, from the keyboard.
                if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                  e.preventDefault();
                  if (!blocked) void start();
                }
              }}
            />
          </div>

          <div className="dw-field">
            <span className="dw-field-label"><label htmlFor="dw-new-audience">Who is it for?</label> <em>optional</em> <HelpTip
              label="Intended users"
              text="Describe the people and setting. A duty manager using a phone mid-shift needs a different interface from office staff." /></span>
            <input id="dw-new-audience" className="input" value={draft.audience} data-testid="dw-new-audience"
                   placeholder="a duty manager on a phone, mid-shift"
                   onChange={(e) => set("audience", e.target.value)} />
          </div>

          <fieldset className="dw-field">
            <legend className="dw-field-label">
              How will you know it works? <em>optional</em> <HelpTip
                label="Acceptance criteria"
                text="These become checks for the build. Leave them blank and the panel will propose criteria for your approval." />
            </legend>
            <span className="dw-field-hint">These become acceptance criteria.</span>
            {draft.criteria.map((c, i) => (
              <div className="dw-crit-row" key={i}>
                <input className="input" value={c} data-testid={`dw-new-criterion-${i}`}
                       placeholder={i === 0
                         ? "a member of staff can offer a shift in under 30 seconds"
                         : "another thing that must be true"}
                       onChange={(e) => {
                         const next = [...draft.criteria];
                         next[i] = e.target.value;
                         set("criteria", next);
                       }} />
                {draft.criteria.length > 1 ? (
                  <button type="button" className="cap-btn dw-crit-drop"
                          aria-label="Remove this criterion"
                          onClick={() => set("criteria",
                            draft.criteria.filter((_, j) => j !== i))}>×</button>
                ) : null}
              </div>
            ))}
            <button type="button" className="linklike" data-testid="dw-new-criterion-add"
                    onClick={() => set("criteria", [...draft.criteria, ""])}>
              + another
            </button>
          </fieldset>
        </div>
      ) : null}

      {step === 1 ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-1">
          <span className="dw-field-label">Which path should it take? <HelpTip
            label="Workflow paths"
            text="Every path produces software you can review. Paths differ in the review, evidence, and approval gates along the way." /></span>
          <div className="dw-wiz-choices" role="radiogroup" aria-label="Workflow">
            <button type="button" role="radio" className="dw-wiz-choice"
                    aria-checked={draft.workflowKey === ""}
                    data-selected={draft.workflowKey === ""}
                    data-testid="dw-new-workflow-default"
                    onClick={() => set("workflowKey", "")}>
              <span className="dw-wiz-choice-name">This project&apos;s usual path</span>
              <span className="detail">
                Whatever this project maps for new work. The same resolution every other entry
                point uses.
              </span>
            </button>
            {workflows.map((w) => {
              const entry = describe(w.workflow_key);
              return (
                <button type="button" role="radio" className="dw-wiz-choice" key={w.id}
                        aria-checked={draft.workflowKey === w.workflow_key}
                        data-selected={draft.workflowKey === w.workflow_key}
                        data-testid={`dw-new-workflow-${w.workflow_key}`}
                        onClick={() => set("workflowKey", w.workflow_key)}>
                  <span className="dw-wiz-choice-name">{w.name}</span>
                  {entry ? <span className="detail">{entry.description}</span> : null}
                  {entry ? (
                    <span className="dw-wiz-choice-meta detail">
                      {entry.step_count} steps
                      {entry.gates?.length
                        ? ` · you approve: ${entry.gates.join(", ")}`
                        : " · no approval gates"}
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>

          {/* WHAT THIS WORKFLOW NEEDS SUPPLIED.
              Rendered FROM the chosen workflow's declaration, not hardcoded: a custom workflow
              that fetches a video, trims a clip and publishes to a channel declares those, and
              until now was startable only by a caller that already knew the field names.
              Nothing renders for a workflow that declares none, which is every catalogue one. */}
          {declaredInputs.length ? (
            <div className="dw-wiz-inputs" data-testid="dw-workflow-inputs">
              <span className="dw-field-label">What this path needs <HelpTip
                label="Workflow inputs"
                text="These values are read when the run starts. Put lasting choices such as a channel, icon, or house style in the skill or agent instead." /></span>
              {declaredInputs.map((field) => (
                <div className="dw-wiz-input" key={field.name}>
                  <span className="dw-field-label">
                    <label htmlFor={`dw-workflow-input-${field.name}`}>{field.label}</label>
                    {field.required ? <em className="dw-req" aria-hidden> required</em> : null}
                    {field.help ? <HelpTip label={field.label} text={field.help} /> : null}
                  </span>
                  {field.help ? <span className="dw-field-hint">{field.help}</span> : null}
                  {/* Multi-line instructions need enough room to review the complete value. */}
                  {field.kind === "multiline" ? (
                    <textarea
                      id={`dw-workflow-input-${field.name}`}
                      rows={3}
                      value={draft.inputs[field.name] ?? ""}
                      placeholder={field.example}
                      data-testid={`dw-workflow-input-${field.name}`}
                      aria-required={field.required || undefined}
                      onChange={(e) =>
                        setDraft((d) => ({ ...d,
                          inputs: { ...d.inputs, [field.name]: e.target.value } }))}
                    />
                  ) : (
                    <input
                      id={`dw-workflow-input-${field.name}`}
                      type={field.kind === "number" ? "number" : "text"}
                      inputMode={field.kind === "number" ? "numeric" : undefined}
                      value={draft.inputs[field.name] ?? ""}
                      placeholder={field.example}
                      data-testid={`dw-workflow-input-${field.name}`}
                      aria-required={field.required || undefined}
                      onChange={(e) =>
                        setDraft((d) => ({ ...d,
                          inputs: { ...d.inputs, [field.name]: e.target.value } }))}
                    />
                  )}
                </div>
              ))}
            </div>
          ) : null}

          {/* WHO WILL BE ON IT. The wizard collected a title, a directive, criteria, a workflow
              and a repository, and never once mentioned agents — so a project was created with
              no say in who would build it. The defaults are shown pre-selected rather than as
              an empty box: they are what BOSS seeds anyway, and an empty picker would imply
              nobody is coming. */}
          <span className="dw-field-label dw-agents-label">Who works on it</span>
          <span className="dw-field-hint">
            These are the agents that would run this path anyway. Take one off and it may not
            sign off this app&rsquo;s gates; add one and it may.
          </span>
          <div className="aot-list" data-testid="dw-wiz-agents">
            {(draft.agents.length ? draft.agents : defaultAgents).map((key) => (
              <AgentChip key={key} roleKey={key}
                         label={allRoles.find((r) => r.role_key === key)?.display_name
                                || key.replace(/_/g, " ")}>
                <button type="button" className="aot-x" aria-label={`Remove ${key}`}
                        onClick={() => set("agents",
                          (draft.agents.length ? draft.agents : defaultAgents)
                            .filter((k) => k !== key))}>✕</button>
              </AgentChip>
            ))}
            <select className="mr-select aot-pick" value="" data-testid="dw-wiz-agent-add"
                    onChange={(e) => {
                      if (!e.target.value) return;
                      const now = draft.agents.length ? draft.agents : defaultAgents;
                      set("agents", [...now, e.target.value]);
                    }}>
              <option value="">+ Add an agent…</option>
              {allRoles
                .filter((r) => !(draft.agents.length ? draft.agents : defaultAgents)
                  .includes(r.role_key))
                .map((r) => (
                  <option key={r.role_key} value={r.role_key}>
                    {r.display_name || r.role_key}
                  </option>
                ))}
            </select>
          </div>
          {draft.agents.length ? (
            <span className="dw-field-hint">
              Changed from the default.{" "}
              <button type="button" className="dw-linkish"
                      onClick={() => set("agents", [])}>Put it back</button>
            </span>
          ) : null}
        </div>
      ) : null}

      {step === 2 ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-2">
          <span className="dw-field-label">Which repository?</span>
          <span className="dw-field-hint">
            Where the branch and the pull request go.
            {suggested
              ? ` Pre-selected: ${suggested.name}${
                  suggested.source === "registry_primary" ? " — this project's primary." : "."}`
              : " This project has no repository registered, so the platform default is used."}
          </span>
          <div className="dw-wiz-choices" role="radiogroup" aria-label="Repository">
            <button type="button" role="radio" className="dw-wiz-choice"
                    aria-checked={draft.repository === ""}
                    data-selected={draft.repository === ""}
                    data-testid="dw-new-repo-default"
                    onClick={() => set("repository", "")}>
              <span className="dw-wiz-choice-name">
                {suggested ? suggested.name : "The project default"}
              </span>
              <span className="detail">
                {suggested
                  ? "What this project resolves to today. Leave it unless you mean elsewhere."
                  : "Resolved when the build starts."}
              </span>
            </button>
            {repos
              .filter((r) => r.full_name !== suggested?.name)
              .map((r) => (
                <button type="button" role="radio" className="dw-wiz-choice" key={r.id}
                        aria-checked={draft.repository === r.full_name}
                        data-selected={draft.repository === r.full_name}
                        data-testid={`dw-new-repo-${r.full_name}`}
                        onClick={() => set("repository", r.full_name)}>
                  <span className="dw-wiz-choice-name">{r.full_name}</span>
                  <span className="detail">
                    {r.role ? `${r.role} · ` : ""}branch {r.default_branch || "main"}
                  </span>
                </button>
              ))}
          </div>

          <fieldset className="dw-field">
            <legend className="dw-field-label">
              Anything it should read first? <em>optional</em>
            </legend>
            <span className="dw-field-hint">
              Links the agents must open before designing or writing — an API doc, a repo whose
              conventions to follow, a design. They are fetched and read, not indexed or
              searched, so they have to be reachable without a login. A link that cannot be
              opened is reported, never guessed at.
            </span>
            {[...draft.links, ""].map((link, i) => (
              <input className="input dw-link-row" key={i} value={link}
                     data-testid={`dw-new-link-${i}`}
                     placeholder="https://docs.example.com/api"
                     onChange={(e) => {
                       const next = [...draft.links];
                       if (i < next.length) next[i] = e.target.value;
                       else next.push(e.target.value);
                       set("links", next.filter((l, j) => l.trim() || j < next.length - 1));
                     }} />
            ))}
          </fieldset>
        </div>
      ) : null}

      {step === 3 ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-3">
          {/* WHAT IS ABOUT TO HAPPEN, IN THE ORDER IT WILL HAPPEN. A confirmation screen that
              only repeats the form is a second form. This says what the press DOES, including
              the parts that cost money and the parts that will stop and wait for a person. */}
          <dl className="dw-wiz-review">
            <dt>Building</dt>
            <dd data-testid="dw-review-directive">{briefFrom(draft) || "—"}</dd>
            <dt>Path</dt>
            <dd data-testid="dw-review-workflow">
              {draft.workflowKey
                ? (workflows.find((w) => w.workflow_key === draft.workflowKey)?.name
                   ?? draft.workflowKey)
                : "This project's usual path"}
            </dd>
            <dt>Repository</dt>
            <dd data-testid="dw-review-repo">
              {draft.repository || suggested?.name || "the project default"}
            </dd>
            {/* WHO RUNS IT, on the screen that says what the press does.
                It is chosen on step 2 and was reported twice as missing — a thing on a screen
                you have to click through to is a thing nobody finds. Repeated here, where
                everything about to happen is confirmed, and editable in place. */}
            <dt>Agents</dt>
            <dd data-testid="dw-review-agents">
              <div className="aot-list">
                {(draft.agents.length ? draft.agents : defaultAgents).map((key) => (
                  <AgentChip key={key} roleKey={key}
                             label={allRoles.find((r) => r.role_key === key)?.display_name
                                    || key.replace(/_/g, " ")}
                             note={draft.agents.length ? "chosen" : "by default"}>
                    <button type="button" className="aot-x" aria-label={`Remove ${key}`}
                            onClick={() => set("agents",
                              (draft.agents.length ? draft.agents : defaultAgents)
                                .filter((k) => k !== key))}>✕</button>
                  </AgentChip>
                ))}
                <select className="mr-select aot-pick" value=""
                        data-testid="dw-review-agent-add"
                        onChange={(e) => {
                          if (!e.target.value) return;
                          const now = draft.agents.length ? draft.agents : defaultAgents;
                          set("agents", [...now, e.target.value]);
                        }}>
                  <option value="">+ Add an agent…</option>
                  {allRoles
                    .filter((r) => !(draft.agents.length ? draft.agents : defaultAgents)
                      .includes(r.role_key))
                    .map((r) => (
                      <option key={r.role_key} value={r.role_key}>
                        {r.display_name || r.role_key}
                      </option>
                    ))}
                </select>
              </div>
            </dd>
            {/* WHAT THE WORKFLOW WILL BE GIVEN. The review listed the brief, the path, the
                repository and the agents, and said nothing about the fields the run is
                actually started from — so an operator whose directive is standing in for the
                instruction could not see that it was. */}
            {declaredInputs.length ? (
              <>
                <dt>It will be given</dt>
                <dd data-testid="dw-review-inputs">
                  <ul>
                    {declaredInputs
                      .filter((f) => inputsToSend[f.name])
                      .map((f) => (
                        <li key={f.name}>
                          <span className="detail">{f.label}: </span>
                          {inputsToSend[f.name]}
                        </li>
                      ))}
                  </ul>
                </dd>
              </>
            ) : null}
            {criteriaFrom(draft).length ? (
              <>
                <dt>Done when</dt>
                <dd data-testid="dw-review-criteria">
                  <ul>{criteriaFrom(draft).map((c) => <li key={c}>{c}</li>)}</ul>
                </dd>
              </>
            ) : null}
            {linksFrom(draft).length ? (
              <>
                <dt>Reads first</dt>
                <dd data-testid="dw-review-links">
                  <ul>{linksFrom(draft).map((l) => <li key={l}><code>{l}</code></li>)}</ul>
                </dd>
              </>
            ) : null}
          </dl>
          <p className="detail dw-wiz-warn">
            Starting spends model budget: research, a panel and a build. It will stop and ask
            you before it deploys anything.
          </p>
        </div>
      ) : null}

      {blocked ? <p className="dw-new-error" data-testid="dw-wiz-blocked">{blocked}</p> : null}

      <div className="dw-wiz-foot">
        {step > 0 ? (
          <button type="button" className="cap-btn" data-testid="dw-wiz-back"
                  onClick={() => setStep((n) => n - 1)}>Back</button>
        ) : null}
        <span className="dw-wiz-spacer" />
        {/* START IS AVAILABLE FROM THE FIRST STEP. Everything after the description has a
            defensible default, so making somebody walk four screens to accept four defaults
            would be ceremony. The wizard is there for the operator who wants it. */}
        {step < STEPS.length - 1 ? (
          <>
            <button type="button" className="linklike" data-testid="dw-wiz-start-now"
                    disabled={busy || !!blocked}
                    onClick={() => void start()}>
              {busy ? "Starting…" : "Start with defaults"}
            </button>
            <button type="button" className="cap-btn primary" data-testid="dw-wiz-next"
                    disabled={!!blocked}
                    onClick={() => setStep((n) => n + 1)}>Next</button>
          </>
        ) : (
          <button type="button" className="cap-btn primary" data-testid="dw-new-start"
                  disabled={busy || !!blocked}
                  onClick={() => void start()}>
            {busy ? "Starting…" : "Start the project"}
          </button>
        )}
      </div>
      {error ? <p className="dw-new-error" data-testid="dw-new-error">{error}</p> : null}
    </section>
  );
}


/**
 * The input a RE-RUN should start from.
 *
 * "Run this workflow again" copied the previous run's input whole — including the `adopt_*`
 * keys that tell the runtime "this work already happened, go and watch it". So re-running a
 * project adopted the same finished build: research recorded as not-run, the old panel and the
 * old approval adopted, and `build` green in seconds against code written yesterday. That is a
 * re-observation, not a re-run, and it is the opposite of what somebody pressing it wants.
 *
 * The directive, the title, the repository and the deploy target are kept — they are what the
 * work IS and where it ships. What is dropped is every claim about work that has already
 * happened, so the new run does its own research, convenes its own panel, and builds.
 */
export function inputForRerun(input: Record<string, unknown> | null | undefined) {
  const next: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(input ?? {})) {
    if (key.startsWith("adopt_")) continue;
    next[key] = value;
  }
  return next;
}

/** The four states an operator acts on differently, plus everything. */
/** Somewhere this project can ship to. Offered at the release gate so "deploy" is a choice
 *  about WHERE, not just whether. */
type DeployTarget = {
  id: string; app_name: string; environment: string; status?: string;
  /** "host:port" for each address the deployer published. The app's actual URL lives here —
   *  a deploy step that cannot reach this can report success and still show no way in. */
  domains?: string[];
  /** The machine it is bound to. Absent means unbound, which is why a deploy would refuse. */
  host_id?: string | null;
};

/** A machine this project can ship to. Choosing one is what MAKES a target: `deploy_app`
 *  refuses without an active target and never creates infrastructure itself, so a project
 *  built for the first time had nowhere to go. */
type DeployHost = { id: string; name: string; address?: string; status?: string };

/** How the runtime words a gate somebody said no to. */
const DENIED = /^step '(.+)' was rejected$/;

const BUCKETS = ["needs_you", "running", "blocked", "done"] as const;
type Bucket = typeof BUCKETS[number];
const BUCKET_LABEL: Record<Bucket | "all", string> = {
  all: "All",
  needs_you: "Needs you",
  running: "Running",
  blocked: "Stopped",
  done: "Done",
};

export function RunsView({ runs, openRun, setOpenRun, onAdvance, busy, capabilities,
                   onRetryStep, onSkipStep, onRerun, onStop, onDelete, onStopAndRerun,
                   onBuildOnThis, onRecheck,
                   onSetTarget, onPrepareTarget,
                   onRunPreview,
                   onBound, projectId, actor, roles,
                   previewStarting,
                   targets, hosts }: {
  projectId: string;
  actor: string;
  /** Every agent this project has, for the "Agents on this" picker. */
  roles: Array<{ role_key: string; display_name?: string | null }>;
  runs: Run[];
  openRun: string | null;
  setOpenRun: (id: string | null) => void;
  onAdvance: () => void;
  busy: string | null;
  capabilities: Capability[];
  onRetryStep: (runId: string, stepId: string) => void;
  /** Go on without a failed step, for the reason the operator gave. */
  onSkipStep: (runId: string, stepId: string, reason: string) => void;
  onRerun: (run: Run) => void;
  /** Stop a live run: cancels the work it started, not only its bookkeeping. */
  onStop: (run: Run) => void;
  onDelete: (run: Run) => void;
  /** Stop it and start the same workflow again on the same directive, in that order. */
  onStopAndRerun: (run: Run) => void;
  onBuildOnThis: (run: Run) => void;
  onRecheck: (run: Run) => void;
  onSetTarget: (runId: string, targetId: string) => void;
  onPrepareTarget: (runId: string, hostId: string) => void;
  onRunPreview: (buildRunId: string) => void;
  /** Re-read the runs — used after a binding lands so the step shows it. */
  onBound: () => void;
  /** The build whose preview was just asked for and has not appeared yet. */
  previewStarting?: string;
  targets: DeployTarget[];
  hosts: DeployHost[];
}) {
  // Polled, not fetched once: a machine degrades DURING a run, which is exactly when the
  // operator is staring at the screen wondering why it is slow.
  const hostAdvice = useHostAdvice(projectId);
  const [bucket, setBucket] = useState<Bucket | "all">("all");
  if (runs.length === 0) {
    return (
      <div className="dw-empty" data-testid="dw-no-runs">
        <p>Nothing is running on a delivery workflow yet.</p>
        <p className="detail">
          Install the catalogue, apply a workflow to a class of work, and start something.
        </p>
      </div>
    );
  }
  // WAITING, BLOCKED AND DEAD-LETTERED ARE NOT "RUNNING".
  //
  // Every run sat under one heading called Running, so a run stopped dead and a run waiting on
  // a person looked like a run making progress. These are the four states an operator acts on
  // differently: one needs them, one needs nothing, one needs a decision about a failure, and
  // one is history. A run that stopped at a failed step is the third kind and goes under
  // "Needs you" — see `bucketOf` and `runNeedsYou`.
  const counts = BUCKETS.reduce((acc, b) => {
    acc[b] = runs.filter((r) => bucketOf(r) === b).length;
    return acc;
  }, {} as Record<Bucket, number>);
  const shown = [...runs].filter((r) => bucket === "all" || bucketOf(r) === bucket)
    .sort(byLiveness);

  return (
    <div className="dw-runs">
      <div className="dw-runs-head">
        <div className="dw-filters" role="tablist" aria-label="Run state">
          {(["all", ...BUCKETS] as const).map((b) => (
            <button key={b} type="button" role="tab" aria-selected={bucket === b}
                    className="dw-filter" data-active={bucket === b}
                    data-tone={b} onClick={() => setBucket(b)}
                    data-testid={`dw-filter-${b}`}>
              {BUCKET_LABEL[b]}
              <em>{b === "all" ? runs.length : counts[b as Bucket]}</em>
            </button>
          ))}
        </div>
        <button type="button" className="cap-btn" onClick={onAdvance} disabled={!!busy}
                data-testid="dw-advance">
          {busy === "Advance" ? "Advancing…" : "Advance now"}
        </button>
      </div>
      {shown.length === 0 ? (
        <p className="dw-empty detail" data-testid="dw-none-in-bucket">
          Nothing {BUCKET_LABEL[bucket].toLowerCase()}.
        </p>
      ) : null}
      {shown.map((run) => {
        const open = openRun === run.id;
        // The step this run stopped at for the operator, if any — see `stoppedStep`.
        const stopped = stoppedStep(run);
        const stoppedCapability = stopped
          ? capabilities.find((c) => c.key === stopped.capability) : undefined;
        return (
          // OPEN IS A STATE THE CARD SHOULD LOOK LIKE.
          //
          // "It's hard to differentiate between different workflows — I don't see a good
          // boundary, the left side border is just not enough." Correct: every card had the same
          // 1px border and the same background, so an OPEN card's content ran into the next
          // card's header with nothing but 10px of gap between them. A 3px status stripe down
          // the left cannot separate two hundred lines of step detail from the row beneath it.
          <article key={run.id} className="dw-run" data-status={run.status}
                   data-open={open ? "true" : undefined} data-testid="dw-run">
            {/* WHY THIS RUN IS SLOW, NEXT TO THE NUMBER THAT MISLEADS. The elapsed time is
                what gets read as "BOSS is regressing"; if the machine took that time, it has
                to say so in the same place. Renders nothing on a healthy host. */}
            {run.status === "running" || run.status === "waiting"
              ? <HostAdviceBanner advice={hostAdvice} compact /> : null}
            <button type="button" className="dw-run-head" aria-expanded={open}
                    onClick={() => setOpenRun(open ? null : run.id)}>
              <span className="dw-badge" data-tone={tone(run.status)}>{run.status}</span>
              <span className="dw-run-name">
                {runSubject(run) ?? run.workflow_name}
                {runSubject(run) ? (
                  <em className="dw-run-wf" data-testid="dw-run-workflow">
                    {run.workflow_name} v{run.version}
                  </em>
                ) : null}
              </span>
              {runSubject(run) ? null : (
                <span className="detail dw-run-version">v{run.version}</span>
              )}
              {/* Progress as a count, not only a bar: "4 of 7" survives a screenshot. */}
              <span className="dw-run-progress">
                {run.steps_done} of {run.step_count}
                <i className="dw-bar" aria-hidden>
                  <i style={{ width: `${Math.round((run.steps_done / Math.max(1, run.step_count)) * 100)}%` }} />
                </i>
              </span>
              {/* WHICH OF THE TWO IS THIS. "Run this workflow again" leaves the earlier run
                  alone, by design — so the list showed two rows with the same title, the same
                  "5 of 8" and the same "waiting on release_manager", and nothing an operator
                  could use to tell which was today's. The start time is the distinguishing
                  fact, so it is on the row rather than hidden in the expanded detail. */}
              {run.created_at ? (
                <span className="detail dw-run-started" data-testid="dw-run-started">
                  started {startedLabel(run.created_at)}
                </span>
              ) : null}
              {run.waiting_on ? (
                <span className="dw-waiting" data-testid="dw-waiting">
                  waiting on {run.waiting_on}
                </span>
              ) : null}
            </button>
            {stopped ? (
              // STOPPED, AND WAITING ON YOU — SAID ON THE CARD, WITH BOTH ANSWERS.
              //
              // A run that stopped at a failed step showed no reason here (the runtime wrote
              // `error`, this rendered only `blocked_reason`), sat under "Stopped" beside runs
              // somebody cancelled, and offered "Run this workflow again" and "Remove this run"
              // — both of which throw the run away. The two answers that keep it were inside the
              // run, on the step, two clicks deep. The runtime now writes the sentence
              // ("Stopped at Research: … Try it again, or skip it and carry on.") and the card
              // carries it with the buttons, so the decision is visible without opening anything.
              <div className="dw-blocked dw-stopped" data-testid="dw-run-stopped">
                <p>
                  {run.blocked_reason
                    || `Stopped at ${stoppedCapability?.label ?? stopped.step_id}: `
                       + `${stopped.error || run.error || "its work failed"}.`
                       + stoppedAnswers(stepActions(run, stopped.step_id))}
                </p>
                <FailedStepActions key={stopped.step_id} step={stopped}
                                   capability={stoppedCapability} busy={busy}
                                   actions={stepActions(run, stopped.step_id)}
                                   idPrefix="dw-run-stopped"
                                   onRetry={() => onRetryStep(run.id, stopped.step_id)}
                                   onSkip={(reason) => onSkipStep(run.id, stopped.step_id, reason)} />
              </div>
            ) : run.blocked_reason ? (
              // A DENIED RELEASE IS A DECISION, NOT A FAULT. "step 'deploy' was rejected"
              // rendered in the same red as a crash, with no hint of what happens next — so a
              // deliberate no looked like a failure the operator had to debug.
              <p className="dw-blocked" data-denied={DENIED.test(run.blocked_reason)}
                 data-testid="dw-blocked">
                {DENIED.test(run.blocked_reason)
                  ? `You denied ${run.blocked_reason.replace(DENIED, "$1")}. Nothing shipped — `
                    + "reopen it with “Try … again”, or leave the run as it is."
                  : run.blocked_reason}
              </p>
            ) : null}
            {/* The "Agents on this" panel is the rail's FIRST NODE now, not a block above it.
                A panel over the chain is a thing you have to notice; a node in the chain is
                where somebody reading the steps already is. */}
            {open ? (
              <StepPath run={run} actor={actor} roles={roles} capabilities={capabilities} targets={targets} hosts={hosts}
                        onRetryStep={(stepId) => onRetryStep(run.id, stepId)}
                        onSkipStep={(stepId, reason) => onSkipStep(run.id, stepId, reason)}
                        onRerun={() => onRerun(run)} onRecheck={() => onRecheck(run)}
                        onStop={() => onStop(run)}
                        onDelete={() => onDelete(run)}
                        onStopAndRerun={() => onStopAndRerun(run)}
                        onBuildOnThis={() => onBuildOnThis(run)}
                        busy={busy}
                        onSetTarget={(targetId) => onSetTarget(run.id, targetId)}
                        onPrepareTarget={(hostId) => onPrepareTarget(run.id, hostId)}
                        onRunPreview={onRunPreview}
                        onBound={onBound}
                        projectId={projectId}
                        previewStarting={previewStarting} />
            ) : null}
          </article>
        );
      })}
    </div>
  );
}


/* ── The path the work takes ─────────────────────────────────────────────────── */

/** Where a step sits in the story: done, happening now, still ahead, or never happened. */
/** Findings the build shipped with: reported, not fatal, and never shown as clean. */
function hasFindings(step: RunStep): boolean {
  const f = (step.output as { findings?: unknown } | undefined)?.findings;
  return Array.isArray(f) && f.length > 0;
}

function phaseOf(step: RunStep):
    "done" | "now" | "ahead" | "skipped" | "failed" | "findings" | "failed_on" {
  // FAILED, AND THE WORK WENT ON. Research that died is not "not run" — that hides something
  // worth fixing — and it is not a stopped run either, because the project carried on without
  // it. Its own state, so neither reading is offered.
  if (failedButContinued(step)) return "failed_on";
  if (notApplicable(step) || step.status === "skipped") return "skipped";
  if (step.status === "failed" || step.status === "deadlettered" || step.status === "cancelled") {
    return "failed";
  }
  if (step.status === "succeeded") return hasFindings(step) ? "findings" : "done";
  if (["running", "leased", "ready", "waiting", "queued"].includes(step.status)) return "now";
  return "ahead";
}

/** The run the page should open on.
 *
 *  Not simply the newest. This page answers two questions — "what is happening" and "what
 *  needs me" — and the second is the one somebody came here for. A run waiting on a decision
 *  is the only thing on the page that will not move until a person acts, so it opens first
 *  even when something newer is running.
 *
 *  Exported because "which one is the important one" is a rule worth testing, not a detail of
 *  a render.
 */
export function runToOpen(runs: Run[]): string | null {
  if (!runs.length) return null;
  // An open approval OR a step the run stopped at — the same rule the "Needs you" bucket reads.
  const needsMe = runs.find(runNeedsYou);
  if (needsMe) return needsMe.id;
  const live = runs.find((r) => ACTIVE_RUN_STATES.has(r.status));
  return (live ?? runs[0]).id;
}

/** A step needing a person, right now. The one thing on this page that is an instruction. */
/** Gate states that can still be answered. Anything else has been decided or has lapsed. */
const OPEN_GATE = new Set(["pending", "delegated"]);

/** What a rail node says under its name — or nothing, when nothing is worth saying.
 *
 *  Ten nodes wrapped to two rows, and half their second lines read "0s" or "succeeded". A step
 *  that took no measurable time has no duration worth printing, and "succeeded" is the tick
 *  beside it said twice. Dropping both halves the rail's height and loses nothing: the
 *  durations that matter — 4m 20s of research, 18m 22s of build — are exactly the ones kept.
 *
 *  Deliberately NOT the anonymous-dots version some redesigns reach for. The rail is the only
 *  place a run's timing is visible, and that is most of what it is for.
 */
export function railMeta(step: RunStep): string {
  const took = stepDuration(step);
  if (took && took !== "0s") return took;
  // A terminal state the tick already shows is not information; anything else is.
  return ["succeeded", "completed"].includes(step.status) ? "" : step.status;
}

export function needsYou(step: RunStep): boolean {
  // THE GATE'S STATE, NOT ONLY THE STEP'S.
  //
  // This was `decision_id && status === "waiting"`, which is true for a gate nobody has
  // answered AND for one answered thirty seconds ago — the step stays `waiting` until a worker
  // picks the answer up. So an operator approved a deploy, came back to the workflow, and was
  // shown the same "Review and decide" button as before, with no sign their click had landed.
  //
  // It is worse for an expired gate: the step waits forever, and the button leads to a decision
  // that cannot be answered from there.
  if (!step.decision_id || step.status !== "waiting") return false;
  // Unknown (an older API, or a decision row that has gone) keeps the old behaviour: offering
  // an answerable gate is recoverable, hiding one is not.
  return !step.decision_status || OPEN_GATE.has(step.decision_status);
}

/** A gate that has been answered but whose step has not moved yet. */
export function decidedButNotMoved(step: RunStep): string {
  if (!step.decision_id || step.status !== "waiting" || !step.decision_status) return "";
  if (OPEN_GATE.has(step.decision_status)) return "";
  return step.decision_status;
}

/**
 * THE WORKFLOW AS A PATH, not a list of rows.
 *
 * A pipeline is a shape — research, then a panel, then a build, then a preview — and an
 * operator reads "where has this got to" off that shape in one glance. Rendering it as nine
 * stacked rows of identical text made them read nine statuses and assemble the shape in their
 * head, every time.
 *
 * The rail carries the state: finished segments are solid, the one in flight animates, what is
 * ahead is dim, and a phase that never happened is dashed rather than green. Clicking a node
 * opens everything known about that step below it — duration, error, what it produced, and the
 * doors to the pages where it can be debugged.
 */
function StepPath({ run, capabilities, targets, hosts, onRetryStep, onSkipStep, onRerun, onStop,
                   onDelete,
                   onStopAndRerun, onBuildOnThis, onRecheck,
                   onSetTarget,
                   onPrepareTarget, onRunPreview, onBound, projectId,
                   previewStarting, busy, actor, roles }: {
  projectId: string;
  actor: string;
  roles: Array<{ role_key: string; display_name?: string | null }>;
  run: Run; capabilities: Capability[]; targets: DeployTarget[]; hosts: DeployHost[];
  onRetryStep: (stepId: string) => void; onRerun: () => void; onRecheck: () => void;
  onSkipStep: (stepId: string, reason: string) => void;
  /** Stop a live run — the work it started, not only its bookkeeping. */
  onStop: () => void;
  onDelete: () => void;
  /** Stop it and start the same workflow again from the beginning, in that order. */
  onStopAndRerun: () => void;
  onBuildOnThis: () => void;
  onSetTarget: (targetId: string) => void; onPrepareTarget: (hostId: string) => void;
  onRunPreview: (buildRunId: string) => void;
  /** Re-read the runs — used after a binding lands so the step shows it. */
  onBound: () => void;
  previewStarting?: string;
  busy: string | null;
}) {
  // Has a preview been asked for on THIS build and not yet appeared? The URL is the finish
  // line; the POST returning is not.
  // THE BUILD A PREVIEW IS FOR. Read from whichever step produced it rather than a step named
  // "build", because a customer can rename their steps and the canonical field is the contract.
  const buildRunId = (() => {
    const fromInput = (run.input ?? {})["adopt_code_task_run_id"];
    if (typeof fromInput === "string" && fromInput.trim()) return fromInput.trim();
    for (const s of run.steps) {
      const produced = (s.output ?? {}) as Record<string, unknown>;
      const direct = produced.code_task_run_id;
      if (typeof direct === "string" && direct.startsWith("crun_")) return direct;
      const awaited = produced.await as { kind?: string; id?: string } | undefined;
      if (awaited?.kind === "code_task" && awaited.id) return awaited.id;
    }
    return null;
  })();
  const byKey = new Map(capabilities.map((c) => [c.key, c]));
  const steps = run.steps;
  // A FINISHED RUN'S ASSIGNMENT IS HISTORY. Offering "+ Assign" on a completed workflow invites
  // a change that cannot affect anything — the work is done and the gates it would have
  // governed are already answered. Shown, not editable.
  // The SERVER's fact (`finished`), not a list kept here — the list this replaced had already
  // left out `compensated`.
  const terminal = run.finished === true;
  // Open on the step that wants attention, then the one in flight, then the first. Landing on
  // "research succeeded" when a gate three steps along is waiting for you is the wrong answer
  // to "what is happening".
  const initial = steps.find(needsYou) ?? stoppedStep(run) ?? steps.find((s) => phaseOf(s) === "now")
    ?? steps.find((s) => phaseOf(s) === "failed") ?? steps[0];
  const [picked, setPicked] = useState<string | null>(initial?.step_id ?? null);
  const current = steps.find((s) => s.step_id === picked) ?? initial;

  // WHERE THE CALLOUT POINTS.
  //
  // The rail wraps, so the picked step can be on either row and its position changes with the
  // window. Measured from the DOM rather than computed from an index: a computed position is
  // right until a label wraps to two lines or the rail reflows, and then it points at the
  // wrong box while looking exactly as confident.
  const railRef = useRef<HTMLOListElement | null>(null);
  const detailRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const place = () => {
      const rail = railRef.current;
      const detail = detailRef.current;
      if (!rail || !detail) return;
      const node = rail.querySelector<HTMLElement>('[data-picked="true"] .dw-node-btn');
      const panel = detail.querySelector<HTMLElement>(".dw-detail");
      if (!node || !panel) {
        detail.style.removeProperty("--dw-arrow-x");
        return;
      }
      // MEASURED IN THE PANEL'S OWN COORDINATES. `offsetLeft` is relative to whichever
      // ancestor happens to be positioned, which is not the panel — so the notch landed at an
      // offset that looked plausible and pointed at the wrong step.
      const box = node.getBoundingClientRect();
      const frame = panel.getBoundingClientRect();
      const centre = box.left + box.width / 2 - frame.left;
      // Never off the end: a notch hanging past the panel's corner reads as a rendering bug
      // rather than as a pointer. 18px keeps it clear of the rounded corner.
      const clamped = Math.max(18, Math.min(frame.width - 18, centre));
      detail.style.setProperty("--dw-arrow-x", `${Math.round(clamped)}px`);
    };
    place();
    // The rail reflows on resize, and a label that wraps moves every box after it.
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [picked, steps.length]);

  // ASSIGNMENT IS STEP ONE, in the chain where it can be seen.
  //
  // It was a panel above the rail and the operator could not find it — "where when creating a
  // new project do we have the option to assign the agents?" It is in the wizard's second
  // screen, which is exactly the problem: a thing you have to go looking for.
  //
  // A UI NODE, NOT A CAPABILITY. Making it a real step would mean editing all twelve catalogue
  // workflows, a new capability in the runtime, and a step that can execute, fail and block a
  // run. Assignment is a property of the app, not a stage of its delivery — so the rail shows
  // it first and the runtime never sees it. It carries no duration and no status badge,
  // because it does not run.
  const AGENTS_NODE = "__agents__";
  const showAgents = picked === AGENTS_NODE;

  return (
    <div className="dw-path" data-testid="dw-path">
      <ol className="dw-rail" aria-label="Workflow steps" ref={railRef}>
        <li className="dw-node dw-node-agents" data-phase="done"
            data-picked={showAgents ? "true" : undefined} data-testid="dw-node-agents">
          <button type="button" className="dw-node-btn"
                  onClick={() => setPicked(showAgents ? (initial?.step_id ?? null) : AGENTS_NODE)}>
            {/* `dw-dot` for the GRID BEHAVIOUR, not the colour: it spans both rows, so the
                first column is the width of the marker. Without it the marker sat in row 1 and
                "auto-confirmed" in row 2 of the SAME column, which sized that column to the
                word and pushed "Agents" to the far right of its own node. */}
            <span className="dw-dot dw-node-dot" aria-hidden>◇</span>
            <span className="dw-node-name">Agents</span>
            <span className="dw-node-meta">
              <span className="detail">{terminal ? "as run" : "auto-confirmed"}</span>
            </span>
          </button>
        </li>
        {steps.map((step, index) => {
          const phase = phaseOf(step);
          const capability = step.capability ? byKey.get(step.capability) : undefined;
          // THE FIRST STEP HAS A PREDECESSOR TOO. It is the Agents node, which is complete
          // before any step runs — so treating "no earlier step" as "nothing has flowed" left
          // Agents and Research as the only pair in the rail with no line between them.
          const previous = index > 0 ? phaseOf(steps[index - 1]) : "done";
          return (
            <li key={step.step_id} className="dw-node" data-phase={phase}
                data-picked={step.step_id === picked} data-testid="dw-node">
              {/* The connector INTO this step: solid once the work has flowed through it,
                  animated while it is flowing, dim before. Drawn for every step including the
                  first, whose predecessor is the Agents node. */}
              <i className="dw-link" aria-hidden
                 data-flow={["done", "skipped", "findings", "failed_on"].includes(previous ?? "")
                   ? (phase === "now" ? "moving" : "past") : "future"} />
              <button type="button" className="dw-node-btn"
                      aria-current={phase === "now" ? "step" : undefined}
                      onClick={() => setPicked(step.step_id)}>
                {/* THE KIND, AS A SHAPE — with the outcome still readable.
                    Ten nodes that differ only by a number and a tick make "find the deploy" a
                    reading task. A glyph is learned once. The states that are NOT plain progress
                    keep their mark, because "this one failed" must never be something you have
                    to infer from an icon. */}
                <span className="dw-dot" data-phase={phase} aria-hidden>
                  {phase === "failed" ? "!"
                    : phase === "failed_on" ? "✕"
                    : phase === "findings" ? "▲"
                    : phase === "skipped" ? "–"
                    : hasStepIcon(step.capability ?? undefined)
                      ? <StepIcon capability={step.capability ?? undefined} />
                      : phase === "done" ? "✓" : index + 1}
                </span>
                <span className="dw-node-name">{capability?.label ?? step.capability ?? step.step_id}</span>
                <span className="dw-node-meta">
                  {needsYou(step) ? (
                    <em className="dw-node-you" data-testid="dw-node-you">needs you</em>
                  ) : phase === "skipped" ? (
                    // "not run" READS AS A FAULT, and the reason was recorded all along.
                    //
                    // Research shows this on most runs and an operator who has watched it run
                    // for four minutes on another run reasonably reads it as broken. It is
                    // not: the step returns `status: not_applicable` with a sentence saying
                    // why — "No research phase — this project was started directly, and the
                    // build was already running when this workflow began" — and the chain
                    // threw that sentence away and printed two words that sound like a defect.
                    // AN OPERATOR'S SKIP IS SAID, NOT HIDDEN IN A TOOLTIP. "skipped ⓘ" read
                    // the same for a branch the run never took and for a failed step somebody
                    // chose to go on without — opposite things. Who and why are on the rail.
                    skippedBy(step) ? (
                      <span className="dw-node-skipped" data-by-operator="true"
                            data-testid="dw-node-skipped-by-operator" title={skippedBy(step)}>
                        {skippedBy(step)}
                      </span>
                    ) : (
                      <span className="dw-node-skipped" data-testid="dw-node-skipped"
                            title={skipReason(step) || "this step did not apply to this run"}>
                        skipped{skipReason(step) ? " ⓘ" : ""}
                      </span>
                    )
                  ) : railMeta(step)}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      {showAgents ? (
        <AgentsOnThis projectId={projectId} actor={actor} roles={roles}
                      appName={appIdentity(run.input as AppRun).name}
                      readOnly={terminal} />
      ) : null}
      {current && !showAgents ? (
        // The custom property is set on this wrapper and INHERITS into .dw-detail, so the
        // notch can be positioned without threading a ref through StepDetail's props.
        <div ref={detailRef} className="dw-detail-wrap">
        <StepDetail
          // KEYED BY THE STEP. Without it React reuses one StepDetail for every step picked on
          // the rail, so state that belongs to one step survived onto the next.
          key={current.step_id}
          step={current}
          // Its position in the run, so the card can wear the same number as the rail node.
          stepIndex={run.steps.findIndex((s) => s.step_id === current.step_id)}
          capability={current.capability ? byKey.get(current.capability) : undefined}
          targets={targets}
          hosts={hosts}
          runInput={run.input ?? {}}
          actions={stepActions(run, current.step_id)}
          onRetry={() => onRetryStep(current.step_id)}
          onSkip={(reason) => onSkipStep(current.step_id, reason)}
          onSetTarget={(targetId) => onSetTarget(targetId)}
          onPrepareTarget={onPrepareTarget}
          buildRunId={buildRunId}
          workflowRunId={run.id}
          onRunPreview={onRunPreview}
          onBound={onBound}
          projectId={projectId}
          previewStarting={previewStarting}
          busy={busy}
        />
        </div>
      ) : null}
      {/* THE REPORT, ONCE THERE IS SOMETHING TO REPORT.
          Not from step zero: a report reading "0 of 10 steps, $0.00, nothing went wrong" is
          noise that teaches people to scroll past the panel before it ever has an answer. It
          appears as soon as one step has finished and keeps filling in, which is also what
          makes it useful DURING a long build rather than only after it. */}
      {run.steps.some((s) => s.status === "succeeded" || s.status === "skipped") ? (
        <RunReportPanel runId={run.id} projectId={projectId} />
      ) : null}

      {/* WORKFLOW MANAGEMENT — exactly the actions the server sent for this run, in its order.
          Which of Re-check, Stop, Stop and run again, Run again, Remove and Build on this
          research a run has is decided by the engine from the run's real state
          (workflow_run_actions.py); see `RunAction`. Nothing here looks at `run.status`. */}
      <RunActionsBar run={run} busy={busy} handlers={{
        recheck: onRecheck, stop: onStop, stop_and_rerun: onStopAndRerun, rerun: onRerun,
        remove: onDelete, build_on_research: onBuildOnThis,
      }} />
    </div>
  );
}

type RunLevelKey = typeof RUN_ACTION_KEYS[number];

/** How each run-level action LOOKS and what it SAYS — never whether it is there.
 *
 *  The history of each, kept with the action it explains:
 *
 *  - recheck — ONE BUTTON, FOR THE WHOLE RUN. The screen re-tests the recorded blockers by
 *    itself on arrival; this is for the operator who has just changed something outside BOSS.
 *  - stop — THE ADVICE NOW EXISTS AS A BUTTON. The screen told an operator to "cancel this run
 *    first" and offered no way to, so three runs piled up on one project.
 *  - stop_and_rerun — ONE PRESS, NOT TWO. Running again while this one is live makes a SECOND
 *    workflow on the same project rather than retrying this one.
 *  - rerun — NOT WHILE IT IS STILL GOING. Offered on a run mid-flight it started a duplicate:
 *    two workflows for one project, both at step 6.
 *  - remove — there was no way to remove a run at all; building one workflow left eighteen on
 *    this screen. Never on a run still going: deleting the record while the work carries on is
 *    the failure that once cost a production database.
 *  - build_on_research — "I don't want to pay for that research twice." Only where a finished
 *    run's research actually succeeded and left a dossier with findings — the rule whose
 *    console-side copy offered it on a cancelled run whose research had failed.
 *
 *  A test holds these keys equal to the engine's, so a new action cannot reach the screen
 *  without its words. */
const RUN_ACTION_VIEW: Record<RunLevelKey, {
  testId: string; tone?: "primary" | "stop"; busyWhen: string[]; busyText: string;
  explainLabel: string; explain: (run: Run) => React.ReactNode;
}> = {
  recheck: {
    testId: "dw-recheck", busyWhen: ["Re-check"], busyText: "Re-checking…",
    explainLabel: "What re-checking does",
    explain: () => "Re-tests what stopped each step against how things are now, and puts back "
      + "the ones whose reason has been dealt with. Nothing runs again unless its blocker is "
      + "genuinely gone.",
  },
  stop: {
    testId: "dw-stop", tone: "stop", busyWhen: ["Stop"], busyText: "Stopping…",
    explainLabel: "What stopping does",
    explain: () => "Stops the work, not just the record: every pending step is cancelled and so "
      + "is anything this run started — including a build that is still going in its own "
      + "sandbox. It cannot be resumed; start it again to try afresh.",
  },
  stop_and_rerun: {
    testId: "dw-stop-and-rerun", busyWhen: ["Stop", "Re-run"], busyText: "Restarting…",
    explainLabel: "What restarting does",
    explain: (run) => <>
      Stops this run and starts {run.workflow_name} again from the beginning on the same
      directive. Starting it again WITHOUT stopping would leave two workflows running on one
      project, which is why it happens in that order. For one step that needs another go, use
      &ldquo;Try … again&rdquo; on the step instead — that keeps everything already done.
    </>,
  },
  rerun: {
    testId: "dw-rerun", busyWhen: ["Re-run"], busyText: "Starting…",
    explainLabel: "What running it again does",
    explain: (run) => <>
      Starts {run.workflow_name} again on the same directive — its own research, its own
      panel, its own build. The existing run is left as it is.
    </>,
  },
  remove: {
    testId: "dw-delete-run", tone: "stop", busyWhen: ["Remove"], busyText: "Removing…",
    explainLabel: "What removing does",
    explain: () => "Takes this run off the screen for good, with its steps and its history. "
      + "Nothing it produced is touched — files it wrote, issues it opened and anything it "
      + "published stay exactly where they are. It cannot be undone.",
  },
  build_on_research: {
    testId: "dw-build-on-this", tone: "primary", busyWhen: ["Build on this research"],
    busyText: "Starting…", explainLabel: "What building on this research does",
    explain: () => <>
      Starts Standard Product Delivery on the same directive and hands it this
      run&rsquo;s dossier. The research step adopts what is already there — it does not
      run again, and it does not cost anything a second time. The panel that follows argues
      from those findings.
    </>,
  },
};

/** The run's workflow-management row: one button per run-level action the server sent.
 *
 *  A ROW OF ACTIONS, NOT A COLUMN OF ESSAYS — each sentence sits on the dot beside its own
 *  button. A DISABLED ACTION SAYS WHY, on the button (`title`) and in its dot, because a greyed
 *  button with no reason reads as broken. */
export function RunActionsBar({ run, busy, handlers }: {
  run: Run; busy: string | null;
  handlers: Record<RunLevelKey, () => void>;
}) {
  const actions = runLevelActions(run)
    .filter((a): a is RunAction & { key: RunLevelKey } => a.key in RUN_ACTION_VIEW);
  if (!actions.length) return null;
  return (
    <div className="dw-run-actions" data-testid="dw-run-actions">
      {/* NAMING THE GROUP: none of these is part of the step above. */}
      <span className="dw-run-actions-label">Workflow management</span>
      {actions.map((action) => {
        const view = RUN_ACTION_VIEW[action.key];
        return (
          <span className="dw-run-action" key={action.key}>
            <button type="button"
                    className={`cap-btn${view.tone === "primary" ? " primary"
                      : view.tone === "stop" ? " dw-stop" : ""}`}
                    onClick={handlers[action.key]}
                    disabled={!!busy || !action.enabled}
                    title={action.reason ?? undefined}
                    data-enabled={action.enabled ? "true" : "false"}
                    data-testid={view.testId}>
              {busy && view.busyWhen.includes(busy) ? view.busyText : action.label}
            </button>
            <Explain label={view.explainLabel}>
              {action.enabled ? view.explain(run) : `Not available: ${action.reason}.`}
            </Explain>
          </span>
        );
      })}
    </div>
  );
}

/**
 * WHERE THIS SHIPS — the half of the release decision that was missing.
 *
 * `deploy_app` refuses unless an ACTIVE target exists for the environment, and it never creates
 * one: it does not make infrastructure. So a project built for the first time had nowhere to
 * go, and the gate could only offer targets belonging to OTHER apps — picking one would have
 * shipped this app over somebody else's.
 *
 * Choosing a HOST is the act that was missing. A target is that host plus this app's name, its
 * environment and a free port, and the button that makes one also points the run at it.
 */
function DeployWhere({ targets, hosts, runInput, buildRunId, busy, bound, hostName,
                      onSetTarget, onPrepareTarget, onBound }: {
  targets: DeployTarget[]; hosts: DeployHost[]; runInput: Record<string, unknown>;
  /** The code task whose build is being shipped — the app the target binds to. */
  buildRunId: string;
  busy: string | null;
  /** The target this app is ALREADY bound to, resolved by app identity. Settled, not a form. */
  bound?: DeployTarget;
  /** That target's host, in the words the host list uses. */
  hostName?: string;
  onSetTarget: (targetId: string) => void; onPrepareTarget: (hostId: string) => void;
  onBound: () => void;
}) {
  // CHANGING WHERE IT SHIPS IS RARE; BEING TOLD WHERE IT SHIPS IS NOT.
  //
  // The form was rendered whenever the console had not noticed the binding, which read as "this
  // is not done yet" over a target that existed. Once bound, this states the destination and
  // keeps the form one deliberate click away.
  const [changing, setChanging] = useState(false);
  const chosenId = typeof runInput.target_id === "string" ? runInput.target_id : "";
  const chosen = targets.find((t) => t.id === chosenId);
  // THE APP'S NAME, NOT THE BRIEF IT WAS BUILT FROM.
  //
  // This was the raw title — the operator's whole directive — and it is the DEFAULT VALUE of
  // the "Set target" form, so pressing that button created a deployment target called
  // "Build a lead triage inbox.Who it is for: a small team drowning in in…". Target resolution
  // matches on app identity, so the app then had no target it could recognise, and the deploy
  // refused with "no 'staging' target of its own" while listing that very target among the
  // ones belonging to OTHER apps. Measured on deptgt_68bc4259, created by executive_console.
  //
  // `appIdentity` is the namer the rest of the console titles cards with; the form should have
  // been using it all along.
  const appName = appIdentity(runInput as AppRun).name;
  // A target for THIS app, not one of the others. Matching by name is how the API keys them.
  const mine = targets.filter((t) => t.app_name === appName);

  // ALREADY BOUND. State it as a fact and stop showing a form.
  //
  // `bound` is resolved by app IDENTITY against the whole target list, which is stricter than
  // either of the two things this used to check: `chosen` needs a `target_id` on the run input
  // (the set-target path does not always write one) and `mine` compares names by string
  // equality (and 'Lead‐Triage Inbox' carries a U+2010 hyphen). Both said "unbound" over a live
  // target, which is what put an empty form in front of an operator who had already filled it.
  const settled = bound ?? chosen;
  if (settled && !changing) {
    const where = (settled.domains ?? []).find((d: string) => !!d);
    return (
      <div className="dw-bound" data-testid="dw-deploy-bound">
        <span className="dw-bound-ok" aria-hidden>✓</span>
        <span>
          Ships to <strong>{settled.app_name}</strong> · {settled.environment}
          {hostName ? <> on <strong>{hostName}</strong></> : null}
          {where ? <span className="detail"> ({where})</span> : null}
        </span>
        <button type="button" className="cap-btn dw-bound-change" disabled={!!busy}
                data-testid="dw-deploy-change" onClick={() => setChanging(true)}>
          Change
        </button>
      </div>
    );
  }
  if (mine.length && !changing) {
    return (
      <label className="mr-field-label dw-target">
        Ship to
        <select className="mr-select" data-testid="dw-deploy-target" defaultValue=""
                disabled={!!busy}
                onChange={(e) => { if (e.target.value) onSetTarget(e.target.value); }}>
          <option value="">Choose a target…</option>
          {mine.map((t) => (
            <option key={t.id} value={t.id}>{t.app_name} · {t.environment}</option>
          ))}
        </select>
      </label>
    );
  }
  if (hosts.length) {
    return (
      <>
        <BindToHost hosts={hosts} appName={appName} buildRunId={buildRunId} busy={busy}
                    onPrepareTarget={onPrepareTarget}
                    onBound={() => { setChanging(false); onBound(); }} />
        {changing ? (
          <button type="button" className="cap-btn dw-bound-change"
                  onClick={() => setChanging(false)}>
            Keep the current target
          </button>
        ) : null}
      </>
    );
  }
  return (
    <p className="dw-target-set detail" data-testid="dw-deploy-nowhere">
      Nowhere to ship: this project has no verified host. Add one under Infra and Models, then
      come back — approving now would fail at the first connection.
    </p>
  );
}

/** Everything known about one step, in the order an operator asks for it. */
/** Is this the deploy step?
 *
 *  `step.capability` IS NULL on the wire — the runs endpoint does not populate it — so
 *  `step.capability === "deploy"` was false every time, and the target picker never rendered
 *  in ANY of the three versions shipped. Three fixes to a control whose only real problem was
 *  that its condition could not be true.
 *
 *  step_id is what the runtime actually carries; the capability prop resolved from the
 *  catalogue is the fallback for a workflow whose author renamed the step.
 */
//: A run that has not finished. Re-running one of these produces a duplicate workflow rather
//: than a retry, which is exactly what an operator hit: two runs for one project, both live.
const ACTIVE_RUN_STATES = new Set(["running", "waiting", "ready", "leased", "queued", "paused"]);

/** PLAN A DEPLOYMENT, FROM THE STEP WHOSE JOB IS SHIPPING.
 *
 *  A deploy step marked SUCCEEDED whose own detail read `status=blocked`, on an app that
 *  answers "not found" — and no control anywhere to try again. The operator set the target
 *  repeatedly, which was never the problem: binding says WHERE it would go, and nothing said
 *  GO. The gate only appears while a run is waiting at it, so once the step had been recorded
 *  as done there was no way to ship at all, from any screen.
 *
 *  The `deploy` op creates a ChangeSet for review. It cannot claim a deployment is running
 *  or shipped until that ChangeSet is approved and applied.
 */
function ShipNow({ projectId, buildRunId, target, alreadyLive, onDone }: {
  projectId: string; buildRunId: string; target: DeployTarget;
  /** Something already serves at this target; the plan proposes replacing it. */
  alreadyLive: boolean;
  onDone: () => void;
}) {
  const [state, setState] = useState<"idle" | "planning" | "planned">("idle");
  const [plan, setPlan] = useState<PlannedDeployment | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const ship = useCallback(async () => {
    setState("planning");
    setMsg(null);
    setPlan(null);
    try {
      const r = await apiWrite("/api/app-deploy", {
        method: "POST", headers: { "Content-Type": "application/json" }, cache: "no-store",
        body: JSON.stringify({
          op: "deploy", project_id: projectId, run_id: buildRunId,
          target_id: target.id, environment: target.environment,
          actor: "executive_console",
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        setState("idle");
        setMsg(d?.error ?? `could not plan the deployment (HTTP ${r.status})`);
        return;
      }
      const nextPlan = plannedDeploymentFrom(d);
      if (!nextPlan) {
        setState("idle");
        setMsg("Deployment request did not return a reviewable ChangeSet. No deployment was confirmed.");
        return;
      }
      setPlan(nextPlan);
      setState("planned");
      onDone();
    } catch (e) {
      setState("idle");
      setMsg(String((e as Error)?.message ?? e));
    }
  }, [projectId, buildRunId, target, onDone]);

  return (
    <div className="dw-ship" data-testid="dw-ship">
      <button type="button" className="cap-btn primary" disabled={state !== "idle"}
              onClick={ship} data-testid="dw-ship-now">
        {state === "planning" ? "Planning…"
          : alreadyLive ? "Plan redeployment" : "Plan deployment"}
      </button>
      <span className="detail">
        {alreadyLive
          ? <>Creates a ChangeSet to review before replacing what runs at{" "}
               {target.domains?.[0] ?? target.app_name}.</>
          : <>Creates a ChangeSet to review before deploying <b>{target.app_name}</b> · {target.environment}
             {target.domains?.[0] ? ` (${target.domains[0]})` : ""}.</>}
      </span>
      {plan ? (
        <span className="detail dw-ship-msg" data-testid="dw-ship-plan" role="status">
          Nothing has deployed. ChangeSet <code>{plan.changeset_id}</code> is ready.
          Next, open <a href={consoleHref(
            typeof window !== "undefined" ? window.location.search : "", { area: "infra" },
          )}>ProviderOps ChangeSet Review</a>, approve it, then apply it.
        </span>
      ) : null}
      {msg ? <span className="detail dw-ship-msg" data-testid="dw-ship-msg" role="alert">{msg}</span> : null}
    </div>
  );
}

/** Open the console's side dock with a question already typed.
 *
 *  An operator reading a step that refuses to ship should not have to copy the error, find the
 *  Agents area and retype the context. The dock already exists at the shell level and is
 *  driven by two search params — this is the same handoff the Infra panel uses, so the
 *  workflow view gains the agent without growing a second chat surface of its own.
 *
 *  `agent` carries the RUN, so the conversation resolves what it is about server-side rather
 *  than the console shipping a context blob.
 */
/** WHO ANSWERS FOR THIS STEP: the workflow's choice, else the capability's default.
 *
 *  Two steps can run the same capability against different accounts — "publish to our channel"
 *  and "publish to a client's" are one step kind and two agents — so the workflow has to be
 *  able to say. One function, because a resolver used in two places with two orders is a bug
 *  waiting for the second caller.
 */
function stepCaptain(step: RunStep, capability?: Capability): string {
  const named = String(step.captain ?? "").trim();
  return named || String(capability?.captain ?? "").trim();
}

/** A registered role key, as an operator would say it. */
function roleLabel(roleKey: string): string {
  return roleKey.replace(/_/g, " ").replace(/\b[a-z]/g, (c) => c.toUpperCase());
}

/** Open the dock about `subject`, addressed to the step's captain.
 *
 *  ADDRESSED, NOT BROADCAST: the captain answers from that role's own record rather than as
 *  BOSS-in-general with a name in the banner. And ABOUT THE THING, NOT A PARAPHRASE OF IT — see
 *  lib/dock-subject.ts for boss-1, where a paraphrase of a Preview step was answered as a
 *  question about deployments. */
function askBossAbout(subject: DockSubject | null, question: string, captain?: string): void {
  openDockAbout({ subject, question, role: captain ? roleLabel(captain) : null });
}

/** What the operator is asking about a step, in their words — and nothing else.
 *
 *  This used to be "On the Preview step (status failed), BOSS says: \"<error cut at 400
 *  characters>\". Why is it in this state, and what is the fix? Check the app's code and the
 *  host before answering." The error now travels whole, as the step's own record, resolved by
 *  the server from the subject; and "check the host" was an instruction to go exploring that
 *  sent boss-1's turn to the deployment tools. */
export function stepQuestion(step: Pick<RunStep, "step_id" | "status">, label?: string): string {
  const name = label || step.step_id;
  switch (step.status) {
    case "failed":
    case "deadlettered":
      return `Why did the ${name} step fail, and what is the fix?`;
    case "waiting":
    case "blocked":
      return `What is the ${name} step waiting on, and what do I need to do?`;
    case "running":
    case "leased":
      return `Is the ${name} step making progress, or is it stuck?`;
    default:
      return `Why is the ${name} step ${step.status}?`;
  }
}

/** ANSWER THE GATE WHERE THE DECISION CAN BE READ.
 *
 *  Two buttons on the step answered a release without showing what was being released — no
 *  recommendation, no cost, no risk tier, no note, and no record of who else is waiting on the
 *  same call. Approving a deployment off a step badge is a decision made blind. The Approvals
 *  page is where that context lives, so this goes there with the decision already selected.
 */
function ReviewAndDecide({ step }: { step: RunStep }) {
  const role = (step.approval as { approver_role?: string })?.approver_role;
  return (
    <div className="dw-step-decide" data-testid="dw-step-decide">
      {/* PRIMARY AGAIN, because it is now only rendered when it can actually be pressed — the
          blocked case does not draw a button at all rather than a quieter one. */}
      <a className="cap-btn primary dw-decide-btn"
         data-testid="dw-step-review"
         href={consoleHref(typeof window !== "undefined" ? window.location.search : "",
                           { area: "decisions", decision_filter: "needs_action",
                             decision: step.decision_id })}>
        Review and decide →
      </a>
      {/* NOT THE ROLE AGAIN. The step header says "needs release_manager"; repeating it here
          is the fourth time that name appears on one screen. When there is no role to name,
          the sentence still earns its place by saying what the link opens. */}
      {role ? null : (
        <span className="detail">
          Opens the approval with its recommendation, cost and risk.
        </span>
      )}
    </div>
  );
}

/** BIND THIS APP TO A HOST, ON THE STEP WHERE SHIPPING IS DECIDED.
 *
 *  Asked for repeatedly, and what shipped each time was less than what was asked for: a bare
 *  host dropdown that fired on change. That is not a binding, it is a guess — it picks the
 *  name and the port for you and gives you nothing to confirm. The project card has had the
 *  real control all along (host · name · port · Set target), and the deploy step, which is
 *  exactly where an operator is deciding where this build goes, had a lesser version.
 *
 *  Same three fields, same explicit button, same endpoint the project card posts to. The
 *  operator sets the target and THEN approves the release; nothing is chosen on their behalf.
 */
function BindToHost({ hosts, appName, buildRunId, busy, onPrepareTarget, onBound }: {
  hosts: DeployHost[]; appName: string; buildRunId: string; busy: string | null;
  onPrepareTarget: (hostId: string) => void;
  /** Re-read the runs once the binding lands, so the step SHOWS what it is bound to. */
  onBound: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  return (
    <form
      className="dw-bind"
      data-testid="dw-bind-form"
      onSubmit={async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const fd = new FormData(e.currentTarget as HTMLFormElement);
        const hostId = String(fd.get("host_id") || "");
        if (!hostId) { setMsg("choose a host first"); return; }
        setSaving(true); setMsg(null);
        // WITHOUT A BUILD THERE IS NO APP TO BIND, so fall back to the run-scoped path that
        // creates the target from the workflow's own title. Both end at the same target row.
        if (buildRunId) {
          try {
            const r = await apiWrite("/api/app-deploy", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                op: "set-target", actor: "executive_console", run_id: buildRunId,
                host_id: hostId, app_name: fd.get("app_name"), app_port: fd.get("app_port"),
              }),
              cache: "no-store",
            });
            const d = await r.json().catch(() => ({}));
            setMsg(r.ok
              ? `bound to ${d?.host ?? "the host"} — approve the release below to ship it`
              : (d?.error ?? `could not set the target (HTTP ${r.status})`));
            // WITHOUT THIS THE SCREEN DOES NOT MOVE. The write succeeded and the step went on
            // showing the same empty form, which reads exactly like a button that did nothing.
            if (r.ok) onBound();
          } catch (err) {
            setMsg(String((err as Error)?.message ?? err));
          }
        } else {
          onPrepareTarget(hostId);
          setMsg("binding…");
        }
        setSaving(false);
      }}
    >
      <span className="detail">App&rsquo;s target:</span>
      <select name="host_id" className="mr-select" data-testid="dw-deploy-host"
              aria-label="Host to deploy this app to" defaultValue={hosts[0]?.id ?? ""}>
        {hosts.map((h) => (
          <option key={h.id} value={h.id}>
            {h.name}{h.address ? ` · ${h.address}` : ""}
          </option>
        ))}
      </select>
      {/* The DISPLAY name and the name a container is created under are not always the same
          string, and the second is the one a deploy has to live with. */}
      <input name="app_name" defaultValue={appName} data-testid="dw-deploy-appname"
             aria-label="App name on the host" style={{ width: 150, fontSize: 12 }} />
      <input name="app_port" placeholder="port (auto)" data-testid="dw-deploy-port"
             aria-label="App port — blank lets BOSS pick a free one"
             style={{ width: 96, fontSize: 12 }} />
      <button className="cap-btn" type="submit" disabled={saving || !!busy}
              data-testid="dw-deploy-settarget">
        {saving ? "Saving…" : "Set target"}
      </button>
      {msg ? <span className="detail dw-bind-msg">{msg}</span> : null}
    </form>
  );
}

/** The hosts an operator can actually ship to, out of whatever the endpoint answered.
 *
 *  `hosts`, not `items`. GET /v1/deployment-hosts answers {"hosts": [...]} while the TARGETS
 *  endpoint answers {"items": [...]}, and this read `items` from the hosts response — got
 *  undefined, filtered an empty list, and told the operator "this project has no verified
 *  host. Add one under Infra and Models" while Infra showed hetzner-ci-01 VERIFIED and READY
 *  TO DEPLOY. Extracted so the shape is asserted by a test rather than by a screenshot.
 */
export function verifiedHostsFrom(body: unknown): DeployHost[] {
  const b = (body ?? {}) as { hosts?: unknown; items?: unknown };
  const raw = Array.isArray(b.hosts) ? b.hosts : Array.isArray(b.items) ? b.items : [];
  return (raw as DeployHost[]).filter(
    (h) => ["verified", "ready", "active"].includes(h?.status ?? ""));
}

// Same lesson as isDeployStep: `capability` is not always on the wire, and gating the ONLY
// preview controls on it alone left a step titled "Preview" with no way to run one.
export function isPreviewStep(step: RunStep): boolean {
  return step.step_id === "preview" || step.capability === "preview";
}

export function isDeployStep(step: RunStep, capability?: Capability): boolean {
  return step.step_id === "deploy" || step.capability === "deploy"
    || capability?.key === "deploy";
}


/**
 * An explanation attached to the thing it explains.
 *
 * "Instead of a full paragraph we can go with a tooltip." Right, and better than what I had:
 * a per-STEP `?` reveals every sentence at once, which means the operator still has to work out
 * which sentence is about the control they are looking at. A dot beside each control answers one
 * question, where the question is asked.
 *
 * Click, not hover: hover has no touch or keyboard equivalent, and these sentences are the ones
 * a new operator most needs. Escape and click-away close it, because a popover that only closes
 * by pressing the dot again traps the pointer.
 */
function Explain({ children, label = "What this does" }: {
  children: React.ReactNode; label?: string;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <span className="dw-explain" ref={wrap}>
      <button type="button" className="dw-explain-dot" data-testid="dw-explain"
              aria-expanded={open} aria-label={label}
              onClick={() => setOpen((o) => !o)}>i</button>
      {open ? (
        <span className="dw-explain-pop" role="note" data-testid="dw-explain-pop">
          {children}
        </span>
      ) : null}
    </span>
  );
}

/**
 * Teaching copy, shown until it has taught.
 *
 * Every sentence on the deploy step was added to fix a real confusion — "the user might click
 * Review and decide without setting the target", "he may click Set target multiple times" — and
 * each one is correct. What none of them accounted for is the twentieth reading. Eight lines of
 * prose around three controls is right once and noise thereafter, and the feedback was exactly
 * that: good information, reads as distraction.
 *
 * So it is not deleted, it is EARNED OUT. Verbose until the operator has completed the thing it
 * explains, quiet after, and one press of `?` brings it all back for good. Per browser, because
 * "have you done this before" is a fact about the person, not about the run.
 */
function useLearned(topic: string): [boolean, () => void] {
  const key = `boss.learned.${topic}`;
  // Starts false on the server AND on the first client paint, so the markup matches and the
  // hint is never hydrated away mid-read; the effect below settles it.
  const [learned, setLearned] = useState(false);
  useEffect(() => {
    try { setLearned(window.localStorage.getItem(key) === "1"); } catch { /* private mode */ }
  }, [key]);
  const mark = useCallback(() => {
    try { window.localStorage.setItem(key, "1"); } catch { /* private mode */ }
    setLearned(true);
  }, [key]);
  return [learned, mark];
}

/** "Try … again" and "Skip and proceed" for a step whose work failed.
 *
 *  BACK IN THE QUEUE. A step that failed for a reason since dealt with — a target that did not
 *  exist, a service that was down — had no way back: the run sat stopped and the only recourse
 *  was to start the whole workflow again and repeat everything before it.
 *
 *  OR GO ON WITHOUT IT. A failed step stops its run — every kind of step, since research that
 *  died was shown green and the run walked on into a panel and a build (boss-1, 2026-10-02).
 *  Whether the run proceeds without it is the operator's call, so both choices sit together.
 *  Skip is not offered on an approval: a refused gate is somebody's "no", and skipping it would
 *  turn that into a yes.
 *
 *  ONE COMPONENT, TWO PLACES. The step's own detail, and the run card itself — where a stopped
 *  run says why it stopped and offers both answers without being opened first. `idPrefix`
 *  keeps the two sets of test ids distinct when both are on screen. */
export function FailedStepActions({ step, capability, actions, busy, onRetry, onSkip,
                            idPrefix = "dw-step" }: {
  step: RunStep; capability?: Capability; busy: string | null;
  /** This step's Try again and Skip, as the server decided them (`stepActions`). Whichever is
   *  absent is not rendered; whichever is disabled says why. Nothing here reads a status. */
  actions: { retry?: RunAction; skip?: RunAction };
  onRetry: () => void; onSkip: (reason: string) => void; idPrefix?: string;
}) {
  // The reason box for "Skip and proceed" — open only after the operator asks to skip, so the
  // decision is two deliberate acts: choose to skip, then say why.
  const [skipOpen, setSkipOpen] = useState(false);
  const [skipWhy, setSkipWhy] = useState("");
  const name = capability?.label ?? step.step_id;
  const { retry, skip } = actions;
  if (!retry && !skip) return null;
  return (
    <div className="dw-step-decide" data-testid={`${idPrefix}-decide`}>
      {retry ? (
        <>
          <button type="button" className="cap-btn primary" onClick={onRetry}
                  disabled={!!busy || !retry.enabled} title={retry.reason ?? undefined}
                  data-testid={`${idPrefix}-retry`}>
            {busy === "Retry" ? "Queueing…" : retry.label}
          </button>
          <Explain label="What retrying this step does">
            {retry.enabled ? "Runs this step only; everything before it stays done."
              : `Not available: ${retry.reason}.`}
          </Explain>
        </>
      ) : null}
      {skip ? (
        <button type="button" className="cap-btn" onClick={() => setSkipOpen(true)}
                disabled={!!busy || skipOpen || !skip.enabled} title={skip.reason ?? undefined}
                data-testid={`${idPrefix}-skip`}>
          {busy === "Skip" ? "Skipping…" : skip.label}
        </button>
      ) : null}
      {skip ? (
        <Explain label="What skipping this step does">
          {skip.enabled
            ? "Marks it skipped with your reason and carries on. Steps that use what it would "
              + "have produced are told it was skipped — never handed a made-up value."
            : `Not available: ${skip.reason}.`}
        </Explain>
      ) : null}
      {skip?.enabled && skipOpen ? (
        <form className="dw-skip-why" data-testid={`${idPrefix}-skip-form`}
              onSubmit={(e) => {
                e.preventDefault();
                if (!skipWhy.trim()) return;
                onSkip(skipWhy.trim());
                setSkipOpen(false);
                setSkipWhy("");
              }}>
          <label>
            Why go on without {name}?{" "}
            <input type="text" value={skipWhy} autoFocus required
                   data-testid={`${idPrefix}-skip-reason`}
                   placeholder="kept on the run's record"
                   onChange={(e) => setSkipWhy(e.target.value)} />
          </label>
          <button type="submit" className="cap-btn primary"
                  disabled={!!busy || !skipWhy.trim()} data-testid={`${idPrefix}-skip-confirm`}>
            Skip it
          </button>
          <button type="button" className="linklike"
                  onClick={() => { setSkipOpen(false); setSkipWhy(""); }}>Cancel</button>
        </form>
      ) : null}
    </div>
  );
}

function StepDetail({ step, stepIndex, capability, targets, hosts, runInput, onRetry, onSkip,
                     actions,
                     onSetTarget, onPrepareTarget, onRunPreview, onBound, projectId, buildRunId,
                     workflowRunId, previewStarting, busy }: {
  projectId: string;
  /** The workflow run this step belongs to — what "Ask about this step" is about. */
  workflowRunId?: string;
  /** This step's Try again / Skip, from the run's server-decided `actions`. */
  actions: { retry?: RunAction; skip?: RunAction };
  /** Where this step sits in the run, so the card can carry the rail's number. */
  stepIndex?: number;
  step: RunStep; capability?: Capability; targets: DeployTarget[]; hosts: DeployHost[];
  runInput: Record<string, unknown>;
  onRetry: () => void; onSetTarget: (targetId: string) => void;
  /** Skip this failed step and let the run proceed. Never offered on an approval gate. */
  onSkip: (reason: string) => void;
  onPrepareTarget: (hostId: string) => void;
  /** The code task this run built — what a preview is started FOR. */
  buildRunId: string | null;
  onRunPreview: (buildRunId: string) => void;
  /** Re-read the runs — used after a binding lands so the step shows it. */
  onBound: () => void;
  /** The build whose preview was asked for and has not arrived yet. */
  previewStarting?: string;
  busy: string | null;
}) {
  const phase = phaseOf(step);
  // HAS ANYONE SHIPPED FROM THIS PROJECT? — not "has this browser".
  //
  // Keying only on localStorage was wrong in the one case that matters: an operator who has
  // bound targets and shipped for weeks has no flag, so they got the full first-run lecture and
  // not even the `?` to close it. They looked at the new build and correctly saw no change.
  //
  // An existing deployment target is proof somebody has done this. The browser flag still
  // counts — it is set the moment a target is bound, which covers the operator whose first act
  // is the one being explained.
  const [flaggedDeploy, markDeployLearned] = useLearned("deploy_step");
  const learnedDeploy = flaggedDeploy || targets.length > 0;
  const [helpOpen, setHelpOpen] = useState(false);
  const showHelp = helpOpen || !learnedDeploy;
  // THE ADDRESS OF THE THING THAT WAS JUST SHIPPED.
  //
  // Only on a deploy that SUCCEEDED, and only from the target this run is actually bound to —
  // `domains` holds "host:port", which is what the deployer published. Linking on anything
  // less than success would repeat the mistake this view keeps making: reporting a state the
  // system is not in.
  //
  // WHICH TARGET, ASKED OF THE DEPLOY THAT RAN — then of the run, then of the app's name.
  //
  // This asked the run's input for a `target_id` and otherwise compared `t.app_name` to the raw
  // TITLE, which is the operator's whole directive. So the app whose title is literally its name
  // ("Change Calendar") got its link and every other app got nothing: Lead-Triage Inbox shipped
  // successfully to deptgt_33af7f on 192.0.2.202:3014 and the step showed no link at all.
  //
  // The step's own output is the authority here — it records the target this deployment
  // actually used — and the name comparison is by app identity, which is how the rest of the
  // console decides two names are one app. ("Lead‐Triage Inbox", as stored, carries a U+2010
  // hyphen; string equality would not have matched it even against the right name.)
  const shippedTo = (() => {
    const out = (step.output ?? {}) as Record<string, unknown>;
    const fromStep = typeof out.target_id === "string" ? out.target_id : "";
    const byStep = targets.find((t) => t.id === fromStep);
    if (byStep) return byStep;
    const id = typeof runInput.target_id === "string" ? runInput.target_id : "";
    const byId = targets.find((t) => t.id === id);
    if (byId) return byId;
    const wanted = identityKey(appIdentity(runInput as AppRun).name);
    return wanted
      ? targets.find((t) => identityKey(String(t.app_name ?? "")) === wanted)
      : undefined;
  })();
  // The machine, in the words the host list uses. A target row carries a host id, and
  // "dh_361e66c46098" is not an answer to "where does this go".
  const boundHostName = (() => {
    const hostId = String(shippedTo?.host_id ?? "");
    const host = hosts.find((h) => h.id === hostId);
    if (!host) return "";
    return `${host.name}${host.address ? ` · ${host.address}` : ""}`;
  })();
  // THE TARGET CONTROL, MADE ONCE AND PLACED WHEREVER THE LAYOUT PUTS IT. It renders the bound
  // target as a fact with Change, or the form to set one — and `onBound` is also what records
  // that this operator has now done it, which is what quietens the step's teaching copy.
  const targetControl = (
    <DeployWhere targets={targets} hosts={hosts} runInput={runInput}
                 buildRunId={buildRunId ?? ""} busy={busy}
                 bound={shippedTo} hostName={boundHostName}
                 onSetTarget={onSetTarget} onPrepareTarget={onPrepareTarget}
                 onBound={() => { markDeployLearned(); onBound(); }} />
  );
  // WHETHER SOMETHING IS ACTUALLY SHIPPED THERE — asked of the deployments, not of the step.
  //
  // This first used the step's own status, and the step is exactly what cannot be trusted
  // here: HookWatch's deploy step reads SUCCEEDED because of a blocked run recorded before
  // `blocked` stopped counting as success. So the console offered "Open the app →
  // 192.0.2.202:8000" and "Replaces what is running there" for an address with no container on
  // it at all. A link to a dead app is the same lie in a new place.
  const [lastDeploy, setLastDeploy] = useState<{ status: string } | null>(null);
  useEffect(() => {
    if (!isDeployStep(step, capability) || !buildRunId) return;
    let alive = true;
    // THE APP'S NAME, NOT THE BRIEF. This asked for deployments of an app called "Build a lead
    // triage inbox.\nWho it is for: …", got none, and concluded nothing had shipped — which
    // suppressed the link even when the target resolved.
    const name = appIdentity(runInput as AppRun).name;
    (async () => {
      try {
        const r = await fetch(
          `/api/run-deployments?run_ids=${encodeURIComponent(buildRunId)}`
          + `&app_name=${encodeURIComponent(name)}`, { cache: "no-store" });
        if (!r.ok) return;
        const body = await r.json();
        const list = (body?.runs?.[buildRunId]?.deployments ?? []) as Array<{
          status?: string; started_at?: string;
        }>;
        const newest = [...list].sort((a, b) =>
          String(b.started_at ?? "").localeCompare(String(a.started_at ?? "")))[0];
        if (alive && newest) setLastDeploy({ status: String(newest.status ?? "") });
      } catch { /* no deployment history: the step simply offers to ship */ }
    })();
    return () => { alive = false; };
  }, [buildRunId, step, capability, runInput]);

  const liveAppUrl = (() => {
    if (!isDeployStep(step, capability)) return "";
    if (lastDeploy?.status !== "succeeded") return "";
    const domain = (shippedTo?.domains ?? []).find((d: string) => !!d);
    if (!domain) return "";
    return /^https?:\/\//.test(domain) ? domain : `http://${domain}`;
  })();
  const blocker = step.blocker ?? null;
  // The live preview is resolved per-step by the API and lives ON the step, not in the output
  // the runtime recorded — merged here so the link builder sees one object.
  const merged = { ...(step.output || {}),
                   ...(step.live_preview ? { live_preview: step.live_preview } : {}) };
  const runAppTitle = typeof runInput.title === "string" ? runInput.title : "";
  const links = stepLinks(merged, runAppTitle);
  const previewHref = links.find((l) => l.label === "open the preview")?.href ?? null;
  // ONE WAY IN. The preview block renders "Open preview" as a button; leaving the link of the
  // same name in the row below it puts the identical href on screen twice, four words apart.
  const visibleLinks = links.filter((l) => l.label !== "open the preview");
  // Asked for on THIS build and not yet serving. The URL is the finish line; a POST returning
  // is not — which is why the old busy flag cleared a second after the click.
  const starting = !!previewStarting && previewStarting === buildRunId && !previewHref;
  const approval = step.approval as { mode?: string; approver_role?: string };
  // A manual gate that has not been answered. The one condition under which "waiting" and
  // "gate · <role>" are the same sentence twice.
  const gateWaiting = approval?.mode === "manual" && step.status === "waiting";
  // WHAT THE STEP RECORDED — de-duplicated, and not on the page by default.
  //
  // Rendered flat, this was the heaviest thing on a succeeded step: six labelled fields of
  // which AWAIT and RUN ID held the SAME id, BINDING ID was "—", and PROJECT ID and REPOSITORY
  // are facts about the whole run rather than this step. None of it is an action and none of it
  // is read twice. It is evidence, so it is kept and filed.
  const produced = Object.entries(step.output || {})
    .filter(([k]) => !["capability", "tool", "inputs", "note", "status"].includes(k))
    // An empty field is a row that says nothing at all.
    .filter(([, v]) => !(v === null || v === undefined || v === ""))
    // A LOOPBACK URL IS NOT AN ADDRESS ANYBODY CAN USE. `preview_url` is where the app listens
    // ON THE HOST — 127.0.0.1:42543 — and printing it under a heading reading PREVIEW URL
    // invites somebody to copy it, paste it, and get nothing. The link above is the openable
    // one; this grid is for facts, and that is not one.
    .filter(([, v]) => !(typeof v === "string" && isLoopbackPreviewUrl(v)))
    .slice(0, 6)
    // THE SAME ID UNDER TWO HEADINGS IS ONE FACT AND TWO ROWS. `await` and `run_id` are
    // routinely identical on a build step; the first one wins.
    .filter(([, v], i, all) =>
      typeof v !== "string" || all.findIndex(([, w]) => w === v) === i);

  return (
    <section className="dw-detail" data-phase={phase} data-testid="dw-detail">
      <header className="dw-detail-head">
        {/* ONE CHIP, NOT TWO THAT EACH SAY HALF.
            A gated step showed `waiting` AND `gate · release_manager`, while the run header
            above said "waiting on release_manager" and the button below said "release_manager
            signs this off" — the same fact four times on one screen. A gate that is waiting IS
            "needs <role>", so it is said once, here. */}
        {gateWaiting ? null : (
          <span className="dw-badge"
                data-tone={phase === "skipped" ? "muted"
                  : phase === "failed_on" ? "bad" : tone(step.status)}
                data-testid={phase === "failed_on" ? "dw-step-failed-on"
                  : phase === "skipped" ? "dw-step-na" : undefined}>
            {phase === "failed_on" ? "failed — went on"
              // Same wording as the chain node, for the same reason: this step chose to stand
              // aside and said why. The sentence renders directly below as the note.
              : phase === "skipped" ? "skipped" : step.status}
          </span>
        )}
        {/* THE NUMBER, TYING THE CARD TO THE RAIL ABOVE IT. Taken from the reference design:
            the rail says step 8 is Deploy and the card said only "Deploy", so the two had to be
            matched by name. */}
        {typeof stepIndex === "number" ? (
          <span className="dw-detail-n" data-tone={phase} data-testid="dw-detail-n">
            {stepIndex + 1}
          </span>
        ) : null}
        {/* The same glyph the rail node wears, so the card and the node are obviously the same
            step without reading either label. */}
        {hasStepIcon(step.capability ?? undefined) ? (
          <span className="dw-detail-glyph" data-testid="dw-detail-glyph">
            <StepIcon capability={step.capability ?? undefined} size={15} />
          </span>
        ) : null}
        <h4>{capability?.label ?? step.capability ?? step.step_id}</h4>
        {/* WHAT THIS STEP IS, ON A DOT. This was a permanent line under every step header —
            "The whole inner pipeline: implement, adversarial review, security check, run the
            tests, open a gated pull request. Succeeds when a PR exists." Correct, and read
            once. It is the same class of sentence as everything else that moved. */}
        {capability?.summary ? (
          <Explain label={`What the ${capability.label ?? "step"} step does`}>
            {capability.summary}
          </Explain>
        ) : null}
        {stepDuration(step) ? (
          <span className="dw-step-took" data-testid="dw-step-took">{stepDuration(step)}</span>
        ) : null}
        {approval?.mode === "manual" ? (
          <span className="dw-gate" data-tone={gateWaiting ? "waiting" : undefined}
                data-testid="dw-step-gate"
                title="Requires a person">
            {gateWaiting
              ? `needs ${approval.approver_role || "a person"}`
              : `gate${approval.approver_role ? ` · ${approval.approver_role}` : ""}`}
          </span>
        ) : null}
        {step.due_at ? (
          <span className="detail dw-due">due {step.due_at.slice(0, 16).replace("T", " ")}</span>
        ) : null}
        {step.timeout_applied ? (
          <span className="dw-timeout">{step.timeout_applied.replace("_", " ")}</span>
        ) : null}
        {/* ONE CONTROL FOR ALL OF IT. Eight sentences each with their own disclosure would be
            eight more things on the screen; this is one, and it is the last item in the header
            so it never competes with the step's name or its state. */}
        {isDeployStep(step, capability) && learnedDeploy ? (
          <button type="button" className="dw-help-toggle" data-testid="dw-help-toggle"
                  aria-expanded={helpOpen} aria-label="Explain this step"
                  onClick={() => setHelpOpen((o) => !o)}>?</button>
        ) : null}
      </header>


      {/* A STEP THAT SUCCEEDED IS NOT SHOWING YOU A FAILURE.
          `note` is written by whichever attempt last wrote one, and a step that was refused
          and then went on to succeed keeps the refusal. So a Deploy marked SUCCEEDED, whose
          app was up and answering 200, displayed "no preview is running for this app right
          now … Start a fresh preview, then deploy" in red — an instruction to redo work that
          had already been done. On a succeeded step the note is history: kept, because it
          explains the delay, but not dressed as a problem you still have. */}
      {/* WHAT STOPPED IT — AND WHETHER THAT IS STILL TRUE.
          This rendered a past attempt's error as a statement about now, so after the missing
          deployment target was created the step went on insisting, in red, that the app had
          nowhere to ship to. The read path re-tests the recorded blocker; when it has cleared,
          the failure is labelled as history and the reason is given. */}
      {step.error && step.status !== "succeeded" ? (
        <>
          {blocker?.cleared ? (
            <p className="dw-step-cleared" data-testid="dw-step-blocker-cleared">
              <strong>This no longer applies.</strong> {blocker.reason}
            </p>
          ) : null}
          <p className={blocker?.cleared ? "dw-step-error dw-step-error-stale" : "dw-step-error"}
             data-testid="dw-step-error">
            {blocker?.cleared ? "Recorded on the last attempt: " : ""}{step.error}
          </p>
        </>
      ) : null}
      {/* HISTORY GOES ON THE DOT; A LIVE REASON STAYS ON THE PAGE.
          "Earlier: this app is already previewable — adopted the running preview" is how a
          succeeded step explains why it took no time. Worth keeping and not worth a line. On a
          step that did NOT succeed the same field is the reason it is sitting there, so it
          stays where it cannot be missed. */}
      {step.output?.note ? (
        step.status === "succeeded" ? (
          <Explain label="Why this step recorded no work">
            {String(step.output.note)}
          </Explain>
        ) : (
          <p className="dw-step-note"
             data-tone={phase === "failed_on" ? "bad" : undefined}
             data-testid="dw-step-note">
            {String(step.output.note)}
          </p>
        )
      ) : null}

      {/* WHERE THE THING IS. The point of a deploy is an address somebody can open, and the
          step said everything except that: the target it shipped to, the run id, the health
          key names — and no link. The address is on the target it is bound to. */}
      {/* NOT AFTER IT HAS BEEN APPROVED.
          The gate is signed off and a worker is about to ship it — the panel says so, two lines
          below — and "Deploy now" was still offered beside that sentence. Pressing it would
          start a SECOND deploy of the same build while the first was being picked up. An
          approved gate is a decision already taken; the button that takes it does not belong
          on screen any more. */}
      {/* AND NOT WHILE THE GATE IS STILL OPEN.
          This step is gated on release_manager and unanswered, and a blue "Redeploy" sat above
          the gate — pressing it POSTs the deploy directly, with no decision, which is the one
          thing a manual gate exists to prevent. It also read as proof the step was done: two
          primary buttons saying the app is deployed, above a line asking for approval to
          deploy it. While there is an answerable gate, the gate IS the way to ship; the button
          comes back once the decision has been taken, for the re-ship case it was added for. */}
      {isDeployStep(step, capability) && shippedTo && buildRunId
        && !needsYou(step)
        && decidedButNotMoved(step) !== "approved" ? (
        <ShipNow projectId={projectId} buildRunId={buildRunId} target={shippedTo}
                 alreadyLive={!!liveAppUrl} onDone={onBound} />
      ) : null}

      {/* WHOSE DEPLOYMENT IS THAT, THOUGH.
          An address answering does not mean THIS run shipped. This app had a deployment from a
          run five days earlier, so a step still waiting for approval led with "Open the app —
          shipped and answering": true about the app, false about the step, and read top-down it
          says the deploy is done and then asks to approve the deploy. When this step has not
          succeeded the same fact is stated as what it is — the previous release — and does not
          take the primary button off the gate. */}
      {liveAppUrl ? (
        <p className="dw-step-live" data-testid="dw-step-live">
          <a className={`cap-btn${
               isDeployStep(step, capability) && step.status !== "succeeded" ? "" : " primary"}`}
             href={liveAppUrl} target="_blank" rel="noreferrer"
             data-testid="dw-open-app">Open the app ↗</a>
          <span className="detail" data-testid="dw-step-live-note">
            {isDeployStep(step, capability) && step.status !== "succeeded"
              ? <>{liveAppUrl} — answering, from an earlier release. This build has not
                  shipped yet.</>
              : <>{liveAppUrl} — shipped and answering.</>}
          </span>
        </p>
      ) : null}
      {observedGates(step, runAppTitle) ? (
        <p className="dw-step-gates" data-testid="dw-step-gates">
          {observedGates(step, runAppTitle)}
        </p>
      ) : null}

      {/* CHOOSE WHERE IT SHIPS BEFORE IT ASKS TO SHIP.
          This picker existed only inside the approval block, so it appeared for a few seconds
          at the gate and not before — and if the run was not sitting at the gate there was no
          way to set a target at all. The operator reported exactly that: no option on the
          deploy step to point it at a host. It belongs to the step for its whole life, from
          the moment you can see the step until it has shipped. */}
      {/* ALWAYS ON THE DEPLOY STEP. NO CONDITIONS.
          This was gated on `!succeeded && !needsYou`, which hid it on precisely the step the
          operator was looking at — a deploy that had already reported succeeded. They asked
          for this control repeatedly and it was never once on screen, because each version of
          the condition excluded the state their run happened to be in.

          Binding an app to a host is not a thing you do only while a step is pending: after a
          failed or wrongly-"succeeded" deploy you need to set the target and run it again,
          which is exactly the moment the old condition removed it. */}
      {/* TWO THINGS, IN ORDER, AND IT NEVER SAID SO.
          The step showed a host dropdown, two boxes and a "Set target" button beside a "Review
          and decide" button, with nothing to say which came first, whether the first was
          required, or what either did. So: "the user might click on Review and decide without
          setting the target, or he may click set target multiple times". Both are the layout's
          fault. Numbering them, marking the one that is done, and saying what the second one
          releases is the whole fix. */}
      {/* ONE PICKER, PLACED TWICE — never two pickers. `DeployWhere` already renders either the
          bound target as a fact with Change, or the form; what changes between the two layouts
          below is the scaffolding around it, so the control itself is created once. Two call
          sites would be one edit away from rendering both, which is the reported bug
          ("he may click set target multiple times") waiting to happen again. */}
      {/* NUMBERS TEACH ORDER, AND ONCE THE ORDER IS SATISFIED THEY HOLD UP NOTHING.
          Taken from the Gemini sketch, which is right about this: with a target already bound
          there is exactly ONE thing an operator can do here, and a two-item numbered plan around
          one button is scaffolding left standing after the building is up. Before that, the
          numbers are the whole point — the reported bug was pressing Review and decide without
          setting a target — so they stay until they have done their job. */}
      {isDeployStep(step, capability) && shippedTo ? (
        <div className="dw-ship-single" data-testid="dw-ship-single">
          {targetControl}
          {needsYou(step) ? (
            <div className="dw-ship-single-act">
              <ReviewAndDecide step={step} />
              <Explain label="What approving does">
                The press that ships it: BOSS builds a container from this app and runs it on{" "}
                {boundHostName || "the bound host"} at{" "}
                {(shippedTo.domains ?? []).find((d: string) => !!d) || "its address"}. Nothing
                is sent until it is approved.
              </Explain>
            </div>
          ) : null}
        </div>
      ) : isDeployStep(step, capability) ? (
        <ol className="dw-ship-plan" data-testid="dw-step-target">
          {/* ACTIVE IS THE ONE THAT CAN BE ACTED ON, AND IT WAS THE OTHER ONE.
              `data-blocked` went on step 2 exactly when step 1 was unfinished — and the CSS
              drew that as the highlight, with the primary button inside it. So the screen boxed
              the step it was simultaneously telling you would fail ("there is nowhere to put the
              container until step 1 is done") while leaving the step that actually needed doing
              flat and unmarked. Highlight follows the prerequisite now: step 1 until a target
              exists, step 2 after. */}
          <li className="dw-ship-step" data-done={shippedTo ? "true" : undefined}
              data-active={!shippedTo ? "true" : undefined}
              data-testid="dw-ship-where">
            <span className="dw-ship-n">1</span>
            <div className="dw-ship-body">
              <div className="dw-ship-title">
                Where it ships
                <span className="dw-ship-state">
                  {shippedTo ? "set" : "needs you first"}
                </span>
                {!shippedTo && !showHelp ? (
                  <Explain label="How setting a target works">
                    Pick the machine, confirm the name the container gets, and press Set target.
                    Leave the port blank and BOSS picks a free one. This creates the destination;
                    it does not ship anything.
                  </Explain>
                ) : null}
              </div>
              {targetControl}
              {!shippedTo && showHelp ? (
                <p className="detail dw-ship-hint" data-testid="dw-help-where">
                  Pick the machine, confirm the name the container gets, and press Set target.
                  Leave the port blank and BOSS picks a free one. This creates the destination;
                  it does not ship anything.
                </p>
              ) : null}
            </div>
          </li>
          <li className="dw-ship-step" data-testid="dw-ship-approve"
              data-active={shippedTo && needsYou(step) ? "true" : undefined}
              data-blocked={!shippedTo ? "true" : undefined}>
            <span className="dw-ship-n">2</span>
            <div className="dw-ship-body">
              <div className="dw-ship-title">
                Approve the release
                {!showHelp ? (
                  <Explain label="What approving does">
                    {shippedTo
                      ? <>The press that ships it: BOSS builds a container from this app and runs
                          it on {boundHostName || "the bound host"}. Nothing is sent until it is
                          approved.</>
                      : <>Approving now would fail at the deploy — there is nowhere to put the
                          container until step 1 is done.</>}
                  </Explain>
                ) : null}
                <span className="dw-ship-state">
                  {/* "WAITING ON YOU" WAS THE OTHER HALF OF THE SAME LIE. It is waiting on
                      step 1 — and saying it wants the operator, in amber, beside a button that
                      would fail, is three signals all pointing at the wrong step. */}
                  {!shippedTo ? "after step 1"
                    : step.status === "waiting" ? "waiting on you" : step.status}
                </span>
              </div>
              {/* A STEP THAT CANNOT RUN DOES NOT NEED A PARAGRAPH.
                  Blocked, this was four lines — an explanation, a button that would fail and a
                  note about who signs it off — taking the same footprint as the step that
                  actually wanted doing. "after step 1" beside the title already says it; the
                  rest is available under `?` for whoever wants it. */}
              {shippedTo && showHelp ? (
                <p className="detail dw-ship-hint">
                  This is the press that ships it: BOSS builds a container from this app and
                  runs it on {boundHostName || "the bound host"} at{" "}
                  {(shippedTo.domains ?? []).find((d: string) => !!d) || "its address"}.
                  Nothing is sent until it is approved.
                </p>
              ) : !shippedTo && showHelp ? (
                <p className="detail dw-ship-hint" data-testid="dw-help-approve">
                  Approving now would fail at the deploy — there is nowhere to put the container
                  until step 1 is done.
                </p>
              ) : null}
              {/* THE BUTTON, INSIDE THE THING IT DOES. It used to sit three blocks further
                  down, past "Ask BOSS about this step", which is how it came to look like a
                  peer of "Set target" rather than the step after it. */}
              {needsYou(step) && shippedTo ? <ReviewAndDecide step={step} /> : null}
            </div>
          </li>
        </ol>
      ) : null}

      {/* ASK ABOUT THIS STEP, WITHOUT LEAVING IT.
          A step that will not ship states its reason and then leaves the operator to act on
          it alone. The dock can read the run, the code, the host and the logs — so the
          question it opens with is this step's own words, not a blank box. */}
      <div className="dw-step-ask">
        {/* ASK THE PERSON WHOSE STEP THIS IS.
            "Ask BOSS about this step" reached BOSS-in-general: no lens, no remit, no memory of
            anything this kind of work has produced before. The role-addressed path already
            existed and is strictly better — it reads the role's own definition and the positions
            that role has actually taken on this project — it simply had nothing telling it that
            a deploy belongs to the Deployment Manager and a security review to the Security
            Reviewer. The engine says so now, per capability. */}
        {stepCaptain(step, capability) ? (
          <AgentChip roleKey={stepCaptain(step, capability)}
                     note={step.captain ? "named by this workflow" : "answers for this step"} />
        ) : null}
        <button type="button" className="cap-btn" data-testid="dw-ask-boss"
                title={stepCaptain(step, capability)
                  ? `Opens the side dock addressed to the `
                    + `${roleLabel(stepCaptain(step, capability))},`
                    + " with this step's state already filled in"
                  : "Opens the side dock with this step's state and message already filled in"}
                onClick={() => askBossAbout(
                  workflowRunId
                    ? { kind: "workflow_step", id: workflowRunId, step: step.step_id }
                    : buildRunId ? { kind: "code_task", id: buildRunId } : null,
                  stepQuestion(step, capability?.label),
                  stepCaptain(step, capability))}>
          {stepCaptain(step, capability)
            ? `Ask the ${roleLabel(stepCaptain(step, capability))}`
            : "Ask BOSS about this step"}
        </button>
      </div>

      {/* Try again / Skip and proceed — see FailedStepActions for why both sit together. */}
      {/* WHICH OF THE TWO, IF EITHER, IS THE SERVER'S CALL — `actions` is this step's slice of
          the run's list, and FailedStepActions renders nothing when it is empty.
          KEYED BY THE STEP, so a reason half-typed for one step is not still open, pre-filled,
          when the operator clicks a different failed step in the rail. */}
      <FailedStepActions key={step.step_id} step={step} capability={capability} busy={busy}
                         actions={actions} onRetry={onRetry} onSkip={onSkip} />

      {/* ANSWER THE APPROVAL WHERE IT IS SEEN. A gate answerable only in another area makes the
          operator leave the screen that told them it was waiting. */}
      {/* On a deploy this is rendered inside step 2 above, where it belongs in the sequence. */}
      {needsYou(step) && !isDeployStep(step, capability) ? <ReviewAndDecide step={step} /> : null}

      {/* THE CLICK LANDED. SAY SO.
          An operator approved a deploy, came back here, and saw the identical "waiting · gate ·
          Review and decide" they had left — because the step stays `waiting` until a worker
          picks the answer up, and this view read only the step. Their reasonable conclusion was
          that the approval had not worked. It had. */}
      {decidedButNotMoved(step) ? (
        <div className="dw-step-decided" data-testid="dw-step-decided">
          {decidedButNotMoved(step) === "approved" ? (
            <>
              <strong>Approved — starting.</strong>{" "}
              <Explain label="What happens next">
                The gate is signed off; the step moves as soon as a worker picks it up, usually
                within a minute. This view refreshes on its own.
              </Explain>
            </>
          ) : decidedButNotMoved(step) === "expired" ? (
            <>
              <strong>The gate expired before anyone answered it.</strong>{" "}
              <Explain label="What an expired gate means">
                Nothing was applied — the default was to hold — so this step is waiting on a
                question that can still be answered. Opening it reopens the decision.
              </Explain>{" "}
              <a className="cap-btn dw-decide-btn" data-testid="dw-step-reopen"
                 href={consoleHref(typeof window !== "undefined" ? window.location.search : "",
                                   { area: "decisions", decision: step.decision_id })}>
                Answer it now →
              </a>
            </>
          ) : (
            <>
              <strong>Decided: {decidedButNotMoved(step)}.</strong>{" "}
              <Explain label="Why the step has not moved">
                This step is still marked waiting; it will not proceed on its own.
              </Explain>{" "}
              <a className="cap-btn dw-decide-btn"
                 href={consoleHref(typeof window !== "undefined" ? window.location.search : "",
                                   { area: "decisions", decision: step.decision_id })}>
                Open the decision →
              </a>
            </>
          )}
        </div>
      ) : null}

      {/* RUN IT, OR OPEN IT. The preview step reported what it had recorded and offered no way
          to do anything: no button to start one, and — once the recorded preview expired — no
          link either. Factory has had "Open preview" and a way to start one all along; this is
          the same two controls, on the step whose whole subject is the preview. */}
      {isPreviewStep(step) ? (
        <div className="dw-step-decide" data-testid="dw-preview-controls">
          {/* Pending until the URL arrives: the request was accepted in a second, the app
              takes a minute. Cleared by the URL showing up, not by the POST returning. */}
          {previewHref ? (
            <a className="cap-btn primary" href={previewHref} target="_blank" rel="noreferrer"
               data-testid="dw-preview-open">Open preview</a>
          ) : null}
          {buildRunId ? (
            <button type="button" className="cap-btn"
                    disabled={!!busy || starting}
                    data-testid="dw-preview-run" onClick={() => onRunPreview(buildRunId)}>
              {starting ? "Starting…"
                : previewHref ? "Start a fresh one" : "Run preview"}
            </button>
          ) : null}
          {/* WHAT STAYS VISIBLE AND WHAT MOVES TO THE DOT.
              Not everything grey is an explanation. Progress — "starting a preview, this takes
              up to a minute" — is the whole reason a minute of silence does not read as a click
              that did nothing, and a build MISMATCH is a warning: the link opens a different
              build than the one on screen. Both stay. The two sentences that merely describe
              what a preview is go on the dot. */}
          {starting ? (
            <span className="detail" data-testid="dw-preview-hint">
              Starting a preview — this takes up to a minute. It will appear here on its own.
            </span>
          ) : previewHref && step.live_preview?.same_build === false ? (
            <>
              {/* A WARNING SHOULD BE SHORT AND LOUD, WHICH IS NOT WHAT I SHIPPED.
                  I argued this had to stay visible — correct — and then left it as two lines of
                  prose, which is the thing being complained about. Six words carry it; the
                  explanation goes where every other explanation went. */}
              <span className="dw-preview-warn" data-testid="dw-preview-hint">
                Preview is from a later build
              </span>
              <Explain label="What that means">
                This build has no preview running. The link opens the same app built from a
                later commit, so what you see may not match this step. Start a fresh one to
                preview this build.
              </Explain>
            </>
          ) : (
            <Explain label="What this preview is">
              {previewHref
                ? "A live preview of this build, on a URL you can open."
                : "Nothing is serving this build right now. Starting one takes a few seconds."}
            </Explain>
          )}
        </div>
      ) : null}

      {/* WHERE THE WORK IT PRODUCED LIVES. Debugging a step means reading what it actually did. */}
      {visibleLinks.length ? (
        <div className="dw-step-links" data-testid="dw-step-links">
          {visibleLinks.map((l) => (
            l.external ? (
              <a key={l.label} href={l.href} target="_blank" rel="noreferrer"
                 className="dw-step-link">{l.label} ↗</a>
            ) : (
              <a key={l.label} href={l.href} className="dw-step-link">{l.label}</a>
            )
          ))}
        </div>
      ) : null}

      {/* THE DOCUMENT, ON THE STEP THAT WROTE IT. `GET /v1/artifacts/{id}` has existed all
          along and nothing called it, so even a Document step that produced something had
          nowhere to show it — an id in the output grid is not a document. */}
      {typeof (step.output || {}).document_id === "string" ? (
        <ChangeRecord documentId={String((step.output as Record<string, unknown>).document_id)} />
      ) : null}

      {produced.length ? (
        <details className="dw-step-out-wrap" data-testid="dw-step-out-wrap">
          <summary>What this step recorded · {produced.length}</summary>
          <dl className="dw-step-out" data-testid="dw-step-out">
            {produced.map(([k, v]) => (
              <div key={k}>
                <dt>{k.replace(/_/g, " ")}</dt>
                <dd>{summarise(v)}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </section>
  );
}

/** The change record, fetched when it is opened rather than with every run.
 *
 *  Collapsed by default and loaded on expand: a workflow list showing ten runs should not pull
 *  ten documents to render a summary nobody has asked to read.
 */
function ChangeRecord({ documentId }: { documentId: string }) {
  const [state, setState] = useState<
    { kind: "shut" } | { kind: "loading" }
    | { kind: "text"; markdown: string; defects: Array<{ kind: string; detail: string }> }
    | { kind: "failed"; why: string }>({ kind: "shut" });

  const open = async () => {
    if (state.kind !== "shut") return;
    setState({ kind: "loading" });
    try {
      const r = await fetch(`/api/artifacts/${encodeURIComponent(documentId)}`,
                            { cache: "no-store" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        setState({ kind: "failed", why: String(body?.error ?? `HTTP ${r.status}`) });
        return;
      }
      const markdown = String(body?.content?.markdown ?? "").trim();
      const defects = Array.isArray(body?.content?.defects) ? body.content.defects : [];
      setState(markdown
        ? { kind: "text", markdown, defects }
        // Stored but empty is a different fact from unreadable, and saying "failed" for it
        // would send someone looking for a fault that is not there.
        : { kind: "failed", why: "the record was stored with no body" });
    } catch (e) {
      setState({ kind: "failed", why: String((e as Error)?.message ?? e) });
    }
  };

  return (
    <details className="dw-record" data-testid="dw-change-record"
             onToggle={(e) => { if ((e.currentTarget as HTMLDetailsElement).open) void open(); }}>
      <summary>Read the change record</summary>
      {state.kind === "loading" ? <p className="detail">Reading…</p> : null}
      {state.kind === "failed" ? (
        <p className="detail" data-testid="dw-change-record-failed">
          Could not read it — {state.why}.
        </p>
      ) : null}
      {state.kind === "text" ? (
        <>
          <div className="dw-record-acts">
            {/* A DOCUMENT SOMEBODY CAN OPEN. Markdown in a <pre> is the right storage format
                shown in the wrong delivery format — "I dont think anyone will be able to read
                that". Rendered server-side on demand, so a record written before the PDF
                existed still downloads. */}
            <a className="cap-btn" href={`/api/artifacts/${encodeURIComponent(documentId)}/pdf`}
               data-testid="dw-record-pdf" target="_blank" rel="noreferrer">
              Download as PDF
            </a>
            <span className="detail">{state.defects.length
              ? `${state.defects.length} check${state.defects.length === 1 ? "" : "s"} flagged this document`
              : "Passed every check BOSS can make automatically."}</span>
          </div>
          {/* What the checks found, in the document's own view rather than in a log. */}
          {state.defects.length ? (
            <ul className="dw-record-defects" data-testid="dw-record-defects">
              {state.defects.map((d) => <li key={d.kind}>{d.detail}</li>)}
            </ul>
          ) : null}
          <div className="dw-record-body">
            <Markdown text={state.markdown} />
          </div>
        </>
      ) : null}
    </details>
  );
}

/** WHAT THIS WORKFLOW WILL DO, above the steps that edit it.
 *
 *  Reported: "I want to see the workflow and its definition, what it will do, then trigger
 *  the run… we need to know what kind of results it will bring, any agents associated or any
 *  task associated — right now those info are missing. I also don't know how to customize a
 *  specific workflow step."
 *
 *  Every fact here already existed: the capability palette holds what a step does, what it
 *  produces, which agent captains it and what forces a gate; the stored bindings hold where
 *  each input comes from. Nothing joined them, so the operator's view of a workflow was four
 *  labels and four dropdowns.
 *
 *  Collapsed by default AFTER the first look would be wrong — this is the thing they came to
 *  read, so it is open, and the step editor below it is what they scroll to.
 */
function WorkflowPlanPanel({ plan, onCustomize }: {
  plan: WorkflowPlan;
  /** Jump the editor to one step. "How do I customize a specific step" had no answer that
   *  did not involve counting rows in a second list. */
  onCustomize: (stepId: string) => void;
}) {
  if (!plan.published) {
    return (
      <section className="stack-card dw-plan" data-testid="dw-plan">
        <p className="detail">{plan.summary}</p>
      </section>
    );
  }
  return (
    <section className="stack-card dw-plan" data-testid="dw-plan">
      <header className="dw-plan-head">
        <h4>What this will do</h4>
        <span className="detail">
          v{plan.version}
          {typeof plan.typical_minutes === "number" && plan.typical_minutes > 0
            ? ` · ~${plan.typical_minutes} min` : ""}
        </span>
      </header>

      {/* The plan in a sentence, for the person who will not read the table. */}
      <p className="dw-plan-summary" data-testid="dw-plan-summary">{plan.summary}</p>

      {/* THE THREE QUESTIONS, ANSWERED AT THE TOP rather than inferred from the list. */}
      <dl className="dw-plan-facts">
        <div>
          <dt>You supply</dt>
          <dd data-testid="dw-plan-supply">
            {plan.you_must_supply.length
              ? plan.you_must_supply.join(", ")
              : "nothing — every input comes from a step"}
          </dd>
        </div>
        <div>
          <dt>It produces</dt>
          <dd data-testid="dw-plan-results">
            {plan.results.length ? plan.results.join(", ") : "no recorded output"}
          </dd>
        </div>
        <div>
          <dt>Agents</dt>
          <dd data-testid="dw-plan-agents">
            {plan.agents.length
              ? plan.agents.map((a) => a.replace(/_/g, " ")).join(", ")
              : "none assigned"}
          </dd>
        </div>
      </dl>

      {/* A WORKFLOW THAT CANNOT START SHOULD SAY SO HERE, not when the run deadletters 41
          seconds later with "needs 'media_artifact_id', which is not set". */}
      {plan.can_start === false && plan.blocked_because?.length ? (
        <p className="dw-plan-blocked" data-testid="dw-plan-blocked">
          <strong>This cannot run yet.</strong>{" "}
          {plan.blocked_because.join("; ")}.
        </p>
      ) : null}

      <ol className="dw-plan-steps">
        {plan.steps.map((step) => (
          <li key={step.id} className="dw-plan-step"
              data-gate={step.approval === "manual" || undefined}
              data-testid={`dw-plan-step-${step.id}`}>
            <div className="dw-plan-step-head">
              <span className="dw-plan-num">{step.position}</span>
              <strong>{step.label}</strong>
              <span className="dw-plan-approval" data-forced={step.approval_is_forced || undefined}>
                {step.approval === "manual" ? "stops for you" : "runs automatically"}
              </span>
            </div>

            {step.does ? <p className="detail dw-plan-does">{step.does}</p> : null}

            <p className="detail dw-plan-meta">
              {step.agent ? <>Run by <strong>{step.agent.replace(/_/g, " ")}</strong></> : null}
              {step.runs ? <> · runs <code>{step.runs}</code></> : null}
              {typeof step.risk_tier === "number" ? <> · risk {step.risk_tier}</> : null}
            </p>

            {/* WHY it stops, not just that it does. "Forced" is the difference between a
                setting the operator can change and one the risk floor decided for them. */}
            {step.approval === "manual" ? (
              <p className="detail dw-plan-note">{step.approval_note}</p>
            ) : null}

            {step.inputs.length ? (
              <ul className="dw-plan-io">
                {step.inputs.map((input) => (
                  <li key={input.name} data-ready={input.ready}>
                    <code>{input.name}</code> — {input.source}
                  </li>
                ))}
              </ul>
            ) : null}

            {step.produces.length ? (
              <p className="detail dw-plan-produces">
                Produces: {step.produces.join(", ")}
              </p>
            ) : null}

            <button type="button" className="linklike"
                    onClick={() => onCustomize(step.id)}
                    data-testid={`dw-plan-customize-${step.id}`}>
              Customize this step
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}

/* ── Catalogue ───────────────────────────────────────────────────────────────── */

/** The workflows this operator made, as opposed to the ones BOSS ships.
 *
 *  ONE DEFINITION, because the tab count and the grid disagreed. The Catalogue tab read
 *  `catalogue.length` — shipped entries only — while the screen underneath it also renders
 *  these. So an operator who authored a workflow saw it appear on the tab and the number stay
 *  at 12, which reads as "it was not saved".
 *
 *  `is_catalogue` is a thin reed to lean on: in the database it is also false for the 33
 *  bootstrap workflows that run BOSS itself, and they are kept out of here only because the
 *  endpoint filters to the `product` namespace upstream. It holds, but it holds by accident of
 *  someone else's WHERE clause. Worth keying on namespace once we decide whether an operator
 *  can promote their own workflow into the shipped catalogue.
 */
function ownWorkflows(installed: Installed[]): Installed[] {
  return installed.filter((i) => !i.is_catalogue);
}


/** A CUSTOMER'S OWN WORKFLOWS, ON THEIR OWN SCREEN.
 *
 *  These sat under the twelve BOSS ships, on a tab called Catalogue — so the thing somebody
 *  built was filed beneath the things they did not, and the count on the tab was the two added
 *  together. Examples make that worse: install three and your own work is below a list you did
 *  not write.
 *
 *  Same cards, their own tab, plus the one thing a workflow of your own needs that a catalogue
 *  entry does not — a way to say when it should run without anybody asking. */
function YoursView({ installed, schedules, busy, onStart, onEditMine, scheduleDraft,
                     onOpenSchedule, onCancelSchedule, onChangeSchedule, onSubmitSchedule,
                     onToggleSchedule, onDeleteSchedule }: {
  installed: Installed[];
  schedules: Schedule[];
  busy: string | null;
  onStart: (key: string, workClass: string) => void;
  onEditMine: (definitionId: string) => void;
  scheduleDraft: ScheduleDraft | null;
  onOpenSchedule: (item: Installed, existing?: Schedule) => void;
  onCancelSchedule: () => void;
  onChangeSchedule: (next: ScheduleDraft) => void;
  onSubmitSchedule: (draft: ScheduleDraft) => void;
  onToggleSchedule: (id: string, enabled: boolean) => void;
  onDeleteSchedule: (id: string) => void;
}) {
  const ours = useMemo(() => ownWorkflows(installed), [installed]);
  const byWorkflow = useMemo(() => {
    const map = new Map<string, Schedule[]>();
    for (const s of schedules) {
      map.set(s.workflow_key, [...(map.get(s.workflow_key) ?? []), s]);
    }
    return map;
  }, [schedules]);

  if (ours.length === 0) {
    return (
      <div className="dw-catalogue">
        <p className="detail" data-testid="dw-yours-empty">
          Nothing of your own yet. Build one on <b>Design a workflow</b>, or run one of the
          setup scripts in <code>examples/</code> to get a working one to take apart.
        </p>
      </div>
    );
  }

  const drafting = scheduleDraft
    ? installed.find((item) => item.workflow_key === scheduleDraft.workflowKey)
    : undefined;
  const draftFields = workflowInputFields(drafting?.input_schema);
  const draftMissing = scheduleDraft ? missingRequired(draftFields, scheduleDraft.values) : [];

  return (
    <div className="dw-catalogue">
      {scheduleDraft && drafting ? (
        <form className="dw-sched-draft-form" data-testid="dw-sched-draft-form"
              onSubmit={(event) => { event.preventDefault(); onSubmitSchedule(scheduleDraft); }}>
          <span className="dw-field-label">
            <label htmlFor="dw-sched-draft-zone">
              {scheduleDraft.scheduleId ? "Edit" : "Run"} &quot;{drafting.name}&quot; every
              Monday at 08:00 — in which time zone?
            </label>
          </span>
          <input id="dw-sched-draft-zone" className="input" value={scheduleDraft.timezone}
                 data-testid="dw-sched-draft-zone"
                 onChange={(event) => onChangeSchedule(
                   { ...scheduleDraft, timezone: event.target.value })} />
          {draftFields.map((field) => (
            <div className="dw-wiz-input" key={field.name}>
              <span className="dw-field-label">
                <label htmlFor={`dw-sched-draft-input-${field.name}`}>{field.label}</label>
                {field.required ? <em className="dw-req"> required</em> : null}
              </span>
              {field.kind === "multiline" ? (
                <textarea id={`dw-sched-draft-input-${field.name}`} rows={3}
                          value={scheduleDraft.values[field.name] ?? ""}
                          data-testid={`dw-sched-draft-input-${field.name}`}
                          onChange={(event) => onChangeSchedule({
                            ...scheduleDraft,
                            values: { ...scheduleDraft.values, [field.name]: event.target.value },
                          })} />
              ) : (
                <input id={`dw-sched-draft-input-${field.name}`} className="input"
                       value={scheduleDraft.values[field.name] ?? ""}
                       data-testid={`dw-sched-draft-input-${field.name}`}
                       onChange={(event) => onChangeSchedule({
                         ...scheduleDraft,
                         values: { ...scheduleDraft.values, [field.name]: event.target.value },
                       })} />
              )}
            </div>
          ))}
          <div className="dw-card-actions">
            <button type="submit" className="cap-btn primary"
                    disabled={!!busy || !scheduleDraft.timezone.trim() || draftMissing.length > 0}
                    data-testid="dw-sched-draft-save">
              {draftMissing.length ? `Needs ${draftMissing.join(", ")}` : "Save schedule"}
            </button>
            <button type="button" className="cap-btn" onClick={onCancelSchedule}>Cancel</button>
          </div>
        </form>
      ) : null}
      <div className="dw-grid">
        {ours.map((item) => {
          const mine = byWorkflow.get(item.workflow_key) ?? [];
          return (
            <article key={item.id} className="dw-card" data-testid={`dw-mine-${item.workflow_key}`}>
              <header className="dw-card-head">
                <h4>{item.name}</h4>
                <span className="dw-class" data-class="yours">yours</span>
              </header>
              <p className="detail dw-card-desc">{item.description || "No description."}</p>
              <dl className="dw-stats">
                <div><dt>Steps</dt><dd>{item.step_count ?? 0}</dd></div>
                <div><dt>Version</dt><dd>{item.active_version ?? "—"}</dd></div>
                <div><dt>Status</dt><dd>{item.active_version ? "published" : "draft"}</dd></div>
              </dl>
              {item.steps?.length ? (
                <ol className="dw-card-steps" data-testid={`dw-mine-steps-${item.workflow_key}`}>
                  {item.steps.map((step, i) => (
                    <li key={step.id || i}>
                      <span className="dw-card-step-n">{i + 1}</span>
                      <span className="dw-card-step-label">{step.label}</span>
                      {/* WHO RUNS IT, on the card. The product's claim is that agents do the
                          work and steps track it, and a card listing three step names with no
                          agent anywhere invites the question "did you assign them or not?" —
                          asked about a workflow where all three were assigned correctly.
                          Empty is not blank: on an agent step it means BOSS chooses per run. */}
                      {step.captain ? (
                        <span className="dw-card-step-agent" title={`Runs as ${step.captain}`}>
                          {step.captain}
                        </span>
                      ) : step.agent_work ? (
                        <span className="dw-card-step-agent" data-auto="true"
                              title="BOSS picks the agent whose skills fit the instruction">
                          agent chosen per run
                        </span>
                      ) : null}
                      {step.agent_work ? (
                        <span className="dw-card-step-kind" title="This step has no fixed skill — it works from your instructions">
                          your instructions
                        </span>
                      ) : null}
                      {step.approval === "manual" ? (
                        <span className="dw-card-step-gate" title="stops for a person">
                          needs you
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ol>
              ) : null}

              {/* WHEN IT RUNS WITHOUT ANYBODY ASKING. Rendered as the sentence the API
                  produced, so the console cannot describe a schedule differently from the
                  thing that fires it. */}
              {mine.length ? (
                <ul className="dw-card-schedules" data-testid={`dw-sched-${item.workflow_key}`}>
                  {mine.map((s) => (
                    <li key={s.id} data-enabled={s.enabled}>
                      <span className="dw-sched-when">{s.describes}</span>
                      <span className="detail">
                        {s.enabled
                          // formatDateTime, not toLocaleString: the server renders in one zone
                          // and the browser in another, and the mismatch is a hydration error
                          // rather than a cosmetic difference. There is a guard for this.
                          ? (s.next_run_at ? `next ${formatDateTime(s.next_run_at)}` : "")
                          : "paused"}
                        {s.last_error ? ` · last run failed: ${s.last_error}` : ""}
                      </span>
                      <span className="dw-sched-actions">
                        <button type="button" className="linklike" disabled={!!busy}
                                data-testid={`dw-sched-toggle-${s.id}`}
                                onClick={() => onToggleSchedule(s.id, !s.enabled)}>
                          {s.enabled ? "pause" : "resume"}
                        </button>
                        <button type="button" className="linklike" disabled={!!busy}
                                data-testid={`dw-sched-edit-${s.id}`}
                                onClick={() => onOpenSchedule(item, s)}>edit inputs</button>
                        <button type="button" className="linklike" disabled={!!busy}
                                onClick={() => onDeleteSchedule(s.id)}>remove</button>
                      </span>
                    </li>
                  ))}
                </ul>
              ) : null}

              <div className="dw-card-actions">
                <button type="button" className="cap-btn primary"
                        disabled={!!busy || !item.active_version}
                        onClick={() => onStart(item.workflow_key, "")}
                        data-testid={`dw-start-mine-${item.workflow_key}`}>
                  {item.active_version ? "Start one" : "No version yet"}
                </button>
                <button type="button" className="cap-btn" disabled={!!busy}
                        onClick={() => onEditMine(item.id)}
                        data-testid={`dw-edit-mine-${item.workflow_key}`}>
                  Edit
                </button>
                {/* Offered only once there is something to run. A schedule pointing at a
                    workflow with no published version is refused by the API anyway, and
                    offering it is how somebody meets that refusal for no reason. */}
                {item.active_version ? (
                  <button type="button" className="cap-btn" disabled={!!busy}
                          data-testid={`dw-schedule-${item.workflow_key}`}
                          onClick={() => onOpenSchedule(item)}>
                    Schedule
                  </button>
                ) : null}
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}

function CatalogueView({ catalogue, installed, busy, onInstall, onApply, onStart, onFork,
                        onEditMine }: {
  catalogue: CatalogueEntry[];
  installed: Installed[];
  busy: string | null;
  onInstall: () => void;
  onApply: (key: string, workClass: string) => void;
  /** The work class travels with the key: the sample must fit the lane it runs in. */
  onStart: (key: string, workClass: string) => void;
  onFork: (key: string) => void;
  /** Open one of the operator's own workflows in the builder, by definition id — the same
   *  handle the builder's "Editing" picker uses, so the card and the dropdown agree. */
  onEditMine: (definitionId: string) => void;
}) {
  // Only the CATALOGUE ones count toward "installed". `installed` also holds the customer's own
  // forks, so counting all of them read "14 of 12 installed in this project" — a number that
  // cannot be true and makes the reader doubt everything else on the card.
  const have = useMemo(
    () => new Set(installed.filter((i) => i.is_catalogue).map((i) => i.workflow_key)),
    [installed],
  );
  // WHAT THE OPERATOR MADE, AS THINGS RATHER THAN AS A NUMBER.
  //
  // This used to be `.length`, rendered as the tail of a sentence — "· 1 of your own" — and
  // that was the ONLY trace of an operator's own workflow anywhere on this screen. The grid
  // below maps `catalogue`, which holds shipped entries only. So a workflow BOSS had just
  // published was real, editable, startable, and invisible: reported as "the workflow itself
  // does not exist in Catalogue and how is it coming to Edit mode, it is completely wrong".
  //
  // It is not the Edit list that was wrong. It was this one, for leaving them out.
  return (
    <div className="dw-catalogue">
      <div className="dw-runs-head">
        <span className="detail">
          {have.size} of {catalogue.length} standard workflows installed
        </span>
        <button type="button" className="cap-btn" onClick={onInstall} disabled={!!busy}
                data-testid="dw-install">
          {busy === "Install the catalogue" ? "Installing…" : "Install all"}
        </button>
      </div>
      <div className="dw-grid">
        {catalogue.map((entry) => (
          <article key={entry.key} className="dw-card" data-class={entry.work_class}
                   data-testid={`dw-card-${entry.key}`}>
            <header className="dw-card-head">
              <h4>{entry.name}</h4>
              {/* The class of work this is FOR, as a chip rather than grey mono text. It is
                  what a customer matches against when choosing, so it should be findable at a
                  glance across twelve cards. */}
              <span className="dw-class" data-class={entry.work_class}>{entry.work_class}</span>
            </header>
            <p className="detail dw-card-desc">{entry.description}</p>

            {/* THE STEPS ARE A SEQUENCE, AND A WRAPPED LIST OF CHIPS SAYS THEY ARE A SET.
                Order is the whole meaning of a workflow, so the card draws it as a rail: each
                step joined to the next, gates marked, and the shape readable before anyone
                commits to it. */}
            <ol className="dw-rail" aria-label="Steps, in order">
              {entry.steps.map((step, index) => {
                const gate = entry.gates.includes(step.id);
                return (
                  <li key={step.id} className="dw-rail-step" data-gate={gate || undefined}>
                    <span className="dw-rail-dot" aria-hidden />
                    <span className="dw-rail-label">{step.capability}</span>
                    {index < entry.steps.length - 1 ? (
                      <span className="dw-rail-link" aria-hidden />
                    ) : null}
                  </li>
                );
              })}
            </ol>

            {/* Three numbers a customer chooses on, each with a label — "10 · ~112 min · 2" is
                unreadable without them. */}
            <dl className="dw-stats">
              <div><dt>Steps</dt><dd>{entry.step_count}</dd></div>
              <div><dt>Typically</dt><dd>{entry.estimated_minutes}<small>min</small></dd></div>
              <div data-emphasis={entry.gates.length > 0 || undefined}>
                <dt>Approvals</dt><dd>{entry.gates.length}</dd>
              </div>
            </dl>

            {entry.requires_project.length > 0 ? (
              <p className="dw-needs">
                <span className="dw-needs-label">Needs</span>
                {entry.requires_project.map((need) => (
                  <span key={need} className="dw-need">{need.replace(/_/g, " ")}</span>
                ))}
              </p>
            ) : <p className="dw-needs dw-needs-none">Nothing to set up first</p>}

            <div className="dw-card-actions">
              {have.has(entry.key) ? (
                <>
                  {/* One primary action. Three equal buttons make a customer read all three
                      every time to find the one they want. */}
                  <button type="button" className="cap-btn primary" disabled={!!busy}
                          onClick={() => onApply(entry.key, entry.work_class)}
                          data-testid={`dw-apply-${entry.key}`}>
                    Apply to {entry.work_class}
                  </button>
                  <button type="button" className="cap-btn" disabled={!!busy}
                          onClick={() => onStart(entry.key, entry.work_class)}>Start one</button>
                  {/* Fork, never edit: the standard stays pristine so "reset" means something. */}
                  <button type="button" className="cap-btn" disabled={!!busy}
                          onClick={() => onFork(entry.key)}
                          data-testid={`dw-fork-${entry.key}`}>Fork to edit</button>
                </>
              ) : (
                <span className="detail">Install the catalogue to use this.</span>
              )}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

/* ── Builder ─────────────────────────────────────────────────────────────────── */

/** Defining a step: name it, choose what it runs, say what it needs.
 *
 *  There is deliberately NO risk field. The tier comes from the skill or MCP tool being bound
 *  and is shown here as a consequence of that choice — a customer who could type their own
 *  number would have a documented way past every approval gate in the product.
 */
function DefineStepForm({ bindable, riskFloor, busy, projectId, onDefine, onCancel,
                         editing }: {
  bindable: Bindable;
  riskFloor: number;
  busy: string | null;
  projectId: string;
  onDefine: (fields: Record<string, unknown>) => Promise<{ ok: boolean; error?: string }>;
  onCancel: () => void;
  /** The step being changed, or absent when defining a new one. ONE FORM FOR BOTH: an edit
   *  form that drifted from the define form would accept fields the other rejects, and the
   *  operator would meet two different sets of rules for the same object. */
  editing?: CustomStep | null;
}) {
  const [label, setLabel] = useState(editing?.label ?? "");
  const [summary, setSummary] = useState(editing?.summary ?? "");
  // One control, not two. "skill:github.create_pr" / "mcp:<server>:<tool>" keeps the choice a
  // single decision — picking a kind and then a thing is two ways to get it half-made.
  const [bindingRef, setBindingRef] = useState(
    editing
      ? (editing.binding_kind === "skill"
          ? `skill:${editing.skill_id ?? ""}`
          : `mcp:${(editing as { mcp_server_id?: string }).mcp_server_id ?? ""}:`
            + `${editing.mcp_tool_name ?? ""}`)
      : "");
  const [inputs, setInputs] = useState(
    (editing?.inputs ?? []).map((i) => i.name).join(", "));
  // WHAT THE STEP KNOWS. A binding says which tool runs; it says nothing about the standard
  // the step has to hold to, and that lived in the head of whoever defined it.
  const [knowledge, setKnowledge] = useState(editing?.knowledge ?? "");
  const [failure, setFailure] = useState<string | null>(null);

  const chosen = useMemo(() => {
    if (bindingRef.startsWith("skill:")) {
      const id = bindingRef.slice(6);
      const skill = bindable.skills.find((s) => s.skill_id === id);
      return skill
        ? { risk: skill.risk_tier, trusted: skill.trusted, unclassified: skill.unclassified,
            what: id }
        : null;
    }
    if (bindingRef.startsWith("mcp:")) {
      const [, serverId, ...rest] = bindingRef.split(":");
      const server = bindable.mcp_servers.find((m) => m.id === serverId);
      const tool = server?.tools.find((t) => t.name === rest.join(":"));
      return server && tool
        ? { risk: tool.risk_tier, trusted: server.trusted, unclassified: false,
            what: `${server.name} · ${tool.name}` }
        : null;
    }
    return null;
  }, [bindingRef, bindable]);

  const blocked = chosen !== null && chosen.risk >= riskFloor && !chosen.trusted;
  // How many things can ACTUALLY be picked, which is not the same as how many servers exist.
  const choices = bindable.skills.length
    + bindable.mcp_servers.reduce((n, server) => n + server.tools.length, 0);
  const unreachable = bindable.mcp_servers.filter((m) => !m.reachable).map((m) => m.name);

  return (
    <form className="dw-define-form" data-testid="dw-define-form" onSubmit={async (e) => {
      e.preventDefault();
      setFailure(null);
      const [kind, ...rest] = bindingRef.split(":");
      const fields: Record<string, unknown> = {
        project_id: projectId,
        label: label.trim(),
        summary: summary.trim(),
        binding_kind: kind === "skill" ? "skill" : "mcp_tool",
        inputs: inputs.split(",").map((i) => i.trim()).filter(Boolean),
        knowledge: knowledge.trim(),
      };
      if (kind === "skill") {
        fields.skill_id = rest.join(":");
      } else {
        fields.mcp_server_id = rest[0];
        fields.mcp_tool_name = rest.slice(1).join(":");
      }
      // The KEY, when this is an edit. It is what workflow specs bind to, so it travels as an
      // identifier and is never one of the fields being changed — renaming a step must not
      // orphan every workflow that places it.
      if (editing) fields.step_key = editing.step_key;
      const result = await onDefine(fields);
      if (!result.ok) {
        setFailure(result.error
                   ?? (editing ? "could not change the step" : "could not define the step"));
      }
    }}>
      <label className="field">
        <span>Name</span>
        <input className="input" autoFocus value={label} data-testid="dw-define-label"
               placeholder="File a compliance ticket"
               onChange={(e) => setLabel(e.target.value)} />
      </label>
      <label className="field">
        <span>Runs</span>
        <select className="input" value={bindingRef} data-testid="dw-define-binding"
                onChange={(e) => setBindingRef(e.target.value)}>
          <option value="">Choose a skill or tool</option>
          {bindable.skills.length ? (
            <optgroup label="Your skills">
              {bindable.skills.map((skill) => (
                <option key={skill.skill_id} value={`skill:${skill.skill_id}`}>
                  {skill.skill_id}{skill.unclassified ? " (unclassified)" : ""}
                </option>
              ))}
            </optgroup>
          ) : null}
          {bindable.mcp_servers.map((server) => (
            <optgroup key={server.id}
                      label={server.reachable ? server.name : `${server.name} (unreachable)`}>
              {server.tools.map((tool) => (
                <option key={tool.name} value={`mcp:${server.id}:${tool.name}`}>
                  {tool.name}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Needs</span>
        <input className="input" value={inputs} data-testid="dw-define-inputs"
               placeholder="ticket, severity"
               onChange={(e) => setInputs(e.target.value)} />
      </label>
      {/* WHAT THIS STEP KNOWS. Carried into every execution, so the step behaves the same on
          its hundredth run as on the one where somebody was watching. */}
      <label className="field">
        <span>What should it know?</span>
        <textarea className="textarea" rows={4} value={knowledge}
                  data-testid="dw-step-knowledge"
                  placeholder="House style, the clauses that matter, the internal doc that explains this system — anything the step must hold to that its tool does not already know."
                  onChange={(e) => setKnowledge(e.target.value)} />
        <span className="detail">
          Optional. Handed to the skill or tool on every run as context.
        </span>
      </label>
      <label className="field">
        <span>What it does</span>
        <input className="input" value={summary} placeholder="Optional, one line"
               onChange={(e) => setSummary(e.target.value)} />
      </label>

      {/* The consequence of the binding, stated before the step is created rather than at
          publish time when the workflow is already drawn. */}
      {chosen ? (
        <p className="detail dw-define-risk" data-testid="dw-define-risk">
          Risk tier {chosen.risk}, from {chosen.what}
          {chosen.unclassified ? " — nobody has classified this skill, so it is treated as "
                                 + "the floor" : ""}
          {chosen.risk >= riskFloor
            ? ". This step will always stop for a person."
            : ". This step can run automatically."}
        </p>
      ) : null}
      {blocked ? (
        <p className="detail dw-define-blocked" data-testid="dw-define-blocked">
          This is untrusted and at tier {chosen?.risk}. You can define it, but a workflow using
          it will not publish until it has been reviewed.
        </p>
      ) : null}
      {failure ? <p className="detail dw-define-error">{failure}</p> : null}

      <div className="dw-define-actions">
        <button type="submit" className="cap-btn" data-testid="dw-define-save"
                disabled={!!busy || !label.trim() || !bindingRef || choices === 0}>
          {editing ? "Save changes" : "Define"}
        </button>
        <button type="button" className="cap-btn" onClick={onCancel}>Cancel</button>
      </div>
      {choices === 0 ? (
        // Counted in CHOICES, not in servers. A project with three registered MCP servers that
        // are all unreachable has three empty groups in the dropdown and nothing to pick — and
        // saying nothing there reads as a broken control rather than an empty one.
        <p className="detail dw-define-empty" data-testid="dw-define-empty">
          Nothing to bind a step to yet.{" "}
          {unreachable.length
            ? `${unreachable.join(", ")} ${unreachable.length === 1 ? "is" : "are"} `
              + "registered but not answering, so their tools cannot be listed. "
            : ""}
          Register a skill, or connect an MCP server, and it will appear here.
        </p>
      ) : null}
    </form>
  );
}


/* ── Your steps ───────────────────────────────────────────────────────────────── */

/**
 * The customer's OWN steps, managed somewhere they fit.
 *
 * This lived in the builder's palette — the define button beneath however many steps already
 * existed, so with eleven of them the way to make a new one was the one thing you had to
 * scroll to find, on a panel whose job is placing steps rather than authoring them.
 *
 * Splitting it is not just tidying. A step is a durable object of its own: it names a skill,
 * carries a risk tier, and can be placed in several workflows or none. Managing it inside the
 * editor for ONE workflow made it look like a property of that workflow.
 *
 * The palette still lists these, because that is where you place one. It no longer tries to be
 * where you author one too.
 */
function StepsView({
  customSteps, bindable, riskFloor, projectId, busy, onDefineStep, onRemoveStep,
}: {
  customSteps: CustomStep[];
  bindable: Bindable;
  riskFloor: number;
  projectId: string;
  busy: string | null;
  onDefineStep: (fields: Record<string, unknown>) => Promise<{ ok: boolean; error?: string }>;
  onRemoveStep: (stepKey: string) => void;
}) {
  const [defining, setDefining] = useState(false);
  /** The step being changed. A step could be defined and removed and never corrected, so
   *  fixing a summary meant deleting it — and delete gives the replacement a NEW KEY, which
   *  every workflow placing the old one silently loses. */
  const [editing, setEditing] = useState<CustomStep | null>(null);
  const unusable = customSteps.filter((c) => !c.usable);

  return (
    <section className="dw-steps" data-testid="dw-steps">
      <div className="section-head">
        <div>
          <span className="eyebrow">Your steps</span>
          <h2>Things this project can do that BOSS does not ship.</h2>
          <p className="detail">
            A step of your own runs one of your skills or an MCP tool — filing a ticket, telling
            a compliance system, publishing a video. Define it once here and place it in any
            workflow.
          </p>
        </div>
        {!defining && !editing ? (
          <button type="button" className="cap-btn primary" disabled={!!busy}
                  onClick={() => setDefining(true)} data-testid="dw-define-step">
            + Define a step
          </button>
        ) : null}
      </div>

      {defining || editing ? (
        <DefineStepForm bindable={bindable} riskFloor={riskFloor} busy={busy}
                        projectId={projectId} editing={editing}
                        // Keyed by the step, so switching from one step to another rebuilds
                        // the form rather than leaving the first one's values in the fields.
                        key={editing?.step_key ?? "new"}
                        onCancel={() => { setDefining(false); setEditing(null); }}
                        onDefine={async (fields) => {
                          const result = await onDefineStep(fields);
                          if (result.ok) { setDefining(false); setEditing(null); }
                          return result;
                        }} />
      ) : null}

      {/* BROKEN ONES FIRST, AND SAID PLAINLY. A step bound to a skill that no longer exists
          sits in the palette looking placeable and fails the run that places it. Buried in a
          list it is invisible; at the top with its reason it is a thing to fix. */}
      {unusable.length > 0 ? (
        <p className="dw-notice" data-testid="dw-steps-unusable">
          {unusable.length === 1
            ? `1 step cannot run: ${unusable[0].unusable_reason}`
            : `${unusable.length} steps cannot run — each says why below.`}
        </p>
      ) : null}

      {customSteps.length === 0 && !defining ? (
        <p className="detail" data-testid="dw-steps-empty">
          None yet. Define one above, or describe what you want to the Program Manager on the
          Design tab and let BOSS write it.
        </p>
      ) : (
        <ul className="skill-list" data-testid="dw-steps-list">
          {customSteps.map((c) => (
            <li key={c.step_key} className="skill-row" data-enabled={c.usable}>
              <span className="skill-row-main">
                <b>{c.label}</b>
                <code className="detail">{c.step_key}</code>
                <span className="cap-badge tone-muted">{c.binding_kind}</span>
                {c.approval_forced_manual ? (
                  <span className="cap-badge tone-amber">always approved</span>
                ) : null}
                {!c.usable ? <span className="cap-badge tone-pink">cannot run</span> : null}
              </span>
              <span className="detail">
                {/* WHAT IT RUNS, not what it was named. A step called "Cleanup" bound to a
                    tier-3 tool should not read as harmless. */}
                {c.usable ? `runs ${c.risk_source}` : c.unusable_reason}
              </span>
              {c.summary ? <span className="detail">{c.summary}</span> : null}
              <span className="dw-step-actions">
                <button type="button" className="linklike" disabled={!!busy}
                        data-testid={`dw-step-edit-${c.step_key}`}
                        onClick={() => { setDefining(false); setEditing(c); }}>
                  Edit
                </button>
                <button type="button" className="linklike" disabled={!!busy}
                        data-testid={`dw-step-remove-${c.step_key}`}
                        onClick={() => onRemoveStep(c.step_key)}>
                  Remove
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function BuilderView({ capabilities, blocks, riskFloor, installed, projectId, busy,
                      customSteps, bindable, onManageSteps, stepRoles, holdings, onGranted,
                      justMade, onDismissJustMade,
                      target, setTarget, onSave, onCreate }: {
  capabilities: Capability[];
  blocks: Block[];
  riskFloor: number;
  installed: Installed[];
  projectId: string;
  customSteps: CustomStep[];
  bindable: Bindable;
  /** Take the operator to where steps are authored. The palette places them; it does not
   *  make them. */
  onManageSteps: () => void;
  /** Agents a step can be assigned to, exactly as the publish-time check will accept them. */
  stepRoles: string[];
  /** `role_key` → the skills that agent HOLDS, so the row can say whether the agent named on a
   *  step is actually permitted to run it — and offer the grant instead of failing mid-run. */
  holdings: Record<string, string[]>;
  onGranted: () => void;
  /** The workflow created a moment ago, so the builder can confirm it rather than looking
   *  identical to the state before the click. */
  justMade: { id: string; name: string; from?: string } | null;
  onDismissJustMade: () => void;
  busy: string | null;
  target: string;
  setTarget: (id: string) => void;
  onSave: (definitionId: string, spec: Record<string, unknown>) => void;
  /** Creates the workflow AND writes the steps that are on the canvas. `fromKey` is the
   *  workflow the steps were imported from, so the lineage that makes "Reset to the standard"
   *  meaningful survives — importing into the canvas would otherwise lose it. */
  onCreate: (name: string, spec: Record<string, unknown>, fromKey?: string) => void;
}) {
  // Only a customer's OWN workflows can be edited. Offering a catalogue one here and refusing
  // on save would be a control that lies about what it does.
  const editable = useMemo(() => installed.filter((i) => !i.is_catalogue), [installed]);
  const [draft, setDraft] = useState<Draft[]>([]);
  const [dragging, setDragging] = useState<number | null>(null);
  // What the selected workflow currently IS, and what it was forked from.
  const [loaded, setLoaded] = useState<WorkflowSpec | null>(null);
  const [loadingSpec, setLoadingSpec] = useState(false);
  const [specError, setSpecError] = useState<string | null>(null);
  // WHAT IT WILL DO, as opposed to which steps it has.
  //
  // The header said "4 steps · written from scratch" and the rows said "Edit the video /
  // auto". That is the whole of what an operator could learn before starting a run: not what
  // a step does, not what it produces, not which agent answers for it, not that "auto" on a
  // tier-3 step is going to stop and wait for them anyway.
  const [plan, setPlan] = useState<WorkflowPlan | null>(null);
  //: Which step "Customize this step" sent them to, so the row can say so. Cleared on the
  //: next selection — a highlight that never goes away stops meaning anything.
  const [focusStep, setFocusStep] = useState<string | null>(null);
  /** Which step has its inputs open. One at a time: a canvas with every step expanded is a
   *  form, and the thing being edited is the ORDER as much as the wiring. */
  const [wiringStep, setWiringStep] = useState<string | null>(null);
  //: MAKING ONE COMES FIRST, and the default has to agree with the tabs.
  //:
  //: The tab ORDER already says this — "somebody who opens 'Build your own' is usually here to
  //: build one; editing is what you do on a return visit" — but the state behind it opened on
  //: Edit, so the first tab was the one the eye lands on and the second was the one selected.
  //: Every reset `beginCreating` performs is already the initial value of the state it clears,
  //: so starting here needs nothing stashed: there is no canvas yet to put back.
  const [creating, setCreating] = useState(true);
  // EXCEPT WHEN SOMEBODY ASKED TO EDIT A PARTICULAR ONE.
  //
  // Making "Create" the default tab left the Edit button on a workflow card doing nothing
  // visible: it set the target and switched to this tab, which then opened on Create — so the
  // control named Edit reliably landed on the create screen. The builder's OWN target changes
  // come from a picker that is only rendered while editing, so a target arriving while
  // `creating` is true can only have come from outside, which makes this unambiguous.
  useEffect(() => {
    if (target) setCreating(false);
    // Deliberately keyed on `target` alone: re-running when `creating` changes would fight the
    // tabs, snapping the operator back out of Create every time they pressed it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);
  const [newName, setNewName] = useState("");
  const [importFrom, setImportFrom] = useState("");
  /** The canvas as it was before "New workflow" was pressed, so Cancel puts it back rather
   *  than leaving the operator on an empty canvas for a workflow they did not change. */
  const [stashed, setStashed] = useState<Draft[] | null>(null);
  /** Which workflow's steps are currently on the canvas, and how many came across. Set by
   *  Import, cleared by Cancel — this is what lets the panel say what it just did without
   *  anything having been saved. */
  const [importedFrom, setImportedFrom] = useState<{ key: string; name: string; steps: number } | null>(null);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);


  /** What an import would be called if the operator names nothing. Shown as the placeholder,
   *  so the name the workflow WILL get is visible before the button is pressed.
   *
   *  Two things it has to avoid, both of which happened:
   *    - COMPOUNDING. Copying a copy produced "Minor Feature (copy) (copy)", and the one after
   *      that would have been worse. The existing suffix is stripped before a new one is added.
   *    - COLLIDING. The workflow KEY was made unique but the NAME was not, so the picker
   *      listed two entries reading "Minor Feature (yours) (copy)" and neither said which was
   *      which. Names are now numbered against what the project already has.
   */
  const defaultImportName = useMemo(() => {
    if (!importFrom) return "";
    const source = installed.find((i) => i.workflow_key === importFrom);
    if (!source) return "";
    return uniqueWorkflowName(source.name, installed.map((i) => i.name));
  }, [importFrom, installed]);
  const [defining, setDefining] = useState(false);

  useEffect(() => {
    if (!target && editable.length) setTarget(editable[0].id);
  }, [editable, target, setTarget]);

  /** Turn a stored spec into canvas steps. */
  const toDraft = useCallback((steps: Record<string, unknown>[]): Draft[] =>
    steps.map((step) => {
      const binding = (step.binding ?? {}) as Record<string, unknown>;
      const approval = (step.approval ?? {}) as Record<string, unknown>;
      return {
        id: String(step.id),
        kind: String(step.kind ?? "capability"),
        capability: binding.capability ? String(binding.capability) : undefined,
        customStep: binding.custom_step ? String(binding.custom_step) : undefined,
        approval: approval.mode ? String(approval.mode) : "auto",
        // Carried through untouched. The builder cannot yet AUTHOR a step-to-step reference,
        // which is a gap; silently discarding one somebody else authored is a bug.
        inputs: (binding.inputs ?? undefined) as Record<string, unknown> | undefined,
        captain: step.captain ? String(step.captain) : undefined,
        // AN AGENT STEP, which binds no capability and no custom step. Read back off the same
        // binding it was saved into, so opening a workflow and saving it cannot turn one into a
        // step that runs nothing — the bug that destroyed step wiring once already.
        agentWork: Boolean(binding.agent_work) || undefined,
        instructions: binding.agent_work
          ? ((binding.inputs ?? {}) as Record<string, unknown>).instructions
          : undefined,
        // Everything that is not id/kind/binding/approval/next is the block's own
        // configuration — wait_seconds, workflow_version_id — and must survive a round trip
        // or editing one step would quietly reset another.
        config: Object.fromEntries(
          Object.entries(step).filter(([k]) =>
            !["id", "kind", "binding", "approval", "next"].includes(k)),
        ),
      };
    }), []);

  /** Open the new-workflow panel on an EMPTY canvas. */
  const beginCreating = useCallback(() => {
    setStashed(draft);
    setDraft([]);
    setImportedFrom(null);
    setImportError(null);
    setNewName("");
    setImportFrom("");
    setCreating(true);
  }, [draft]);

  const cancelCreating = useCallback(() => {
    setCreating(false);
    setNewName("");
    setImportFrom("");
    setImportedFrom(null);
    setImportError(null);
    if (stashed) setDraft(stashed);
    setStashed(null);
  }, [stashed]);

  /** IMPORT BRINGS THE STEPS HERE. IT DOES NOT SAVE ANYTHING.
   *
   *  It used to fork server-side on the spot: a workflow appeared in the project before the
   *  operator had decided anything, and the screen jumped into editing it. Nothing is written
   *  now until Create — Import only fills the canvas, so the steps can be changed first and
   *  abandoning the idea leaves nothing behind. */
  const importSteps = useCallback(async () => {
    if (!importFrom) return;
    setImporting(true);
    setImportError(null);
    try {
      const params = new URLSearchParams({
        view: "spec", project_id: projectId, key: importFrom,
      });
      const r = await fetch(`/api/delivery-workflows?${params}`, { cache: "no-store" });
      const body = (await r.json()) as WorkflowSpec & { error?: string };
      if (!r.ok || !body?.spec?.steps) {
        setImportError(body?.error ?? "could not read that workflow's steps");
        return;
      }
      const steps = toDraft(body.spec.steps);
      setDraft(steps);
      setImportedFrom({
        key: importFrom,
        name: installed.find((i) => i.workflow_key === importFrom)?.name ?? importFrom,
        steps: steps.length,
      });
    } catch (e) {
      setImportError(String((e as Error)?.message ?? e));
    } finally {
      setImporting(false);
    }
  }, [importFrom, projectId, toDraft, installed]);

  // OPEN WITH WHAT IS THERE.
  //
  // The builder started empty, so changing one step of a ten-step workflow meant rebuilding
  // the other nine from memory — and saving would have silently replaced the workflow with
  // whatever partial thing had been assembled.
  // KEYED ON THE WORKFLOW, NOT ON THE ARRAY.
  //
  // `editable` is derived from `installed`, which the eight-second poll replaces with a new
  // array every time — so depending on it re-ran this effect constantly, and any hiccup in the
  // refetch cleared the canvas. A customer editing a ten-step workflow could lose it because a
  // background poll happened to fail. The KEY is what actually identifies what to load.
  const targetKey = useMemo(
    () => editable.find((i) => i.id === target)?.workflow_key ?? "",
    [editable, target],
  );

  useEffect(() => {
    // WHILE CREATING, THE CANVAS IS THE NEW WORKFLOW'S.
    //
    // Nothing exists on the server yet, so there is no spec to load — and loading the selected
    // workflow's steps over the ones just imported is exactly the confusion between "the one
    // you are editing" and "the one you are making" that this flow had.
    if (creating) return;
    if (!targetKey) { setLoaded(null); setDraft([]); setPlan(null); return; }
    let live = true;
    setLoadingSpec(true);
    // The plan is fetched ALONGSIDE the spec rather than on a click, because "what will this
    // do" is the question you have while looking at it — making it a second interaction is
    // how it ends up never being asked.
    void (async () => {
      const p = new URLSearchParams({ view: "plan", project_id: projectId, key: targetKey });
      try {
        const r = await fetch(`/api/delivery-workflows?${p}`, { cache: "no-store" });
        const body = (await r.json()) as WorkflowPlan & { error?: string };
        if (live && r.ok && Array.isArray(body?.steps)) setPlan(body);
      } catch {
        /* the plan is an explanation; failing to load one must not break the editor */
      }
    })();
    void (async () => {
      const params = new URLSearchParams({
        view: "spec", project_id: projectId, key: targetKey,
      });
      try {
        const r = await fetch(`/api/delivery-workflows?${params}`, { cache: "no-store" });
        const body = (await r.json()) as WorkflowSpec & { error?: string };
        if (!live) return;
        if (r.ok && body?.spec?.steps) {
          setLoaded(body);
          setDraft(toDraft(body.spec.steps));
        }
        // NO `else` THAT CLEARS. A failed refetch is a reason to keep what is on the canvas,
        // not to throw it away — the steps already there are the customer's work, and an
        // empty canvas after a network blip is indistinguishable from having deleted it.
        else {
          setSpecError(
            (body as { error?: string })?.error ?? "could not read the current steps",
          );
        }
      } catch (e) {
        if (live) setSpecError(String((e as Error)?.message ?? e));
      } finally {
        if (live) setLoadingSpec(false);
      }
    })();
    return () => { live = false; };
  }, [targetKey, projectId, toDraft, creating]);

  /** Put it back the way the standard has it. */
  const reset = useCallback(() => {
    if (!loaded?.forked_from) return;
    setDraft(toDraft(loaded.forked_from.spec.steps));
  }, [loaded, toDraft]);

  /** Unique per drop. Adding "build" twice with one id is rejected by the runtime, at save
   *  time, after a whole workflow has been laid out. */
  const nextId = (current: Draft[], stem: string) => {
    const taken = new Set(current.map((s) => s.id));
    let n = current.filter((s) => s.id.startsWith(`${stem}_`)).length + 1;
    while (taken.has(`${stem}_${n}`)) n += 1;
    return `${stem}_${n}`;
  };

  const add = (capability: Capability) =>
    setDraft((current) => [...current, {
      id: nextId(current, capability.key),
      kind: "capability",
      capability: capability.key,
      approval: capability.approval_forced_manual ? "manual" : "auto",
      config: {},
    }]);

  /** A step that says what is wanted and lets an agent work out how.
   *
   *  IT BINDS NOTHING, and that is the whole point. Every other step on this canvas names a
   *  skill, so a request the skill cannot express needs BOSS to ship a new one — which is how a
   *  framework turns into a thousand special cases. This one carries an instruction, and the
   *  agent's own skills are the vocabulary.
   *
   *  `auto` is honest here: the agent's loop still stops for a person on any skill at or above
   *  the risk floor, so a step that reaches a publish waits whatever this says.
   */
  const addAgentWork = () =>
    setDraft((current) => [...current, {
      id: nextId(current, "agent_work"),
      kind: "capability",
      agentWork: true,
      approval: "auto",
      config: {},
    }]);

  /** A step the customer defined. Its approval mode follows the resolved risk tier, so a
   *  tier-3 tool arrives on the canvas already marked manual rather than being switched
   *  silently at save time. */
  const addCustom = (step: CustomStep) =>
    setDraft((current) => [...current, {
      id: nextId(current, step.step_key),
      kind: "capability",
      customStep: step.step_key,
      approval: step.approval_forced_manual ? "manual" : "auto",
      config: {},
    }]);

  /** A control block: wait, an approval gate, or another workflow. */
  const addBlock = (block: Block) =>
    setDraft((current) => [...current, {
      id: nextId(current, block.kind),
      kind: block.kind,
      approval: "auto",
      config: { ...(block.defaults ?? {}) },
    }]);

  /** What this step takes, and whether each one is required — the customer's own steps declare
   *  it, the built-in capabilities declare it, and the wiring panel needs one answer. */
  const declaredInputs = useCallback((step: Draft) => {
    if (step.customStep) {
      const mine = customSteps.find((c) => c.step_key === step.customStep);
      return (mine?.inputs ?? []).map((i) => ({ name: i.name, required: i.required }));
    }
    const capability = capabilities.find((c) => c.key === step.capability);
    return [
      ...(capability?.required_inputs ?? []).map((name) => ({ name, required: true })),
      ...(capability?.optional_inputs ?? []).map((name) => ({ name, required: false })),
    ];
  }, [capabilities, customSteps]);

  /** What the steps BEFORE this one produce. Only earlier steps: a workflow that reads from a
   *  step which has not run yet cannot resolve, and the runtime would fail it mid-run rather
   *  than at save — so the choice is not offered in the first place. */
  const upstream = useCallback((index: number) =>
    draft.slice(0, index).map((step) => {
      const mine = step.customStep
        ? customSteps.find((c) => c.step_key === step.customStep)
        : undefined;
      const capability = step.capability
        ? capabilities.find((c) => c.key === step.capability)
        : undefined;
      return {
        id: step.id,
        label: mine?.label ?? capability?.label ?? step.id,
        fields: (mine?.produces ?? capability?.produces ?? []) as string[],
      };
    }), [draft, capabilities, customSteps]);

  /** Change one input's wiring on one step. */
  const setWire = useCallback((stepId: string, input: string, value: unknown) =>
    setDraft((current) => current.map((step) => {
      if (step.id !== stepId) return step;
      const inputs = { ...(step.inputs ?? {}) };
      if (value === undefined) delete inputs[input];
      else inputs[input] = value;
      return { ...step, inputs };
    })), []);

  const move = (from: number, to: number) =>
    setDraft((current) => {
      if (to < 0 || to >= current.length || from === to) return current;
      const next = [...current];
      const [item] = next.splice(from, 1);
      next.splice(to, 0, item);
      return next;
    });

  const spec = useMemo(() => ({
    entry_step: draft[0]?.id,
    steps: draft.map((step, index) => {
      const next = index + 1 < draft.length ? [draft[index + 1].id] : [];
      if (step.kind !== "capability") {
        // A control block carries its own configuration at the top level, which is where the
        // runtime looks for `wait_seconds` and `workflow_version_id`.
        //
        // AND ITS CAPTAIN, which this dropped. An Approval gate is the one step where naming
        // who answers is the whole point, and the save threw it away — so even once the control
        // appeared, choosing `human` on a gate would not have survived the version.
        return {
          id: step.id, kind: step.kind, binding: {}, ...(step.config ?? {}),
          ...(step.captain ? { captain: step.captain } : {}),
          next,
        };
      }
      if (step.agentWork) {
        return {
          id: step.id,
          kind: "capability",
          binding: {
            agent_work: true,
            inputs: {
              // Everything else the step passes along is context the agent is given as facts —
              // an artifact id an earlier step produced, which it must use verbatim.
              ...(step.inputs ?? {}),
              // The instruction is the step. Defaulted to the run's own `instructions` field,
              // because "say what you want when you start it" is the shape almost every one of
              // these has.
              instructions: step.instructions ?? { from_input: "instructions" },
            },
          },
          approval: { mode: step.approval },
          // NO CAPTAIN IS A REAL CHOICE HERE, and a different one from the other kinds: it means
          // "whichever agent fits this instruction", which BOSS works out from what each agent
          // is for and what it holds.
          ...(step.captain ? { captain: step.captain } : {}),
          next,
        };
      }
      if (step.customStep) {
        const defined = customSteps.find((c) => c.step_key === step.customStep);
        return {
          id: step.id,
          kind: "capability",
          binding: {
            custom_step: step.customStep,
            // What the step was saved with wins. The default below — every input read from the
            // run's input by name — is a starting point for a step just dropped on the canvas,
            // not a rule to impose on one that already has wiring.
            inputs: step.inputs ?? Object.fromEntries(
              (defined?.inputs ?? []).map((i) => [i.name, { from_input: i.name }]),
            ),
          },
          approval: { mode: step.approval },
          // Only when one was chosen. An absent captain is the existing behaviour — the step
          // runs as the anonymous workflow runner — and writing an empty string would make
          // every step look like it had been assigned to nobody on purpose.
          ...(step.captain ? { captain: step.captain } : {}),
          next,
        };
      }
      const capability = capabilities.find((c) => c.key === step.capability);
      return {
        id: step.id,
        kind: "capability",
        binding: {
          capability: step.capability,
          // Required inputs are read from the run's input by name. The builder does not invent
          // values — a workflow that hard-codes the thing being built only works once. Same
          // rule as above: existing wiring is preserved rather than overwritten.
          inputs: step.inputs ?? Object.fromEntries(
            (capability?.required_inputs ?? []).map((name) => [name, { from_input: name }]),
          ),
        },
        approval: { mode: step.approval },
        // A capability already has a default captain; naming one here overrides it for this
        // workflow, which the runtime has honoured all along.
        ...(step.captain ? { captain: step.captain } : {}),
        next,
      };
    }),
  }), [draft, capabilities, customSteps]);

  return (
    <div className="dw-builder">
      <aside className="dw-palette" aria-label="Steps you can add">
        {/* THE WAY TO MAKE A STEP, AT THE TOP, WHERE SOMEBODY LOOKING FOR IT WILL LOOK.
            It was moved next to the "Your steps" sub-heading once already, which is still
            below fourteen built-in capabilities: on a normal screen you scroll past the whole
            palette before the control appears, and the operator reported twice that there was
            no obvious way to add a step. The one below stays — this is the discoverable one. */}
        <div className="dw-palette-heading-row">
          <h4 className="dw-palette-heading">Steps</h4>
          {/* STILL THE DISCOVERABLE CONTROL, now a door rather than a form. Authoring a step
              on the panel that places them meant a long form unfolding inside a narrow column
              while the canvas sat behind it. It goes where steps are managed instead. */}
          <button type="button" className="cap-btn dw-define" disabled={!!busy}
                  onClick={onManageSteps} data-testid="dw-define-step-top">
            + New step
          </button>
        </div>
        <p className="detail dw-palette-summary">
          Click a step to add it, or make one of your own that runs your skill or an MCP tool.
        </p>
        {/* FIRST IN THE PALETTE, because it is the general answer and the fourteen below it are
            the specific ones. An operator whose request does not match a listed capability used
            to have nothing to reach for; this is the step for "just do what I asked". */}
        <button type="button" className="dw-palette-item dw-palette-agent"
                onClick={addAgentWork} disabled={!!busy}
                data-testid="dw-palette-agent-work"
                title="The step carries your instructions; the agent uses the skills it holds">
          <span className="dw-palette-label">Let an agent work it out</span>
          <span className="detail dw-palette-summary">
            You write what you want in plain English. An agent does it with the skills it holds —
            no fixed list of operations, so a new kind of request needs a new skill on the agent,
            not a new step here.
          </span>
        </button>
        {capabilities.map((c) => (
          <button key={c.key} type="button" className="dw-palette-item"
                  onClick={() => add(c)} disabled={!!busy}
                  data-testid={`dw-palette-${c.key}`}
                  title={`${c.summary}${c.requires_project.length ? ` · needs ${c.requires_project.join(", ")}` : ""}`}>
            <span className="dw-palette-label">{c.label}</span>
            {/* Risk is shown in the palette, before the step is placed. Finding out at save
                time that a step can never be automatic is finding out too late. */}
            {c.approval_forced_manual ? (
              <span className="dw-gate">always approved</span>
            ) : null}
            <span className="detail dw-palette-summary">{c.summary}</span>
          </button>
        ))}
        <p className="detail dw-floor">
          Anything at risk tier {riskFloor} or above always requires a person. That is not
          configurable.
        </p>

        {/* THE CUSTOMER'S OWN STEPS, in the same palette as the built-in ones. A separate
            drawer for "your" steps would make people learn which of their steps BOSS wrote. */}
        {/* THE CONTROL WITH ITS HEADING, not below whatever happens to be listed. With eleven
            of the customer's own steps the button sat off the bottom of the panel, so the way
            to make one was the one thing you had to scroll to find. */}
        {/* THE PALETTE LISTS THESE; IT NO LONGER AUTHORS THEM.
            Defining a step lived here, beneath however many already existed, so with eleven of
            them the way to make one was the thing you had to scroll to find — on a panel whose
            job is placing steps. Authoring moved to the Your steps tab, which is also where a
            step that cannot run says why. */}
        <div className="dw-palette-heading-row">
          <h4 className="dw-palette-heading">Your steps</h4>
        </div>
        {customSteps.length === 0 ? (
          <p className="detail dw-palette-summary">
            None yet — define one on the <b>Your steps</b> tab, or describe what you want to
            the Program Manager on <b>Design a workflow</b>.
          </p>
        ) : null}
        {customSteps.map((c) => (
          <button key={c.step_key} type="button" className="dw-palette-item dw-palette-mine"
                  onClick={() => addCustom(c)} disabled={!!busy || !c.usable}
                  data-testid={`dw-palette-custom-${c.step_key}`}
                  title={c.usable
                    ? `${c.summary || c.label} · runs ${c.risk_source}`
                    : (c.unusable_reason ?? "")}>
            <span className="dw-palette-label">{c.label}</span>
            {c.approval_forced_manual ? (
              <span className="dw-gate">always approved</span>
            ) : null}
            <span className="detail dw-palette-summary">
              {/* What it RUNS, not what it was named. A step called "Cleanup" bound to a
                  tier-3 tool should not read as harmless in the palette. */}
              {c.usable ? `runs ${c.risk_source}` : c.unusable_reason}
            </span>
            {/* NO × HERE ANY MORE. On a palette item it read as "take this off the
                canvas" and actually deleted the step from the PROJECT — a destructive act in
                the shape of a dismissal. Removing one is on the Your steps tab, where the row
                is about the step itself rather than about placing it. */}
          </button>
        ))}

        {/* CONTROL BLOCKS. The runtime has supported these since it was written and the
            builder never offered them, so a customer could only ever author a straight line.
            The ones that need somewhere to draw a branch are listed but not placeable — a
            palette entry that produces a spec the runtime rejects is a promise the product
            cannot keep. */}
        <h4 className="dw-palette-heading">Flow</h4>
        {blocks.filter((b) => !b.canvas_required).map((b) => (
          <button key={b.kind} type="button" className="dw-palette-item"
                  onClick={() => addBlock(b)} disabled={!!busy}
                  data-testid={`dw-block-${b.kind}`} title={b.summary}>
            <span className="dw-palette-label">{b.label}</span>
            <span className="detail dw-palette-summary">{b.summary}</span>
          </button>
        ))}
        {blocks.some((b) => b.canvas_required) ? (
          <details className="dw-soon">
            <summary className="detail">
              {blocks.filter((b) => b.canvas_required).length} more, once the canvas can draw
              branches
            </summary>
            {blocks.filter((b) => b.canvas_required).map((b) => (
              <p key={b.kind} className="detail dw-soon-item">
                <strong>{b.label}</strong> — {b.summary}
              </p>
            ))}
          </details>
        ) : null}
      </aside>

      <section className="dw-canvas" aria-label="Your workflow">
        {/* SAY IT WORKED, WHERE THE PERSON IS LOOKING.
            Import created the workflow, copied its steps and selected it — and then showed a
            toolbar identical to the one before the click, so it read as "went back to editing",
            which is how a working feature got reported as broken. The confirmation belongs on
            the canvas that changed, not in a sticky line further up the page. */}
        {justMade && justMade.id === target ? (
          <p className="dw-made" data-testid="dw-made">
            <span className="dw-made-tick" aria-hidden>✓</span>
            <span>
              Created <strong>{justMade.name}</strong>
              {justMade.from ? <> from <strong>{justMade.from}</strong></> : null}
              {draft.length ? ` · ${draft.length} step${draft.length === 1 ? "" : "s"} copied` : ""}
              . You are editing it now — change what you want, then save.
            </span>
            <button type="button" className="dw-made-x" onClick={onDismissJustMade}
                    aria-label="Dismiss">×</button>
          </p>
        ) : null}
        {/* TWO TABS, BECAUSE THEY ARE TWO JOBS.
            Editing an existing workflow and authoring a new one were one screen separated only
            by a button: "Editing: Standard Product Delivery" sat beside "Start from: Backend
            Service Setup", and the operator had to work out which of the two workflows the
            controls acted on. The project has 54 definitions, a large share of them named
            "(copy)" and "(copy 2)" — accidental forks are the predictable result of a screen
            that does not say which mode it is in.

            The modes already existed; only the way in and out did not. `beginCreating` stashes
            the canvas and `cancelCreating` restores it, so the tabs are a faithful control for
            state that was always there — no behaviour changes, the choice is just visible. */}
        <div className="dw-mode-tabs" role="tablist" aria-label="What do you want to do?">
          {/* MAKING ONE COMES FIRST. Somebody who opens "Build your own" is usually here to
              build one; editing is what you do on a return visit. Reading order should match
              that, and the first tab is where the eye lands. */}
          <button type="button" role="tab" className="dw-mode-tab"
                  aria-selected={creating} data-testid="dw-tab-new"
                  disabled={!!busy}
                  onClick={() => { if (!creating) beginCreating(); }}>
            <WfIcon id="plus" /> Create a new workflow
          </button>
          <button type="button" role="tab" className="dw-mode-tab"
                  aria-selected={!creating} data-testid="dw-tab-edit"
                  disabled={!!busy}
                  onClick={() => { if (creating) cancelCreating(); }}>
            <WfIcon id="edit" /> Edit an existing workflow
          </button>
        </div>
        <div className="dw-canvas-head" data-mode={creating ? "creating" : "editing"}>
          {/* ONE QUESTION AT A TIME.
              "Editing: Standard Product Delivery" beside "Start from: Backend Service Setup"
              asks the operator to hold two different workflows in mind and says nothing about
              which the buttons act on — and the answer was "the one you are not making". While
              a new workflow is being named, the controls that belong to the old one (which one
              you are editing, reset it, save it) are not just noise, they are wrong. */}
          {/* TWO GROUPS, NOT FIVE CONTROLS IN A ROW.
              Left is what you are working on — which workflow, or the new one you are naming.
              Right is what happens to it. Flat and evenly weighted, the row gave "+ New
              workflow" the same presence as "Reset to the standard", so the one control that
              starts something read as the least important thing on the line. */}
          <div className="dw-head-left">
          {!creating ? (
            <div className="dw-head-picker">
              <label className="field">
                <span><WfIcon id="edit" /> Editing</span>
                <select className="input" value={target}
                        onChange={(e) => setTarget(e.target.value)} data-testid="dw-target">
                  {/* The tab is called "Build your own", and until now the only way in was to
                      fork something — so a screen that promised authorship offered only
                      editing. */}
                  {editable.length === 0
                    ? <option value="">Nothing of your own yet</option>
                    : null}
                  {editable.map((i) => (
                    <option key={i.id} value={i.id}>{i.name}</option>
                  ))}
                </select>
              </label>
              {/* A header made only of controls tells you nothing about the thing they act on.
                  Two facts, in the space the dead gap used to occupy: how big this workflow is,
                  and whether it came from a standard — which is also what decides whether
                  "Reset to the standard" beside it means anything. */}
              {target ? (
                <p className="detail dw-head-meta" data-testid="dw-head-meta">
                  {draft.length} step{draft.length === 1 ? "" : "s"}
                  {loaded?.forked_from
                    ? <> · from <strong>{loaded.forked_from.name}</strong></>
                    : " · written from scratch"}
                </p>
              ) : null}
            </div>
          ) : null}

          {creating ? (
            <form className="dw-new" onSubmit={(e) => {
              e.preventDefault();
              // CREATE IS THE ONLY THING THAT WRITES.
              //
              // Import used to fork on the spot, so a workflow appeared in the project before
              // the operator had decided anything and the screen jumped straight into editing
              // it. Now the canvas is filled first, changed if they want, and only saved here.
              const name = newName.trim() || defaultImportName;
              if (!name) return;
              onCreate(name, spec, importedFrom?.key);
            }}>
              <label className="field">
                <span><WfIcon id="edit" /> Name</span>
                <input className="input" autoFocus value={newName}
                       placeholder={defaultImportName || "Name it"}
                       onChange={(e) => setNewName(e.target.value)}
                       onKeyDown={(e) => { if (e.key === "Escape") cancelCreating(); }}
                       aria-label="Name for the new workflow" data-testid="dw-new-name" />
              </label>
              <label className="field dw-new-from">
                <span><WfIcon id="importIn" /> Start from</span>
                <select className="input" value={importFrom} data-testid="dw-new-from"
                        onChange={(e) => setImportFrom(e.target.value)}>
                  <option value="">Nothing — an empty workflow</option>
                  {/* Every workflow in the project, not only the customer's own: importing a
                      standard is the common case, and it is what "work on top of it" means. */}
                  {installed.map((i) => (
                    <option key={i.id} value={i.workflow_key}>
                      {i.name}{i.is_catalogue ? " (standard)" : ""}
                    </option>
                  ))}
                </select>
              </label>
              {/* The three buttons are ONE group, so they wrap together. Left loose they broke
                  apart at this width and Cancel dropped onto a line of its own, which reads as
                  a layout fault rather than a wrap. */}
              <span className="dw-new-actions">
              {/* Import is a SEPARATE, non-destructive step: it fills the canvas below and
                  nothing else. Pressing it twice, or picking a different source and pressing
                  it again, is safe — no workflow exists yet to duplicate. */}
              <button type="button" className="cap-btn" data-testid="dw-import-steps"
                      onClick={() => void importSteps()}
                      disabled={!!busy || !importFrom || importing}>
                <WfIcon id={importedFrom ? "again" : "importIn"} />
                {importing ? "Importing…" : importedFrom ? "Import again" : "Import steps"}
              </button>
              <button type="submit" className="cap-btn primary"
                      disabled={!!busy || !(newName.trim() || defaultImportName)}
                      data-testid="dw-new-confirm">
                <WfIcon id="check" /> Create
              </button>
              <button type="button" className="cap-btn" onClick={cancelCreating}>
                <WfIcon id="x" /> Cancel
              </button>
              </span>
              {importError ? (
                <span className="detail dw-new-error" data-testid="dw-import-error">
                  <WfIcon id="info" /> Could not import — {importError}
                </span>
              ) : importedFrom ? (
                // Said in the panel, because the panel is where the operator still is. The
                // point of the whole change is that they do NOT get moved somewhere else.
                <span className="detail dw-new-hint" data-testid="dw-imported">
                  <WfIcon id="steps" />
                  {importedFrom.steps} step{importedFrom.steps === 1 ? "" : "s"} from{" "}
                  <strong>{importedFrom.name}</strong> are on the canvas. Change them, then
                  press Create — nothing is saved until you do.
                </span>
              ) : !newName.trim() && !importFrom ? (
                <span className="detail dw-new-hint" data-testid="dw-new-hint">
                  <WfIcon id="info" /> Name it, or pick something to start from.
                </span>
              ) : null}
            </form>
          ) : null}
          {/* The "+ New workflow" button that used to live here is now the second TAB. A
              divider and an accent-tinted CTA were both attempts to say "this is a different
              intention" inside a row that still mixed the two; a tablist says it outright, and
              says which one you are in. */}
          </div>
          {/* Hidden while creating, for the same reason: both act on the workflow in the
              dropdown that is no longer on screen. */}
          <span className="dw-canvas-actions" hidden={creating}>
            {/* RESET TO THE STANDARD. Only offered when there is something to reset TO — a
                fork records what it came from, and a workflow written from scratch has no
                original, so the control would be a lie on one of them. */}
            {!creating && loaded?.forked_from ? (
              <button type="button" className="cap-btn" disabled={!!busy}
                      onClick={reset} data-testid="dw-reset"
                      title={`Put the steps back the way ${loaded.forked_from.name} has them`}>
                <WfIcon id="reset" /> Reset to the standard
              </button>
            ) : null}
            {!creating ? (
              <button type="button" className="cap-btn primary"
                      disabled={!!busy || !target || draft.length === 0}
                      onClick={() => onSave(target, spec)} data-testid="dw-save">
                <WfIcon id="save" />
                {busy === "Save" ? "Saving…" : "Save as a new version"}
              </button>
            ) : null}
          </span>
        </div>

        {specError ? (
          <p className="detail dw-spec-error" data-testid="dw-spec-error">
            Could not re-read the saved steps — {specError}. What is on the canvas is still
            yours; saving will publish it.
          </p>
        ) : null}
        {/* ABOVE THE EDITOR, BECAUSE READING COMES BEFORE CHANGING.
            The operator's order was explicit: "first I want to see the workflow and its
            definition, what it will do, THEN trigger the run". */}
        {!creating && plan ? (
          <WorkflowPlanPanel
            plan={plan}
            onCustomize={(stepId) => {
              // Scroll the editor to that step and focus it, so "customize this step" lands
              // on the row rather than on a list the operator has to search.
              const row = document.querySelector(`[data-step-row="${stepId}"]`);
              if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
              setFocusStep(stepId);
            }}
          />
        ) : null}
        {loadingSpec && draft.length === 0 ? (
          <p className="detail dw-canvas-empty" data-testid="dw-canvas-loading">
            Reading the current steps…
          </p>
        ) : draft.length === 0 ? (
          <div className="dw-canvas-empty" data-testid="dw-canvas-empty">
            <span className="dw-empty-mark" aria-hidden><WfIcon id="empty" /></span>
            <p className="detail">
              {target
                ? "No steps yet — add them from the left, in the order they should run."
                : "Start a new workflow, or fork one of the standards to change it."}
            </p>
          </div>
        ) : (
          <ol className="dw-draft">
            {draft.map((step, index) => {
              const capability = capabilities.find((c) => c.key === step.capability);
              const mine = customSteps.find((c) => c.step_key === step.customStep);
              return (
                <li key={step.id} className="dw-draft-step" draggable
                    data-dragging={dragging === index || undefined}
                    data-drop={dragging !== null && dragging !== index ? "true" : undefined}
                    onDragStart={() => setDragging(index)}
                    onDragEnd={() => setDragging(null)}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={() => { if (dragging !== null) move(dragging, index); setDragging(null); }}
                    // The anchor "Customize this step" scrolls to. Keyed by STEP ID, which is
                    // what the plan knows — an index would break the moment a row is dragged.
                    data-step-row={step.id}
                    data-focused={focusStep === step.id || undefined}
                    data-testid={`dw-draft-${step.id}`}>
                  <span className="dw-draft-grip" aria-hidden>⋮⋮</span>
                  <span className="dw-draft-n">{index + 1}</span>
                  <span className="dw-draft-label">
                    {step.kind !== "capability"
                      ? blocks.find((b) => b.kind === step.kind)?.label ?? step.kind
                      : step.agentWork
                        ? "Let an agent work it out"
                        : step.customStep
                          ? mine?.label ?? step.customStep
                          : capability?.label ?? step.capability}
                  </span>
                  {step.customStep ? <span className="dw-draft-kind">yours</span> : null}
                  {step.agentWork ? <span className="dw-draft-kind">agent</span> : null}
                  {/* A control block is visibly a different sort of thing from a work step. */}
                  {step.kind !== "capability" ? (
                    <span className="dw-draft-kind">flow</span>
                  ) : null}
                  {/* WHAT THE STEP ACTUALLY DOES.
                      The row said "Plan the edit · yours · auto" and nothing else, so a
                      workflow could be reordered, saved and started by somebody who could not
                      find out which skill any of it ran, what it needed, or why one step said
                      manual. Every field here was already on the client; none of it was drawn. */}
                  <span className="dw-draft-what">
                    <span className="dw-draft-runs">
                      {step.agentWork
                        ? (step.captain
                            ? `${(holdings[step.captain] ?? []).length} skill${
                                (holdings[step.captain] ?? []).length === 1 ? "" : "s"
                              } to choose from`
                            : "whichever agent fits the instruction")
                        : mine
                          ? (mine.usable ? `runs ${mine.risk_source}` : mine.unusable_reason)
                          : capability
                            ? `${capability.skills.length} skill${
                                capability.skills.length === 1 ? "" : "s"}${
                                capability.captain ? ` · ${capability.captain}` : ""}`
                            : null}
                    </span>
                    {(mine?.summary || capability?.summary) ? (
                      <span className="dw-draft-summary">
                        {mine?.summary || capability?.summary}
                      </span>
                    ) : null}
                    {mine?.inputs?.length ? (
                      <span className="dw-draft-inputs">
                        needs {mine.inputs.map((i) => i.name).join(", ")}
                      </span>
                    ) : null}
                    {mine?.knowledge ? (
                      // House rules an operator wrote for this step. Shown as a hint rather
                      // than in full: it can be pages, and the row is not where it is read.
                      <span className="dw-draft-inputs" title={mine.knowledge}>
                        carries your notes
                      </span>
                    ) : null}
                    {/* WHY it is manual, where it is not the operator's choice. A locked
                        control with no reason reads as a broken control. */}
                    {(mine?.approval_forced_manual || capability?.approval_forced_manual) ? (
                      <span className="dw-draft-forced">
                        always needs a person — risk tier{" "}
                        {mine?.risk_tier ?? capability?.risk_tier} is at or above the floor
                      </span>
                    ) : null}
                  </span>
                  {/* THE STEP'S WHOLE SPECIFICATION, editable in the row.
                      Two shapes, because both are things operators say: ask me when the run
                      starts (the usual one — the same workflow does a different job each time),
                      or pin it here for a workflow that always does the same thing. */}
                  {step.agentWork ? (
                    <span className="dw-draft-agentwork">
                      <label className="dw-agentwork-mode">
                        <select className="input"
                                value={isFromInput(step.instructions) ? "ask" : "fixed"}
                                data-testid={`dw-agentwork-mode-${step.id}`}
                                onChange={(e) => setDraft((c) => c.map((st, i) => (i === index
                                  ? { ...st, instructions: e.target.value === "ask"
                                      ? { from_input: "instructions" } : "" }
                                  : st)))}>
                          <option value="ask">ask me when the run starts</option>
                          <option value="fixed">always do this</option>
                        </select>
                      </label>
                      {isFromInput(step.instructions) ? (
                        <span className="detail dw-agentwork-hint">
                          The agent is given whatever you type in the{" "}
                          <code>{String((step.instructions as {from_input: string}).from_input)}</code>{" "}
                          box when you start a run.
                        </span>
                      ) : (
                        <textarea className="input dw-agentwork-text" rows={3}
                                  value={typeof step.instructions === "string"
                                         ? step.instructions : ""}
                                  data-testid={`dw-agentwork-text-${step.id}`}
                                  placeholder={"What you want done, in plain English. The agent\n"
                                    + "works out which of its skills to use, and in what order."}
                                  onChange={(e) => setDraft((c) => c.map((st, i) => (i === index
                                    ? { ...st, instructions: e.target.value } : st)))} />
                      )}
                    </span>
                  ) : null}
                  {/* WHO RUNS IT. The premise of the product is that agents do the work and
                      the steps track it — which is already true of BOSS's own pipelines, where
                      a capability's captain becomes the actor. A customer's step ignored it and
                      the builder never offered it, so their workflows had no agent anywhere.
                      Empty is the existing behaviour: the anonymous workflow runner — EXCEPT on
                      an agent step, where it means "whichever agent fits", which BOSS works out
                      from what each one is for and what it holds. */}
                  {/* A GATE NEEDS AN AGENT TOO, AND IT IS THE ONE STEP WHERE THAT MATTERS MOST.
                      This rendered only for `capability`, so an Approval gate — kind
                      `decision` — showed no agent control at all: the operator could place the
                      one step whose entire purpose is that a PERSON answers it, and had nowhere
                      to say who. */}
                  {(step.kind === "capability" || step.kind === "decision")
                   && stepRoles.length ? (
                    <select className="input dw-draft-captain" value={step.captain ?? ""}
                            onChange={(e) => setDraft((c) =>
                              c.map((st, i) => (i === index
                                ? { ...st, captain: e.target.value || undefined } : st)))}
                            aria-label={`Agent for ${step.id}`}
                            data-testid={`dw-draft-captain-${step.id}`}>
                      <option value="">
                        {step.kind === "decision"
                          ? "human (default)"
                          : step.agentWork
                            ? "whichever agent fits — BOSS picks"
                            : capability?.captain
                              ? `${capability.captain} (default)`
                              : "no agent — runs as the workflow"}
                      </option>
                      {stepRoles.map((role) => (
                        <option key={role} value={role}>{role}</option>
                      ))}
                    </select>
                  ) : null}
                  {/* CAN THAT AGENT ACTUALLY DO IT?
                      Naming an agent is half the decision; holding the skill is the other half,
                      and it is the half the run enforces. Checked here, in the row, with the
                      grant one click away — because the alternative is finding out after the
                      run has waited on a human approval. */}
                  {step.agentWork ? (
                    // AN AGENT STEP HAS NO ONE SKILL TO CHECK — the whole of what the agent
                    // holds is what it may use. What is worth saying is when it holds NOTHING,
                    // because then the step is guaranteed to fail.
                    step.captain && (holdings[step.captain] ?? []).length === 0 ? (
                      <span className="dw-draft-grant" data-testid={`dw-empty-${step.id}`}>
                        {step.captain} holds no skills, so it cannot do anything. Open it on the
                        Agents page and give it the skills this work needs.
                      </span>
                    ) : null
                  ) : (
                    <CaptainCanRunIt projectId={projectId} step={step} mine={mine}
                                     holdings={holdings} onGranted={onGranted} />
                  )}
                  <select className="input dw-draft-approval" value={step.approval}
                          disabled={step.kind !== "capability"
                                    || capability?.approval_forced_manual
                                    || mine?.approval_forced_manual}
                          onChange={(e) => setDraft((c) =>
                            c.map((s, i) => (i === index ? { ...s, approval: e.target.value } : s)))}
                          aria-label={`Approval for ${step.id}`}>
                    <option value="auto">auto</option>
                    <option value="manual">manual</option>
                  </select>
                  {/* Keyboard equivalents for the drag. A reorder control reachable only by
                      pointer is a builder half the operators cannot use. */}
                  <span className="dw-draft-moves">
                    <button type="button" className="cap-btn" aria-label={`Move ${step.id} earlier`}
                            onClick={() => move(index, index - 1)} disabled={index === 0}>↑</button>
                    <button type="button" className="cap-btn" aria-label={`Move ${step.id} later`}
                            onClick={() => move(index, index + 1)}
                            disabled={index === draft.length - 1}>↓</button>
                    <button type="button" className="cap-btn" aria-label={`Remove ${step.id}`}
                            onClick={() => setDraft((c) => c.filter((_, i) => i !== index))}>×</button>
                  </span>
                  {/* WHERE EACH INPUT COMES FROM.
                      Every input was written as "ask the operator for it by name", so a
                      workflow built here could not feed one step into the next — the one thing
                      a workflow is for. It also could not pin a value, which is how a publish
                      step chooses between two connected channels. */}
                  {step.kind === "capability" && declaredInputs(step).length ? (
                    <button type="button" className="linklike dw-draft-wire-toggle"
                            data-testid={`dw-wire-${step.id}`}
                            onClick={() => setWiringStep(wiringStep === step.id ? null : step.id)}>
                      {wiringStep === step.id ? "hide inputs" : `inputs (${declaredInputs(step).length})`}
                    </button>
                  ) : null}
                  {wiringStep === step.id ? (
                    <div className="dw-wire" data-testid={`dw-wire-panel-${step.id}`}>
                      {declaredInputs(step).map((declared) => {
                        const wire = (step.inputs ?? {})[declared.name] as
                          Record<string, unknown> | string | undefined;
                        const mode = wire && typeof wire === "object"
                          ? ("from_step" in wire ? "step" : "run")
                          : wire === undefined ? "run" : "fixed";
                        const sources = upstream(index);
                        return (
                          <div key={declared.name} className="dw-wire-row">
                            <label className="dw-wire-name">
                              {declared.name}
                              {declared.required ? null : <span className="detail"> (optional)</span>}
                            </label>
                            <select className="input dw-wire-mode" value={mode}
                                    data-testid={`dw-wire-mode-${step.id}-${declared.name}`}
                                    onChange={(e) => {
                                      const next = e.target.value;
                                      if (next === "run") {
                                        setWire(step.id, declared.name, { from_input: declared.name });
                                      } else if (next === "fixed") {
                                        setWire(step.id, declared.name, "");
                                      } else {
                                        const first = sources[0];
                                        setWire(step.id, declared.name, {
                                          from_step: first?.id ?? "",
                                          field: first?.fields[0] ?? "",
                                        });
                                      }
                                    }}>
                              <option value="run">asked when the run starts</option>
                              <option value="step" disabled={sources.length === 0}>
                                {sources.length ? "from an earlier step" : "from an earlier step (none yet)"}
                              </option>
                              <option value="fixed">a fixed value</option>
                            </select>

                            {mode === "run" ? (
                              <input className="input dw-wire-value"
                                     value={String((wire as { from_input?: string })?.from_input
                                                   ?? declared.name)}
                                     aria-label={`Name asked for ${declared.name}`}
                                     onChange={(e) => setWire(step.id, declared.name,
                                                              { from_input: e.target.value })} />
                            ) : null}

                            {mode === "fixed" ? (
                              <input className="input dw-wire-value" value={String(wire ?? "")}
                                     placeholder="e.g. youtube_2"
                                     aria-label={`Fixed value for ${declared.name}`}
                                     data-testid={`dw-wire-fixed-${step.id}-${declared.name}`}
                                     onChange={(e) => setWire(step.id, declared.name, e.target.value)} />
                            ) : null}

                            {mode === "step" ? (
                              <span className="dw-wire-pair">
                                <select className="input"
                                        value={String((wire as { from_step?: string })?.from_step ?? "")}
                                        aria-label={`Step supplying ${declared.name}`}
                                        data-testid={`dw-wire-step-${step.id}-${declared.name}`}
                                        onChange={(e) => setWire(step.id, declared.name, {
                                          from_step: e.target.value,
                                          field: sources.find((u) => u.id === e.target.value)
                                                   ?.fields[0] ?? "",
                                        })}>
                                  {sources.map((u) => (
                                    <option key={u.id} value={u.id}>{u.label}</option>
                                  ))}
                                </select>
                                {/* FREE TEXT, NOT A CLOSED LIST. `produces` is what a step
                                    DECLARES; a skill can return more, and a picker that only
                                    offered the declared ones would make the rest unreachable. */}
                                <input className="input dw-wire-value"
                                       list={`dw-fields-${step.id}-${declared.name}`}
                                       value={String((wire as { field?: string })?.field ?? "")}
                                       placeholder="which field"
                                       aria-label={`Field supplying ${declared.name}`}
                                       onChange={(e) => setWire(step.id, declared.name, {
                                         from_step: (wire as { from_step?: string })?.from_step ?? "",
                                         field: e.target.value,
                                       })} />
                                <datalist id={`dw-fields-${step.id}-${declared.name}`}>
                                  {(sources.find(
                                    (u) => u.id === (wire as { from_step?: string })?.from_step,
                                  )?.fields ?? []).map((f) => <option key={f} value={f} />)}
                                </datalist>
                              </span>
                            ) : null}
                          </div>
                        );
                      })}
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ol>
        )}
      </section>
    </div>
  );
}


/**
 * Whether the agent named on a step is permitted to run it, said in the row.
 *
 * WHY IT IS ITS OWN COMPONENT. It owns a request — granting the skill — and putting that state
 * in the builder would mean one `busy` flag shared by every row.
 *
 * WHAT IT IS CHECKING. `skill_runtime` lets a step's captain call a skill on one of two
 * grounds: the skill's own `allowed_callers` names it, or the agent HOLDS the skill in
 * `role_skills`. The second is the one an operator controls, so it is the one offered here.
 * Silent when there is nothing to say — a row cluttered with green ticks teaches people to stop
 * reading it.
 */
export function CaptainCanRunIt({ projectId, step, mine, holdings, onGranted }: {
  projectId: string;
  step: Draft;
  mine?: CustomStep;
  holdings: Record<string, string[]>;
  onGranted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");

  const captain = String(step.captain ?? "").trim();
  const skillId = String(mine?.skill_id ?? "").trim();
  // Nothing to check: no agent named, or the step runs an MCP tool rather than a skill — a
  // connector's authority comes from the connection, not from a grant on the agent.
  if (!captain || !skillId) return null;
  if ((holdings[captain] ?? []).includes(skillId)) {
    return (
      <span className="dw-draft-holds" data-testid={`dw-holds-${step.id}`}>
        {captain} holds {skillId}
      </span>
    );
  }

  const grant = async () => {
    setBusy(true);
    setFailed("");
    try {
      const r = await apiWrite("/api/skills", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "assign", project_id: projectId, role_key: captain,
                               skill_id: skillId, actor: "executive_console" }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { setFailed(String(body?.error ?? `HTTP ${r.status}`)); return; }
      onGranted();
    } catch (e) {
      setFailed(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="dw-draft-grant" data-testid={`dw-grant-${step.id}`}>
      {captain} does not hold {skillId} — this step will be refused when it runs.
      <button type="button" className="linklike" disabled={busy}
              data-testid={`dw-grant-do-${step.id}`} onClick={() => void grant()}>
        {busy ? "giving…" : `Give ${skillId} to ${captain}`}
      </button>
      {failed ? <span className="dw-draft-forced"> {failed}</span> : null}
    </span>
  );
}


/** Whether a step's instruction is read from the run's input rather than pinned on the step. */
function isFromInput(value: unknown): boolean {
  return Boolean(value && typeof value === "object" && "from_input" in (value as object));
}
