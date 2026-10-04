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
import React, { useEffect, useState } from "react";

import type { Act, AvatarVariant } from "../../lib/agent-world-types";
import type { StepView } from "../../lib/workflow-editor-types";
import { fetchActivityLog } from "../../lib/activity-log-client";
import type { ActivityLogItem } from "../../lib/activity-log";
import { usd } from "../../lib/money";
import { isLoopbackPreviewUrl } from "../../lib/code-tasks";
import { consoleHref } from "../url-intent";
import { laneAgentOf, type LaneAgent } from "../agents/chain-model";
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
  return consoleHref(search, { area: "activity", wfr: workflowRunId, step: stepId,
                               step_name: names.step || null, run_name: names.run || null });
}

/* ── Who ─────────────────────────────────────────────────────────────────────────── */

export type StepActor = { roleKey: string | null; variant: AvatarVariant; ran: boolean };

/** The one answer to "who does this step": who RAN it, else who the workflow assigns it to. */
export function stepActor(ran: LaneAgent | undefined, planned: StepView["agent"] | undefined): StepActor | null {
  if (ran) return { roleKey: ran.roleKey, variant: ran.variant, ran: true };
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
  return nameOf(actor.roleKey, actor.variant).name;
}

/* ── Output ──────────────────────────────────────────────────────────────────────── */

export type OutputFact = { label: string; value: string; tip?: string };

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
                           opts: { noteShownAbove: boolean }): { facts: OutputFact[]; details: OutputFact[] } {
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
    list.push({ label, value: WORDS[text] ?? text, ...(tip ? { tip } : {}) });
  };
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
    add(details, key.replace(/_/g, " "), value);
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
    const took = Number.isFinite(secs) && secs > 0
      ? (secs < 60 ? ` in ${Math.round(secs)}s` : ` in ${Math.round(secs / 60)}m`) : "";
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
  const ordered = rows.map((r, n) => ({ r, n })).sort((a, b) => loud(a.r) - loud(b.r) || a.n - b.n).map((x) => x.r);
  return {
    rows: ordered, model, cost, calls: calls.length,
    errors: rows.filter((r) => r.level === "error").length,
    warnings: rows.filter((r) => r.level === "warn").length,
  };
}

/** One read of the step's log; re-read while the step is live, never in a hidden tab. */
export function useStepActivity(projectId: string, workflowRunId: string | undefined, stepId: string,
                                live: boolean, version: string): StepActivityState {
  const [state, setState] = useState<StepActivityState>(EMPTY);
  useEffect(() => {
    if (!workflowRunId) { setState({ ...EMPTY, loaded: true }); return; }
    let alive = true;
    let timer: number | undefined;
    const load = async () => {
      const log = await fetchActivityLog(projectId, {
        workflow_run_id: workflowRunId, step_id: stepId, limit: 100,
      });
      if (alive) setState({ ...summariseStepActivity(log.items ?? []), loaded: true });
    };
    const tick = () => {
      timer = window.setTimeout(async () => {
        if (!alive) return;
        if (typeof document === "undefined" || document.visibilityState !== "hidden") await load();
        if (alive) tick();
      }, 5000);
    };
    void load();
    if (live) tick();
    return () => { alive = false; if (timer) window.clearTimeout(timer); };
  }, [projectId, workflowRunId, stepId, live, version]);
  return state;
}

/** "18:02", in UTC, as the run's own "started 4 Oct 18:01Z" says it. */
export function clock(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const d = new Date(t);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}

/* ── The panel's parts ───────────────────────────────────────────────────────────── */

/** Done by / Doing / Queued for / Assigned to / Skipped, from the step's own state. */
export function actorVerb(phase: string, status: string): string {
  if (phase === "skipped") return "Skipped";
  if (phase === "ahead") return "Assigned to";
  if (phase === "now") return ["ready", "queued", "pending"].includes(status) ? "Queued for" : "Doing";
  return "Done by";
}

/** "Done by <face> <name> · $0.03 · model", the step's facts, and the agent's card below them. */
export function StepDoneBy({ act, approval, output, phase, status, planned, viewer, projectId, failure,
                             question, cost, calls, model, onAsk, stepId, facts, skipped,
                             askBossTip = "Ask about this step" }: {
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
  const named = actor ? nameOf(actor.roleKey, actor.variant) : null;
  const priced = !!act || calls > 0;
  const spent = act?.costUsd || cost;
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
          {priced || model ? (
            <span className="wf-doneby-facts">
              {priced ? <span className="wf-doneby-fact" data-testid="dw-step-cost">{usd(spent)}</span> : null}
              {model ? <span className="wf-doneby-fact" data-testid="dw-step-model" data-user-text>{model}</span> : null}
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
                <dd data-tip={f.value} data-user-text>{f.value}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </>
  );
}

/** The step's log as sentences, errors first, and the way into its whole log. */
export function StepActivity({ state, href, live }: { state: StepActivityState; href: string; live: boolean }) {
  const rows = state.rows.slice(0, STEP_ACTIVITY_ROWS);
  const loud = [state.errors ? `${state.errors} error${state.errors === 1 ? "" : "s"}` : "",
                state.warnings ? `${state.warnings} warning${state.warnings === 1 ? "" : "s"}` : ""]
    .filter(Boolean).join(" · ");
  return (
    <section className="wf-step-sec" data-testid="dw-step-activity" aria-label="Activity">
      <header className="wf-step-sec-head">
        <span className="eyebrow">Activity</span>
        {loud ? <span className="wf-step-sec-loud" data-testid="dw-step-log-loud"
                      data-level={state.errors ? "error" : "warn"}>{loud}</span> : null}
        <a className="wf-link wf-step-sec-link" href={href} data-testid="dw-step-log">Step log →</a>
      </header>
      {!state.loaded ? (
        <span className="wf-step-empty">Reading…</span>
      ) : rows.length ? (
        <ol className="wf-step-log">
          {rows.map((r) => (
            <li key={r.id} data-level={r.level} data-testid="dw-step-log-row">
              <time dateTime={r.at}>{clock(r.at)}</time>
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
