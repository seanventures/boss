"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type SVGProps } from "react";
import { formatTime } from "../lib/datetime";
import { Markdown } from "./markdown";
import { pushUrlParam, readUrlParam } from "./url-nav";
import { actorWords } from "../lib/actor-words";

/* ============================================================
   Shared types (structural, match the page feeds)
   ============================================================ */

type Task = {
  id: string;
  title: string;
  status: string;
  priority: string;
  riskTier: number;
  ownerRoleId?: string;
  updatedAt: string;
};
type Job = {
  id: string;
  task_id?: string;
  kind: string;
  status: string;
  attempt: number;
  max_attempts: number;
  last_error?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};
type EventItem = {
  id: string;
  eventType: string;
  actor: string;
  entityType: string;
  entityId: string;
  createdAt: string;
};
type Artifact = { id: string; taskId?: string; artifactType: string; title: string; createdAt: string };
type Decision = { id: string; taskId?: string; question: string; status: string; riskTier: number; createdAt: string };
type Deadletter = {
  id: string;
  reason: string;
  sourceKind: string;
  status: string;
  createdAt: string;
};
type ConversationTurn = {
  role: "system" | "user" | "assistant" | "tool";
  text: string;
  name?: string;
  calls?: { name: string; args: Record<string, unknown> }[];
};
type AgentRun = {
  task_id?: string;
  model?: string;
  tool_calls: number;
  cost_usd_micros: number;
  total_tokens: number;
  transcript: { step: number; tool: string; args: Record<string, unknown>; result_preview: string; deferred?: boolean; resumed?: boolean; thought?: string }[];
  output_text?: string | null;
  status?: string | null;
  conversation?: ConversationTurn[];
};
type TaskOutput = {
  task_id: string;
  kind: string;
  title: string;
  link_kind: "external" | "artifact" | "task";
  url?: string | null;
  artifact_id?: string;
  ref_task_id?: string;
  status?: string;
  preview?: string | null;
  created_at: string;
};

/* ============================================================
   Icons
   ============================================================ */

const mk =
  (d: ReactNode) =>
  (props: SVGProps<SVGSVGElement>) =>
    (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" aria-hidden {...props}>
        {d}
      </svg>
    );

const Ic = {
  task: mk(<><path d="M9 5h6M9 5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2" /><path d="M9 11h6M9 15h4" /></>),
  gear: mk(<><circle cx="12" cy="12" r="3" /><path d="M19 12a7 7 0 0 0-.1-1l2-1.6-2-3.4-2.3 1a7 7 0 0 0-1.7-1L14.5 3h-5l-.4 2.4a7 7 0 0 0-1.7 1l-2.3-1-2 3.4L3 11a7 7 0 0 0 0 2l-2 1.6 2 3.4 2.3-1a7 7 0 0 0 1.7 1l.4 2.4h5l.4-2.4a7 7 0 0 0 1.7-1l2.3 1 2-3.4-2-1.6a7 7 0 0 0 .1-1Z" /></>),
  check: mk(<><circle cx="12" cy="12" r="9" /><path d="M8.5 12.5l2.5 2.5 4.5-5" /></>),
  x: mk(<><circle cx="12" cy="12" r="9" /><path d="M9 9l6 6M15 9l-6 6" /></>),
  clock: mk(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>),
  spinner: mk(<><path d="M12 3a9 9 0 1 0 9 9" /></>),
  retry: mk(<><path d="M3 12a9 9 0 1 0 3-6.7" /><path d="M3 4v5h5" /></>),
  ban: mk(<><circle cx="12" cy="12" r="9" /><path d="M5.6 5.6l12.8 12.8" /></>),
  hand: mk(<><path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V12" /><path d="M11 11.5V4a1.5 1.5 0 0 1 3 0v8" /><path d="M14 11V6.5a1.5 1.5 0 0 1 3 0V14a6 6 0 0 1-6 6h-1a6 6 0 0 1-5-2.7l-2-3a1.6 1.6 0 0 1 2.6-1.8L8 14" /></>),
  doc: mk(<><path d="M6 3h7l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" /><path d="M13 3v5h5M8 13h8M8 17h6" /></>),
  gavel: mk(<><path d="M14 4l6 6-3 3-6-6 3-3Z" /><path d="M11 7l-6 6 3 3 6-6" /><path d="M4 20h8" /></>),
  arrow: mk(<><path d="M5 12h14M13 6l6 6-6 6" /></>),
  wallet: mk(<><rect x="3" y="6" width="18" height="13" rx="2" /><path d="M3 10h18" /><circle cx="16.5" cy="14" r="1.2" /></>),
  bolt: mk(<><path d="M13 2L4 14h7l-1 8 9-12h-7l1-8Z" /></>),
  alert: mk(<><path d="M12 3l9 16H3l9-16Z" /><path d="M12 10v4M12 17.5v.5" /></>),
  shield: mk(<><path d="M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6l7-3Z" /><path d="M9 9l6 6M15 9l-6 6" /></>),
  puzzle: mk(<><path d="M10 4a2 2 0 1 1 4 0v2h2a2 2 0 0 1 2 2v2h-2a2 2 0 1 0 0 4h2v2a2 2 0 0 1-2 2h-2v-2a2 2 0 1 0-4 0v2H8a2 2 0 0 1-2-2v-2H4a2 2 0 1 1 0-4h2V8a2 2 0 0 1 2-2h2V4Z" /></>),
  pulse: mk(<><path d="M3 12h4l2.5 7 4-15 2.5 8H21" /></>),
  dot: mk(<><circle cx="12" cy="12" r="3.5" /></>),
  queue: mk(<><rect x="3" y="4" width="18" height="5" rx="1.5" /><rect x="3" y="11" width="18" height="5" rx="1.5" /><path d="M7 18.5h10" /></>),
  tool: mk(<><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.3 2.3-2-2 2.3-2.3Z" /></>),
  link: mk(<><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.5 1.5" /><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.5-1.5" /></>),
  branch: mk(<><circle cx="6" cy="6" r="2.5" /><circle cx="6" cy="18" r="2.5" /><circle cx="18" cy="8" r="2.5" /><path d="M6 8.5v7M18 10.5c0 3-4 2.5-6 4.5" /></>),
  chat: mk(<><path d="M21 12a8 8 0 0 1-11.5 7.2L4 21l1.8-5.5A8 8 0 1 1 21 12Z" /></>),
  brain: mk(<><path d="M9 5a2.5 2.5 0 0 0-2.5 2.5A2.5 2.5 0 0 0 5 12a2.5 2.5 0 0 0 1.5 2.3A2.5 2.5 0 0 0 9 19a2 2 0 0 0 2-2V5Z" /><path d="M15 5a2.5 2.5 0 0 1 2.5 2.5A2.5 2.5 0 0 1 19 12a2.5 2.5 0 0 1-1.5 2.3A2.5 2.5 0 0 1 15 19a2 2 0 0 1-2-2V5Z" /></>),
  beaker: mk(<><path d="M9 3h6M10 3v6l-5 8a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-8V3" /><path d="M7 14h10" /></>),
  user: mk(<><circle cx="12" cy="8" r="3.5" /><path d="M5 20a7 7 0 0 1 14 0" /></>),
};

type Tone = "green" | "amber" | "red" | "blue" | "muted" | "accent";

/* event-type → icon + label + tone */
function eventMeta(t: string): { Icon: typeof Ic.dot; label: string; tone: Tone } {
  const m: Record<string, { Icon: typeof Ic.dot; label: string; tone: Tone }> = {
    "task.created": { Icon: Ic.task, label: "Task created", tone: "accent" },
    "job.created": { Icon: Ic.gear, label: "Job queued", tone: "muted" },
    "job.leased": { Icon: Ic.hand, label: "Worker leased job", tone: "blue" },
    "job.lease_reclaimed": { Icon: Ic.retry, label: "Stale lease reclaimed", tone: "amber" },
    "job.heartbeat": { Icon: Ic.pulse, label: "Heartbeat", tone: "muted" },
    "job.succeeded": { Icon: Ic.check, label: "Job succeeded", tone: "green" },
    "job.deadlettered": { Icon: Ic.alert, label: "Job dead-lettered", tone: "red" },
    "job.cancelled_budget_blocked": { Icon: Ic.ban, label: "Cancelled, no budget", tone: "red" },
    "plan.created": { Icon: Ic.doc, label: "Plan created", tone: "green" },
    "software_factory.plan_created": { Icon: Ic.doc, label: "Plan created", tone: "green" },
    "software_factory.plan_failed": { Icon: Ic.x, label: "Plan failed", tone: "red" },
    "artifact.created": { Icon: Ic.doc, label: "Artifact created", tone: "muted" },
    "decision.created": { Icon: Ic.gavel, label: "Decision opened", tone: "amber" },
    "decision.approved": { Icon: Ic.check, label: "Decision approved", tone: "green" },
    "decision.rejected": { Icon: Ic.x, label: "Decision rejected", tone: "red" },
    "decision.revision_requested": { Icon: Ic.retry, label: "Revision requested", tone: "amber" },
    "decision.withdrawn": { Icon: Ic.ban, label: "Decision withdrawn", tone: "muted" },
    "decision.follow_on_job_queued": { Icon: Ic.arrow, label: "Follow-on job queued", tone: "blue" },
    "decision.follow_on_blocked": { Icon: Ic.shield, label: "Follow-on blocked", tone: "amber" },
    "budget.reservation_created": { Icon: Ic.wallet, label: "Budget reserved", tone: "amber" },
    "budget.reservation_attached": { Icon: Ic.wallet, label: "Reservation attached", tone: "muted" },
    "budget.reservation_released": { Icon: Ic.wallet, label: "Reservation released", tone: "muted" },
    "budget.reservation_consumed": { Icon: Ic.wallet, label: "Budget consumed", tone: "green" },
    "routing.denied": { Icon: Ic.ban, label: "Routing denied", tone: "red" },
    "policy.denied": { Icon: Ic.shield, label: "Policy denied", tone: "red" },
  };
  if (m[t]) return m[t];
  if (t.startsWith("budget")) return { Icon: Ic.wallet, label: humanize(t), tone: "amber" };
  if (t.startsWith("job")) return { Icon: Ic.gear, label: humanize(t), tone: "muted" };
  if (t.startsWith("decision")) return { Icon: Ic.gavel, label: humanize(t), tone: "amber" };
  if (t.includes("install") || t.startsWith("role") || t.startsWith("pack") || t.startsWith("agent_module")) return { Icon: Ic.puzzle, label: humanize(t), tone: "muted" };
  return { Icon: Ic.dot, label: humanize(t), tone: "muted" };
}

function jobStatusMeta(s: string): { Icon: typeof Ic.dot; tone: Tone; label: string } {
  switch (s) {
    case "succeeded": return { Icon: Ic.check, tone: "green", label: "Succeeded" };
    case "queued": return { Icon: Ic.clock, tone: "muted", label: "Queued" };
    case "leased": case "running": return { Icon: Ic.spinner, tone: "blue", label: s === "leased" ? "Leased" : "Running" };
    case "failed_retryable": return { Icon: Ic.retry, tone: "amber", label: "Retrying" };
    case "failed_terminal": case "deadlettered": return { Icon: Ic.x, tone: "red", label: s === "deadlettered" ? "Dead-lettered" : "Failed" };
    case "cancelled": return { Icon: Ic.ban, tone: "muted", label: "Cancelled" };
    default: return { Icon: Ic.dot, tone: "muted", label: humanize(s) };
  }
}

/* output kind → icon + tone + display label */
function outputMeta(kind: string): { Icon: typeof Ic.dot; tone: Tone; label: string } {
  switch (kind) {
    case "pull_request": return { Icon: Ic.branch, tone: "accent", label: "GitHub PR" };
    case "plan_draft": return { Icon: Ic.doc, tone: "blue", label: "Design doc" };
    case "command_brief": return { Icon: Ic.doc, tone: "muted", label: "Command brief" };
    case "meeting_minutes": return { Icon: Ic.gavel, tone: "accent", label: "Agent meeting, minutes" };
    case "test_report": return { Icon: Ic.beaker, tone: "green", label: "Test report" };
    case "diff_manifest": return { Icon: Ic.doc, tone: "blue", label: "Diff manifest" };
    case "review_packet": return { Icon: Ic.doc, tone: "muted", label: "Review packet" };
    case "release_go_no_go": return { Icon: Ic.shield, tone: "amber", label: "Release go / no-go" };
    case "deployment_rollout": return { Icon: Ic.bolt, tone: "blue", label: "Deployment rollout" };
    case "security_dependency_review": return { Icon: Ic.shield, tone: "amber", label: "Security review" };
    case "incident": return { Icon: Ic.alert, tone: "red", label: "Incident" };
    case "follow_up": return { Icon: Ic.arrow, tone: "blue", label: "Follow-up task" };
    default: return { Icon: Ic.doc, tone: "muted", label: humanize(kind) };
  }
}

/* deliverable display order: PRs and design docs first, child tasks last */
function outputRank(kind: string): number {
  const order: Record<string, number> = {
    pull_request: 0, plan_draft: 1, diff_manifest: 2, test_report: 3,
    review_packet: 4, security_dependency_review: 5, release_go_no_go: 6, deployment_rollout: 7,
    meeting_minutes: 1, command_brief: 40,
  };
  if (kind === "incident") return 90;
  if (kind === "follow_up") return 91;
  return order[kind] ?? 50;
}

/* one turn of the agent <-> LLM conversation, with long content collapsible */
/* clamp a turn's text to ~3 lines with a "… more" toggle */
function TurnText({ text, mono }: { text: string; mono?: boolean }) {
  const [open, setOpen] = useState(false);
  const long = (text || "").length > 220;
  return (
    <>
      <div className={`wf-turn-text${mono ? " mono-soft" : ""}${long && !open ? " clamp" : ""}`}>{mono ? text : <Markdown text={text} />}</div>
      {long ? (
        <span className="wf-agent-more" style={{ cursor: "pointer" }} onClick={() => setOpen((o) => !o)}>
          {open ? "show less ▴" : "… more ▾"}
        </span>
      ) : null}
    </>
  );
}

function ConvoTurn({ turn }: { turn: ConversationTurn }) {
  if (turn.role === "system") {
    return (
      <details className="wf-turn role-system">
        <summary className="wf-turn-role"><Ic.gear /> System prompt <span className="wf-turn-hint">▾</span></summary>
        <div className="wf-turn-text mono-soft">{turn.text}</div>
      </details>
    );
  }
  if (turn.role === "user") {
    return (
      <div className="wf-turn role-user">
        <span className="wf-turn-role"><Ic.user /> Task / prompt</span>
        <TurnText text={turn.text} />
      </div>
    );
  }
  if (turn.role === "assistant") {
    const calls = turn.calls ?? [];
    if (!turn.text && calls.length === 0) return null;
    return (
      <div className="wf-turn role-agent">
        <span className="wf-turn-role"><Ic.brain /> Agent thinking</span>
        {turn.text ? <TurnText text={turn.text} /> : null}
        {calls.length > 0 ? (
          <div className="wf-turn-calls">
            {calls.map((c, i) => {
              const a = Object.keys(c.args || {}).length ? JSON.stringify(c.args) : "";
              return (
                <code key={i} className="mono wf-turn-call">
                  {c.name?.startsWith("mcp_") ? <span className="wf-mcp-badge">MCP</span> : null}
                  → {c.name}({a})
                </code>
              );
            })}
          </div>
        ) : null}
      </div>
    );
  }
  // tool result
  const long = (turn.text || "").length > 220;
  return long ? (
    <details className="wf-turn role-tool">
      <summary className="wf-turn-role"><Ic.tool /> {turn.name} <span className="wf-turn-hint">result ▾</span></summary>
      <div className="wf-turn-text mono-soft">{turn.text}</div>
    </details>
  ) : (
    <div className="wf-turn role-tool">
      <span className="wf-turn-role"><Ic.tool /> {turn.name}</span>
      <div className="wf-turn-text mono-soft">{turn.text}</div>
    </div>
  );
}

/* ============================================================
   Helpers
   ============================================================ */

function humanize(s: string): string {
  return s.replace(/[._]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
function fmtDuration(fromIso: string, toIso: string): string {
  const ms = new Date(toIso).getTime() - new Date(fromIso).getTime();
  if (!isFinite(ms) || ms < 0) return "—";
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}
function fmtTime(iso: string): string {
  // UTC clock, identical on Node SSR and every browser (avoids React #418).
  return formatTime(iso, iso);
}
function Pill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`wf-pill tone-${tone}`}>{children}</span>;
}

/* One agent step, compact by default (one line each, …), expand on click. */
function AgentStepRow({ s }: { s: AgentRun["transcript"][number] }) {
  const [open, setOpen] = useState(false);
  const isMcp = s.tool.startsWith("mcp_");
  const isDeferred = !!s.deferred;
  const argKeys = Object.keys(s.args || {});
  let argsStr = argKeys.length ? JSON.stringify(s.args) : "";
  if (argsStr.length > 2000) argsStr = argsStr.slice(0, 2000) + " …";
  const hasMore =
    (!!s.thought && s.thought.length > 80) ||
    argsStr.length > 70 ||
    (!!s.result_preview && s.result_preview.length > 80);
  return (
    <div className={`wf-agent-step${isDeferred ? " deferred" : ""}${open ? " open" : ""}`}>
      <span className={`wf-ic tone-${isDeferred ? "amber" : isMcp ? "accent" : "blue"}`}><Ic.tool /></span>
      <div
        className="wf-agent-body"
        onClick={() => hasMore && setOpen((o) => !o)}
        style={hasMore ? { cursor: "pointer" } : undefined}
      >
        {s.thought ? <div className="wf-agent-thought">{s.thought}</div> : null}
        <code className="mono wf-agent-tool">
          {isMcp ? <span className="wf-mcp-badge">MCP</span> : null}
          {isDeferred ? <span className="wf-defer-badge"> NEEDS APPROVAL</span> : null}
          {s.resumed ? <span className="wf-resume-badge">↻ APPROVED &amp; RESUMED</span> : null}
          {s.tool}({argsStr})
        </code>
        {s.result_preview ? <div className="wf-agent-result">{s.result_preview}</div> : null}
        {hasMore ? <span className="wf-agent-more">{open ? "show less ▴" : "… more ▾"}</span> : null}
      </div>
    </div>
  );
}

/* ============================================================
   Task Run view (WorkBoard)
   ============================================================ */

export function WorkBoard({
  tasks,
  jobs,
  events,
  artifacts,
  decisions,
  agentRuns = [],
  outputs = [],
  initialTaskId,
}: {
  tasks: Task[];
  jobs: Job[];
  events: EventItem[];
  artifacts: Artifact[];
  decisions: Decision[];
  agentRuns?: AgentRun[];
  outputs?: TaskOutput[];
  initialTaskId?: string;
}) {
  const [selected, setSelected] = useState<string | undefined>(
    initialTaskId ??
      agentRuns[0]?.task_id ?? tasks[0]?.id,
  );
  // Honor a task deep-link (e.g. "View in Work →") on client-side navigation,
  // not just first mount, select the linked task when ?task= actually changes.
  // Guard with a ref so periodic data refreshes (which hand us a NEW `tasks`
  // array reference) don't re-fire this and snap a manual selection back to the
  // URL's task.
  const appliedInitial = useRef<string | undefined>(initialTaskId);
  const detailRef = useRef<HTMLDivElement>(null);
  const focusedDeepLink = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (
      initialTaskId &&
      initialTaskId !== appliedInitial.current
    ) {
      setSelected(initialTaskId);
    }
    appliedInitial.current = initialTaskId;
  }, [initialTaskId]);

  useEffect(() => {
    if (!initialTaskId || selected !== initialTaskId || focusedDeepLink.current === initialTaskId) return;
    focusedDeepLink.current = initialTaskId;
    detailRef.current?.focus({ preventScroll: true });
    detailRef.current?.scrollIntoView({ block: "start", behavior: "auto" });
  }, [initialTaskId, selected]);

  // Select a task AND reflect it in the URL (?task=…) as a back-able history
  // entry; keep state in sync when the user hits Back/Forward.
  const selectTask = useCallback((id?: string) => {
    setSelected(id);
    appliedInitial.current = id;
    pushUrlParam("task", id ?? null);
  }, []);
  useEffect(() => {
    const onPop = () => setSelected(readUrlParam("task"));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const active = tasks.find((t) => t.id === selected) ?? (selected ? undefined : tasks[0]);

  const jobsByTask = (id: string) => jobs.filter((j) => j.task_id === id);
  const decisionsByTask = (id: string) => decisions.filter((d) => d.taskId === id);
  const artifactsByTask = (id: string) => artifacts.filter((a) => a.taskId === id);

  if (tasks.length === 0) {
    return <p className="detail">{initialTaskId
      ? `Task ${initialTaskId} is unavailable in this project's task feed.`
      : "No tasks yet. Issue a CEO command from Overview to start a workflow."}</p>;
  }

  const tJobs = active ? jobsByTask(active.id) : [];
  const tDecisions = active ? decisionsByTask(active.id) : [];
  const tArtifacts = active ? artifactsByTask(active.id) : [];
  const tRun = active ? agentRuns.find((r) => r.task_id === active.id) : undefined;
  const tOutputs = active ? outputs.filter((o) => o.task_id === active.id) : [];

  // related entity ids → timeline
  const relatedIds = new Set<string>([active?.id ?? "", ...tJobs.map((j) => j.id), ...tDecisions.map((d) => d.id), ...tArtifacts.map((a) => a.id)]);
  const timeline = events
    .filter((e) => relatedIds.has(e.entityId))
    .filter((e) => e.eventType !== "job.heartbeat")
    .sort((a, b) => new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime());

  // lifecycle steps
  const hasPlan = tArtifacts.some((a) => a.artifactType === "plan_draft") || tJobs.some((j) => j.kind.startsWith("plan") && j.status === "succeeded");
  const reviewDecision = tDecisions.find((d) => d.question.toLowerCase().includes("review plan"));
  const approved = reviewDecision?.status === "approved";
  const rejected = reviewDecision?.status === "rejected";
  const executed = tJobs.some((j) => !j.kind.startsWith("plan") && (j.status === "succeeded" || j.status.startsWith("failed") || j.status === "deadlettered"));
  const blockedDecision = tDecisions.find((d) => d.status === "pending" && !d.question.toLowerCase().includes("review plan"));

  const steps: { label: string; done: boolean; tone: Tone; sub?: string }[] = [
    { label: "Command received", done: true, tone: "green", sub: "Task created" },
    { label: "Planned", done: hasPlan, tone: hasPlan ? "green" : "muted", sub: hasPlan ? "plan_draft produced" : "waiting for worker" },
    { label: "Decision raised", done: !!reviewDecision, tone: reviewDecision ? (approved ? "green" : rejected ? "red" : "amber") : "muted", sub: reviewDecision ? `review, ${reviewDecision.status}` : "—" },
    { label: "Approved", done: approved, tone: approved ? "green" : rejected ? "red" : "muted", sub: approved ? "human approved" : rejected ? "rejected" : "awaiting approval" },
    { label: "Executed", done: executed, tone: executed ? "green" : "muted", sub: executed ? "follow-on ran" : "—" },
    { label: "Outcome", done: executed, tone: blockedDecision ? "amber" : executed ? "green" : "muted", sub: blockedDecision ? blockedDecision.question : executed ? "complete" : "—" },
  ];

  return (
    <div className="wf-board">
      <div className="wf-tasklist">
        {tasks.map((t) => {
          const js = jobsByTask(t.id);
          const failed = js.some((j) => j.status.startsWith("failed") || j.status === "deadlettered");
          const running = js.some((j) => j.status === "leased" || j.status === "running" || j.status === "queued");
          const tone: Tone = failed ? "red" : running ? "blue" : "green";
          return (
            <button key={t.id} type="button" className={`wf-task${t.id === active?.id ? " active" : ""}`} onClick={() => selectTask(t.id)}>
              <span className={`wf-board-dot tone-${tone}`} />
              <span className="wf-task-body">
                <span className="wf-task-title">{t.title.replace(/^CEO command:\s*/, "")}</span>
                <span className="wf-task-meta">
                  {t.priority.toUpperCase()}, tier {t.riskTier}, {js.length} job{js.length === 1 ? "" : "s"}
                </span>
              </span>
            </button>
          );
        })}
      </div>

      <div className="wf-detail">
        {active ? (
          <>
            <div className="wf-detail-head" ref={detailRef} tabIndex={-1}>
              <div>
                <span className="area-name">Run trace</span>
                <h3 className="wf-detail-title">{active.title.replace(/^CEO command:\s*/, "")}</h3>
                <p className="mono">{active.id}</p>
              </div>
            </div>

            {/* Lifecycle stepper */}
            <div className="wf-steps">
              {steps.map((s, i) => {
                const StepIcon = s.done ? Ic.check : i === steps.findIndex((x) => !x.done) ? Ic.dot : Ic.dot;
                return (
                  <div key={s.label} className={`wf-step${s.done ? " done" : ""}`}>
                    <span className={`wf-step-mark tone-${s.tone}`}><StepIcon /></span>
                    <span className="wf-step-body">
                      <span className="wf-step-label">{s.label}</span>
                      <span className="wf-step-sub">{s.sub}</span>
                    </span>
                  </div>
                );
              })}
            </div>

            {/* Deliverables / Outputs */}
            {tOutputs.length > 0 ? (
              <>
                <div className="wf-section-title">
                  <span className="wf-ic tone-accent"><Ic.link /></span>
                  Deliverables <span className="wf-count">{tOutputs.length}</span>
                  <span style={{ marginLeft: "auto", fontSize: "0.74rem", color: "var(--muted)", fontWeight: 500 }}>
                    where this task&apos;s output landed
                  </span>
                </div>
                <div className="wf-deliverables">
                  {tOutputs
                    .slice()
                    .sort((a, b) => outputRank(a.kind) - outputRank(b.kind))
                    .map((o, i) => {
                      const m = outputMeta(o.kind);
                      if (o.link_kind === "external" && o.url) {
                        return (
                          <a key={i} className="wf-out wf-out-ext" href={o.url} target="_blank" rel="noreferrer">
                            <span className={`wf-ic tone-${m.tone}`}><m.Icon /></span>
                            <span className="wf-out-body">
                              <span className="wf-out-title">{o.title}</span>
                              <span className="wf-out-sub mono">{o.url}</span>
                            </span>
                            <span className="wf-out-go"><Ic.link /> open ↗</span>
                          </a>
                        );
                      }
                      if (o.link_kind === "task" && o.ref_task_id) {
                        return (
                          <button key={i} type="button" className="wf-out" onClick={() => selectTask(o.ref_task_id)}>
                            <span className={`wf-ic tone-${m.tone}`}><m.Icon /></span>
                            <span className="wf-out-body">
                              <span className="wf-out-title">{o.title}</span>
                              <span className="wf-out-sub">{m.label}{o.status ? `, ${o.status}` : ""}</span>
                            </span>
                            <span className="wf-out-go">open task <Ic.arrow /></span>
                          </button>
                        );
                      }
                      // artifact: open its content inline (uri is a filesystem path,
                      // never a browsable link). Fall back to the run's final text.
                      const docText = (o.preview && o.preview.trim())
                        ? o.preview
                        : (o.kind === "plan_draft" ? (tRun?.output_text ?? "") : "");
                      if (docText) {
                        return (
                          <details key={i} className="wf-out wf-out-doc">
                            <summary>
                              <span className={`wf-ic tone-${m.tone}`}><m.Icon /></span>
                              <span className="wf-out-body">
                                <span className="wf-out-title">{o.title}</span>
                                <span className="wf-out-sub">{m.label}, view ▾</span>
                              </span>
                            </summary>
                            <div className="wf-out-doc-body">{docText}</div>
                          </details>
                        );
                      }
                      return o.url ? (
                        <a key={i} className="wf-out" href={o.url} target="_blank" rel="noreferrer">
                          <span className={`wf-ic tone-${m.tone}`}><m.Icon /></span>
                          <span className="wf-out-body">
                            <span className="wf-out-title">{o.title}</span>
                            <span className="wf-out-sub">{m.label}</span>
                          </span>
                          <span className="wf-out-go"><Ic.link /> open ↗</span>
                        </a>
                      ) : (
                        <div key={i} className="wf-out wf-out-static">
                          <span className={`wf-ic tone-${m.tone}`}><m.Icon /></span>
                          <span className="wf-out-body">
                            <span className="wf-out-title">{o.title}</span>
                            <span className="wf-out-sub">{m.label}</span>
                          </span>
                        </div>
                      );
                    })}
                </div>
              </>
            ) : null}

            {/* Jobs */}
            <div className="wf-section-title">Jobs <span className="wf-count">{tJobs.length}</span></div>
            {tJobs.length === 0 ? (
              <p className="detail">No jobs spawned yet.</p>
            ) : (
              <div className="wf-jobs">
                {tJobs
                  .slice()
                  .sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime())
                  .map((j) => {
                    const m = jobStatusMeta(j.status);
                    const err = j.last_error && typeof j.last_error === "object" ? (j.last_error["message"] as string) : undefined;
                    return (
                      <div key={j.id} className="wf-job">
                        <span className={`wf-ic tone-${m.tone}`}><m.Icon /></span>
                        <span className="wf-job-kind"><code className="mono">{j.kind}</code></span>
                        <Pill tone={m.tone}>{m.label}</Pill>
                        <span className="wf-job-dur"><Ic.clock /> {fmtDuration(j.created_at, j.updated_at)}</span>
                        <span className="wf-job-attempt">try {j.attempt}/{j.max_attempts}</span>
                        {err ? <span className="wf-job-err">{err}</span> : null}
                      </div>
                    );
                  })}
              </div>
            )}

            {/* Agent steps */}
            {tRun && tRun.transcript.length > 0 ? (
              <>
                <div className="wf-section-title">
                  <span className="wf-ic tone-blue"><Ic.tool /></span>
                  Agent steps <span className="wf-count">{tRun.tool_calls} tool{tRun.tool_calls === 1 ? "" : "s"}</span>
                  <span style={{ marginLeft: "auto", fontSize: "0.76rem", color: "var(--muted)", fontWeight: 500 }}>
                    {tRun.total_tokens} tokens, ${(tRun.cost_usd_micros / 1e6).toFixed(4)}, {tRun.model}
                  </span>
                </div>
                <div className="wf-agent">
                  {tRun.transcript.map((s, i) => (
                    <AgentStepRow key={i} s={s} />
                  ))}
                </div>
              </>
            ) : null}

            {/* Full agent <-> LLM conversation (collapsed) */}
            {tRun && tRun.conversation && tRun.conversation.length > 0 ? (
              <details className="wf-convo-wrap">
                <summary className="wf-convo-summary">
                  <span className="wf-ic tone-accent"><Ic.chat /></span>
                  <span className="wf-convo-title">Conversation</span>
                  <span className="wf-count">{tRun.conversation.length} turns</span>
                  <span className="wf-convo-hint">agent ⇄ LLM, click to expand the full back-and-forth ▾</span>
                </summary>
                <div className="wf-convo">
                  {tRun.conversation.map((turn, i) => (
                    <ConvoTurn key={i} turn={turn} />
                  ))}
                  {tRun.output_text ? (
                    <div className="wf-turn role-answer">
                      <span className="wf-turn-role"><Ic.check /> Final answer</span>
                      <TurnText text={tRun.output_text} />
                    </div>
                  ) : null}
                </div>
              </details>
            ) : null}

            {/* Timeline */}
            <div className="wf-section-title">Timeline</div>
            <div className="wf-timeline">
              {timeline.map((e, i) => {
                const m = eventMeta(e.eventType);
                const gap = i > 0 ? fmtDuration(timeline[i - 1].createdAt, e.createdAt) : null;
                return (
                  <div key={e.id} className="wf-tl">
                    <span className={`wf-ic tone-${m.tone}`}><m.Icon /></span>
                    <span className="wf-tl-body">
                      <span className="wf-tl-label">{m.label}</span>
                      <span className="wf-tl-meta">{actorWords(e.actor)} ·{fmtTime(e.createdAt)}{gap ? `, +${gap}` : ""}</span>
                    </span>
                  </div>
                );
              })}
            </div>

            {/* Outcome */}
            <div className="wf-section-title">Outcome</div>
            <div className="wf-outcome">
              {tArtifacts.map((a) => (
                <span key={a.id} className="wf-outcome-chip"><Ic.doc /> {humanize(a.artifactType)}</span>
              ))}
              {tDecisions.map((d) => {
                const tone: Tone = d.status === "approved" ? "green" : d.status === "rejected" ? "red" : d.status === "pending" ? "amber" : "muted";
                return (
                  <span key={d.id} className={`wf-outcome-chip tone-${tone}`}><Ic.gavel /> {d.question.slice(0, 40)}, {d.status}</span>
                );
              })}
              {tArtifacts.length === 0 && tDecisions.length === 0 ? <p className="detail">No outcome yet.</p> : null}
            </div>
          </>
        ) : <div className="wf-detail-head" ref={detailRef} tabIndex={-1} role="status">
          <div>
            <span className="area-name">Run trace</span>
            <h3 className="wf-detail-title">Task unavailable</h3>
            <p className="detail">{selected} is unavailable in this project&apos;s task feed.</p>
          </div>
        </div>}
      </div>
    </div>
  );
}

/* ============================================================
   Workflow Health (monitoring dashboard)
   ============================================================ */

export function WorkflowHealth({
  jobStatusCounts,
  jobKindCounts,
  staleLeaseJobIds,
  blockerCount,
  recommendedActions,
  deadletters,
  openTasks,
  pendingDecisions,
}: {
  jobStatusCounts: Record<string, number>;
  jobKindCounts: Record<string, number>;
  staleLeaseJobIds: string[];
  blockerCount: number;
  recommendedActions: string[];
  deadletters: Deadletter[];
  openTasks: number;
  pendingDecisions: number;
}) {
  const c = (k: string) => jobStatusCounts[k] ?? 0;
  const running = c("leased") + c("running");
  const failed = c("failed_terminal") + c("failed_retryable") + c("deadlettered");

  const stats: { label: string; value: number; tone: Tone; Icon: typeof Ic.dot }[] = [
    { label: "Running", value: running, tone: running > 0 ? "blue" : "muted", Icon: Ic.spinner },
    { label: "Queued", value: c("queued"), tone: c("queued") > 0 ? "amber" : "muted", Icon: Ic.clock },
    { label: "Succeeded", value: c("succeeded"), tone: "green", Icon: Ic.check },
    { label: "Failed", value: failed, tone: failed > 0 ? "red" : "muted", Icon: Ic.x },
    { label: "Dead-letters", value: deadletters.length, tone: deadletters.length > 0 ? "red" : "muted", Icon: Ic.alert },
    { label: "Stale leases", value: staleLeaseJobIds.length, tone: staleLeaseJobIds.length > 0 ? "amber" : "muted", Icon: Ic.retry },
    { label: "Open tasks", value: openTasks, tone: "muted", Icon: Ic.task },
    { label: "Decisions", value: pendingDecisions, tone: pendingDecisions > 0 ? "amber" : "muted", Icon: Ic.gavel },
  ];

  const kinds = Object.entries(jobKindCounts).sort((a, b) => b[1] - a[1]);
  const maxKind = Math.max(1, ...kinds.map(([, v]) => v));
  const allClear = failed === 0 && deadletters.length === 0 && staleLeaseJobIds.length === 0 && blockerCount === 0;

  return (
    <div className="wf-health">
      <div className={`wf-banner tone-${allClear ? "green" : failed > 0 || deadletters.length > 0 ? "red" : "amber"}`}>
        <span className="wf-ic"><Ic.shield /></span>
        {allClear
          ? "All workflows healthy, no failures, stale leases, or blockers."
          : `${failed} failed, ${deadletters.length} dead-letter${deadletters.length === 1 ? "" : "s"}, ${staleLeaseJobIds.length} stale lease${staleLeaseJobIds.length === 1 ? "" : "s"}, ${blockerCount} blocker${blockerCount === 1 ? "" : "s"} need attention.`}
      </div>

      <div className="wf-stats">
        {stats.map((s) => (
          <div key={s.label} className={`wf-stat tone-${s.tone}`}>
            <span className="wf-stat-ic"><s.Icon /></span>
            <span className="wf-stat-val">{s.value}</span>
            <span className="wf-stat-label">{s.label}</span>
          </div>
        ))}
      </div>

      <div className="wf-health-split">
        <div className="bx-card wf-pad">
          <div className="wf-section-title">Jobs by kind</div>
          {kinds.length === 0 ? (
            <p className="detail">No jobs recorded yet.</p>
          ) : (
            <div className="wf-bars">
              {kinds.map(([k, v]) => (
                <div key={k} className="wf-bar-row">
                  <code className="mono">{k}</code>
                  <span className="wf-bar"><span className="wf-bar-fill" style={{ width: `${(v / maxKind) * 100}%` }} /></span>
                  <span className="wf-bar-val">{v}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="bx-card wf-pad">
          <div className="wf-section-title">Recent failures</div>
          {deadletters.length === 0 ? (
            <p className="detail">No failed jobs or effects are isolated for review.</p>
          ) : (
            <div className="wf-fails">
              {deadletters.slice(0, 5).map((d) => (
                <div key={d.id} className="wf-fail">
                  <span className="wf-ic tone-red"><Ic.alert /></span>
                  <span className="wf-fail-body">
                    <span className="wf-fail-reason">{d.reason}</span>
                    <span className="wf-tl-meta">{d.sourceKind}, {d.status}, {fmtTime(d.createdAt)}</span>
                  </span>
                </div>
              ))}
            </div>
          )}
          {recommendedActions.length > 0 ? (
            <>
              <div className="wf-section-title" style={{ marginTop: 14 }}>Recommended actions</div>
              <ul className="detail wf-recs">
                {recommendedActions.map((r) => (
                  <li key={r}><Ic.arrow /> {r}</li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
