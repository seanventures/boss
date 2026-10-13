"use client";

/**
 * A run step's panel, led by the step's RUN rather than by the agent (operator, 2026-10-04:
 * "too much info about the agent … more information about that workflow step's run").
 *
 * - `StepDoneBy`: one line, "Done by <face> <name> · $cost · model", the step's facts under it,
 *   and the full agent card (StepSignedBy) one press away, below the facts.
 * - `stepOutput`: what the step MADE, for a person — an allow-list of human outputs; every
 *   machine field (ids, awaits, verdicts) is kept under one "Details" disclosure.
 * - `StepActivity`: the step's log as sentences, errors first, and "Step log", a deep link to
 *   the activity log scoped to this step (`?area=activity&wfr=…&step=…`).
 * - `useStepActivity`: one read of that scope, shared by the Done-by line and Activity.
 *
 * WHO is one answer (`stepActor`): the agent that ran it (the run's work record, else ran_as),
 * else who the workflow assigns it to — the very view the lane node draws — so the node, the
 * chain and this panel cannot name three different people for one step.
 */
import { WhyThisModel, type RouteRow } from "../why-this-model";
import React, { useState } from "react";

import type { Act, AvatarVariant, StepSpend } from "../../lib/agent-world-types";
import type { StepView } from "../../lib/workflow-editor-types";
import type { ActivityLog, ActivityLogItem } from "../../lib/activity-log";
import { useConsoleLive } from "../../lib/console-live";
import { fetchLogPage } from "../../lib/log-api";
import { usePolledRead } from "../../lib/log-stream";
import { clock as logClock, utcTitle } from "../../lib/log-time";
import { logHref } from "../../lib/log-url";
import { isLoopbackPreviewUrl } from "../../lib/code-tasks";
import { humanDuration } from "../../lib/datetime";
import { INCLUDED_TIP, laneAgentOf, spendLabel, type LaneAgent } from "../agents/chain-model";
import { nameOf, RunFace } from "../agents/run-face";
import { StepSignedBy } from "../agents/step-signed-by";
import { RanAs } from "../step-agents-ui";
import { useIdentityVersion } from "../../lib/use-agent-identities";

/** Rows shown in the panel; the rest are one click away in the step log. */
export const STEP_ACTIVITY_ROWS = 5;

/** The activity log, scoped to one step of one workflow run. Keeps the org and project, and
 *  carries the names the panel shows, so the log says "Build, review and test · Lead Triage
 *  Inbox" rather than two ids. */
export function stepLogHref(search: string, workflowRunId: string, stepId: string,
                            names: { step?: string; run?: string } = {}): string {
  return logHref({ wfr: workflowRunId, step: stepId, step_name: names.step || null,
                   run_name: names.run || null }, search);
}

/* ── Who ─────────────────────────────────────────────────────────────────────────── */

export type StepActor = { roleKey: string | null; variant: AvatarVariant; ran: boolean; personId?: string; name?: string };

/** The one answer to "who does this step": who RAN it, else who the workflow assigns it to. */
export function stepActor(ran: LaneAgent | undefined, planned: StepView["agent"] | undefined): StepActor | null {
  if (ran) {
    return { roleKey: ran.roleKey, variant: ran.variant, ran: true,
             ...(ran.personId ? { personId: ran.personId } : {}), ...(ran.name ? { name: ran.name } : {}) };
  }
  if (!planned) return null;
  if (planned.kind === "named") return { roleKey: planned.role_key ?? null, variant: "agent", ran: false };
  if (planned.kind === "auto") {
    return planned.role_key ? { roleKey: planned.role_key, variant: "agent", ran: false }
      : { roleKey: null, variant: "auto", ran: false };
  }
  return { roleKey: null, variant: planned.kind === "person" ? "person" : "you", ran: false };
}

/** The name a step's actor is called by, wherever a step shows it (callsign beside role). */
export function actorName(actor: StepActor): string {
  return nameOf(actor.roleKey, actor.variant, actor.personId, actor.name).name;
}

/* ── Output ──────────────────────────────────────────────────────────────────────── */

export type OutputFact = { label: string; value: string; tip?: string; href?: string };

/** A RECORD ID IS A LINK, NEVER TEXT (rc10 tester: "Details · 11" listed `deprun_…`, `dec_…`,
 *  `chg_…` on a finished deploy step). The kinds a person can open become a named link; the
 *  rest (targets, jobs, acts) are bookkeeping the panel already names elsewhere and drop. */
const ID_LINKS: Array<[RegExp, string, (id: string) => string]> = [
  [/^deprun_/, "Deployment", (id) => `?area=infra&view=deploys&deploy=${encodeURIComponent(`delivery:${id}`)}`],
  [/^sdep_/, "Deployment", (id) => `?area=infra&view=deploys&deploy=${encodeURIComponent(`stack:${id}`)}`],
  [/^dec_/, "Decision", (id) => `?area=approvals&decision=${encodeURIComponent(id)}`],
  [/^chg_/, "Change set", (id) => `?area=infra&view=cloud&cloud=changes&changeset=${encodeURIComponent(id)}`],
  [/^crun_/, "Build", (id) => `?area=code_tasks&build=${encodeURIComponent(id)}`],
  [/^wfr_/, "Workflow run", (id) => `?area=delivery_workflows&wfr=${encodeURIComponent(id)}`],
];
// BOSS's own record prefixes, named: a looser shape matched words like "not_applicable".
const RECORD_ID = /^(deprun|sdep|dec|chg|crun|wfr|deptgt|job|act|rrcpt|res|agt|usr|dh|tsk|mrun|prev|qg|drun|asb|tpl|tapp|disc|mtg)_[A-Za-z0-9]+$/;
/** Lifecycle fields that describe a step IN FLIGHT. On a finished step they are stale: a
 *  succeeded deploy still said "verdict awaiting · next action approve_decision". */
const IN_FLIGHT = new Set(["await", "verdict", "awaited_status", "next_action", "waiting_on",
  "awaiting", "pending_decision"]);
const FINISHED = new Set(["succeeded", "failed", "skipped", "deadlettered", "cancelled", "completed"]);

/** WHAT A PERSON WANTS FROM A STEP'S OUTPUT, by name. An allow-list: a new engine field lands
 *  under Details until someone decides it is an output, rather than on the page by default. */
const HUMAN: Array<[string, string]> = [
  ["summary", "Summary"], ["headline", "Summary"], ["outcome", "Decision"],
  ["approved_by", "Approved by"], ["repository", "Repository"], ["repo", "Repository"],
  ["branch", "Branch"], ["head_branch", "Branch"], ["base_branch", "Base branch"],
  ["app_name", "App"], ["environment", "Environment"], ["subject", "Subject"],
  ["recipient", "Sent to"], ["to", "Sent to"], ["tickets", "Tickets"], ["findings", "Findings"],
  ["title", "Title"],
];
/** Never shown at all: who ran it is the Done-by line's; the project is the whole run's. */
const NEVER = new Set(["inputs", "ran_as", "act_id", "project_id", "capability", "tool", "status"]);
const WORDS: Record<string, string> = { auto_approval: "Automatic", approved: "Approved", rejected: "Rejected" };


/** One value as a line of text, or "" when there is nothing to say (null, empty, a null id). */
export function factText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.length ? `${value.length}` : "";
  if (typeof value === "object") {
    const o = value as Record<string, unknown>;
    // The first id or url that HAS a value: `{job_id: null, run_id: "mrun_…"}` is the run.
    const key = Object.keys(o).find((k) => /(^|_)(id|url)$/.test(k) && factText(o[k]) !== "");
    return key ? factText(o[key]) : "";
  }
  return String(value);
}

function sentenceOf(text: string): string {
  const first = text.split(/(?<=[.!?])\s/)[0] ?? text;
  return first.length > 90 ? `${first.slice(0, 88)}…` : first;
}

/**
 * The step's output for a person (`facts`) and everything else (`details`), each line once.
 *
 * `noteShownAbove`: on a step that did not succeed the note is its live reason and the panel
 * shows it above; on a succeeded step it is what the step has to say, so it is an output.
 */
export function stepOutput(output: Record<string, unknown> | null | undefined,
                           opts: { noteShownAbove: boolean; status?: string }): { facts: OutputFact[]; details: OutputFact[] } {
  const out = output ?? {};
  const facts: OutputFact[] = [];
  const seen = new Set<string>();
  const add = (list: OutputFact[], label: string, raw: unknown, tip?: string) => {
    const text = factText(raw);
    // A loopback address is where an app listens ON THE HOST; nobody else can open it.
    if (!text || text === "—" || isLoopbackPreviewUrl(text)) return;
    // THE SAME TEXT IS ONE FACT. `await` and `run_id` held the same crun_ on every build step.
    if (seen.has(text)) return;
    seen.add(text);
    if (RECORD_ID.test(text)) {
      const link = ID_LINKS.find(([re]) => re.test(text));
      if (!link) return;
      const [, kind, href] = link;
      list.push({ label, value: `Open ${kind.toLowerCase()}`, tip: kind, href: href(text) });
      return;
    }
    list.push({ label, value: WORDS[text] ?? text, ...(tip ? { tip } : {}) });
  };
  const finished = FINISHED.has(String(opts.status ?? ""));
  if (!opts.noteShownAbove && typeof out.note === "string" && out.note.trim()) {
    add(facts, "Note", sentenceOf(out.note.trim()), out.note.trim());
  }
  for (const [key, label] of HUMAN) add(facts, label, out[key]);
  // A preview records its branch inside itself.
  const preview = out.preview && typeof out.preview === "object" ? out.preview as Record<string, unknown> : null;
  if (preview) { add(facts, "Branch", preview.branch); add(facts, "Repository", preview.repo); }
  const details: OutputFact[] = [];
  const human = new Set(["note", ...HUMAN.map(([k]) => k)]);
  for (const [key, value] of Object.entries(out)) {
    if (NEVER.has(key) || human.has(key)) continue;
    if (finished && IN_FLIGHT.has(key)) continue;
    add(details, key.replace(/_/g, " ").replace(/ (id|run id)$/, ""), value);
  }
  return { facts, details: details.slice(0, 12) };
}

/* ── Activity ────────────────────────────────────────────────────────────────────── */

/** The lifecycle bookkeeping every step emits, and raw process output: the step log keeps
 *  them, the panel does not. */
// `workflow.step_succeeded` too: the step's own row says it, with how long it took.
const NOISE = new Set(["workflow.step_ready", "workflow.step_leased", "workflow.step_succeeded",
  "code_task.github_plane",
  "code_task.integration_ref", "code_task.work_checkpointed"]);

const SENTENCES: Record<string, string> = {
  "workflow.step_succeeded": "Step finished",
  "workflow.step_failed": "Step failed",
  "workflow.step_skipped": "Step skipped",
  "workflow.step_waiting": "Waiting for a decision",
  "workflow.research_skipped": "Research skipped",
  "code_task.started": "Work started",
  "code_task.branch_refreshed": "Updated the branch",
  "code_task.auto_preview_skipped": "Preview skipped",
  "code_task.auto_preview_started": "Preview is up",
  "code_task.preview_ready": "Preview is up",
  "code_task.retrospective": "Wrote the retrospective",
  "code_task.review_comments_failed": "Review comments failed",
  "code_task.quality_pass": "Passed the quality check",
  "code_task.app_serve_checked": "Checked the app serves",
  "code_task.tests_reused": "Reused the tests",
  "code_task.step_escalating": "Escalated a stuck stage",
  "code_task.completed": "Work finished",
  "code_task.failed": "Work failed",
};

function words(snake: string): string {
  const s = snake.replace(/[._]/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : "";
}

/** A log row as a sentence a person reads, or null for bookkeeping the panel leaves out. */
export function stepLogSentence(item: Pick<ActivityLogItem, "source" | "kind" | "level" | "title" | "data">): string | null {
  const data = (item.data ?? {}) as Record<string, unknown>;
  if (item.source === "app_log" && item.level !== "error" && item.level !== "warn") return null;
  if (NOISE.has(item.kind)) return null;
  if (item.kind === "code_task.pr_opened") {
    const n = factText(data.number ?? data.pull_request_number ?? data.pr_number);
    return n ? `Opened pull request #${n}` : "Opened a pull request";
  }
  if (item.source === "llm_call") {
    const stage = words(String(data.stage ?? item.kind.replace(/^llm\./, "")));
    return `${stage || "Model"}: model call${data.ok === false ? " failed" : ""}`;
  }
  if (item.source === "workflow_step") {
    const status = String(data.status ?? "");
    const attempt = Number(data.attempt) || 0;
    const secs = Number(data.duration_seconds);
    const took = Number.isFinite(secs) && secs > 0 ? ` in ${humanDuration(secs)}` : "";
    const which = attempt > 1 ? `Attempt ${attempt}` : "Step";
    return status ? `${which} ${status}${took}` : null;
  }
  if (SENTENCES[item.kind]) return SENTENCES[item.kind];
  // Fallback: the classifier's title, without the source's name in front of it.
  return (item.title || words(item.kind)).replace(/^(Code Task|Workflow|Log|Llm)\s+/i, "") || null;
}

export type StepLogRow = { id: string; at: string; text: string; level: string; tip: string };

export type StepActivityState = {
  rows: StepLogRow[];
  loaded: boolean;
  errors: number;
  warnings: number;
  /** The model the step's work ran on, from its model calls. */
  model: string;
  /** What the step's model calls cost, in dollars, and how many there were. */
  cost: number;
  calls: number;
};

const EMPTY: StepActivityState = { rows: [], loaded: false, errors: 0, warnings: 0, model: "", cost: 0, calls: 0 };

/** The step's log as the panel shows it: sentences, bookkeeping out, repeats folded, and
 *  errors and warnings PINNED FIRST — a panel that shows five rows must never hide the error
 *  the step log has. Pure, so it is tested without a fetch. */
export function summariseStepActivity(items: ActivityLogItem[]): Omit<StepActivityState, "loaded"> {
  const calls = items.filter((i) => i.source === "llm_call");
  const model = String(calls.find((c) => typeof c.data?.model === "string" && c.data.model)?.data?.model ?? "");
  const cost = calls.reduce((sum, c) => sum + (Number(c.data?.cost_usd) || 0), 0);
  const rows: StepLogRow[] = [];
  for (const i of items) {
    if (i.level === "verbose") continue;
    const text = stepLogSentence(i);
    if (!text) continue;
    if (rows.length && rows[rows.length - 1].text === text) continue;
    rows.push({ id: i.id, at: i.createdAt, text, level: i.level, tip: i.body || i.title || text });
  }
  const loud = (r: StepLogRow) => (r.level === "error" ? 0 : r.level === "warn" ? 1 : 2);
  // Within each group, newest first by TIME: the reader merges sources, so input order alone
  // put 04:14 above 21:57 above 06:45.
  const t = (r: StepLogRow) => Date.parse(r.at) || 0;
  const ordered = rows.map((r, n) => ({ r, n }))
    .sort((a, b) => loud(a.r) - loud(b.r) || t(b.r) - t(a.r) || a.n - b.n).map((x) => x.r);
  return {
    rows: ordered, model, cost, calls: calls.length,
    errors: rows.filter((r) => r.level === "error").length,
    warnings: rows.filter((r) => r.level === "warn").length,
  };
}

/** The step's log, read in ONE bounded query (`summary=1`: the newest non-verbose rows, errors
 *  first, with the error/warn counts and the step's model calls), re-read every 5s while the
 *  step is live with its ETag — a 304 when nothing happened — and never while the console is
 *  Paused or the tab hidden. It used to page 100 rows through a 50,000-row scan every 5s. */
export function useStepActivity(projectId: string, workflowRunId: string | undefined, stepId: string,
                                live: boolean, version: string): StepActivityState {
  const consoleLive = useConsoleLive();
  const read = usePolledRead<ActivityLog>({
    key: `${projectId}|${workflowRunId ?? ""}|${stepId}|${version}`,
    pollMs: live ? 5000 : null,
    paused: !consoleLive,
    enabled: Boolean(workflowRunId),
    read: async (etag, signal) => {
      const got = await fetchLogPage(projectId, { workflow_run_id: workflowRunId, step_id: stepId,
                                                  summary: 1, limit: 12 }, { etag, signal });
      if (!got.ok) return { ok: false as const, error: got.error };
      return got.data ? { ok: true as const, data: got.data, etag: got.etag }
        : { ok: true as const, notModified: true, etag: got.etag };
    },
  });
  if (!workflowRunId) return { ...EMPTY, loaded: true };
  if (!read.data) return { ...EMPTY, loaded: !read.loading };
  const summary = summariseStepActivity(read.data.items);
  return {
    ...summary,
    errors: read.data.counts?.error ?? summary.errors,
    warnings: read.data.counts?.warn ?? summary.warnings,
    model: read.data.calls?.model || summary.model,
    cost: read.data.calls ? read.data.calls.costUsd : summary.cost,
    calls: read.data.calls ? read.data.calls.count : summary.calls,
    loaded: true,
  };
}

/** `18:02`, local, as every log in the console says a time (lib/log-time); UTC in the tooltip. */
export function clock(iso: string): string {
  return logClock(iso, { seconds: false });
}

/* ── The panel's parts ───────────────────────────────────────────────────────────── */

/** Done by / Doing / Queued for / Assigned to / Skipped, from the step's own state. */
export function actorVerb(phase: string, status: string): string {
  if (phase === "skipped") return "Skipped";
  if (phase === "ahead") return "Assigned to";
  if (phase === "now") return ["ready", "queued", "pending"].includes(status) ? "Queued for" : "Doing";
  // A step that failed was not "done": the run card said "Done by BOSS" over a failed smoke test.
  if (["failed", "deadlettered", "blocked"].includes(status)) return "Tried by";
  if (status === "cancelled") return "Stopped while with";
  return "Done by";
}

/** "Done by <face> <name> · $0.03 · model", the step's facts, and the agent's card below them. */
export function StepDoneBy({ act, approval, output, phase, status, planned, viewer, projectId, failure,
                             question, cost, calls, model, onAsk, stepId, facts, skipped,
                             askBossTip = "Ask about this step", route = null, spend = null }: {
  act: Act | null; approval?: Act | null; output: unknown;
  /** phaseOf(step), and the raw status for Queued vs Doing. */
  phase: string; status: string;
  /** Who the workflow assigns the step to: the lane node's own view of it. */
  planned?: StepView["agent"];
  projectId: string; viewer: string; failure?: string | null; question: string;
  /** Cost and count of the step's own model calls, for when the work record has no cost. */
  cost: number; calls: number; model: string;
  onAsk(): void; stepId: string;
  /** The Ask's tooltip when no agent has the step: the caller's, since the caller owns the door
   *  (it opens the dock through openDockAbout). */
  askBossTip?: string;
  /** "Started 18:02Z", "Attempt 2". */
  facts: string[];
  /** Why a skipped step did not apply. */
  skipped?: string;
  /** The model router's route for this step ("Why this model"), when it recorded one. */
  route?: RouteRow | null;
  /** What the step spent over every attempt it started (chain.steps), when the chain has it. */
  spend?: StepSpend | null;
}) {
  useIdentityVersion();
  const [open, setOpen] = useState(false);
  const verb = actorVerb(phase, status);
  if (phase === "skipped") {
    return (
      <div className="wf-doneby" data-testid="dw-step-doneby">
        <div className="wf-doneby-line">
          <span className="wf-doneby-verb" data-testid="dw-step-skipped">Skipped</span>
          {skipped ? <span className="wf-doneby-why" data-tip={skipped}>{sentenceOf(skipped).replace(/^Skipped:\s*/i, "")}</span> : null}
        </div>
      </div>
    );
  }
  const actor = stepActor(laneAgentOf(output, act, viewer), planned);
  const named = actor ? nameOf(actor.roleKey, actor.variant, actor.personId, actor.name) : null;
  // EVERY ATTEMPT'S SPEND. The step's own act holds none of it — the calls land on its build
  // stages and panel seats, and a retry is a new act — so the chain's per-step sum comes first.
  const spent = spend ? spend.costUsd : act?.costUsd || cost;
  const included = spend ? spend.includedUsd : act?.includedUsd ?? 0;
  // A PRICE ONLY WHERE MODELS WERE USED. A deploy a person approved made no model call, and
  // "$0.00" beside it read as a cost line that had failed to load.
  const priced = calls > 0 || spent > 0 || included > 0;
  const hasCard = !!act || !!(output && typeof output === "object" && (output as { ran_as?: unknown }).ran_as);
  return (
    <div className="wf-doneby" data-testid="dw-step-doneby">
      {actor && named ? (
        <div className="wf-doneby-line">
          <span className="wf-doneby-who">
            <span className="wf-doneby-verb">{verb}</span>
            <RunFace roleKey={actor.roleKey} variant={actor.variant} size={20} />
            <strong className="wf-doneby-name" data-testid="dw-step-doneby-name"
                    data-tip={named.role !== named.name ? named.role : undefined}>{named.name}</strong>
          </span>
          {priced || model || route ? (
            <span className="wf-doneby-facts">
              {priced ? (
                <span className="wf-doneby-fact" data-testid="dw-step-cost"
                      data-tip={[spend && spend.attempts > 1 ? `Across ${spend.attempts} attempts` : "",
                                 included > 0 ? INCLUDED_TIP : ""].filter(Boolean).join("\n") || undefined}>
                  {spendLabel(spent, included)}
                </span>
              ) : null}
              {route ? <span className="wf-doneby-fact" data-testid="dw-step-route"><WhyThisModel route={route} /></span>
                : model ? <span className="wf-doneby-fact" data-testid="dw-step-model" data-user-text>{model}</span> : null}
            </span>
          ) : null}
          <span className="wf-doneby-acts">
            {/* Always an Ask: the agent's own when an agent has the step, BOSS otherwise. */}
            <button type="button" className="wf-link" data-testid="dw-step-ask"
                    data-tip={actor.roleKey ? `Ask ${named.name} about this step` : askBossTip}
                    onClick={onAsk}>
              Ask
            </button>
            {hasCard ? (
              <button type="button" className="wf-link" aria-expanded={open} data-testid="dw-step-agent-toggle"
                      data-tip="Who did it and how it reached them" onClick={() => setOpen((o) => !o)}>
                {open ? "Hide agent" : "Agent"}
              </button>
            ) : null}
          </span>
        </div>
      ) : null}
      {facts.length ? (
        <div className="wf-step-facts" data-testid="dw-step-facts">
          {facts.map((f) => <span key={f}>{f}</span>)}
        </div>
      ) : null}
      {open ? (
        <div className="wf-doneby-card" data-testid="dw-step-agent-card">
          {act ? (
            // ONE ASK. The line above has it; the card's own would be a second, identical door.
            <StepSignedBy act={act} approval={approval} projectId={projectId} viewer={viewer}
                          failure={failure} question={question} askless />
          ) : (
            <RanAs output={output} stepId={stepId} />
          )}
        </div>
      ) : null}
    </div>
  );
}

/** What the step made: human outputs on the page, the machine's fields under Details. */
export function StepOutputFacts({ facts, details }: { facts: OutputFact[]; details: OutputFact[] }) {
  return (
    <>
      {facts.length ? (
        <dl className="wf-stepout" data-testid="dw-step-out">
          {facts.map((f) => (
            <div key={`${f.label}:${f.value}`}>
              <dt>{f.label}</dt>
              <dd data-tip={f.tip ?? f.value} data-user-text>{f.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {details.length ? (
        <details className="wf-stepout-details" data-testid="dw-step-details">
          <summary>Details · {details.length}</summary>
          <dl className="wf-stepout wf-stepout-mono">
            {details.map((f) => (
              <div key={`${f.label}:${f.value}`}>
                <dt>{f.label}</dt>
                <dd data-tip={f.tip ?? f.value} data-user-text>
                  {f.href ? <a className="inf-link" href={f.href}>{f.value}</a> : f.value}
                </dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </>
  );
}

/** The step's log as sentences, errors first, and the way into its whole log. */
export function StepActivity({ state, href, live, settled = false, failed = false }: {
  state: StepActivityState; href: string; live: boolean;
  /** The step SUCCEEDED: what went wrong along the way was fixed by the step itself. */
  settled?: boolean;
  /** The step FAILED (or stopped the run): now its warnings are worth a count too. */
  failed?: boolean;
}) {
  const rows = state.rows.slice(0, STEP_ACTIVITY_ROWS);
  // A BUILD THAT PASSED DID NOT END IN 29 ERRORS. Failing a test before fixing it is how the
  // work is done; once the step has succeeded those are counted as recovered, in a neutral
  // voice, and red is kept for a step that is still failing or has failed. Warnings ("npm WARN
  // deprecated") are noise while a step works: they are counted only once it has failed.
  const errors = state.errors ? `${state.errors} error${state.errors === 1 ? "" : "s"}` : "";
  const loud = settled
    ? (state.errors ? `${state.errors} recovered` : "")
    : failed
      ? [errors, state.warnings ? `${state.warnings} warning${state.warnings === 1 ? "" : "s"}` : ""]
          .filter(Boolean).join(" · ")
      : errors;
  return (
    <section className="wf-step-sec" data-testid="dw-step-activity" aria-label="Activity">
      <header className="wf-step-sec-head">
        <span className="eyebrow">Activity</span>
        {loud ? <span className="wf-step-sec-loud" data-testid="dw-step-log-loud"
                      data-level={settled ? "recovered" : state.errors ? "error" : "warn"}
                      title={settled ? "Problems the step hit and fixed before it succeeded" : undefined}>
                  {loud}</span> : null}
        <a className="wf-link wf-step-sec-link" href={href} data-testid="dw-step-log">Step log →</a>
      </header>
      {!state.loaded ? (
        <span className="wf-step-empty">Reading…</span>
      ) : rows.length ? (
        <ol className="wf-step-log">
          {rows.map((r) => (
            <li key={r.id} data-level={r.level} data-testid="dw-step-log-row">
              <time dateTime={r.at} title={utcTitle(r.at)}>{clock(r.at)}</time>
              <span className="wf-step-log-title" data-tip={r.tip} data-user-text>{r.text}</span>
            </li>
          ))}
        </ol>
      ) : (
        <span className="wf-step-empty">{live ? "Nothing logged yet" : "Nothing logged"}</span>
      )}
    </section>
  );
}

/** A titled block of the step panel (Output). */
export function StepSection({ title, testId, children, empty }: {
  title: string; testId: string; children?: React.ReactNode; empty?: string;
}) {
  return (
    <section className="wf-step-sec" data-testid={testId} aria-label={title}>
      <header className="wf-step-sec-head"><span className="eyebrow">{title}</span></header>
      {children ?? <span className="wf-step-empty">{empty}</span>}
    </section>
  );
}

/** "Started 18:01Z · Attempt 2": when it began and whether it is a retry. */
export function stepFacts(step: { started_at?: string | null; attempt?: number }): string[] {
  const out: string[] = [];
  if (step.started_at && clock(step.started_at)) out.push(`Started ${clock(step.started_at)}Z`);
  if ((step.attempt ?? 0) > 1) out.push(`Attempt ${step.attempt}`);
  return out;
}
