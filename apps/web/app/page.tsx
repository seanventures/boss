import {
  createReplayBundleAction,
  installPackAction,
  queueOutboxMessageAction,
  refreshHealthSnapshotAction,
  recordSelfImprovementCanaryAction,
  recordSelfImprovementRolloutAction,
  resolveDecisionAction,
  rollbackSelfImprovementProposalAction,
  submitSelfImprovementProposalAction,
  submitOutboxDraftAction,
  submitCommandAction,
} from "./actions";
import { fetchAuditSummary } from "../lib/audit";
import { fetchArtifactFeed } from "../lib/artifacts";
import { fetchInboxFeed, fetchOutboxFeed } from "../lib/comms";
import { fetchDeadletterFeed } from "../lib/deadletters";
import { fetchDecisionFeed, type DecisionFeedItem } from "../lib/decisions";
import { fetchEntitlementFeed } from "../lib/entitlements";
import { fetchActivityFeed } from "../lib/events";
import { fetchHealthSnapshotFeed } from "../lib/health-snapshots";
import { fetchMetatronQuery } from "../lib/metatron";
import { fetchPackFeed } from "../lib/packs";
import { fetchReceiptFeed } from "../lib/receipts";
import { fetchRoleFeed } from "../lib/roles";
import { fetchRoutingLogFeed } from "../lib/routing";
import { fetchSelfImprovementFeed } from "../lib/self-improvement";
import { fetchSkillFeed } from "../lib/skills";
import { fetchStatus } from "../lib/status";
import { fetchTaskFeed } from "../lib/tasks";

function formatMicros(amount: unknown): string {
  if (typeof amount !== "number") {
    return "n/a";
  }
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(amount / 1_000_000);
}

function formatRecommendation(decision: DecisionFeedItem): string {
  const action = decision.recommendation.action;
  if (typeof action === "string" && action) {
    return action.replaceAll("_", " ");
  }
  return "review required";
}

function formatDeadline(deadlineAt?: string): string {
  if (!deadlineAt) {
    return "No deadline";
  }
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(deadlineAt));
}

function formatDateTime(value?: string): string {
  if (!value) {
    return "Unknown time";
  }
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function titleCaseToken(value: string): string {
  return value.replaceAll("_", " ");
}

function riskLabel(risk: Record<string, unknown>): string {
  const title = typeof risk.title === "string" ? risk.title : "Unspecified risk";
  const severity =
    typeof risk.severity === "string" ? risk.severity.toUpperCase() : "UNKNOWN";
  return `${severity} / ${title}`;
}

function decisionStatusDetail(decision: DecisionFeedItem): string {
  if (decision.status === "delegated" && decision.delegatedTo) {
    return `Delegated to ${decision.delegatedTo}${
      decision.delegatedBy ? ` by ${decision.delegatedBy}` : ""
    }${decision.delegatedAt ? ` at ${formatDeadline(decision.delegatedAt)}` : ""}.`;
  }
  if (decision.status === "revision_requested") {
    return decision.resolutionNote
      ? `Revision requested: ${decision.resolutionNote}`
      : "Revision was requested on this decision.";
  }
  if (decision.status === "executed") {
    return decision.externalRef
      ? `Executed with evidence: ${decision.externalRef}`
      : "This decision has been marked executed.";
  }
  if (decision.approvedBy) {
    return `Approved by ${decision.approvedBy} at ${formatDeadline(
      decision.approvedAt,
    )}.`;
  }
  return "This card remains explicit until it is approved, rejected, delegated, revised, or safely expired.";
}

type HomePageProps = {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
};

function getParam(value: string | string[] | undefined): string | undefined {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  if (Array.isArray(value) && typeof value[0] === "string" && value[0].trim()) {
    return value[0].trim();
  }
  return undefined;
}

export default async function HomePage({ searchParams }: HomePageProps) {
  const resolvedSearchParams = searchParams ? await searchParams : undefined;
  const metatronQuestion = getParam(resolvedSearchParams?.q);
  const metatronTaskId = getParam(resolvedSearchParams?.task_id);
  const metatronDecisionId = getParam(resolvedSearchParams?.decision_id);
  const [
    status,
    decisions,
    tasks,
    artifacts,
    events,
    healthSnapshots,
    deadletters,
    packs,
    roles,
    entitlements,
    routingLogs,
    skills,
    receipts,
    auditSummary,
    selfImprovement,
    outbox,
    inbox,
    metatron,
  ] =
    await Promise.all([
      fetchStatus(),
      fetchDecisionFeed(),
      fetchTaskFeed(),
      fetchArtifactFeed(),
      fetchActivityFeed(),
      fetchHealthSnapshotFeed(),
      fetchDeadletterFeed(),
      fetchPackFeed(),
      fetchRoleFeed(),
      fetchEntitlementFeed(),
      fetchRoutingLogFeed(),
      fetchSkillFeed(),
      fetchReceiptFeed(),
      fetchAuditSummary(),
      fetchSelfImprovementFeed(),
      fetchOutboxFeed(),
      fetchInboxFeed(),
      fetchMetatronQuery(
        metatronQuestion,
        metatronTaskId,
        metatronDecisionId,
      ),
    ]);

  const deadletterCount = deadletters.items.length;
  const disableCommandEntry = !status.apiReachable || !status.databaseConnected;
  const installedCorePack = packs.items.find((pack) => pack.installed);

  return (
    <main className="shell">
      <div className="frame">
        <section className="hero">
          <div className="hero-grid">
            <div className="hero-copy">
              <span className="eyebrow">Business Operating System Sentinel</span>
              <h1>Executive control for autonomous work.</h1>
              <p>
                Real GitHub and OpenAI integrations are gated behind receipts,
                reservations, and approvals. The console now submits CEO
                commands directly to the live control plane and surfaces the
                resulting tasks, artifacts, events, and deadletters.
              </p>
              <div className="hero-pills">
                <span className={`pill${status.apiReachable ? "" : " warn"}`}>
                  {status.apiReachable ? "API reachable" : "API unavailable"}
                </span>
                <span className={`pill${deadletterCount > 0 ? " warn" : ""}`}>
                  {deadletterCount > 0
                    ? `${deadletterCount} deadletter${deadletterCount === 1 ? "" : "s"}`
                    : "No deadletters"}
                </span>
              </div>
            </div>

            <form action={submitCommandAction} className="composer">
              <div>
                <span className="eyebrow">CEO Command</span>
                <h2>Issue a real outcome.</h2>
              </div>
              <label className="field">
                <span>Outcome</span>
                <textarea
                  name="command"
                  rows={5}
                  className="textarea"
                  placeholder="Build the next operator visibility slice and validate it through the sandbox workflow."
                  disabled={disableCommandEntry}
                  required
                />
              </label>
              <div className="input-grid">
                <label className="field">
                  <span>Deadline</span>
                  <input
                    name="deadline"
                    className="input"
                    placeholder="This week"
                    disabled={disableCommandEntry}
                  />
                </label>
              </div>
              <button className="button" disabled={disableCommandEntry} type="submit">
                Submit command
              </button>
              <p className="detail">
                {disableCommandEntry
                  ? status.blockedReason ??
                    "The control plane is not ready to accept commands."
                  : "Creates a task, command brief, and queued planning job through the API."}
              </p>
            </form>
          </div>
        </section>

        <section className="stack-grid">
          <article className="stack-card">
            <div className="section-head">
              <div>
                <span className="eyebrow">Metatron</span>
                <h2>Status Query</h2>
              </div>
            </div>
            <form method="get" className="composer">
              <label className="field">
                <span>Question</span>
                <input
                  className="input"
                  name="q"
                  defaultValue={metatronQuestion}
                  placeholder="What changed, what is blocked, and what should I do next?"
                />
              </label>
              <div className="input-grid">
                <label className="field">
                  <span>Task scope</span>
                  <input
                    className="input"
                    name="task_id"
                    defaultValue={metatronTaskId}
                    placeholder="Optional task id"
                  />
                </label>
                <label className="field">
                  <span>Decision scope</span>
                  <input
                    className="input"
                    name="decision_id"
                    defaultValue={metatronDecisionId}
                    placeholder="Optional decision id"
                  />
                </label>
              </div>
              <button className="button" type="submit">
                Ask Metatron
              </button>
              <p className="detail">
                Answers are generated from live control-plane state, include
                freshness, and return explicit references.
              </p>
            </form>
            {metatron ? (
              <div className="decision-grid">
                <div>
                  <h4>Answer</h4>
                  <p>{metatron.answer}</p>
                  <p className="detail">
                    {metatron.apiReachable
                      ? `Fresh as of ${formatDateTime(metatron.freshnessAt)}.`
                      : metatron.blockedReason}
                  </p>
                </div>
                <div>
                  <h4>Suggested actions</h4>
                  {metatron.suggestedActions.length > 0 ? (
                    <ul className="detail">
                      {metatron.suggestedActions.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="detail">No suggested action.</p>
                  )}
                </div>
                <div>
                  <h4>References</h4>
                  {metatron.references.length > 0 ? (
                    <div className="feed-list">
                      {metatron.references.map((reference) => (
                        <div key={`${reference.objectType}:${reference.objectId}`}>
                          <div>{reference.title}</div>
                          <p className="detail">
                            {reference.objectType} / {reference.objectId}
                            {reference.status ? ` / ${reference.status}` : ""}
                          </p>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="detail">No references returned.</p>
                  )}
                </div>
              </div>
            ) : (
              <p className="detail">
                Ask a scoped question to get a freshness-stamped answer from the
                live control plane.
              </p>
            )}
          </article>
        </section>

        <section className="grid">
          <article>
            <h2>Project</h2>
            <div className="metric">{status.projectName}</div>
            <p className="detail">
              {status.orgName} / {status.workspaceName} / {status.projectId}
            </p>
          </article>
          <article>
            <h2>Database</h2>
            <div className="metric">
              {status.databaseConnected ? "Connected" : "Offline"}
            </div>
            <p className="detail">
              Budget and workflow state are sourced from Postgres.
            </p>
          </article>
          <article>
            <h2>Budget State</h2>
            <div className="metric">{status.budgetState}</div>
            <p className="detail">
              Real model and GitHub actions will fail closed without an active
              reservation.
            </p>
          </article>
          <article>
            <h2>Pending Decisions</h2>
            <div className="metric">{status.pendingDecisions}</div>
            <p className="detail">
              Decisions stay explicit when credentials, budget, or approvals are
              missing.
            </p>
          </article>
          <article>
            <h2>Active Jobs</h2>
            <div className="metric">{status.activeJobs}</div>
            <p className="detail">
              Leased or running jobs visible from the current control-plane state.
            </p>
          </article>
        </section>

        <section className="grid">
          <article>
            <h2>Open Tasks</h2>
            <div className="metric">{status.openTasks}</div>
            <p className="detail">
              Recent task state is shown below with direct IDs from the API.
            </p>
          </article>
          <article>
            <h2>Blocking State</h2>
            <div className="metric">
              {status.blockedReason ? "Visible" : "Clear"}
            </div>
            <p className="detail">
              {status.blockedReason ??
                "No bootstrap blocker reported by the control plane."}
            </p>
          </article>
          <article>
            <h2>Artifacts</h2>
            <div className="metric">{artifacts.items.length}</div>
            <p className="detail">
              Recent review packets, plans, merge results, and command briefs.
            </p>
          </article>
          <article>
            <h2>Events</h2>
            <div className="metric">{events.items.length}</div>
            <p className="detail">
              What changed is derived from immutable event records.
            </p>
          </article>
          <article>
            <h2>Deadletters</h2>
            <div className="metric">{status.deadletters}</div>
            <p className="detail">
              Incidents must become visible deadletters or blocked states, never silent drops.
            </p>
          </article>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">Health Snapshots</span>
              <h2>Durable system state for what changed and what is risky.</h2>
            </div>
            <p className="status-meta">
              Metatron-style visibility now has a canonical snapshot object with queue, budget, workload, and incident state.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Live top risks</h3>
              <p className="detail">
                {status.latestSnapshotAt
                  ? `Latest snapshot at ${formatDateTime(status.latestSnapshotAt)}.`
                  : "No persisted health snapshot yet. Refresh one from the console."}
              </p>
              <form action={refreshHealthSnapshotAction}>
                <button className="button secondary" type="submit">
                  Refresh snapshot
                </button>
              </form>
              <div className="feed-list">
                {status.topRisks.length === 0 ? (
                  <p className="detail">No current risk summary available.</p>
                ) : (
                  status.topRisks.map((risk, index) => (
                    <div className="feed-row" key={`risk-${index}`}>
                      <div>
                        <h4>{riskLabel(risk)}</h4>
                        <p className="detail">
                          {typeof risk.detail === "string"
                            ? risk.detail
                            : "No additional risk detail."}
                        </p>
                      </div>
                      <span className="pill">
                        {typeof risk.category === "string"
                          ? risk.category
                          : "status"}
                      </span>
                    </div>
                  ))
                )}
              </div>
            </article>

            <article className="stack-card">
              <h3>Recent snapshots</h3>
              {!healthSnapshots.apiReachable ? (
                <p className="detail">
                  {healthSnapshots.blockedReason ?? "Health snapshot API is unavailable."}
                </p>
              ) : healthSnapshots.items.length === 0 ? (
                <p className="detail">No health snapshots have been persisted yet.</p>
              ) : (
                <div className="feed-list">
                  {healthSnapshots.items.map((snapshot) => (
                    <div className="feed-row" key={snapshot.id}>
                      <div>
                        <h4>
                          {snapshot.budgetState} / {formatDateTime(snapshot.createdAt)}
                        </h4>
                        <p className="detail">
                          {Number(snapshot.summary.open_tasks ?? 0)} open tasks /{" "}
                          {Number(snapshot.summary.pending_decisions ?? 0)} pending decisions /{" "}
                          {Number(snapshot.summary.deadletters ?? 0)} deadletters
                        </p>
                        <p className="detail">
                          Generated by {snapshot.generatedBy}
                        </p>
                      </div>
                      <code className="mono">{snapshot.id}</code>
                    </div>
                  ))}
                </div>
              )}
            </article>
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">Audit & Replay</span>
              <h2>Exportable lineage for real runs.</h2>
            </div>
            <p className="status-meta">
              Every receipt, reservation, routing choice, and event should be reconstructable through the API.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Audit export snapshot</h3>
              {!auditSummary.apiReachable ? (
                <p className="detail">
                  {auditSummary.blockedReason ?? "Audit export is unavailable."}
                </p>
              ) : (
                <>
                  <div className="feed-list">
                    <div className="feed-row">
                      <div>
                        <h4>{auditSummary.counts.tasks} tasks</h4>
                        <p className="detail">
                          {auditSummary.counts.jobs} jobs / {auditSummary.counts.decisions} decisions / {auditSummary.counts.artifacts} artifacts
                        </p>
                      </div>
                      <span className="pill">{auditSummary.counts.receipts} receipts</span>
                    </div>
                    <div className="feed-row">
                      <div>
                        <h4>{auditSummary.counts.budgetReservations} reservations</h4>
                        <p className="detail">
                          {auditSummary.counts.routingLogs} routing logs / {auditSummary.counts.events} events
                        </p>
                      </div>
                      <span className="pill">
                        {auditSummary.generatedAt
                          ? formatDateTime(auditSummary.generatedAt)
                          : "live"}
                      </span>
                    </div>
                  </div>
                  <form action={createReplayBundleAction}>
                    <button className="button secondary" type="submit">
                      Create replay bundle
                    </button>
                  </form>
                </>
              )}
            </article>

            <article className="stack-card">
              <h3>Recent receipts</h3>
              {!receipts.apiReachable ? (
                <p className="detail">
                  {receipts.blockedReason ?? "Receipt API is unavailable."}
                </p>
              ) : receipts.items.length === 0 ? (
                <p className="detail">No side-effect receipts have been recorded yet.</p>
              ) : (
                <div className="feed-list">
                  {receipts.items.map((receipt) => (
                    <div className="feed-row" key={receipt.id}>
                      <div>
                        <h4>{titleCaseToken(receipt.effectKind)}</h4>
                        <p className="detail">
                          {receipt.status} / {formatDateTime(receipt.createdAt)}
                        </p>
                        <p className="detail">
                          {receipt.providerRef ?? receipt.dedupeKey}
                        </p>
                      </div>
                      <code className="mono">
                        {receipt.createdByJobId ?? receipt.id}
                      </code>
                    </div>
                  ))}
                </div>
              )}
            </article>
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">Self-Improvement</span>
              <h2>Change BOSS only through governed rollout steps.</h2>
            </div>
            <p className="status-meta">
              Every self-improvement now becomes a task, proposal artifact, approval gate, canary report, rollout record, and rollback record.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Propose an improvement</h3>
              <form action={submitSelfImprovementProposalAction} className="composer compact">
                <label className="field">
                  <span>Title</span>
                  <input
                    name="title"
                    className="input"
                    placeholder="Tighten decision expiry defaults"
                    required
                  />
                </label>
                <label className="field">
                  <span>Summary</span>
                  <textarea
                    name="summary"
                    rows={3}
                    className="textarea"
                    placeholder="Describe the product or control-plane improvement."
                    required
                  />
                </label>
                <label className="field">
                  <span>PR or diff reference</span>
                  <input
                    name="change_ref"
                    className="input"
                    placeholder="https://github.com/seanheiney/boss/pull/123"
                    required
                  />
                </label>
                <label className="field">
                  <span>Diff summary</span>
                  <textarea
                    name="diff_summary"
                    rows={3}
                    className="textarea"
                    placeholder="Summarize what will change and why."
                    required
                  />
                </label>
                <label className="field">
                  <span>Expected impact</span>
                  <textarea
                    name="expected_impact"
                    rows={3}
                    className="textarea"
                    placeholder={"One item per line\nLower decision latency\nStronger audit visibility"}
                  />
                </label>
                <label className="field">
                  <span>Test plan</span>
                  <textarea
                    name="test_plan"
                    rows={3}
                    className="textarea"
                    placeholder={"One item per line\nAPI tests\nSmoke suite"}
                  />
                </label>
                <label className="field">
                  <span>Canary scope</span>
                  <textarea
                    name="canary_scope"
                    rows={2}
                    className="textarea"
                    placeholder={"One item per line\ndogfood workspace only"}
                  />
                </label>
                <label className="field">
                  <span>Rollback plan</span>
                  <textarea
                    name="rollback_plan"
                    rows={2}
                    className="textarea"
                    placeholder={"One item per line\nRevert PR\nDisable flag"}
                  />
                </label>
                <label className="field">
                  <span>Success metrics</span>
                  <textarea
                    name="success_metrics"
                    rows={2}
                    className="textarea"
                    placeholder={"One item per line\nNo unsafe expirations"}
                  />
                </label>
                <button className="button secondary" type="submit">
                  Submit improvement proposal
                </button>
              </form>
            </article>

            <article className="stack-card">
              <h3>Proposal pipeline</h3>
              {!selfImprovement.apiReachable ? (
                <p className="detail">
                  {selfImprovement.blockedReason ?? "Self-improvement API is unavailable."}
                </p>
              ) : selfImprovement.items.length === 0 ? (
                <p className="detail">No self-improvement proposals have been created yet.</p>
              ) : (
                <div className="feed-list">
                  {selfImprovement.items.map((proposal) => (
                    <div className="feed-row" key={proposal.id}>
                      <div>
                        <h4>{proposal.title}</h4>
                        <p className="detail">
                          {proposal.status} / risk {proposal.riskTier} / {formatDateTime(proposal.updatedAt)}
                        </p>
                        <p className="detail">{proposal.changeRef}</p>
                        <p className="detail">
                          {proposal.currentDecisionId
                            ? `Pending decision: ${proposal.currentDecisionId}`
                            : proposal.statusNote ?? proposal.summary}
                        </p>
                      </div>
                      {proposal.status === "canary_approved" ? (
                        <div className="button-stack">
                          <form action={recordSelfImprovementCanaryAction}>
                            <input type="hidden" name="proposal_id" value={proposal.id} />
                            <input type="hidden" name="outcome" value="succeeded" />
                            <button className="button secondary" type="submit">
                              Record canary success
                            </button>
                          </form>
                          <form action={recordSelfImprovementCanaryAction}>
                            <input type="hidden" name="proposal_id" value={proposal.id} />
                            <input type="hidden" name="outcome" value="failed" />
                            <button className="button secondary" type="submit">
                              Record canary failure
                            </button>
                          </form>
                        </div>
                      ) : proposal.status === "rollout_approved" ? (
                        <form action={recordSelfImprovementRolloutAction}>
                          <input type="hidden" name="proposal_id" value={proposal.id} />
                          <button className="button secondary" type="submit">
                            Record rollout
                          </button>
                        </form>
                      ) : proposal.status === "rolled_out" ? (
                        <form action={rollbackSelfImprovementProposalAction}>
                          <input type="hidden" name="proposal_id" value={proposal.id} />
                          <button className="button secondary" type="submit">
                            Record rollback
                          </button>
                        </form>
                      ) : (
                        <code className="mono">{proposal.id}</code>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </article>
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">Communications</span>
              <h2>Outbox discipline before external sends.</h2>
            </div>
            <p className="status-meta">
              Messages are drafted and queued through the API first. Real Slack delivery stays policy-gated and receipt-backed.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Draft outbound message</h3>
              <form action={submitOutboxDraftAction} className="composer compact">
                <label className="field">
                  <span>Channel</span>
                  <input
                    name="channel"
                    className="input"
                    placeholder="slack:#ops"
                    required
                  />
                </label>
                <label className="field">
                  <span>Subject</span>
                  <input
                    name="subject"
                    className="input"
                    placeholder="Daily BOSS digest"
                  />
                </label>
                <label className="field">
                  <span>Body</span>
                  <textarea
                    name="body"
                    rows={4}
                    className="textarea"
                    placeholder="Summarize what changed and what needs approval."
                    required
                  />
                </label>
                <button className="button secondary" type="submit">
                  Create outbox draft
                </button>
              </form>
            </article>

            <article className="stack-card">
              <h3>Recent outbox</h3>
              {!outbox.apiReachable ? (
                <p className="detail">
                  {outbox.blockedReason ?? "Outbox API is unavailable."}
                </p>
              ) : outbox.items.length === 0 ? (
                <p className="detail">No outbound messages have been drafted yet.</p>
              ) : (
                <div className="feed-list">
                  {outbox.items.map((message) => (
                    <div className="feed-row" key={message.id}>
                      <div>
                        <h4>{message.subject ?? message.channel}</h4>
                        <p className="detail">
                          {message.channel} / {message.status} / {formatDateTime(message.createdAt)}
                        </p>
                        <p className="detail">
                          {message.receiptId ?? message.body}
                        </p>
                      </div>
                      {message.status === "draft" ? (
                        <form action={queueOutboxMessageAction}>
                          <input type="hidden" name="message_id" value={message.id} />
                          <button className="button secondary" type="submit">
                            Queue send
                          </button>
                        </form>
                      ) : (
                        <code className="mono">{message.id}</code>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </article>

            <article className="stack-card">
              <h3>Recent inbox</h3>
              {!inbox.apiReachable ? (
                <p className="detail">
                  {inbox.blockedReason ?? "Inbox API is unavailable."}
                </p>
              ) : inbox.items.length === 0 ? (
                <p className="detail">No inbound messages have been ingested yet.</p>
              ) : (
                <div className="feed-list">
                  {inbox.items.map((message) => (
                    <div className="feed-row" key={message.id}>
                      <div>
                        <h4>{message.subject ?? message.channel}</h4>
                        <p className="detail">
                          {message.channel} / {message.status} / {formatDateTime(message.createdAt)}
                        </p>
                        <p className="detail">
                          {message.rawRef ?? "No provider reference"}
                        </p>
                      </div>
                      <code className="mono">{message.id}</code>
                    </div>
                  ))}
                </div>
              )}
            </article>
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">Company Pack</span>
              <h2>Installed roles and operating charter.</h2>
            </div>
            <p className="status-meta">
              Company structure is now installed from spec-backed packs, not hardcoded UI assumptions.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Installed packs</h3>
              {!packs.apiReachable ? (
                <p className="detail">
                  {packs.blockedReason ?? "Pack API is unavailable."}
                </p>
              ) : packs.items.length === 0 ? (
                <p className="detail">No packs are available.</p>
              ) : (
                <div className="feed-list">
                  {packs.items.map((pack) => (
                    <div className="feed-row" key={pack.packId}>
                      <div>
                        <h4>{pack.packId}</h4>
                        <p className="detail">
                          {pack.installed
                            ? `Installed by ${pack.installedBy ?? "system"}${pack.installedAt ? ` at ${formatDateTime(pack.installedAt)}` : ""}.`
                            : pack.description}
                        </p>
                        <p className="detail">
                          v{pack.version} / {pack.roles.length} roles / {pack.status}
                        </p>
                      </div>
                      {pack.installed ? (
                        <span className="pill">installed</span>
                      ) : (
                        <form action={installPackAction}>
                          <input type="hidden" name="pack_id" value={pack.packId} />
                          <button className="button secondary" type="submit">
                            Install
                          </button>
                        </form>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </article>

            <article className="stack-card">
              <h3>Installed roles</h3>
              {!roles.apiReachable ? (
                <p className="detail">
                  {roles.blockedReason ?? "Role API is unavailable."}
                </p>
              ) : roles.items.length === 0 ? (
                <p className="detail">
                  {installedCorePack
                    ? "No installed roles were returned for the active pack."
                    : "Install a company pack to provision role specs."}
                </p>
              ) : (
                <div className="feed-list">
                  {roles.items.map((role) => (
                    <div className="feed-row" key={role.id}>
                      <div>
                        <h4>{role.displayName}</h4>
                        <p className="detail">
                          {titleCaseToken(role.roleKey)} / autonomy {role.autonomyLevel} / trust{" "}
                          {role.trustScore}
                        </p>
                        <p className="detail">
                          {role.successfulRuns} successful runs / {role.policyViolationCount} policy
                          {" "}violations / {role.scopeViolationCount} scope violations
                        </p>
                        {role.lastReviewedAt ? (
                          <p className="detail">
                            Last reviewed {formatDateTime(role.lastReviewedAt)}
                            {role.lastViolationAt
                              ? ` / last violation ${formatDateTime(role.lastViolationAt)}`
                              : ""}
                          </p>
                        ) : null}
                      </div>
                      <code className="mono">{role.id}</code>
                    </div>
                  ))}
                </div>
              )}
            </article>
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">AI Capacity</span>
              <h2>Entitlements, routing, and skill registry.</h2>
            </div>
            <p className="status-meta">
              BOSS routes only across official or approved sources and logs every broker choice.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Entitlement sources</h3>
              {!entitlements.apiReachable ? (
                <p className="detail">
                  {entitlements.blockedReason ?? "Entitlement API is unavailable."}
                </p>
              ) : entitlements.items.length === 0 ? (
                <p className="detail">No entitlement sources are registered.</p>
              ) : (
                <div className="feed-list">
                  {entitlements.items.map((source) => (
                    <div className="feed-row" key={source.id}>
                      <div>
                        <h4>{source.sourceId}</h4>
                        <p className="detail">
                          {source.provider} / {titleCaseToken(source.sourceKind)} /{" "}
                          {source.effectiveStatus}
                        </p>
                        <p className="detail">
                          {source.supportedModels.length > 0
                            ? source.supportedModels.join(", ")
                            : "No runtime-backed models"}
                        </p>
                      </div>
                      <span className={`pill${source.runtimeSupported ? "" : " warn"}`}>
                        {source.runtimeSupported ? "runtime ready" : "registry only"}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </article>

            <article className="stack-card">
              <h3>Routing logs</h3>
              {!routingLogs.apiReachable ? (
                <p className="detail">
                  {routingLogs.blockedReason ?? "Routing log API is unavailable."}
                </p>
              ) : routingLogs.items.length === 0 ? (
                <p className="detail">No routing decisions have been logged yet.</p>
              ) : (
                <div className="feed-list">
                  {routingLogs.items.map((route) => (
                    <div className="feed-row" key={route.id}>
                      <div>
                        <h4>{route.skillId ?? route.taskKind}</h4>
                        <p className="detail">
                          {route.selectedSourceId
                            ? `${route.selectedSourceId} -> ${route.selectedModelId ?? "no model id"}`
                            : `Denied: ${route.denialReason ?? "unknown"}`}
                        </p>
                        <p className="detail">
                          {route.workloadClass} / {formatDateTime(route.createdAt)}
                        </p>
                      </div>
                      <code className="mono">{route.id}</code>
                    </div>
                  ))}
                </div>
              )}
            </article>

            <article className="stack-card">
              <h3>Broker skills</h3>
              {!skills.apiReachable ? (
                <p className="detail">
                  {skills.blockedReason ?? "Skill API is unavailable."}
                </p>
              ) : skills.items.length === 0 ? (
                <p className="detail">No broker or model skills are registered.</p>
              ) : (
                <div className="feed-list">
                  {skills.items.slice(0, 6).map((skill) => (
                    <div className="feed-row" key={skill.id}>
                      <div>
                        <h4>{skill.id}</h4>
                        <p className="detail">
                          {titleCaseToken(skill.kind)} / risk {skill.riskTier}
                        </p>
                      </div>
                      <span className="pill">{titleCaseToken(skill.kind)}</span>
                    </div>
                  ))}
                </div>
              )}
            </article>
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">Company Workspace</span>
              <h2>Tasks and artifacts with real lineage.</h2>
            </div>
            <p className="status-meta">
              The console reads canonical work objects directly from the control
              plane API.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Recent tasks</h3>
              {!tasks.apiReachable ? (
                <p className="detail">
                  {tasks.blockedReason ?? "Task API is unavailable."}
                </p>
              ) : tasks.items.length === 0 ? (
                <p className="detail">No tasks have been created yet.</p>
              ) : (
                <div className="feed-list">
                  {tasks.items.map((task) => (
                    <div className="feed-row" key={task.id}>
                      <div>
                        <h4>{task.title}</h4>
                        <p className="detail">
                          {task.status} / {task.priority} / risk {task.riskTier}
                        </p>
                      </div>
                      <code className="mono">{task.id}</code>
                    </div>
                  ))}
                </div>
              )}
            </article>

            <article className="stack-card">
              <h3>Recent artifacts</h3>
              {!artifacts.apiReachable ? (
                <p className="detail">
                  {artifacts.blockedReason ?? "Artifact API is unavailable."}
                </p>
              ) : artifacts.items.length === 0 ? (
                <p className="detail">No artifacts have been created yet.</p>
              ) : (
                <div className="feed-list">
                  {artifacts.items.map((artifact) => (
                    <div className="feed-row" key={artifact.id}>
                      <div>
                        <h4>{artifact.title}</h4>
                        <p className="detail">
                          {titleCaseToken(artifact.artifactType)} /{" "}
                          {formatDateTime(artifact.createdAt)}
                        </p>
                      </div>
                      <code className="mono">{artifact.id}</code>
                    </div>
                  ))}
                </div>
              )}
            </article>
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">What Changed</span>
              <h2>Recent events and visible failures.</h2>
            </div>
            <p className="status-meta">
              No silent drops: deadletters remain visible beside the event feed.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Recent events</h3>
              {!events.apiReachable ? (
                <p className="detail">
                  {events.blockedReason ?? "Event feed is unavailable."}
                </p>
              ) : events.items.length === 0 ? (
                <p className="detail">No events have been emitted yet.</p>
              ) : (
                <div className="feed-list">
                  {events.items.map((event) => (
                    <div className="feed-row" key={event.id}>
                      <div>
                        <h4>{titleCaseToken(event.eventType)}</h4>
                        <p className="detail">
                          {event.actor} / {event.entityType} / {formatDateTime(event.createdAt)}
                        </p>
                      </div>
                      <code className="mono">{event.entityId}</code>
                    </div>
                  ))}
                </div>
              )}
            </article>

            <article className="stack-card">
              <h3>Deadletters</h3>
              {!deadletters.apiReachable ? (
                <p className="detail">
                  {deadletters.blockedReason ?? "Deadletter API is unavailable."}
                </p>
              ) : deadletters.items.length === 0 ? (
                <p className="detail">
                  No failed jobs or effects are currently isolated for review.
                </p>
              ) : (
                <div className="feed-list">
                  {deadletters.items.map((deadletter) => (
                    <div className="feed-row" key={deadletter.id}>
                      <div>
                        <h4>{deadletter.reason}</h4>
                        <p className="detail">
                          {deadletter.sourceKind} / {formatDateTime(deadletter.createdAt)}
                        </p>
                      </div>
                      <code className="mono">{deadletter.sourceId}</code>
                    </div>
                  ))}
                </div>
              )}
            </article>
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">Decision Inbox</span>
              <h2>Explicit approvals before real effects.</h2>
            </div>
            <p className="status-meta">
              Decisions are served by the control plane API and expire to a safe
              default when their deadline passes.
            </p>
          </div>

          {!decisions.apiReachable ? (
            <article className="decision-card empty">
              <h3>Decision API unavailable</h3>
              <p className="detail">
                {decisions.blockedReason ??
                  "The executive console could not reach the decision inbox."}
              </p>
            </article>
          ) : decisions.items.length === 0 ? (
            <article className="decision-card empty">
              <h3>No active decisions</h3>
              <p className="detail">
                The control plane does not currently need an approval, rejection,
                or safe-default expiration.
              </p>
            </article>
          ) : (
            <div className="decision-list">
              {decisions.items.map((decision) => {
                const estimatedCost =
                  typeof decision.costSummary.estimated_usd_micros === "number"
                    ? decision.costSummary.estimated_usd_micros
                    : undefined;
                const isPending =
                  decision.status === "pending" || decision.status === "delegated";
                const isApproved = decision.status === "approved";

                return (
                  <article className="decision-card" key={decision.id}>
                    <div className="decision-head">
                      <div>
                        <span className={`pill${isPending ? " warn" : ""}`}>
                          {decision.status}
                        </span>
                        <h3>{decision.question}</h3>
                      </div>
                      <div className="decision-meta">
                        <span>Risk tier {decision.riskTier}</span>
                        <span>Default: {decision.defaultAction ?? "hold"}</span>
                        <span>Deadline: {formatDeadline(decision.deadlineAt)}</span>
                      </div>
                    </div>

                    <div className="decision-grid">
                      <div>
                        <h4>Recommendation</h4>
                        <p className="detail">{formatRecommendation(decision)}</p>
                      </div>
                      <div>
                        <h4>Estimated Cost</h4>
                        <p className="detail">{formatMicros(estimatedCost)}</p>
                      </div>
                      <div>
                        <h4>Options</h4>
                        <p className="detail">
                          {decision.options
                            .map((option) =>
                              typeof option.label === "string"
                                ? option.label
                                : typeof option.action === "string"
                                  ? option.action
                                  : "review",
                            )
                            .join(" / ")}
                        </p>
                      </div>
                    </div>

                    <div className="decision-footer">
                      <p className="detail">{decisionStatusDetail(decision)}</p>
                      {isPending ? (
                        <form action={resolveDecisionAction} className="decision-actions">
                          <input
                            type="hidden"
                            name="decision_id"
                            value={decision.id}
                          />
                          <textarea
                            className="textarea decision-note"
                            name="note"
                            rows={3}
                            placeholder="Optional note or required revision request rationale."
                          />
                          <input
                            className="input"
                            name="delegate_to"
                            placeholder="Delegate to role or approver"
                          />
                          <button className="button" name="action" value="approve">
                            Approve
                          </button>
                          <button
                            className="button secondary"
                            name="action"
                            value="reject"
                          >
                            Reject
                          </button>
                          <button
                            className="button secondary"
                            name="action"
                            value="revise"
                          >
                            Request revision
                          </button>
                          <button
                            className="button secondary"
                            name="action"
                            value="delegate"
                          >
                            Delegate
                          </button>
                        </form>
                      ) : null}
                      {isApproved ? (
                        <form action={resolveDecisionAction} className="decision-actions">
                          <input
                            type="hidden"
                            name="decision_id"
                            value={decision.id}
                          />
                          <textarea
                            className="textarea decision-note"
                            name="note"
                            rows={2}
                            placeholder="Record what was executed."
                          />
                          <input
                            className="input"
                            name="external_ref"
                            placeholder="External reference (PR URL, commit SHA, ticket)"
                          />
                          <button className="button" name="action" value="execute">
                            Mark executed
                          </button>
                        </form>
                      ) : null}
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
