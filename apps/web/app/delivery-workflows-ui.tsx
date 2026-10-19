"use client";

import { GatedButton, useGate } from "../components/gated";
import type { Permission } from "../lib/identity/permissions.gen";
import { routeForStep, useRunRoutes } from "./why-this-model";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ShareRunButton } from "./improve/share-run";
import { humanDuration } from "../lib/datetime";

import { ActionPreview, actionPreviewOf } from "./action-preview";
import { appIdentity, identityKey, type AppRun } from "./app-identity";
import { notifyChanged, onChanged } from "./changed";
import { Markdown } from "./markdown";
import { AgentsOnThis } from "./agents-on-this";
import { consoleHref, readComposeIntent } from "./url-intent";
import { SelfFixStrip } from "./self-fix/strip";
import { RunAutonomy } from "./self-fix/run-autonomy";
import { NEW_BUILD_TIP } from "../lib/compose-links";
import { StepIcon, hasStepIcon } from "./step-icons";
import { isLoopbackPreviewUrl, operatorPreviewOpenHref } from "../lib/code-tasks";
import { previewRecordFromStepOutput } from "../lib/previews";
import { openDockAbout, type DockSubject } from "../lib/dock-subject";
import { missingRequired, suppliedInputs, workflowInputFields } from "../lib/workflow-inputs";
import { HostAdviceBanner, useHostAdvice } from "./host-advice";
import { StartRefusal, asReadiness, type PendingRefusal } from "./workflow-readiness-ui";
import { RunReportPanel } from "./run-report-ui";
import { plannedDeploymentFrom } from "./deploy-ui";
import { WriteInFlightError, apiWrite, runPress } from "../lib/api-write";
import { withAnsweredGates } from "../lib/decided-gates";
import { useAnsweredGates } from "../lib/use-answered-gates";
import { stepAgentChip } from "./step-agents-ui";
// Agent World (S-runs): who did each step, the chain, the Act card and Credits.
import { approvalOf, laneAgentOf, stepActOf } from "./agents/chain-model";
import { useRunChain } from "./agents/run-acts";
import { RunChain } from "./agents/run-chain";
import { CreditsButton } from "./agents/run-credits";
import { askAboutAct } from "./agents/act-card";
import { StepActivity, StepDoneBy, StepOutputFacts, StepSection, stepFacts, stepLogHref, stepOutput,
         useStepActivity } from "./workflows/step-run";
import type { StepView } from "../lib/workflow-editor-types";
import { viewOf } from "./workflows/flow/node-facts";
import type { Act, StepSpend } from "../lib/agent-world-types";
import { browserHref } from "../lib/browser-links";
import { StackDeployStep, stackDeployOf, type StepStackDeploy } from "./workflow-stack-deploy-step";
import { MonitoringPackCard, packForStep, type MonitoringPack } from "./workflows/monitoring-pack";
import { DefineStepForm } from "./workflows/define-step-form";
import { CaptainCanRunIt } from "./workflows/captain-can-run-it";
import { Library, type Schedule } from "./workflows/library";
import { get, post } from "./workflows/delivery-api";
import { NewProjectComposer } from "./workflows/new-project-composer";
import { DossierCompact } from "./discoveries-ui";
import { MeetingCompact } from "./meeting-room-ui";
import { ArchitecturePanel } from "./architecture-ui";
import { readComposerPrefill } from "../lib/research-panels";
import { isTestRun, pinnedOutput, rerunNote } from "../lib/workflow-test-run";
// The Runs composer moved to app/workflows/; its helpers are re-exported so imports hold.
export {
  blockingReason, briefFrom, composerFields, criteriaFrom, linksFrom, repositoryHint,
  type NewProjectDraft,
} from "./workflows/new-project-composer";
import {
  FlowLane, WorkflowEditor, runLaneSpec, runPalette, useRunDefinition,
} from "./workflows/studio-parts";
import { useOnScreen } from "../lib/area-shown";
import { forEachProgress } from "../lib/workflow-for-each";

// Moved to app/workflows/ (Workflow Studio, phase 0); re-exported so existing imports hold.
export { CaptainCanRunIt, DefineStepForm };


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
export type CustomStep = {
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

export type Bindable = {
  /** From the ONE skill catalogue: authored, shipped and registered skills alike. `usable`
   *  false (turned off, or what it runs through is gone) comes with the reason. */
  skills: { skill_id: string; risk_tier: number; trusted: boolean; unclassified: boolean;
            label?: string; source?: "authored" | "builtin" | "registered";
            usable?: boolean; reason?: string | null }[];
  mcp_servers: {
    id: string; name: string; risk_tier: number; trusted: boolean; reachable: boolean;
    error?: string; status_note?: string;
    tools: { name: string; description: string; risk_tier: number }[];
  }[];
};

export type CatalogueEntry = {
  key: string;
  name: string;
  description: string;
  work_class: string;
  step_count: number;
  steps: { id: string; capability: string }[];
  gates: string[];
  /** Approvals answered without a person — the PRD approval, by the panel's own decision. */
  automatic_approvals?: string[];
  /** Deploy steps the environment's policy decides (approval.follows_policy): no one's gate
   *  until a project's policy is read (GET /v1/delivery-workflows/asks). */
  policy_decides?: string[];
  requires_project: string[];
  /** Everything it needs from a project, in the start preflight's words. */
  needs?: { key: string; label: string }[];
  /** What it spends beyond its minutes — PLRR is a second build pass. */
  cost_notes?: { step: string; note: string }[];
  estimated_minutes: number;
  input_schema?: unknown;
  /** The template's own example brief, for "Try an example". */
  example?: { title?: string; directive?: string } | null;
};

export type Installed = {
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
  /** The classes of work this workflow is the one used for. */
  used_for?: string[];
  /** WHICH steps, not just how many. "Steps 4" says how big a workflow is and nothing about
   *  what it does, so the only way to find out was to open the editor. */
  steps?: { id: string; label: string; kind: string; mine: boolean; approval: string;
            /** WHO RUNS IT. Empty means different things by kind — see the card. */
            captain?: string; agent_work?: boolean; capability?: string | null;
            /** A Deploy step's words and the environment whose policy ships it unasked
             *  (approval "policy"), from deploy_policy.step_answer. */
            says?: string; policy?: string }[];
  active_version?: number | null;
  /** What starting it will ask for. Every catalogue template declares one; absent only on a
   *  workflow somebody made without inputs. */
  input_schema?: unknown;
};

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
  /** What approving this step's gate will do — the email, tickets or video — resolved from
   *  its real inputs (action-preview.tsx). Present only while the step waits. */
  action_preview?: Record<string, unknown> | null;
  /** Whether this step's RECORDED blocker still holds, re-tested when the run was read. */
  blocker?: { cleared?: boolean; checkable?: boolean; reason?: string; missing?: string } | null;
  /** Set only on a deploy step that ships to a STACK target: that deployment (and where it
   *  goes). Its decision is the step's only approval — see workflow-stack-deploy-step.tsx. */
  stack_deploy?: StepStackDeploy | null;
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
  /** "test" for a test run (#512): pinned steps did not run; it is left out of the list unless
   *  "Show test runs" is on. */
  mode?: string | null;
  /** Set when this run was started from a step of another (`Run again from here`). */
  rerun_of?: { run_id: string; from_step: string } | null;
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
  /** Where the run stands, decided by the SERVER (`workflow_run_actions.standing`): `active`,
   *  `stopped` (at a failed step, waiting for the operator), `closed` or `compensating`. This
   *  file kept three lists of its own — LIVE, a closed list and ACTIVE_RUN_STATES — none held
   *  to the engine's; LIVE left out `paused` and `compensating`, so a paused run the server
   *  offers Stop on was filed under "Done" (review of 0fd6c45). */
  standing?: RunStanding;
};

export type RunStanding = "active" | "stopped" | "closed" | "compensating";

/** Is anything about this run still moving — work in flight, or an undo in progress? */
export function runIsLive(run: { standing?: RunStanding }): boolean {
  return run.standing === "active" || run.standing === "compensating";
}

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
  // KEYED ON WHAT THE ROW WAS RECORDED AS. The schema rewrite (db/schema.sql) turned every
  // legacy row into status `failed` with output.status `failed` and `recorded_as:
  // failed_continued`, so a check on output.status alone could no longer match anything and
  // "failed — went on" had become dead code (review of c8de36b).
  const out = (step.output ?? {}) as { status?: string; recorded_as?: string };
  return out.recorded_as === "failed_continued" || out.status === "failed_continued";
}

/** A step that succeeded by DOING NOTHING: a bare notify, video_edit or social_publish, which
 *  no tool performs, records an honest `status: recorded` no-op.
 *
 *  NOT A ✓. "Publish to a channel" showed a green tick when nothing was published, while the
 *  engine's own test said it "must not read as work done" — the test checked the engine and
 *  not what the operator sees (review of c8de36b). Its own state, saying what to do: bind a
 *  skill or a custom step that does the work. */
export function didNothing(step: RunStep): boolean {
  return step.status === "succeeded"
    && (step.output as { status?: string } | undefined)?.status === "recorded";
}

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
  // A fan-out branch an `any` join went on without: a visible partial failure, not a stop.
  if ((step.output as { tolerated_by_join?: unknown } | undefined)?.tolerated_by_join) return false;
  // A step that failed into its error path (#512): the run went on where the workflow said to.
  if (errorPathTaken(step)) return false;
  return !step.decision_id;
}

/** The first step this run stopped at for the operator, or null. */
export function stoppedStep(run: { status: string; standing?: RunStanding;
                                   steps?: RunStep[] | null;
                                   actions?: Run["actions"] }): RunStep | null {
  // A run somebody stopped, or one that finished, is history — nothing there waits on anybody.
  // The SERVER says which runs are closed; this used to be a hand copy of the engine's list.
  if (run.standing === "closed") return null;
  // AN APPROVED STEP THAT THEN FAILED STILL WAITS ON YOU. `stoppedForYou` reads a decision id
  // as "a person's no", but a deploy approved and then refused by its own check carries the
  // approval's id too — and the card said "Try it again" with no button. When the server
  // offers Retry or Skip on a stopped step, that step is the operator's, whatever it carries.
  return (run.steps ?? []).find((step) => stoppedForYou(step)
    || (STOPPED_STEP_STATUSES.includes(step.status)
        && (run.actions ?? []).some((a) => a.step_id === step.step_id
                                        && (a.key === "retry_step" || a.key === "skip_step"))))
    ?? null;
}

/** Does this run need a person right now — an open approval, or a step it stopped at? The ONE
 *  rule the bucket, the page's opening run and the run card all read, so they cannot disagree. */
export function runNeedsYou(run: { status: string; standing?: RunStanding;
                                   steps?: RunStep[] | null }): boolean {
  return (run.steps ?? []).some(needsYou) || stoppedStep(run) !== null;
}

/** Which heading a run is listed under. Exported so the rule is tested, not only rendered. */
export function bucketOf(run: { status: string; standing?: RunStanding;
                                steps?: RunStep[] | null }):
    "needs_you" | "running" | "blocked" | "done" {
  if (runNeedsYou(run)) return "needs_you";
  // "Stopped" holds runs that stopped without finishing: stopped at a step, held up
  // (`blocked`), or stopped by somebody (`cancelled`). Everything else that is still moving —
  // paused and compensating included — is "Running".
  if (run.standing === "stopped" || run.status === "blocked" || run.status === "cancelled") {
    return "blocked";
  }
  return runIsLive(run) ? "running" : "done";
}

/** Who skipped a step and why, when an OPERATOR skipped it — "" for a branch never taken.
 *  The two read the same on the rail otherwise, and they mean opposite things: one is a
 *  decision somebody made about this step, the other is a path the run did not go down. */
export function skippedBy(step: RunStep): string {
  const out = (step.output ?? {}) as {
    skipped_by_operator?: unknown; skipped_by?: unknown; skipped_by_name?: unknown; skip_reason?: unknown };
  if (step.status !== "skipped" || !out.skipped_by_operator) return "";
  // A name, never a raw member id ("skipped by usr_kdzh…"): the recorded name, a role key
  // read as words, or "an operator".
  const name = String(out.skipped_by_name ?? "").trim();
  const raw = String(out.skipped_by ?? "").trim();
  const who = name || (raw && !/^(usr|mem|svc|acct)_/i.test(raw) ? raw.replace(/_/g, " ") : "") || "an operator";
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
  const live = (r: Run) => (runIsLive(r) ? 0 : 1);
  if (live(a) !== live(b)) return live(a) - live(b);
  return (b.created_at ?? b.updated_at ?? "").localeCompare(a.created_at ?? a.updated_at ?? "");
}

/** The canvas step type now lives with the spec it is read from (lib/workflow-spec.ts); the
 *  name stays for the modules that import it from here. */
export type { DraftStep as Draft } from "../lib/workflow-spec";

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

/** The operator's presses on this panel: one at a time, the one running named by `busy`.
 *  ONLY presses. The automatic recheck below is not one — when it shared this scope, of several
 *  runs ready to continue only the first was put back, and a Retry pressed during it vanished. */
const PANEL_PRESSES = "delivery-workflows-panel";

/**
 * A run's own answer, taken from ANOTHER screen — the Command Center's Retry.
 *
 * Run actions leave the console from this module only (a-runs-buttons-come-from-its-actions
 * scans for any other sender). The console-a stream's Command Center posted `retry_step` with a
 * raw fetch on its own say-so; it now passes the answer the run offered (the engine's
 * stopped_for_operator) and it is sent here, keyed by the press like every other write.
 */
/** What a run's answer (retry, skip, recheck, stop, an input) needs: every one is workflow_run:control. */
export const RUN_ANSWER_PERMISSION: Permission = "workflow_run:control";

export async function sendRunAnswer(projectId: string, actor: string, runId: string,
                                    answer: RunAction, reason?: string) {
  // SKIP NEEDS A REASON: the engine keeps it on the step (skip-step 422s without one).
  const why = (reason ?? "").trim();
  return runPress({
    press: { scope: `run-answer:${runId}`, label: answer.label },
    fingerprint: JSON.stringify([answer.key, runId, answer.step_id ?? null, why]),
  }, (key) => post(answer.key, {
    project_id: projectId, actor, run_id: runId,
    ...(answer.step_id ? { step_id: answer.step_id } : {}),
    ...(why ? { reason: why } : {}),
  }, key));
}

/** The Workflows area's three tabs (operator, 2026-10-13): New project · Runs · Library. The
 *  composer is its own tab, never stacked over the runs ("where are my runs?"). The editor is not
 *  a tab: `&wf=` replaces all three, full width. */
export type AreaTab = "new" | "runs" | "library";

/** The tabs in order, for the tablist and its arrow keys. */
export const AREA_TABS: readonly AreaTab[] = ["new", "runs", "library"];

/** What the URL says this area is showing. Read on every popstate, so Back works.
 *
 *  A run named in the URL (`wfr=`) is a place in Runs: a link to a run opens Runs even when the
 *  URL still says `tab=new` from an earlier visit, because a link copies the query it was on. */
export function readAreaUrl(search: string): { tab: AreaTab; wf: string | null } {
  const params = new URLSearchParams(search);
  const raw = params.get("tab");
  return {
    tab: raw === "library" ? "library" : raw === "new" && !params.get("wfr") ? "new" : "runs",
    wf: params.get("wf")?.trim() || null,
  };
}

/** The query string with `tab=` corrected to the tab actually shown, or null when it already
 *  says so (or names none). */
export function urlWithTab(search: string, shown: AreaTab): string | null {
  const params = new URLSearchParams(search);
  const named = params.get("tab");
  if (named === null || named === shown) return null;
  params.set("tab", shown);
  return `?${params}`;
}

/** Whether the URL names a tab at all. With none, an area with no runs yet opens on New project:
 *  an empty list is a dead end, and the composer is what there is to do. */
export function namesATab(search: string): boolean {
  return new URLSearchParams(search).has("tab");
}

/** The arrow-key move in a tablist (WAI-ARIA APG, as Infra's AreaHead): wraps, Home and End. */
export function tabKeyTarget(key: string, index: number, count: number): number {
  if (key === "ArrowRight") return (index + 1) % count;
  if (key === "ArrowLeft") return (index - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return -1;
}

/** Change this area's URL and tell every panel listening on it. */
function setAreaUrl(patch: Record<string, string | null>, mode: "push" | "replace" = "push") {
  try {
    const url = new URL(window.location.href);
    for (const [key, value] of Object.entries(patch)) {
      if (value === null) url.searchParams.delete(key); else url.searchParams.set(key, value);
    }
    if (url.toString() === window.location.href) return;
    if (mode === "push") window.history.pushState(null, "", `${url.pathname}${url.search}`);
    else window.history.replaceState(null, "", `${url.pathname}${url.search}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

/** The editor's own parameters; cleared together when it closes. */
const EDITOR_PARAMS = { wf: null, step: null, view: null, v: null, from: null } as const;

export function DeliveryWorkflows({ projectId, actor }: { projectId: string; actor: string }) {
  const [tab, setTabState] = useState<AreaTab>("runs");
  /** Whether the URL named the tab; when it did not, an empty project opens on New project. */
  const [tabNamed, setTabNamed] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  useEffect(() => {
    const read = () => {
      const now = readAreaUrl(window.location.search);
      // THE URL SAYS WHERE YOU ARE (rc10.5 walk m4): a run link opened Runs over a stale
      // `tab=new`, and the URL went on saying New project, so a reload or a copied link lied.
      const fixed = urlWithTab(window.location.search, now.tab);
      if (fixed !== null) {
        try {
          window.history.replaceState(null, "", `${window.location.pathname}${fixed}${window.location.hash}`);
        } catch { /* non-browser */ }
      }
      setTabState(now.tab);
      setTabNamed(namesATab(window.location.search));
      setEditing(now.wf);
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  /** Switch tab, in the URL (pushed, so Back returns to the tab you were on). New project drops
   *  the run the URL named: `wfr=` is a place in Runs and would win the next read. */
  const setTab = useCallback((next: AreaTab, mode: "push" | "replace" = "push") => {
    setTabState(next);
    setTabNamed(true);
    setAreaUrl({ tab: next, ...EDITOR_PARAMS, ...(next === "new" ? { wfr: null } : {}) }, mode);
  }, []);
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  /** The tab an arrow key moved to, to focus once its panel has rendered. */
  const focusTab = useRef<AreaTab | null>(null);
  useEffect(() => {
    const want = focusTab.current;
    if (!want || want !== tab) return;
    focusTab.current = null;
    tabRefs.current[AREA_TABS.indexOf(want)]?.focus();
  });

  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [riskFloor, setRiskFloor] = useState(2);
  const [catalogue, setCatalogue] = useState<CatalogueEntry[]>([]);
  const [installed, setInstalled] = useState<Installed[]>([]);
  const [customSteps, setCustomSteps] = useState<CustomStep[]>([]);
  const [loadedRuns, setRuns] = useState<Run[]>([]);
  // A GATE JUST ANSWERED READS AS ANSWERED NOW (BOSSAPP-511): the overlay is applied to the runs
  // every view below reads, and the answer's "changed" note refetches the truth.
  const answeredVersion = useAnsweredGates();
  const runs = useMemo(() => withAnsweredGates(loadedRuns), [loadedRuns, answeredVersion]);
  // WHERE THIS PROJECT CAN SHIP. Fetched once for the release gate: choosing a target is part
  // of the deploy decision, and a gate that only says yes/no cannot express "yes, to staging".
  const [targets, setTargets] = useState<DeployTarget[]>([]);
  const [hosts, setHosts] = useState<DeployHost[]>([]);
  // EVERY AGENT THIS PROJECT HAS, so "assign" can offer them by name rather than by role_key.
  // One fetch for the whole screen: a picker per run would be one request per run.
  const [roles, setRoles] = useState<Array<{ role_key: string; display_name?: string | null }>>([]);
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
   * the target really was created, and the step went on rendering the same empty form, because
   * the only evidence that a target now exists lives in this array. */
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
    // — and `return` leaves the whole function. So when /api/deployment-targets answered 503,
    // the hosts fetch NEVER RAN, and the deploy step said "this project has no verified host"
    // while Infra showed one verified. One endpoint being down must not silence the other.
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
  // A PREVIEW IS ASKED FOR IN A SECOND AND ARRIVES IN A MINUTE. This holds the build's id from
  // the click until a preview URL actually shows up (or the wait is plainly too long), and the
  // poll below refreshes so it appears without another click.
  const [previewStarting, setPreviewStarting] = useState<{ runId: string; at: number } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  /** A start this panel made that was refused for what the project lacks. */
  const [refusal, setRefusal] = useState<PendingRefusal | null>(null);
  const [openRun, setOpenRun] = useState<string | null>(null);
  // ONCE, on the first load that has runs. `useRef` rather than a dependency on `openRun`,
  // because the list re-polls every few seconds: re-deciding each time would reopen a run the
  // operator had just closed, which is a page that argues with you.
  const openedOnce = useRef(false);
  /** The run Start just made: Runs opens on it, highlighted and scrolled into view. */
  const [justStarted, setJustStarted] = useState<string | null>(null);
  /** The workflow Start chose, so the composer opens on it rather than on a blank question. */
  const [startWith, setStartWith] = useState<string | undefined>(undefined);
  /** "Try example" — the standard's own example brief, filled in for the operator to read. */
  const [startExample, setStartExample] =
    useState<{ title?: string; directive?: string } | null>(null);
  /** What a compose link asked for: the mode, the app, and a running preview of it. */
  const [compose, setCompose] = useState<{ mode: "new" | "add" | null; app: string | null;
                                           preview: string | null; n: number } | null>(null);
  /** EVERY START OPENS THE FORM: the composer on that workflow, with its readiness. A run is
   *  never started blind on a sample directive (boss-1 map, 2026-10-02). */
  const openComposerOn = useCallback((key: string,
                                      example?: { title?: string; directive?: string } | null) => {
    setStartWith(key);
    setStartExample(example ?? null);
    setCompose(null);
    setTab("new");
  }, [setTab]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  // TEST RUNS ARE LEFT OUT OF THE LIST unless asked for (#512): a test is not production work.
  const [showTests, setShowTests] = useState(false);

  const load = useCallback(async () => {
    const [caps, cat, inst, live, mine, sched] = await Promise.all([
      get<{ capabilities: Capability[]; manual_approval_risk_floor: number }>(
        "capabilities", projectId),
      get<{ items: CatalogueEntry[] }>("catalogue", projectId),
      get<{ items: Installed[] }>("installed", projectId),
      get<{ runs: Run[] }>(showTests ? "runs_with_tests" : "runs", projectId),
      get<{ steps: CustomStep[] }>("custom_steps", projectId),
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
    setRiskFloor((caps as { manual_approval_risk_floor: number }).manual_approval_risk_floor ?? 2);
    setCatalogue((cat as { items: CatalogueEntry[] }).items ?? []);
    setInstalled((inst as { items: Installed[] }).items ?? []);
    setRuns((live as { runs: Run[] }).runs ?? []);
    setCustomSteps((mine as { steps?: CustomStep[] }).steps ?? []);
    setState("ready");
    setError(null);
  }, [projectId, showTests]);

  useEffect(() => { void load(); }, [load]);
  // A target set on a Factory card, or an action BOSS ran in the dock, changes what this screen
  // is showing. Reload rather than wait for the next poll.
  useEffect(() => onChanged(() => { void load(); }, "workflows"), [load]);

  // ARRIVING AT THIS SCREEN RE-TESTS WHAT STOPPED THINGS.
  //
  // A failed step's error is a record of a past attempt, and this page rendered it as a
  // statement about now. The read path reports, per step, whether the recorded blocker still
  // holds; when it does not, this puts the step back without being asked. Safe to be
  // automatic: `recheck` re-queues ONLY steps whose blocker has actually cleared.
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
      // A recheck that did not get through is tried again on the next poll.
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

  // KEEP ASKING, EVEN WHEN NOTHING IS RUNNING. A console cannot know that work has STARTED
  // without asking: a project kicked off from the Command Center, the Factory form, or by
  // approving a PRD creates its run somewhere else. Fast while something is in flight, slower
  // when idle — idle still means "watch for work appearing".
  const anyLive = runs.some(runIsLive);
  // OFF SCREEN, NOTHING (lib/area-shown.ts): this list polled every 8-15 s from a hidden area
  // for as long as the console was open. Back on screen it reads at once.
  const onScreen = useOnScreen();
  const wasOnScreen = useRef(onScreen);
  useEffect(() => {
    if (!onScreen) { wasOnScreen.current = false; return; }
    if (!wasOnScreen.current) { wasOnScreen.current = true; void load(); }
    const t = window.setInterval(() => { void load(); }, anyLive ? 8000 : 15000);
    return () => window.clearInterval(t);
  }, [anyLive, load, onScreen]);

  // A RUN THAT IS ON ITS WAY. Arriving from Build, the run may not be in the list yet. Poll
  // fast until it shows rather than presenting an empty list, which reads as "nothing happened".
  const awaited = openRun && !runs.some((r) => r.id === openRun) ? openRun : null;
  useEffect(() => {
    if (!awaited || !onScreen) return;
    const t = window.setInterval(() => { void load(); }, 2000);
    return () => window.clearInterval(t);
  }, [awaited, load, onScreen]);

  // SENT HERE TO START SOMETHING. `?new=1` and `?compose=new|add[&app=…]` (the Factory's and
  // the Command Center's "Add a feature") are one-shot commands, consumed on read so a later
  // navigation cannot re-deliver them.
  useEffect(() => {
    const read = () => {
      const intent = readComposeIntent(window.location.search);
      if (!intent.open) return;
      setTabState("new");
      setTabNamed(true);
      setStartWith(undefined);
      setStartExample(null);
      setCompose((c) => ({ mode: intent.mode, app: intent.app, preview: intent.preview,
                           n: (c?.n ?? 0) + 1 }));
      window.setTimeout(() => {
        document.querySelector<HTMLTextAreaElement>(
          intent.mode === "add" ? '[data-testid="np-gain"]' : '[data-testid="dw-new-directive"]')?.focus();
      }, 0);
      try {
        // Consumed, and the URL says where it landed: the New project tab, not an editor left
        // open, and not a run (`wfr=` is a place in Runs).
        const next = new URLSearchParams(intent.nextSearch);
        next.set("tab", "new");
        next.delete("wfr");
        for (const key of Object.keys(EDITOR_PARAMS)) next.delete(key);
        window.history.replaceState(null, "",
          `${window.location.pathname}?${next}${window.location.hash}`);
        setEditing(null);
      } catch { /* non-browser — the composer is on screen either way */ }
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  // PREFILLED FROM WHERE IT CAME. A dossier outcome's or a panel's "Build it" carries its brief
  // (lib/research-panels composerHref): the composer opens on the product path with it written
  // in, and the brief is consumed like `new=1`, so a later navigation cannot re-deliver it.
  useEffect(() => {
    const read = () => {
      const pre = readComposerPrefill(window.location.search);
      if (!pre) return;
      setStartWith("standard_product_delivery");
      setStartExample(pre);
      setTabState("new");
      setTabNamed(true);
      try {
        const next = new URLSearchParams(window.location.search);
        for (const key of ["compose", "brief", "title", "new", "wfr"]) next.delete(key);
        next.set("tab", "new");
        window.history.replaceState(null, "", `${window.location.pathname}?${next}${window.location.hash}`);
      } catch { /* non-browser — the composer is on screen either way */ }
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  // OPEN THE RUN THE OPERATOR WAS SENT HERE TO SEE (`?wfr=<id>`).
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
    if (!previewStarting || !onScreen) return;
    const timer = window.setInterval(() => {
      if (Date.now() - previewStarting.at > 180_000) { setPreviewStarting(null); return; }
      void load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [previewStarting, load, onScreen]);

  // ONE PRESS AT A TIME, AND A REFUSED PRESS IS SAID OUT LOUD. Every press on this panel goes
  // through `pressOn`, which holds the panel's press scope in lib/api-write's one registry
  // (registered before any await) and is the ONLY thing that sets `busy`. The same press again
  // joins the running one; a different press is refused in the operator's words.
  type PressResult<T> = T | { ok: false; error: string; refused: true };
  const scheduleGate = useGate("workflow_schedule:manage");
  const stepGate = useGate("workflow:create");
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

  // `dedupeFromPress`: a start that must be a NEW run per press, and the same run for its
  // duplicates, gets `dedupe_key: <prefix>:<press key>` — minted from the press INSIDE it, so
  // it is not part of what the press is recognised by (review of 6ca9119).
  const actRef = useRef<((label: string, action: string, payload: Record<string, unknown>,
                          dedupeFromPress?: string) => Promise<unknown>) | null>(null);
  const act = useCallback((label: string, action: string, payload: Record<string, unknown>,
                           dedupeFromPress?: string) =>
    pressOn(label, [action, payload], async (key) => {
      setNotice(null);
      setRefusal(null);
      const result = await post(action, {
        project_id: projectId, actor, ...payload,
        ...(dedupeFromPress ? { dedupe_key: `${dedupeFromPress}:${key}` } : {}),
      }, key);
      // A START REFUSED FOR WHAT THE PROJECT LACKS gets the checklist and "<label> anyway",
      // whichever button started it. The retry is the same press with exactly the keys ticked.
      const readiness = !result.ok ? asReadiness(result.readiness) : null;
      if (readiness) {
        setRefusal({ label, message: result.error ?? "", readiness,
                     retry: (keys) => actRef.current?.(label, action,
                                                       { ...payload, start_anyway: keys },
                                                       dedupeFromPress) });
        return result;
      }
      // The engine's own sentence, not "something went wrong". A START IS NOT A FINISH: where
      // the engine says what happens next (start_preview), say that instead of "done".
      const next = result.ok && action === "start_preview"
        ? (result.data as { message?: unknown } | undefined)?.message : undefined;
      setNotice(result.ok ? (typeof next === "string" && next ? `${label}: ${next}` : `${label} — done.`)
                          : `${label} — ${result.error}`);
      // The Factory card, the deployment panels and the Library render what this just changed.
      if (result.ok) notifyChanged("workflows", { action });
      // RELOAD WHILE STILL BUSY, so the button does not snap back before the screen updates.
      if (result.ok) await load();
      return result;
    }), [pressOn, projectId, actor, load]);
  actRef.current = act;

  // OPEN THE ONE THAT WANTS YOU. A page whose every run is collapsed makes an operator click
  // to find out whether anything needs them, which is the question the page exists to answer.
  useEffect(() => {
    if (openedOnce.current || !runs.length) return;
    openedOnce.current = true;
    setOpenRun((current) => current ?? runToOpen(runs));
  }, [runs]);

  const [scheduleFor, setScheduleFor] = useState<string | null>(null);
  const openWorkflow = useCallback((key: string, extra: Record<string, string | null> = {}) => {
    setEditing(key);
    setAreaUrl({ ...EDITOR_PARAMS, ...extra, wf: key });
  }, []);

  // THE EDITOR REPLACES THE TABS, full width. It loads its own data, so it does not wait on
  // this area's run list.
  if (editing) {
    return (
      <div className="dw" data-testid="delivery-workflows">
        <WorkflowEditor projectId={projectId} workflowKey={editing} actor={actor}
                        usedFor={installed.find((w) => w.workflow_key === editing)?.used_for}
                        onBack={() => { setEditing(null); setTab("library"); }}
                        onStart={(key) => { setEditing(null); openComposerOn(key); }}
                        // The schedule popover lives on the workflow's Library card.
                        onSchedule={(key) => { setEditing(null); setScheduleFor(key); setTab("library"); }} />
      </div>
    );
  }

  if (state === "loading") {
    return <p className="detail" data-testid="dw-loading">Loading workflows…</p>;
  }
  if (state === "failed") {
    return (
      <p className="detail" style={{ color: "var(--red)" }} data-testid="dw-error">
        Could not load workflows — {error}
      </p>
    );
  }

  // AN EMPTY PROJECT OPENS ON NEW PROJECT, unless the URL asked for a tab: a list with nothing
  // in it is a dead end, and starting something is all there is to do.
  const shownTab: AreaTab = !tabNamed && tab === "runs" && runs.length === 0 ? "new" : tab;
  const tabs = (
    <div className="wf-tabs" role="tablist" aria-label="Workflows">
      {AREA_TABS.map((key, i) => (
        // ONE TAB STOP: the selected tab is the stop, the arrows move along (and select), Home
        // and End go to the ends — Infra's AreaHead, the console's tablist.
        <button key={key} type="button" role="tab" aria-selected={shownTab === key} className="wf-tab"
                id={`dw-tab-${key}`} aria-controls={`dw-panel-${key}`}
                tabIndex={shownTab === key ? 0 : -1} ref={(el) => { tabRefs.current[i] = el; }}
                data-testid={`dw-tab-${key}`}
                title={key === "new" ? NEW_BUILD_TIP : undefined}
                onKeyDown={(e) => {
                  const to = tabKeyTarget(e.key, i, AREA_TABS.length);
                  if (to < 0) return;
                  e.preventDefault();
                  tabRefs.current[to]?.focus();
                  // Each panel draws the tablist in its own header, so the tab focused above is
                  // replaced by the next render; focus follows to the new one (effect below).
                  focusTab.current = AREA_TABS[to];
                  setTab(AREA_TABS[to]);
                }}
                onClick={() => setTab(key)}>
          {key === "new" ? <>New project</> : key === "runs" ? <>Runs <em>({runs.length})</em></> : "Library"}
        </button>
      ))}
    </div>
  );
  /** Leave the composer: its choices cleared, back to the runs. */
  const closeComposer = () => {
    setStartWith(undefined); setStartExample(null); setCompose(null);
  };
  const installedStandards = new Set(installed.filter((i) => i.is_catalogue).map((i) => i.workflow_key));

  return (
    <div className="dw" data-testid="delivery-workflows">
      {/* WHAT IS HAPPENING RIGHT NOW. One indicator every action gets for free, for as long as
          the action is running. */}
      {busy ? (
        <p className="dw-notice dw-working" data-testid="dw-working" role="status" aria-live="polite">
          <span className="dw-spin" aria-hidden />
          {busy}…
        </p>
      ) : notice ? (
        <p className="dw-notice" data-testid="dw-notice">{notice}</p>
      ) : null}
      {refusal ? (
        <StartRefusal refusal={refusal} projectId={projectId} busy={!!busy}
                      onDismiss={() => setRefusal(null)} />
      ) : null}

      {shownTab === "library" ? (
        <div role="tabpanel" id="dw-panel-library" aria-labelledby="dw-tab-library">
        <Library
          projectId={projectId} tabs={tabs} busy={busy} schedules={schedules}
          inputSchemas={Object.fromEntries([
            ...catalogue.map((c) => [c.key, c.input_schema] as const),
            ...installed.map((i) => [i.workflow_key, i.input_schema] as const),
          ])}
          examples={Object.fromEntries(catalogue.map((c) => [c.key, c.example ?? null]))}
          workClasses={[...new Set(catalogue.map((c) => c.work_class).filter(Boolean))].sort()}
          uninstalledStandards={catalogue.filter((c) => !installedStandards.has(c.key)).length}
          onInstallStandards={() => void act("Install the standards", "install", {})}
          customSteps={customSteps} riskFloor={riskFloor}
          onStart={(key, example) => openComposerOn(key, example)}
          onOpen={(key) => openWorkflow(key)}
          onNew={() => openWorkflow("new")}
          onDuplicate={(key) => openWorkflow("new", { from: key })}
          scheduleFor={scheduleFor} onScheduleShown={() => setScheduleFor(null)}
          onUseFor={(key, workClass) =>
            void act(`Use for ${workClass}`, "apply", { workflow_key: key, work_class: workClass })}
          onSaveSchedule={async (draft) => {
            const chosen = installed.find((w) => w.workflow_key === draft.workflowKey);
            const fields = workflowInputFields(chosen?.input_schema);
            // REQUIRED INPUTS ARE STORED WITH THE SCHEDULE: it runs them unattended.
            if (missingRequired(fields, draft.values).length || !draft.timezone.trim()) return false;
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
                // THE UNATTENDED START ANYWAY, stored with the schedule.
                start_anyway: draft.startAnyway,
                ...(draft.scheduleId ? { schedule_id: draft.scheduleId } : {}),
              });
            return Boolean((result as { ok?: boolean }).ok);
          }}
          onToggleSchedule={(id, enabled) => scheduleGate.allowed && void pressOn(
            enabled ? "Resume schedule" : "Pause schedule", id, async (key) => {
              const r = await apiWrite("/api/delivery-workflows", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "schedule_enabled", schedule_id: id,
                                       enabled, project_id: projectId, actor }),
              }, { key });
              const body = await r.json();
              if (!r.ok) { setNotice(String(body?.error ?? `API returned ${r.status}`)); return; }
              await load();
            })}
          onDeleteSchedule={(id) => scheduleGate.allowed && void pressOn("Remove schedule", id, async (key) => {
            const r = await apiWrite(
              `/api/delivery-workflows?view=schedules&schedule_id=${encodeURIComponent(id)}`
                + `&project_id=${encodeURIComponent(projectId)}`
                + `&actor=${encodeURIComponent(actor)}`,
              { method: "DELETE" }, { key });
            const body = await r.json();
            if (!r.ok) { setNotice(String(body?.error ?? `API returned ${r.status}`)); return; }
            await load();
          })}
          onRemoveStep={(stepKey) => stepGate.allowed && void pressOn(`Remove ${stepKey}`, stepKey, async (key) => {
            const r = await apiWrite(
              `/api/delivery-workflows?project_id=${encodeURIComponent(projectId)}`
                + `&step_key=${encodeURIComponent(stepKey)}`
                + `&actor=${encodeURIComponent(actor)}`,
              { method: "DELETE" }, { key },
            );
            const body = await r.json();
            if (!r.ok) { setNotice(String(body?.error ?? `API returned ${r.status}`)); return; }
            setNotice(`Removed ${stepKey}`);
            await load();
          })}
          onStepsChanged={() => void load()}
        />
        </div>
      ) : shownTab === "new" ? (
        <div className="wf-runs-area" role="tabpanel" id="dw-panel-new" aria-labelledby="dw-tab-new"
             data-testid="dw-panel-new">
          <header className="wf-lib-head">
            {tabs}
          </header>
          <div className="dw-composer-open">
            <NewProjectComposer
              projectId={projectId} actor={actor} workflows={installed} catalogue={catalogue}
              startWith={startWith} example={startExample}
              mode={compose?.mode ?? (startWith ? "new" : null)} appKey={compose?.app}
              previewId={compose?.preview}
              // KEYED ON THE CHOICE, so Start on a different workflow (or a link to another
              // app) rebuilds the composer rather than leaving it on the first one's inputs.
              key={`${startWith ?? "blank"}:${startExample ? "example" : "own"}:${compose?.n ?? 0}`}
              // Cancel goes back to the runs; with no runs yet there is nothing to go back to.
              onCancel={runs.length ? () => { closeComposer(); setTab("runs"); } : undefined}
              // STARTED: to Runs, on the new run — opened, highlighted and scrolled into view.
              onStarted={(runId) => {
                closeComposer();
                if (runId) { setOpenRun(runId); setJustStarted(runId); }
                setTab("runs");
                if (runId) setAreaUrl({ wfr: runId }, "replace");
                setNotice(runId ? `Started ${runId}.` : "Started.");
                void load();
              }}
            />
          </div>
        </div>
      ) : (
        <div className="wf-runs-area" role="tabpanel" id="dw-panel-runs" aria-labelledby="dw-tab-runs"
             data-testid="dw-panel-runs">
          <header className="wf-lib-head">
            {tabs}
          </header>
          {awaited ? (
            <p className="dw-awaiting detail" data-testid="dw-awaiting">
              Starting <code>{awaited}</code>…
            </p>
          ) : null}
          <RunsView
            projectId={projectId}
            runs={runs} openRun={openRun} setOpenRun={setOpenRun} busy={busy}
            highlightRun={justStarted} onNewProject={() => setTab("new")}
            capabilities={capabilities} targets={targets} hosts={hosts}
            actor={actor} roles={roles}
            onOpenWorkflow={(key) => openWorkflow(key)}
            // NOT THE WORKER'S PUMP. What an operator means by "Advance" is "is anything stuck
            // that could move now?", which is a re-check of each unfinished run: the console key
            // may do that, and it says what it put back.
            onAdvance={() => void pressOn("Advance", null, async (key) => {
              const open = runsToRecheckInBulk(runs);
              let requeued = 0;
              const failed: string[] = [];
              for (const run of open) {
                // One press, one key: the server derives each request's key from the press AND
                // its body, so every run is re-checked once however often Advance is pressed.
                const r = await post("recheck", { project_id: projectId, actor, run_id: run.id },
                                     key);
                if (!r.ok) failed.push(r.error ?? "refused");
                else requeued += ((r.data as { requeued?: unknown[] })?.requeued ?? []).length;
              }
              const checked = `Checked ${open.length} live run${open.length === 1 ? "" : "s"}. `;
              setNotice(failed.length ? `${checked}Check again — ${failed[0]}`
                : !open.length ? "No run is live, so there is nothing to check. To revive a "
                  + "failed run, open it and use Re-check on that run."
                : requeued ? `${checked}Put ${requeued} step${requeued === 1 ? "" : "s"} `
                  + "back in the queue; they run on the next tick."
                : checked
                  + "Nothing could move yet — each run is waiting on a person or on work in flight.");
              await load();
            })}
            onRetryStep={(runId, stepId, note) =>
              act("Retry", "retry_step", { run_id: runId, step_id: stepId, ...(note ? { note } : {}) })}
            // RUN AGAIN FROM HERE (#512): a NEW run from this step; every earlier step keeps what
            // this run produced, as pins that say where they came from. One run per press.
            onRerunFrom={(run, stepId) => act("Run again from here", "rerun_from",
              { run_id: run.id, from_step: stepId }, `rerun:${run.id}:${stepId}`)}
            showTests={showTests} onShowTests={setShowTests}
            // SKIP AND PROCEED. Going on without a failed step is a decision, so the step asks
            // why and the reason is kept on the step and in the run's history.
            onSkipStep={(runId, stepId, reason) =>
              act("Skip", "skip_step", { run_id: runId, step_id: stepId, reason })}
            onSetTarget={(runId, targetId) =>
              act("Set target", "set_input", { run_id: runId, values: { target_id: targetId } })}
            onPrepareTarget={(runId, hostId) =>
              act("Prepare target", "prepare_target",
                  { run_id: runId, host_id: hostId, environment: "staging" })}
            onRunPreview={(buildRunId) => {
              setPreviewStarting({ runId: buildRunId, at: Date.now() });
              return act("Starting a preview", "start_preview", { run_id: buildRunId });
            }}
            previewStarting={previewStarting?.runId ?? ""}
            // Both: the run's input changes AND the target list gains a row.
            onBound={() => { void load(); void reloadTargets(); }}
            onRecheck={(run) => act("Re-check", "recheck", { run_id: run.id })}
            onStop={(run) => act("Stop", "stop", {
              run_id: run.id, reason: "stopped by the operator from the console",
            })}
            onDelete={async (run) => {
              // ASKED, BECAUSE IT CANNOT BE UNDONE.
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
              // THE CHOICE MADE FOR THIS RUN CARRIES OVER (`rerun_of`).
              rerun_of: run.id,
              // One fresh run per press; a retried or doubled request returns the same run.
            }, `again:${run.id}`)}
            // STOP, THEN START — as ONE request, checked before anything is stopped
            // (`replaces_run`): a refusal leaves the original running and draws the checklist.
            onStopAndRerun={async (run) => {
              await act("Stop and run again", "start", {
                workflow_key: run.workflow_key, input: inputForRerun(run.input),
                replaces_run: run.id,
                // A RUN IS REPLACED ONCE, however often this is pressed.
                dedupe_key: `replace:${run.id}`,
              });
            }}
            // BUILD ON WHAT THIS ALREADY FOUND. The server resolves the dossier from the run; the
            // payload names the workflow (not only the class) because `project.new` is shared.
            onBuildOnThis={(run) => act("Build on this research", "start", {
              workflow_key: "standard_product_delivery",
              work_class: "project.new", from_run: run.id,
              input: inputForRerun(run.input),
            })}
          />
        </div>
      )}
    </div>
  );
}

/* ── Running ─────────────────────────────────────────────────────────────────── */

/** How long a step took, or has been going. The single most asked question of a pipeline. */
function stepDuration(step: { started_at?: string | null; completed_at?: string | null;
                              decision_approved_at?: string | null }): string {
  if (!step.started_at) return "";
  // WORKING TIME, NOT WAITING TIME. A gated step starts when it is created and then waits for a
  // person; timing it from there showed "14h 16m" for a deploy that ran in under a minute. The
  // clock starts when its gate was answered, if that came later.
  const approved = step.decision_approved_at ? Date.parse(step.decision_approved_at) : NaN;
  const start = Math.max(Date.parse(step.started_at), Number.isFinite(approved) ? approved : -Infinity);
  const end = step.completed_at ? Date.parse(step.completed_at) : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
  const text = humanDuration((end - start) / 1000);
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
    // ITS LOG IS THE STEP'S, not the build's: the panel's Activity section links straight to
    // this step's own rows and the work it started (`?area=activity&wfr=…&step=…`).
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
               href: browserHref(href) });
  }
  return out;
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

/** The run statuses a bulk "Check every run again" touches: work that is still going. */
export const LIVE_RUN_STATUSES = ["queued", "validating", "running", "waiting", "paused",
                                  "blocked"] as const;

/**
 * The runs "Check every run again" re-checks. ONLY LIVE ONES.
 *
 * It used to re-check every run that was not completed, cancelled or compensated, which
 * included failed and dead-lettered runs from long ago. recheck_run puts a run back to running
 * when its blocker has cleared, so one click could revive old failed work across the project,
 * one POST per run. Reviving a failed run is a decision about THAT run; its own Re-check does it.
 */
export function runsToRecheckInBulk<T extends { status: string }>(runs: T[]): T[] {
  return runs.filter((run) => (LIVE_RUN_STATUSES as readonly string[]).includes(run.status));
}

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
                   previewStarting, onOpenWorkflow,
                   targets, hosts, highlightRun, onNewProject,
                   onRerunFrom, showTests, onShowTests }: {
  projectId: string;
  /** Start a NEW run from one step of a finished run, earlier steps pinned to this run's. */
  onRerunFrom?: (run: Run, stepId: string) => void;
  /** Whether test runs are listed; absent = the toggle is not offered. */
  showTests?: boolean;
  onShowTests?: (on: boolean) => void;
  /** The run Start just made: highlighted, and scrolled into view once it is in the list. */
  highlightRun?: string | null;
  /** The empty list's way out: the New project tab. */
  onNewProject?: () => void;
  actor: string;
  /** Open the workflow a run follows, in the editor. */
  onOpenWorkflow?: (workflowKey: string) => void;
  /** Every agent this project has, for the "Agents on this" picker. */
  roles: Array<{ role_key: string; display_name?: string | null }>;
  runs: Run[];
  openRun: string | null;
  setOpenRun: (id: string | null) => void;
  onAdvance: () => void;
  busy: string | null;
  capabilities: Capability[];
  onRetryStep: (runId: string, stepId: string, note?: string) => void;
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
  // A NEW RUN IS SHOWN WHEREVER THE FILTER WAS: a filter that hid it would read as "Start did
  // nothing". Scrolled into view once, when it first appears in the list (it may arrive a poll
  // after Start answers).
  const listRef = useRef<HTMLDivElement | null>(null);
  const scrolledTo = useRef<string | null>(null);
  const highlightShown = !!highlightRun && runs.some((r) => r.id === highlightRun);
  useEffect(() => {
    if (highlightRun) setBucket("all");
  }, [highlightRun]);
  useEffect(() => {
    if (!highlightRun || !highlightShown || scrolledTo.current === highlightRun) return;
    const el = [...(listRef.current?.querySelectorAll<HTMLElement>("[data-run-id]") ?? [])]
      .find((x) => x.dataset.runId === highlightRun);
    if (!el) return;
    scrolledTo.current = highlightRun;
    try { el.scrollIntoView({ block: "nearest", behavior: "smooth" }); } catch { /* old browser */ }
  }, [highlightRun, highlightShown, bucket]);
  if (runs.length === 0) {
    return (
      <div className="dw-empty" data-testid="dw-no-runs">
        <p title="Start one in New project, or press Start on a workflow in the Library">
          Nothing running yet
        </p>
        {onNewProject ? (
          <button type="button" className="wf-btn wf-btn-primary" data-testid="dw-empty-new"
                  onClick={onNewProject}>
            Start a project
          </button>
        ) : null}
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
    <div className="dw-runs" ref={listRef}>
      {/* WHY RUNS ARE SLOW, ONCE. The host's advice (a nearly full disk) is about the machine,
          not about one run, and it was repeated on every live card. One banner above the list,
          while anything is running. Renders nothing on a healthy host. */}
      {runs.some((r) => r.status === "running" || r.status === "waiting")
        ? <HostAdviceBanner advice={hostAdvice} compact /> : null}
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
        <GatedButton permission="workflow_run:control" type="button" className="cap-btn" onClick={onAdvance} disabled={!!busy}
                data-testid="dw-advance"
                title="Re-test what stopped each live run and put back what can now move. A failed run is re-checked from its own page.">
          {busy === "Advance" ? "Checking…" : "Check every run again"}
        </GatedButton>
        {onShowTests ? (
          <label className="detail dw-show-tests" data-testid="dw-show-tests">
            <input type="checkbox" checked={!!showTests}
                   onChange={(e) => onShowTests(e.target.checked)} /> Show test runs
          </label>
        ) : null}
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
          <article key={run.id} className="dw-run" data-status={run.status} data-run-id={run.id}
                   data-open={open ? "true" : undefined} data-testid="dw-run"
                   data-just-started={run.id === highlightRun ? "true" : undefined}>
            {/* WHY THIS RUN IS SLOW, NEXT TO THE NUMBER THAT MISLEADS. The elapsed time is
                what gets read as "BOSS is regressing"; if the machine took that time, it has
                to say so in the same place. Renders nothing on a healthy host. */}
            <button type="button" className="dw-run-head" aria-expanded={open}
                    onClick={() => setOpenRun(open ? null : run.id)}>
              <span className="dw-badge" data-tone={tone(run.status)}>{run.status}</span>
              {isTestRun(run) ? (
                <span className="dw-badge" data-tone="test" data-testid="dw-run-test"
                      title="A test run: pinned steps returned their pinned output and did not run">
                  Test run
                </span>
              ) : null}
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
                  waiting on {/^[a-z0-9_]+$/.test(run.waiting_on)
                    ? roleLabel(run.waiting_on) : run.waiting_on}
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
                                   onRetry={(note) => onRetryStep(run.id, stopped.step_id, note)}
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
            {open ? (
              <StepPath run={run} onOpenWorkflow={onOpenWorkflow} actor={actor} roles={roles} capabilities={capabilities} targets={targets} hosts={hosts}
                        onRetryStep={(stepId, note) => onRetryStep(run.id, stepId, note)}
                        onSkipStep={(stepId, reason) => onSkipStep(run.id, stepId, reason)}
                        onRerun={() => onRerun(run)} onRecheck={() => onRecheck(run)}
                        onStop={() => onStop(run)}
                        onDelete={() => onDelete(run)}
                        onStopAndRerun={() => onStopAndRerun(run)}
                        onBuildOnThis={() => onBuildOnThis(run)}
                        onRerunFrom={onRerunFrom ? (stepId) => onRerunFrom(run, stepId) : undefined}
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

export function phaseOf(step: RunStep):
    "done" | "now" | "ahead" | "skipped" | "failed" | "findings" | "failed_on" | "nothing" {
  // FAILED, AND THE WORK WENT ON. Research that died is not "not run" — that hides something
  // worth fixing — and it is not a stopped run either, because the project carried on without
  // it. Its own state, so neither reading is offered.
  if (failedButContinued(step)) return "failed_on";
  if (notApplicable(step) || step.status === "skipped") return "skipped";
  if (step.status === "failed" || step.status === "deadlettered" || step.status === "cancelled") {
    return "failed";
  }
  if (didNothing(step)) return "nothing";
  if (step.status === "succeeded") return hasFindings(step) ? "findings" : "done";
  if (["running", "leased", "ready", "waiting", "queued"].includes(step.status)) return "now";
  return "ahead";
}

/** What a step that did nothing says, wherever it is shown. */
export const DID_NOTHING =
  "nothing was done — no skill or custom step does this work; bind one to this step";

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
  const live = runs.find(runIsLive);
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

/** A step's place on the run lane: the phase its left edge shows and the words under it.
 *
 *  The states that are NOT plain progress keep their own words, because "this one failed" or
 *  "this did nothing" must never be something you infer from a colour: a step that did nothing
 *  is never drawn as done, and an operator's skip says who and why. */
/** The step the engine sent the run to after this one failed for good (#512), or null. The engine
 *  records it as `output.error_path_taken` (and a `workflow.error_path_taken` event). */
export function errorPathTaken(step: RunStep): string | null {
  const to = (step.output as { error_path_taken?: unknown } | undefined)?.error_path_taken;
  return typeof to === "string" && to ? to : null;
}

export function laneStateOf(step: RunStep): { phase: string; meta: string; tip?: string; errorPath?: boolean } {
  if (needsYou(step)) return { phase: "needs_you", meta: "needs you" };
  switch (phaseOf(step)) {
    case "nothing": return { phase: "skipped", meta: "nothing done", tip: DID_NOTHING };
    case "skipped": {
      const by = skippedBy(step);
      if (by) return { phase: "skipped", meta: by, tip: by };
      return { phase: "skipped", meta: "skipped", tip: skipReason(step) || "this step did not apply to this run" };
    }
    case "failed": if (errorPathTaken(step)) {
      return { phase: "failed", meta: timedOut(step) ? "timed out — took its error path" : "failed — took its error path",
               tip: "It failed, and the run went on at its error path", errorPath: true };
    }
    return timedOut(step)
      ? { phase: "failed", meta: "timed out", tip: "A try ran longer than the step's time limit" }
      : { phase: "failed", meta: railMeta(step) || "failed" };
    case "failed_on": return { phase: "failed", meta: "failed — went on",
                               tip: "It failed, and the run went on without it" };
    case "findings": return { phase: "done", meta: railMeta(step) || "findings", tip: "Finished with findings" };
    case "done": return { phase: "done", meta: railMeta(step) };
    // A For each going through its list says how far it is: "3 of 10 done".
    case "now": return { phase: "moving", meta: forEachProgress(step) ?? railMeta(step) };
    default: return { phase: "future", meta: railMeta(step) || "pending" };
  }
}

/** A step whose last try ran past its time limit (#512): the engine fails it with `step_timeout`,
 *  as its error or as the reason it records. */
export function timedOut(step: RunStep): boolean {
  const out = (step.output ?? {}) as { reason?: unknown; error?: unknown };
  return [step.error, out.reason, out.error].some((v) => typeof v === "string" && /\bstep_timeout\b/.test(v));
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
                   onDelete, onRerunFrom,
                   onStopAndRerun, onBuildOnThis, onRecheck,
                   onSetTarget,
                   onPrepareTarget, onRunPreview, onBound, projectId,
                   previewStarting, busy, actor, roles, onOpenWorkflow }: {
  projectId: string;
  actor: string;
  roles: Array<{ role_key: string; display_name?: string | null }>;
  run: Run; capabilities: Capability[]; targets: DeployTarget[]; hosts: DeployHost[];
  /** Open the workflow this run follows in the editor. */
  onOpenWorkflow?: (workflowKey: string) => void;
  onRetryStep: (stepId: string, note?: string) => void; onRerun: () => void; onRecheck: () => void;
  onSkipStep: (stepId: string, reason: string) => void;
  /** Run again from one step (#512): offered on a finished run, for the step picked. */
  onRerunFrom?: (stepId: string) => void;
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
  // THE STEP IS IN THE URL (`step=`), so Back from a step's full dossier or meeting room lands
  // on the step it was opened from, not on whichever step the run would open on by itself.
  // Read after mount, never during render: the server has no URL to read, and a first paint
  // that differs from it is a hydration error.
  const [picked, setPickedState] = useState<string | null>(initial?.step_id ?? null);
  const setPicked = useCallback((id: string | null) => {
    setPickedState(id);
    try {
      const url = new URL(window.location.href);
      if (id) url.searchParams.set("step", id); else url.searchParams.delete("step");
      window.history.replaceState(window.history.state, "", url.toString());
    } catch { /* non-browser */ }
  }, []);
  useEffect(() => {
    const onPop = () => {
      const q = new URLSearchParams(window.location.search);
      const asked = q.get("step");
      // Only THIS run's step: `?wfr=<another run>&step=build` names that run's build, not ours.
      const run_ = q.get("wfr");
      if (run_ && run_ !== run.id) return;
      if (asked && steps.some((s) => s.step_id === asked)) setPickedState(asked);
    };
    onPop();
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [steps, run.id]);
  const current = steps.find((s) => s.step_id === picked) ?? initial;
  // A LINK TO ONE STEP LANDS ON THAT STEP (`?wfr=<this run>&step=<id>` — the Log's Step ↗, a
  // step panel's link). It opened on the run's own choice above, so every such link showed
  // the first step that wanted attention instead. Read after mount (the URL is the client's).
  const stepIds = steps.map((s) => s.step_id).join("|");
  useEffect(() => {
    const read = () => {
      const q = new URLSearchParams(window.location.search);
      const wanted = q.get("step");
      if (q.get("wfr") === run.id && wanted && stepIds.split("|").includes(wanted)) setPicked(wanted);
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, [run.id, stepIds]);

  // WHERE THE CALLOUT POINTS. Measured from the DOM rather than computed from an index: the
  // lane fits itself to the width, so a computed position is right until it is not.
  const laneRef = useRef<HTMLDivElement | null>(null);
  const detailRef = useRef<HTMLDivElement | null>(null);
  // Drawn from the definition version this run ran, as the designer draws it (titles, custom
  // steps, agents, branches); from the run's own step list only until that has loaded.
  const definition = useRunDefinition(projectId, run.workflow_key, run.version);
  const laneSpec = useMemo(() => runLaneSpec(steps, definition.spec), [steps, definition.spec]);
  const lanePalette = useMemo(() => definition.palette ?? runPalette(capabilities, roles),
    [definition.palette, capabilities, roles]);
  // WHO DID EACH STEP, from the run's signed work: one read, shared by the lane's faces, the
  // chain, the drawer and Credits, so they cannot disagree. Polled only while the run is live.
  const { chain } = useRunChain(projectId, run.id, !terminal);
  const laneState = useMemo(() => Object.fromEntries(steps.map((s) => {
    const agent = laneAgentOf(s.output, stepActOf(chain, s.step_id), actor);
    return [s.step_id, agent ? { ...laneStateOf(s), agent } : laneStateOf(s)];
  })), [steps, chain, actor]);

  // WHO WORKS THIS RUN is a property of the app, not a stage of its delivery — so it is a chip
  // on the run's bar, not a node in the lane the runtime never runs.
  const [showAgents, setShowAgents] = useState(false);

  useEffect(() => {
    const place = () => {
      const lane = laneRef.current;
      const detail = detailRef.current;
      if (!lane || !detail || !picked) return;
      const node = lane.querySelector<HTMLElement>(`[data-step-id="${CSS.escape(picked)}"]`)
        ?? lane.querySelector<HTMLElement>('[data-selected="true"]');
      const panel = detail.querySelector<HTMLElement>(".dw-detail");
      if (!node || !panel) {
        detail.style.removeProperty("--dw-arrow-x");
        return;
      }
      // MEASURED IN THE PANEL'S OWN COORDINATES, and never off its end: a notch hanging past
      // the corner reads as a rendering bug rather than as a pointer.
      const box = node.getBoundingClientRect();
      const frame = panel.getBoundingClientRect();
      // A step scrolled out of the lane's view has nothing on screen to point at.
      const view = lane.querySelector<HTMLElement>(".wf-lane-viewport")?.getBoundingClientRect();
      const mid = box.left + box.width / 2;
      const hidden = !!view && (mid < view.left || mid > view.right);
      if (hidden) panel.setAttribute("data-arrow", "off"); else panel.removeAttribute("data-arrow");
      const centre = mid - frame.left;
      const clamped = Math.max(18, Math.min(frame.width - 18, centre));
      detail.style.setProperty("--dw-arrow-x", `${Math.round(clamped)}px`);
    };
    place();
    // RE-MEASURED WHENEVER THE NODES MAY HAVE MOVED, not only when the window does. The notch
    // was measured on a pick and a window resize, so it pointed at a stale x after the run's
    // saved workflow replaced the fallback lane (branches drawn), after the Agents chip was
    // toggled off (a fresh wrapper with no position), and after the lane refitted to a
    // container that resized on its own (review of ws/ia, 2026-10-03). The dependencies cover
    // the first two; the observers cover the lane reflowing for any reason.
    window.addEventListener("resize", place);
    const lane = laneRef.current;
    // The lane also scrolls sideways inside itself, which moves the node without resizing it.
    const viewport = lane?.querySelector(".wf-lane-viewport");
    viewport?.addEventListener("scroll", place, { passive: true });
    const detail = detailRef.current;
    const sized = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(place);
    const moved = typeof MutationObserver === "undefined" ? null : new MutationObserver(place);
    if (lane) {
      sized?.observe(lane);
      moved?.observe(lane, { subtree: true, childList: true, attributes: true });
    }
    if (detail) sized?.observe(detail);
    return () => {
      window.removeEventListener("resize", place);
      viewport?.removeEventListener("scroll", place);
      sized?.disconnect();
      moved?.disconnect();
    };
  }, [picked, steps.length, laneSpec, showAgents]);

  return (
    <div className="dw-path" data-testid="dw-path">
      <div className="wf-run-bar">
        <button type="button" className="wf-tag" aria-pressed={showAgents}
                data-testid="dw-run-agents" title={terminal ? "Agents, as run" : "Agents on this run"}
                onClick={() => setShowAgents((v) => !v)}>
          Agents
        </button>
        <CreditsButton chain={chain} runId={run.id} projectId={projectId} viewer={actor}
                       title={runSubject(run) ?? run.workflow_name} live={!terminal} />
        {onOpenWorkflow ? (
          <button type="button" className="wf-link" data-testid="dw-run-open-workflow"
                  title={`${run.workflow_name} v${run.version}`}
                  onClick={() => onOpenWorkflow(run.workflow_key)}>
            Open workflow
          </button>
        ) : null}
        {isTestRun(run) ? (
          <span className="wf-tag" data-testid="dw-run-is-test"
                title="Pinned steps returned their pinned output; steps that could spend were refused unless allowed">
            Test run
          </span>
        ) : null}
        {rerunNote(run) ? (
          <span className="detail" data-testid="dw-run-rerun-of">{rerunNote(run)}</span>
        ) : null}
        <RunAutonomy projectId={projectId} runId={run.id} live={!terminal} />
      </div>
      <RunChain chain={chain} viewer={actor} />
      <div className="wf-run-lane" ref={laneRef} data-testid="dw-run-lane">
        <FlowLane spec={laneSpec} palette={lanePalette} mode="run" label="Workflow steps"
                  selection={showAgents ? null : picked} runState={laneState}
                  onSelect={(id) => {
                    // Only a step this run has: a branch it never took has nothing to show.
                    if (id && steps.some((s) => s.step_id === id)) { setShowAgents(false); setPicked(id); }
                  }} />
      </div>
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
          actor={actor}
          onRetry={(note) => onRetryStep(current.step_id, note)}
          onSkip={(reason) => onSkipStep(current.step_id, reason)}
          onSetTarget={(targetId) => onSetTarget(targetId)}
          onPrepareTarget={onPrepareTarget}
          buildRunId={buildRunId}
          workflowRunId={run.id}
          act={stepActOf(chain, current.step_id)}
          planned={(() => {
            const spec = laneSpec.steps.find((x) => x.id === current.step_id);
            return spec ? viewOf(spec, lanePalette).agent : undefined;
          })()}
          approval={approvalOf(chain, stepActOf(chain, current.step_id))}
          spend={chain?.steps?.[current.step_id] ?? null}
          onRunPreview={onRunPreview}
          onBound={onBound}
          projectId={projectId}
          previewStarting={previewStarting}
          busy={busy}
          panelBuild={buildStageOf(steps)}
          monitoringPack={packForStep(run.steps, current, (id) => {
            const spec = laneSpec.steps.find((x) => x.id === id) as { next?: unknown } | undefined;
            return Array.isArray(spec?.next) ? spec.next.map(String) : [];
          })}
        />
        {pinnedOutput(current.output) ? (
          <p className="detail" data-testid="dw-step-pinned">
            Pinned: this step did not run; it returned the output it was given.
          </p>
        ) : null}
        {terminal && onRerunFrom ? (
          <button type="button" className="wf-link" data-testid="dw-rerun-from"
                  disabled={!!busy}
                  title="Start a new run from this step. Every step before it keeps what this run produced."
                  onClick={() => onRerunFrom(current.step_id)}>
            Run again from here
          </button>
        ) : null}
        </div>
      ) : null}
      {/* THE REPORT, ONCE THERE IS SOMETHING TO REPORT.
          Not from step zero: a report reading "0 of 10 steps, $0.00, nothing went wrong" is
          noise that teaches people to scroll past the panel before it ever has an answer. It
          appears as soon as one step has finished and keeps filling in, which is also what
          makes it useful DURING a long build rather than only after it. */}
      {run.steps.some((s) => s.status === "succeeded" || s.status === "skipped") ? (
        <RunReportPanel runId={run.id} projectId={projectId}
                        researchAction={(run.actions ?? []).find(
                          (a) => a.key === "build_on_research")} />
      ) : null}

      {/* WORKFLOW MANAGEMENT — exactly the actions the server sent for this run, in its order.
          Which of Re-check, Stop, Stop and run again, Run again, Remove and Build on this
          research a run has is decided by the engine from the run's real state
          (workflow_run_actions.py); see `RunAction`. Nothing here looks at `run.status`. */}
      <RunActionsBar run={run} busy={busy} handlers={{
        recheck: onRecheck, stop: onStop, stop_and_rerun: onStopAndRerun, rerun: onRerun,
        remove: onDelete, build_on_research: onBuildOnThis,
      }} />
      {/* Share this run: a press, never a setting (docs/product/RUN_SCORECARDS.md). */}
      <ShareRunButton projectId={projectId} actor={actor} unitKind="workflow_run" unitId={run.id} />
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
function DeployWhere({ targets, hosts, runInput, buildRunId, busy, bound, hostName, live,
                      onSetTarget, onPrepareTarget, onBound }: {
  targets: DeployTarget[]; hosts: DeployHost[]; runInput: Record<string, unknown>;
  /** The code task whose build is being shipped — the app the target binds to. */
  buildRunId: string;
  busy: string | null;
  /** The target this app is ALREADY bound to, resolved by app identity. Settled, not a form. */
  bound?: DeployTarget;
  /** That target's host, in the words the host list uses. */
  hostName?: string;
  /** The app is live there and the step already says "Live at <address>". */
  live?: boolean;
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
    // The address once: on a host already named by it, only the port is new information.
    const domain = (settled.domains ?? []).find((d: string) => !!d) ?? "";
    const [domainHost, port] = domain.replace(/^https?:\/\//, "").split("/")[0].split(":");
    const where = !domain ? "" : hostName && hostName.split(" · ").includes(domainHost)
      ? (port ? `port ${port}` : "") : domain;
    return (
      <div className="dw-bound" data-testid="dw-deploy-bound">
        <span className="dw-bound-ok" aria-hidden>✓</span>
        <span>
          Ships to <strong>{settled.app_name}</strong> · {settled.environment}
          {/* The address once: when the step already says "Live at http://host:port", the host
              and port here repeated it. */}
          {hostName && !live ? <> on <strong>{hostName}</strong></> : null}
          {where && !live ? <span className="detail"> ({where})</span> : null}
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
function ShipNow({ projectId, buildRunId, target, again, onDone }: {
  projectId: string; buildRunId: string; target: DeployTarget;
  /** This app has been deployed before: the request replaces what runs there. */
  again: boolean;
  onDone: () => void;
}) {
  const [state, setState] = useState<"idle" | "planning">("idle");
  const [msg, setMsg] = useState<string | null>(null);

  const ship = useCallback(async () => {
    setState("planning");
    setMsg(null);
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
      if (!plannedDeploymentFrom(d)) {
        setState("idle");
        setMsg("Deployment request did not return a reviewable ChangeSet. No deployment was confirmed.");
        return;
      }
      // The request now exists; the status line above it (DeployStatusLine) takes over and
      // says "Waiting for approval", and this button steps aside until that deploy settles.
      setState("idle");
      onDone();
    } catch (e) {
      setState("idle");
      setMsg(String((e as Error)?.message ?? e));
    }
  }, [projectId, buildRunId, target, onDone]);

  return (
    <div className="dw-ship" data-testid="dw-ship">
      <GatedButton permission="deployment:create" type="button" className={`cap-btn${again ? "" : " primary"}`}
              disabled={state !== "idle"} onClick={ship} data-testid="dw-ship-now"
              title={`Asks for approval, then ships ${target.app_name} · ${target.environment}${
                target.domains?.[0] ? ` to ${target.domains[0]}` : ""}`}>
        {state === "planning" ? "Asking…" : again ? "Deploy again" : "Deploy"}
      </GatedButton>
      {msg ? <span className="detail dw-ship-msg" data-testid="dw-ship-msg" role="alert">{msg}</span> : null}
    </div>
  );
}

/** A deploy of this build as the server reads it (GET /v1/code-tasks/{id}/deployments). */
export type NewestDeploy = {
  ref: string; phase: string; word: string; tone: string; sentence: string;
  openUrl: string | null; refresh: () => void;
};
/** Deploys still in hand: asking a person, starting, or running. */
export const OPEN_PHASES = new Set(["needs_approval", "queued", "deploying"]);

/** The newest deploy of a build, re-read every 3 s while it moves and 8 s while it asks. */
export function useNewestDeploy(buildRunId: string | null, appName: string): NewestDeploy | null {
  const [seen, setSeen] = useState<Omit<NewestDeploy, "refresh"> | null>(null);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    if (!buildRunId) { setSeen(null); return; }
    let alive = true;
    (async () => {
      try {
        const r = await fetch(
          `/api/run-deployments?run_ids=${encodeURIComponent(buildRunId)}`
          + `&app_name=${encodeURIComponent(appName)}`, { cache: "no-store" });
        if (!r.ok) return;
        const body = await r.json();
        const list = (body?.runs?.[buildRunId]?.deployments ?? []) as Array<Record<string, unknown>>;
        const newest = [...list].sort((a, b) =>
          String(b.started_at ?? "").localeCompare(String(a.started_at ?? "")))[0];
        if (!alive) return;
        setSeen(newest ? {
          ref: String(newest.ref ?? ""), phase: String(newest.phase ?? ""),
          word: String(newest.word ?? newest.status ?? ""), tone: String(newest.tone ?? ""),
          sentence: String(newest.sentence ?? ""),
          openUrl: typeof newest.open_url === "string" && newest.open_url ? newest.open_url : null,
        } : null);
      } catch { /* no deployment history: the step simply offers to ship */ }
    })();
    return () => { alive = false; };
  }, [buildRunId, appName, tick]);
  const phase = seen?.phase ?? "";
  useEffect(() => {
    if (!OPEN_PHASES.has(phase)) return;
    const t = setTimeout(refresh, phase === "needs_approval" ? 8_000 : 3_000);
    return () => clearTimeout(t);
  }, [phase, tick, refresh]);
  return seen ? { ...seen, refresh } : null;
}

/** The deploy's state, in the words every surface uses, with the ways into it. */
export function DeployStatusLine({ deploy, stepSucceeded }: { deploy: NewestDeploy; stepSucceeded: boolean }) {
  // Keeps the org, as every console link does.
  const org = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("org") : null;
  const href = deploy.ref ? `?${org ? `org=${encodeURIComponent(org)}&` : ""}area=infra&view=deploys&deploy=${
    encodeURIComponent(deploy.ref)}` : "";
  // A live app that this step did not ship is the previous release, and is said so.
  const earlier = !stepSucceeded && deploy.phase === "live";
  return (
    <p className="dw-step-live" data-testid="dw-deploy-status" data-phase={deploy.phase}>
      <span className="dw-badge" data-tone={deploy.tone} data-testid="dw-deploy-word">{deploy.word}</span>
      <span className="detail" data-testid="dw-deploy-sentence">
        {earlier ? "The previous release: " : ""}{deploy.sentence}
      </span>
      {deploy.openUrl ? (
        <a className={`cap-btn${earlier ? "" : " primary"}`} href={deploy.openUrl} target="_blank"
           rel="noreferrer" data-testid="dw-open-app">Open the app ↗</a>
      ) : null}
      {href ? <a className="cap-btn" href={href} data-testid="dw-open-deploy"
                 title="This deploy's steps, log and health check">Deploy details</a> : null}
    </p>
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
  return stepAgentChip(step.captain, step.output, capability?.captain)?.role ?? "";
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
    case "succeeded":
    case "completed":
      return `What did the ${name} step do?`;
    case "skipped":
      return `Why was the ${name} step skipped?`;
    case "pending":
    case "ready":
    case "queued":
      return `When will the ${name} step start?`;
    default:
      // A status read as an adjective ("Why is the build step succeeded?") is not English;
      // the status is quoted as what it is.
      return `What does "${step.status}" mean for the ${name} step?`;
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
  // WHAT IT WILL DO, ON THE STEP TOO. The decision card has the buttons; the step at least
  // shows what is waiting on them, so "Email the newsletter — needs you" says which email.
  const preview = actionPreviewOf(step.action_preview);
  return (
    <>
    {preview ? <ActionPreview preview={preview} /> : null}
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
    </>
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
      <GatedButton permission="deployment_target:manage" className="cap-btn" type="submit" disabled={saving || !!busy}
              data-testid="dw-deploy-settarget">
        {saving ? "Saving…" : "Set target"}
      </GatedButton>
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
/** The longest retry note the API accepts (WorkflowRetryStepBody.note). */
export const RETRY_NOTE_LIMIT = 4000;

/** The note's example, fitted to the step: a deploy is not fixed by a login test. */
export function retryExample(capability: string): string {
  const kind = String(capability || "").toLowerCase();
  if (kind.includes("deploy")) return "e.g. deploy on port 8080";
  if (kind.includes("build") || kind.includes("test") || kind.includes("fix")) {
    return "e.g. the login test fails — read DEMO_PASSWORD from the environment";
  }
  if (kind.includes("preview")) return "e.g. start the app with npm run dev";
  return "e.g. what to do differently this time";
}

export function FailedStepActions({ step, capability, actions, busy, onRetry, onSkip,
                            idPrefix = "dw-step" }: {
  step: RunStep; capability?: Capability; busy: string | null;
  /** This step's Try again and Skip, as the server decided them (`stepActions`). Whichever is
   *  absent is not rendered; whichever is disabled says why. Nothing here reads a status. */
  actions: { retry?: RunAction; skip?: RunAction };
  onRetry: (note?: string) => void; onSkip: (reason: string) => void; idPrefix?: string;
}) {
  // RETRY WITH A NOTE. Try again opens one optional field first — what should change — and the
  // note goes with the retry; a retried build hands it to the builder, verbatim.
  const [retryOpen, setRetryOpen] = useState(false);
  const [retryNote, setRetryNote] = useState("");
  // The reason box for "Skip and proceed" — open only after the operator asks to skip, so the
  // decision is two deliberate acts: choose to skip, then say why.
  const [skipOpen, setSkipOpen] = useState(false);
  const [skipWhy, setSkipWhy] = useState("");
  const name = capability?.label ?? step.step_id;
  const { retry, skip } = actions;
  if (!retry && !skip) return null;
  // ONE RETRY ON SCREEN. The note form used to open BESIDE the Retry and Skip buttons, so two
  // Retry buttons stood side by side and the cramped box cut its own example off (operator,
  // 16 Oct). An open panel now replaces the button row; Cancel brings it back.
  const panelOpen = (retry?.enabled && retryOpen) || (skip?.enabled && skipOpen);
  return (
    <div className="dw-step-decide" data-testid={`${idPrefix}-decide`}>
      {panelOpen ? null : retry ? (
        <>
          <GatedButton permission="workflow_run:control" type="button" className="cap-btn primary"
                  onClick={() => setRetryOpen(true)}
                  disabled={!!busy || retryOpen || !retry.enabled} title={retry.reason ?? undefined}
                  data-testid={`${idPrefix}-retry`}>
            {busy === "Retry" ? "Queueing…" : retry.label}
          </GatedButton>
          <Explain label="What retrying this step does">
            {retry.enabled ? "Runs this step only; everything before it stays done. "
              + "You can say what should change — a build gets your note."
              : `Not available: ${retry.reason}.`}
          </Explain>
        </>
      ) : null}
      {skip && !panelOpen ? (
        <GatedButton permission="workflow_run:control" type="button" className="cap-btn" onClick={() => setSkipOpen(true)}
                disabled={!!busy || skipOpen || !skip.enabled} title={skip.reason ?? undefined}
                data-testid={`${idPrefix}-skip`}>
          {busy === "Skip" ? "Skipping…" : skip.label}
        </GatedButton>
      ) : null}
      {skip && !panelOpen ? (
        <Explain label="What skipping this step does">
          {skip.enabled
            ? "Marks it skipped with your reason and carries on. Steps that use what it would "
              + "have produced are told it was skipped — never handed a made-up value."
            : `Not available: ${skip.reason}.`}
        </Explain>
      ) : null}
      {retry?.enabled && retryOpen ? (
        <form className="dw-retry-panel" data-testid={`${idPrefix}-retry-form`}
              aria-label={`Retry ${name}`}
              onSubmit={(e) => {
                e.preventDefault();
                const note = retryNote.trim();
                onRetry(note || undefined);
                setRetryOpen(false);
                setRetryNote("");
              }}>
          <div className="dw-retry-title">Retry {name}</div>
          <p className="dw-retry-what">Runs {name} again. Steps before it stay done.</p>
          <label title="Optional. Sent with the retry; a build gets it as instructions.">
            What should change? (optional)
            <textarea value={retryNote} autoFocus rows={3} maxLength={RETRY_NOTE_LIMIT}
                      data-testid={`${idPrefix}-retry-note`}
                      placeholder={retryExample(step.capability ?? step.step_id)}
                      onChange={(e) => setRetryNote(e.target.value)} />
          </label>
          <div className="dw-retry-actions">
            <button type="submit" className="cap-btn primary" disabled={!!busy}
                    data-testid={`${idPrefix}-retry-confirm`}>
              Retry {name}
            </button>
            <button type="button" className="linklike"
                    onClick={() => { setRetryOpen(false); setRetryNote(""); }}>Cancel</button>
          </div>
        </form>
      ) : null}
      {skip?.enabled && skipOpen ? (
        <form className="dw-retry-panel" data-testid={`${idPrefix}-skip-form`}
              onSubmit={(e) => {
                e.preventDefault();
                if (!skipWhy.trim()) return;
                onSkip(skipWhy.trim());
                setSkipOpen(false);
                setSkipWhy("");
              }}>
          <div className="dw-retry-title">Skip {name}</div>
          <p className="dw-retry-what">The run carries on without it; later steps are told it was skipped.</p>
          <label>
            Why go on without {name}?
            <input type="text" value={skipWhy} autoFocus required
                   data-testid={`${idPrefix}-skip-reason`}
                   placeholder="kept on the run's record"
                   onChange={(e) => setSkipWhy(e.target.value)} />
          </label>
          <div className="dw-retry-actions">
            <button type="submit" className="cap-btn primary"
                    disabled={!!busy || !skipWhy.trim()} data-testid={`${idPrefix}-skip-confirm`}>
              Skip it
            </button>
            <button type="button" className="linklike"
                    onClick={() => { setSkipOpen(false); setSkipWhy(""); }}>Cancel</button>
          </div>
        </form>
      ) : null}
    </div>
  );
}

/**
 * THE STEP'S OWN WORK, IN THE STEP. A research step's dossier and a panel step's meeting are
 * what an operator clicked the step to see; they used to be a link to another area ("open the
 * research", "open the panel"). The step panel now shows them inline — the verdict or outcome
 * first, live progress while the step runs — with the full view one press away.
 */
export function stepWorkOf(step: Pick<RunStep, "capability" | "output">):
  { kind: "discovery" | "meeting" | "architecture"; id: string } | null {
  const out = (step.output ?? {}) as Record<string, unknown>;
  const awaited = (out.await as { kind?: string; id?: string } | undefined) ?? {};
  const str = (k: string) => (typeof out[k] === "string" ? (out[k] as string) : "");
  const cap = String(step.capability ?? out.capability ?? "");
  if (cap === "research" || awaited.kind === "discovery") {
    const id = str("discovery_id") || (awaited.kind === "discovery" ? awaited.id ?? "" : "");
    return id ? { kind: "discovery", id } : null;
  }
  if (cap === "architecture" || awaited.kind === "architecture") {
    const id = str("architecture_review_id")
      || (awaited.kind === "architecture" ? awaited.id ?? "" : "");
    return id ? { kind: "architecture", id } : null;
  }
  if (["meeting", "prd_panel", "panel"].includes(cap) || awaited.kind === "meeting") {
    const id = str("meeting_run_id") || (awaited.kind === "meeting" ? awaited.id ?? "" : "");
    return id ? { kind: "meeting", id } : null;
  }
  return null;
}

/**
 * The step's links, less the ones something else on the step already offers — ONE way in to
 * each thing. The preview block has its own "Open preview"; a research step's dossier and a
 * panel step's meeting are on the step with "Open full view", and the panel's compact view
 * carries "Open the PR".
 */
export function visibleStepLinks<L extends { label: string }>(links: L[],
  work: { kind: "discovery" | "meeting" | "architecture" } | null): L[] {
  return links.filter((l) => l.label !== "open the preview"
    && !(work?.kind === "discovery" && l.label === "open the research")
    && !(work?.kind === "meeting" && (l.label === "open the panel" || l.label.startsWith("pull request #"))));
}

/** The run's own build step as a stage state, for a panel step's "Build" stage. */
export function buildStageOf(steps: Array<Pick<RunStep, "capability" | "status">>):
  "done" | "now" | "todo" | "stopped" | "skipped" | undefined {
  const b = steps.find((s) => s.capability === "build");
  if (!b) return undefined;
  if (b.status === "succeeded") return "done";
  if (["running", "waiting", "leased", "ready"].includes(b.status)) return "now";
  if (b.status === "skipped" || b.status === "cancelled") return "skipped";
  if (b.status === "failed" || b.status === "deadlettered") return "stopped";
  return "todo";
}

function StepDetail({ step, stepIndex, capability, targets, hosts, runInput, onRetry, onSkip,
                     actions, actor = "", act = null, approval: gateAnswer = null, planned,
                     onSetTarget, onPrepareTarget, onRunPreview, onBound, projectId, buildRunId,
                     workflowRunId, previewStarting, busy, panelBuild, spend = null,
                     monitoringPack = null }: {
  projectId: string;
  /** Set up monitoring's pack: on its own step, and on the release that carries it. */
  monitoringPack?: MonitoringPack | null;
  /** What this step spent over every attempt (chain.steps). */
  spend?: StepSpend | null;
  /** The run's build step, as the panel step's "Build" stage shows it. */
  panelBuild?: "done" | "now" | "todo" | "stopped" | "skipped";
  /** Who is acting — what a stack deployment's Approve/Reject is recorded as. */
  actor?: string;
  /** The workflow run this step belongs to — what "Ask about this step" is about. */
  workflowRunId?: string;
  /** The signed work for this step, once recorded: who did it and how it reached them. */
  act?: Act | null;
  /** Who the workflow assigns this step to — the lane node's own view, so they agree. */
  planned?: StepView["agent"];
  /** The person's answer to this step's gate, when there was one. */
  approval?: Act | null;
  /** This step's Try again / Skip, from the run's server-decided `actions`. */
  actions: { retry?: RunAction; skip?: RunAction };
  /** Where this step sits in the run, so the card can carry the rail's number. */
  stepIndex?: number;
  step: RunStep; capability?: Capability; targets: DeployTarget[]; hosts: DeployHost[];
  runInput: Record<string, unknown>;
  onRetry: (note?: string) => void; onSetTarget: (targetId: string) => void;
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
  // A DEPLOY THAT SHIPS TO A STACK TARGET. Its deployment's decision is the step's only
  // approval, so none of the per-app container flow below applies to it: no Set target, no
  // "Approve the release", no Ship now.
  const stackDeploy = isDeployStep(step, capability) ? stackDeployOf(step) : null;
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
    // Said once: a host named by its address read "192.0.2.202 · 192.0.2.202".
    return host.address && host.address !== host.name ? `${host.name} · ${host.address}` : host.name;
  })();
  // THE TARGET CONTROL, MADE ONCE AND PLACED WHEREVER THE LAYOUT PUTS IT. It renders the bound
  // target as a fact with Change, or the form to set one — and `onBound` is also what records
  // that this operator has now done it, which is what quietens the step's teaching copy.
  const name = appIdentity(runInput as AppRun).name;
  const lastDeploy = useNewestDeploy(isDeployStep(step, capability) && !stackDeploy ? buildRunId : null, name);
  const targetControl = (
    <DeployWhere targets={targets} hosts={hosts} runInput={runInput}
                 buildRunId={buildRunId ?? ""} busy={busy}
                 bound={shippedTo} hostName={boundHostName} live={!!lastDeploy?.openUrl}
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
  // THE SERVER'S ONE READING OF THE NEWEST DEPLOY (infra_view.deploy_status): its word, the
  // sentence Infra shows, and the app link only once it went live with its health check
  // passed. This step used to derive all three itself and said "Nothing has deployed" over a
  // deploy that had shipped. Re-read while the deploy is moving, so approve → starting →
  // deploying → live is watched here, not discovered by refreshing. (`lastDeploy`, above the
  // target control, which uses it.)
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
  const work = stepWorkOf(step);
  const visibleLinks = visibleStepLinks(links, work);
  // Asked for on THIS build and not yet serving. The URL is the finish line; a POST returning
  // is not — which is why the old busy flag cleared a second after the click.
  // A FRESH ONE IS ON ITS WAY UNTIL A DIFFERENT LINK ARRIVES. This used to require "no link
  // yet", so "Start a fresh one" — pressed exactly when a link already exists — ended its own
  // starting state the moment it began: no "Starting…", the old link unchanged, and a click
  // that looked dead for the minute the new preview took (boss-1, 2026-10-03).
  const [freshFrom, setFreshFrom] = useState<string | null | undefined>(undefined);
  const asked = !!previewStarting && previewStarting === buildRunId;
  useEffect(() => { if (!asked) setFreshFrom(undefined); }, [asked]);
  const starting = asked && (freshFrom !== undefined ? previewHref === freshFrom : !previewHref);
  const approval = step.approval as { mode?: string; approver_role?: string; follows_policy?: boolean };
  // A manual gate that has not been answered. The one condition under which "waiting" and
  // "gate · <role>" are the same sentence twice.
  const gateWaiting = approval?.mode === "manual" && step.status === "waiting";
  // WHAT THE STEP MADE, FOR A PERSON: an allow-list of human outputs (the PR, the branch, the
  // decision, the note), with every machine field — awaits, run ids, verdicts — under one
  // Details disclosure, each text once and never "null" (stepOutput, app/workflows/step-run).
  const output = stepOutput(step.output as Record<string, unknown>,
                            { noteShownAbove: step.status !== "succeeded" || phase === "skipped",
                              status: step.status });

  // THE STEP'S OWN LOG: its rows and the work it started, read once and shared by the Done-by
  // line (model, cost) and the Activity section, so they cannot disagree.
  const activity = useStepActivity(projectId, workflowRunId, step.step_id, phase === "now",
                                   `${step.status}:${step.attempt}`);
  // WHY THIS MODEL: the model router's route for this step (docs/product/MODEL_ROUTER.md).
  const stepRoutes = useRunRoutes(projectId, workflowRunId, phase === "now");
  const logHref = workflowRunId
    ? stepLogHref(typeof window !== "undefined" ? window.location.search : "", workflowRunId, step.step_id,
                  { step: capability?.label ?? step.capability ?? step.step_id,
                    run: appIdentity(runInput as AppRun).name || runAppTitle })
    : "";
  const facts = stepFacts(step);
  const finished = !["now", "ahead"].includes(phase);
  const question = stepQuestion(step, capability?.label);
  const ask = () => {
    if (act?.roleKey) { askAboutAct(act, question); return; }
    askBossAbout(
      workflowRunId ? { kind: "workflow_step", id: workflowRunId, step: step.step_id }
        : buildRunId ? { kind: "code_task", id: buildRunId } : null,
      question, stepCaptain(step, capability));
  };
  const hasOutput = isPreviewStep(step) || visibleLinks.length > 0
    || typeof (step.output || {}).document_id === "string" || output.facts.length > 0
    || output.details.length > 0;

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
                  : phase === "nothing" ? "wait"
                  : phase === "failed_on" ? "bad" : tone(step.status)}
                data-testid={phase === "failed_on" ? "dw-step-failed-on"
                  : phase === "nothing" ? "dw-step-did-nothing"
                  : phase === "skipped" ? "dw-step-na" : undefined}>
            {phase === "failed_on" ? "failed — went on"
              : phase === "nothing" ? DID_NOTHING
              // Same wording as the chain node, for the same reason: this step chose to stand
              // aside and said why. The sentence renders directly below as the note. An
              // operator's skip says who skipped it, as the rail does — "skipped" alone read
              // the same as a branch the run never took.
              : phase === "skipped" ? (skippedBy(step) || "skipped") : step.status}
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
        {stepDuration(step) && phase !== "skipped" ? (
          <span className="dw-step-took" data-testid="dw-step-took">{stepDuration(step)}</span>
        ) : null}
        {approval?.mode === "manual" ? (
          <span className="dw-gate" data-tone={gateWaiting ? "waiting" : undefined}
                data-testid="dw-step-gate"
                // A DEPLOY THAT FOLLOWS ITS ENVIRONMENT'S POLICY is not promised to a person:
                // the policy may ship it unasked (deploy_policy), and the chip says so.
                title={approval.follows_policy && !gateWaiting
                  ? "Asks a person unless this environment's deploy policy ships without asking"
                  : "Requires a person"}>
            {gateWaiting
              ? `needs ${approval.approver_role || "a person"}`
              : approval.follows_policy ? "gate · or by policy"
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

      {/* WHO, IN ONE LINE. The step's run leads this panel; the agent that did it is a face,
          a name, what it cost and the model, with its whole card one press away (operator,
          2026-10-04: "too much info about the agent"). */}
      <StepDoneBy act={act} approval={gateAnswer} output={step.output} phase={phase}
                  status={step.status} planned={planned} projectId={projectId} viewer={actor}
                  failure={phase === "failed" ? step.error : null} question={question}
                  cost={activity.cost} calls={activity.calls} model={activity.model}
                  onAsk={ask} askBossTip="Ask BOSS about this step" stepId={step.step_id} facts={facts}
                  route={routeForStep(stepRoutes, step.step_id)} spend={spend}
                  skipped={phase === "skipped" ? (skippedBy(step) || skipReason(step) || "Did not apply to this run") : undefined} />

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
        // On a succeeded step the note is what it has to say: it is the Output's Note (or the
        // Skipped line's reason), not a dot here.
        step.status === "succeeded" ? null : (
          <p className="dw-step-note"
             data-tone={phase === "failed_on" ? "bad" : undefined}
             data-testid="dw-step-note">
            {String(step.output.note)}
          </p>
        )
      ) : null}

      {work?.kind === "discovery" ? (
        <div className="dw-step-work" data-testid="dw-step-dossier"><DossierCompact discoveryId={work.id} /></div>
      ) : work?.kind === "meeting" ? (
        <div className="dw-step-work" data-testid="dw-step-meeting">
          <MeetingCompact meetingId={work.id} projectId={projectId} buildState={panelBuild} />
        </div>
      ) : work?.kind === "architecture" ? (
        <div className="dw-step-work" data-testid="dw-step-architecture">
          <ArchitecturePanel reviewId={work.id} projectId={projectId} actorId={actor}
                             stepStatus={`${step.status}:${String(step.output?.architecture_version ?? "")}`} />
        </div>
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
      {/* WHERE THE DEPLOY IS, IN THE SERVER'S WORDS, with the way into it. */}
      {lastDeploy ? <DeployStatusLine deploy={lastDeploy} stepSucceeded={step.status === "succeeded"} /> : null}
      {/* ONE OPEN REQUEST AT A TIME. While a deploy of this app waits for approval, starts or
          runs, that deploy IS the way forward: a second "Plan deployment" press made a second
          request, and the operator approved one while the other still asked. */}
      {isDeployStep(step, capability) && !stackDeploy && shippedTo && buildRunId
        && !needsYou(step)
        && decidedButNotMoved(step) !== "approved"
        && !(lastDeploy && OPEN_PHASES.has(lastDeploy.phase)) ? (
        <ShipNow projectId={projectId} buildRunId={buildRunId} target={shippedTo}
                 again={!!lastDeploy} onDone={() => { lastDeploy?.refresh(); onBound(); }} />
      ) : null}

      {/* WHOSE DEPLOYMENT IS THAT, THOUGH.
          An address answering does not mean THIS run shipped. This app had a deployment from a
          run five days earlier, so a step still waiting for approval led with "Open the app —
          shipped and answering": true about the app, false about the step, and read top-down it
          says the deploy is done and then asks to approve the deploy. When this step has not
          succeeded the same fact is stated as what it is — the previous release — and does not
          take the primary button off the gate. */}
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
      {stackDeploy ? (
        <>
          <StackDeployStep stack={stackDeploy} actor={actor} onChanged={onBound} />
          {/* A gate opened before stack deploys stopped being gated (a run already waiting at
              it when BOSS was upgraded) is still answerable — through the Approvals page, the
              way it was raised. New runs never have one. */}
          {needsYou(step) ? <ReviewAndDecide step={step} /> : null}
        </>
      ) : isDeployStep(step, capability) && shippedTo ? (
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

      {/* Try again / Skip and proceed — see FailedStepActions for why both sit together. */}
      {/* WHICH OF THE TWO, IF EITHER, IS THE SERVER'S CALL — `actions` is this step's slice of
          the run's list, and FailedStepActions renders nothing when it is empty.
          KEYED BY THE STEP, so a reason half-typed for one step is not still open, pre-filled,
          when the operator clicks a different failed step in the rail. */}
      <FailedStepActions key={step.step_id} step={step} capability={capability} busy={busy}
                         actions={actions} onRetry={onRetry} onSkip={onSkip} />

      {/* BOSS IS FIXING ITSELF (docs/product/BOSS_FIXES_ITSELF.md §10.1): a step parked on a bug
          in BOSS's own code says what BOSS is doing about it, live, from the step's own output. */}
      <SelfFixStrip output={step.output as Record<string, unknown>} projectId={projectId} actor={actor} />

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

      {/* OUTPUT, THEN ACTIVITY: what the step made, and what it did to make it. */}
      <StepSection title="Output" testId="dw-step-output"
                   empty={finished ? "Nothing recorded" : "No output yet"}>
        {hasOutput ? (<>
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
                      data-testid="dw-preview-run"
                      onClick={() => { setFreshFrom(previewHref); onRunPreview(buildRunId); }}>
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

        {monitoringPack ? <MonitoringPackCard pack={monitoringPack} onCard={step.capability === "deploy" && step.status === "waiting"} /> : null}

        <StepOutputFacts facts={output.facts} details={output.details} />
        </>) : undefined}
      </StepSection>
      {workflowRunId ? <StepActivity state={activity} href={logHref} live={phase === "now"}
                                      settled={phase === "done" || phase === "findings"}
                                      failed={phase === "failed" || phase === "failed_on"} /> : null}
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
      // /v1, THROUGH THE CONSOLE'S PROXY. This was /api/artifacts, which no route handler
      // serves, so every record read "Could not read it — HTTP 404" and every PDF link 404'd.
      // tests/every-client-fetch-has-a-route enumerates these paths now.
      const r = await fetch(`/v1/artifacts/${encodeURIComponent(documentId)}`,
                            { cache: "no-store" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        setState({ kind: "failed", why: String(body?.detail ?? body?.error ?? `HTTP ${r.status}`) });
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
            <a className="cap-btn" href={`/v1/artifacts/${encodeURIComponent(documentId)}/pdf`}
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
