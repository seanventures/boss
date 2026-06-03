import {
  createReplayBundleAction,
  createBreakGlassGrantAction,
  createComplianceEvidenceBundleAction,
  createDecisionDigestAction,
  installAgentModuleAction,
  installPackAction,
  queueOutboxMessageAction,
  refreshHealthSnapshotAction,
  recordSelfImprovementCanaryAction,
  recordSelfImprovementRolloutAction,
  revokeBreakGlassGrantAction,
  resolveDecisionAction,
  rollbackSelfImprovementProposalAction,
  submitSelfImprovementProposalAction,
  submitOutboxDraftAction,
  submitCommandAction,
} from "./actions";
import { fetchAuditSummary } from "../lib/audit";
import { fetchArtifactFeed } from "../lib/artifacts";
import { fetchAgentModuleFeed } from "../lib/agent-modules";
import { fetchAuthSession } from "../lib/auth-session";
import { fetchInboxFeed, fetchOutboxFeed } from "../lib/comms";
import { fetchBudgetAllocationFeed } from "../lib/budget-allocations";
import { fetchBudgetStatus } from "../lib/budget-status";
import { fetchBreakGlassGrantFeed } from "../lib/break-glass";
import { fetchChangeJournalFeed } from "../lib/change-journal";
import { fetchDeadletterFeed } from "../lib/deadletters";
import { fetchDecisionFeed, type DecisionFeedItem } from "../lib/decisions";
import { fetchEntitlementFeed } from "../lib/entitlements";
import { fetchActivityFeed } from "../lib/events";
import { fetchHealthSnapshotFeed } from "../lib/health-snapshots";
import { fetchInfrastructureResourceFeed } from "../lib/infrastructure";
import { fetchMemberFeed } from "../lib/members";
import { fetchMetatronQuery } from "../lib/metatron";
import { fetchPackFeed } from "../lib/packs";
import { fetchProjectFeed } from "../lib/projects";
import { fetchReceiptFeed } from "../lib/receipts";
import { fetchRoleFeed } from "../lib/roles";
import { fetchRoutingLogFeed } from "../lib/routing";
import { fetchSelfImprovementFeed } from "../lib/self-improvement";
import { fetchSkillFeed } from "../lib/skills";
import { fetchStatus } from "../lib/status";
import { fetchTaskFeed } from "../lib/tasks";
import { PROJECT_ID } from "../lib/client";

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

function formatDurationSeconds(value?: number | null): string {
  if (typeof value !== "number") {
    return "No active burn rate";
  }
  if (value <= 0) {
    return "At cap";
  }
  const hours = Math.floor(value / 3600);
  const days = Math.floor(hours / 24);
  if (days > 0) {
    return `${days}d ${hours % 24}h`;
  }
  if (hours > 0) {
    return `${hours}h`;
  }
  return `${Math.max(Math.floor(value / 60), 1)}m`;
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

function entitlementBadge(
  source: {
    effectiveStatus: string;
    runtimeSupported: boolean;
    credentialPresent: boolean;
  },
): string {
  if (source.effectiveStatus === "active") {
    return "active";
  }
  if (source.runtimeSupported && !source.credentialPresent) {
    return "credentials required";
  }
  if (source.runtimeSupported) {
    return "adapter ready";
  }
  return "registry only";
}

function entitlementDetail(
  source: {
    runtimeSupported: boolean;
    credentialPresent: boolean;
    credentialRef?: string;
  },
): string {
  if (source.runtimeSupported && source.credentialPresent) {
    return "Adapter support is enabled and credentials are present.";
  }
  if (source.runtimeSupported && !source.credentialPresent) {
    return source.credentialRef
      ? `Adapter support is enabled, but ${source.credentialRef} is not configured.`
      : "Adapter support is enabled, but credentials are missing.";
  }
  return "This source is currently registry inventory only and cannot execute work.";
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
  const requestedProjectId =
    getParam(resolvedSearchParams?.project_id) ?? PROJECT_ID;
  const metatronQuestion = getParam(resolvedSearchParams?.q);
  const metatronTaskId = getParam(resolvedSearchParams?.task_id);
  const metatronDecisionId = getParam(resolvedSearchParams?.decision_id);
  const requestedActorId = getParam(resolvedSearchParams?.actor);
  const projects = await fetchProjectFeed(requestedProjectId);
  const activeProjectId = projects.items.some(
    (project) => project.id === requestedProjectId,
  )
    ? requestedProjectId
    : projects.items[0]?.id ?? requestedProjectId;
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
    agentModules,
    entitlements,
    routingLogs,
    skills,
    receipts,
    infrastructureResources,
    budgetStatus,
    budgetAllocations,
    auditSummary,
    changeJournal,
    selfImprovement,
    outbox,
    inbox,
    members,
    metatron,
    authSession,
    breakGlassGrants,
  ] =
    await Promise.all([
      fetchStatus(activeProjectId),
      fetchDecisionFeed(activeProjectId),
      fetchTaskFeed(activeProjectId),
      fetchArtifactFeed(activeProjectId),
      fetchActivityFeed(activeProjectId),
      fetchHealthSnapshotFeed(activeProjectId),
      fetchDeadletterFeed(activeProjectId),
      fetchPackFeed(activeProjectId),
      fetchRoleFeed(activeProjectId),
      fetchAgentModuleFeed(activeProjectId),
      fetchEntitlementFeed(activeProjectId),
      fetchRoutingLogFeed(activeProjectId),
      fetchSkillFeed(),
      fetchReceiptFeed(activeProjectId),
      fetchInfrastructureResourceFeed(activeProjectId),
      fetchBudgetStatus(activeProjectId),
      fetchBudgetAllocationFeed(activeProjectId),
      fetchAuditSummary(activeProjectId),
      fetchChangeJournalFeed(activeProjectId),
      fetchSelfImprovementFeed(activeProjectId),
      fetchOutboxFeed(activeProjectId),
      fetchInboxFeed(activeProjectId),
      fetchMemberFeed(activeProjectId),
      fetchMetatronQuery(
        metatronQuestion,
        metatronTaskId,
        metatronDecisionId,
        activeProjectId,
      ),
      fetchAuthSession(),
      fetchBreakGlassGrantFeed(activeProjectId),
    ]);

  const deadletterCount = deadletters.items.length;
  const disableCommandEntry = !status.apiReachable || !status.databaseConnected;
  const installedCorePack = packs.items.find((pack) => pack.installed);
  const activeActorId =
    requestedActorId &&
    members.items.some((member) => member.memberId === requestedActorId)
      ? requestedActorId
      : members.items.some((member) => member.memberId === "executive_console")
        ? "executive_console"
        : members.items[0]?.memberId ?? "executive_console";
  const activeActor = members.items.find(
    (member) => member.memberId === activeActorId,
  );

  return (
    <main className="shell" aria-labelledby="boss-console-title">
      <a className="skip-link" href="#decision-inbox">
        Skip to decision inbox
      </a>
      <div className="frame">
        <section className="hero">
          <div className="hero-grid">
            <div className="hero-copy">
              <span className="eyebrow">Business Operating System Sentinel</span>
              <h1 id="boss-console-title">
                Executive control for autonomous work.
              </h1>
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
              {projects.apiReachable && projects.items.length > 0 ? (
                <form method="get" className="composer compact">
                  <label className="field">
                    <span>Project scope</span>
                    <select
                      className="input"
                      name="project_id"
                      defaultValue={activeProjectId}
                    >
                      {projects.items.map((project) => (
                        <option key={project.id} value={project.id}>
                          {project.orgName} / {project.workspaceName} / {project.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span>Active actor</span>
                    <select
                      className="input"
                      name="actor"
                      defaultValue={activeActorId}
                    >
                      {members.items.length > 0 ? (
                        members.items.map((member) => (
                          <option key={member.id} value={member.memberId}>
                            {member.displayName} ({member.memberId})
                          </option>
                        ))
                      ) : (
                        <option value={activeActorId}>{activeActorId}</option>
                      )}
                    </select>
                  </label>
                  <button className="button secondary" type="submit">
                    Apply scope
                  </button>
                  <p className="detail">
                    Every write action now carries the selected project member actor.
                  </p>
                </form>
              ) : (
                <p className="detail">
                  {projects.blockedReason ??
                    "Project registry is unavailable, so the console is using its default scope."}
                </p>
              )}
              <p className="detail">
                Active actor: {activeActor?.displayName ?? activeActorId} / roles{" "}
                {activeActor?.roleKeys.join(", ") || "unknown"} / permissions{" "}
                {activeActor?.permissions.length ?? 0}
                {!members.apiReachable && members.blockedReason
                  ? ` (${members.blockedReason})`
                  : ""}
              </p>
            </div>

            <form action={submitCommandAction} className="composer">
              <input type="hidden" name="project_id" value={activeProjectId} />
              <input type="hidden" name="actor" value={activeActorId} />
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
              <input type="hidden" name="project_id" value={activeProjectId} />
              <input type="hidden" name="actor" value={activeActorId} />
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
            <h2>Auth Session</h2>
            <div className="metric">
              {authSession.actorBindingRequired
                ? authSession.principalMemberId ?? "Bound"
                : authSession.authRequired
                  ? "API key"
                  : "Local"}
            </div>
            <p className="detail">
              {authSession.apiReachable
                ? authSession.message
                : authSession.blockedReason ?? "Auth session API unavailable."}
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
              <span className="eyebrow">Break-Glass Access</span>
              <h2>Emergency RBAC with TTL and postmortem tasks.</h2>
            </div>
            <p className="status-meta">
              PRD 9.2 requires identity, reason, scope, TTL, event log, and a
              postmortem task for emergency access.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Grant temporary access</h3>
              <form action={createBreakGlassGrantAction} className="composer compact">
                <input type="hidden" name="project_id" value={activeProjectId} />
                <input type="hidden" name="actor" value={activeActorId} />
                <label className="field">
                  <span>Target member</span>
                  <select className="input" name="target_member_id" required>
                    {members.items.map((member) => (
                      <option key={member.id} value={member.memberId}>
                        {member.displayName} ({member.memberId})
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Granted permissions</span>
                  <textarea
                    name="granted_permissions"
                    rows={3}
                    className="textarea"
                    placeholder={"One permission per line\nmember_manage\naudit_export"}
                    required
                  />
                </label>
                <label className="field">
                  <span>Reason</span>
                  <textarea
                    name="reason"
                    rows={3}
                    className="textarea"
                    placeholder="Describe the incident, scope, and why normal access is insufficient."
                    required
                  />
                </label>
                <div className="input-grid">
                  <label className="field">
                    <span>TTL minutes</span>
                    <input
                      className="input"
                      name="ttl_minutes"
                      type="number"
                      min={5}
                      max={1440}
                      defaultValue={60}
                      required
                    />
                  </label>
                  <label className="field">
                    <span>Scope JSON</span>
                    <input
                      className="input"
                      name="scope_json"
                      placeholder='{"incident_id":"inc_123"}'
                    />
                  </label>
                </div>
                <button className="button secondary" type="submit">
                  Grant break-glass access
                </button>
              </form>
            </article>

            <article className="stack-card">
              <h3>Recent grants</h3>
              {!breakGlassGrants.apiReachable ? (
                <p className="detail">
                  {breakGlassGrants.blockedReason ??
                    "Break-glass API is unavailable."}
                </p>
              ) : breakGlassGrants.items.length === 0 ? (
                <p className="detail">
                  No break-glass grants have been created for this project.
                </p>
              ) : (
                <div className="feed-list">
                  {breakGlassGrants.items.map((grant) => (
                    <div className="feed-row" key={grant.id}>
                      <div>
                        <h4>
                          {grant.targetMemberId} / {grant.status}
                        </h4>
                        <p className="detail">
                          Permissions: {grant.grantedPermissions.join(", ")}
                        </p>
                        <p className="detail">
                          Expires {formatDateTime(grant.expiresAt)} / requested by{" "}
                          {grant.requestedBy}
                        </p>
                        <p className="detail">
                          Postmortem task: {grant.postmortemTaskId ?? "missing"}
                        </p>
                        <p className="detail">{grant.reason}</p>
                      </div>
                      {grant.status === "active" ? (
                        <form
                          action={revokeBreakGlassGrantAction}
                          className="composer compact"
                        >
                          <input type="hidden" name="grant_id" value={grant.id} />
                          <input
                            type="hidden"
                            name="project_id"
                            value={activeProjectId}
                          />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <label className="field">
                            <span>Revocation reason</span>
                            <input
                              className="input"
                              name="reason"
                              placeholder="Emergency access no longer needed."
                              required
                            />
                          </label>
                          <button className="button secondary" type="submit">
                            Revoke
                          </button>
                        </form>
                      ) : (
                        <code className="mono">{grant.id}</code>
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
              <span className="eyebrow">Budget Control Center</span>
              <h2>Live spend, forecast, and cap pressure.</h2>
            </div>
            <p className="status-meta">
              Source: `/v1/budget/status` for project {activeProjectId}.
            </p>
          </div>

          <article className="stack-card">
            {!budgetStatus.apiReachable ? (
              <p className="detail">
                {budgetStatus.blockedReason ?? "Budget status API is unavailable."}
              </p>
            ) : (
              <>
                <div className="grid compact-grid">
                  <article>
                    <h2>Actual Spend</h2>
                    <div className="metric">
                      {formatMicros(budgetStatus.actualUsdMicros)}
                    </div>
                    <p className="detail">Cost receipts recorded for this project.</p>
                  </article>
                  <article>
                    <h2>Reserved</h2>
                    <div className="metric">
                      {formatMicros(budgetStatus.reservedUsdMicros)}
                    </div>
                    <p className="detail">
                      {budgetStatus.activeReservationsCount} active reservation
                      {budgetStatus.activeReservationsCount === 1 ? "" : "s"}.
                    </p>
                  </article>
                  <article>
                    <h2>Forecast 24h</h2>
                    <div className="metric">
                      {formatMicros(budgetStatus.forecasted24hUsdMicros)}
                    </div>
                    <p className="detail">
                      Observed spend over {budgetStatus.burnWindowHours}h plus
                      active reservations.
                    </p>
                  </article>
                  <article>
                    <h2>Time To Cap</h2>
                    <div className="metric">
                      {formatDurationSeconds(budgetStatus.timeToCapSeconds)}
                    </div>
                    <p className="detail">
                      Burn rate{" "}
                      {formatMicros(budgetStatus.burnRateUsdMicrosPerHour)}
                      /hour.
                    </p>
                  </article>
                </div>
                <div className="decision-grid">
                  <div>
                    <h4>Cap pressure</h4>
                    <p className="detail">
                      State {budgetStatus.budgetState}; committed{" "}
                      {formatMicros(budgetStatus.committedUsdMicros)} of{" "}
                      {formatMicros(budgetStatus.capUsdMicros)} with{" "}
                      {Math.round(budgetStatus.utilizationRatio * 100)}%
                      utilization.
                    </p>
                  </div>
                  <div>
                    <h4>Top cost drivers</h4>
                    {budgetStatus.topCostDrivers.length > 0 ? (
                      <ul className="detail">
                        {budgetStatus.topCostDrivers.map((driver) => (
                          <li key={driver.skillOrModelId}>
                            {driver.skillOrModelId}:{" "}
                            {formatMicros(driver.amountUsdMicros)}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="detail">No cost receipts recorded yet.</p>
                    )}
                  </div>
                </div>
              </>
            )}
          </article>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">Budget Allocations</span>
              <h2>Hierarchical spend envelopes for departments and initiatives.</h2>
            </div>
            <p className="status-meta">
              Child allocations and reservations cannot exceed the parent envelope.
            </p>
          </div>

          <article className="stack-card">
            {!budgetAllocations.apiReachable ? (
              <p className="detail">
                {budgetAllocations.blockedReason ??
                  "Budget allocation API is unavailable."}
              </p>
            ) : budgetAllocations.items.length === 0 ? (
              <p className="detail">
                No budget allocations have been created for this project yet.
              </p>
            ) : (
              <div className="feed-list">
                {budgetAllocations.items.map((allocation) => (
                  <div className="feed-row" key={allocation.id}>
                    <div>
                      <h4>
                        {titleCaseToken(allocation.scopeType)} /{" "}
                        {allocation.scopeId}
                      </h4>
                      <p className="detail">
                        Amount {formatMicros(allocation.amountUsdMicros)} / available{" "}
                        {formatMicros(allocation.availableUsdMicros)}
                      </p>
                      <p className="detail">
                        Child allocations{" "}
                        {formatMicros(allocation.childAllocatedUsdMicros)} / reserved{" "}
                        {formatMicros(allocation.reservedUsdMicros)} / actual{" "}
                        {formatMicros(allocation.actualUsdMicros)}
                      </p>
                      <p className="detail">
                        {formatDateTime(allocation.periodStart)} to{" "}
                        {formatDateTime(allocation.periodEnd)}
                      </p>
                    </div>
                    <code className="mono">
                      {allocation.parentAllocationId ?? allocation.id}
                    </code>
                  </div>
                ))}
              </div>
            )}
          </article>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">Infrastructure Resources</span>
              <h2>Receipt-backed droplets with TTL and janitor visibility.</h2>
            </div>
            <p className="status-meta">
              Source: `/v1/infra/resources` for project {activeProjectId}.
            </p>
          </div>

          <article className="stack-card">
            {!infrastructureResources.apiReachable ? (
              <p className="detail">
                {infrastructureResources.blockedReason ??
                  "Infrastructure resource API is unavailable."}
              </p>
            ) : infrastructureResources.items.length === 0 ? (
              <p className="detail">
                No provisioned infrastructure resources are currently recorded for
                this project.
              </p>
            ) : (
              <div className="feed-list">
                {infrastructureResources.items.map((resource) => (
                  <div className="feed-row" key={resource.id}>
                    <div>
                      <h4>
                        {resource.resourceName} / {resource.status}
                      </h4>
                      <p className="detail">
                        {resource.provider} {resource.resourceType} in{" "}
                        {resource.region} on {resource.size} / boundary{" "}
                        {resource.dataBoundary}
                      </p>
                      <p className="detail">
                        TTL {resource.ttlMinutes}m / expires{" "}
                        {formatDateTime(resource.expiresAt)}
                      </p>
                      <p className="detail">
                        {resource.providerRef ?? resource.externalResourceId}
                      </p>
                      {resource.terminationReason ? (
                        <p className="detail">
                          Termination: {resource.terminationReason}
                          {resource.terminatedAt
                            ? ` at ${formatDateTime(resource.terminatedAt)}`
                            : ""}
                        </p>
                      ) : null}
                    </div>
                    <code className="mono">{resource.id}</code>
                  </div>
                ))}
              </div>
            )}
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
                <input type="hidden" name="project_id" value={activeProjectId} />
                <input type="hidden" name="actor" value={activeActorId} />
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
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <button className="button secondary" type="submit">
                      Create replay bundle
                    </button>
                  </form>
                  <form action={createComplianceEvidenceBundleAction}>
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <button className="button secondary" type="submit">
                      Create compliance evidence
                    </button>
                  </form>
                </>
              )}
            </article>

            <article className="stack-card">
              <h3>Change journal</h3>
              {!changeJournal.apiReachable ? (
                <p className="detail">
                  {changeJournal.blockedReason ?? "Change journal API is unavailable."}
                </p>
              ) : changeJournal.items.length === 0 ? (
                <p className="detail">
                  No change artifacts have been recorded yet.
                </p>
              ) : (
                <div className="feed-list">
                  {changeJournal.items.map((item) => (
                    <div className="feed-row" key={item.id}>
                      <div>
                        <h4>{item.title}</h4>
                        <p className="detail">
                          {titleCaseToken(item.changeKind)} / {item.artifactType} /{" "}
                          {formatDateTime(item.createdAt)}
                        </p>
                        {item.summary ? (
                          <p className="detail">{item.summary}</p>
                        ) : null}
                        {item.diffSummary ? (
                          <p className="detail">Diff: {item.diffSummary}</p>
                        ) : null}
                        {item.filesChanged.length > 0 ? (
                          <p className="detail">
                            Files: {item.filesChanged.slice(0, 4).join(", ")}
                          </p>
                        ) : null}
                        {item.rollbackPlan ? (
                          <p className="detail">Rollback: {item.rollbackPlan}</p>
                        ) : null}
                      </div>
                      <code className="mono">{item.taskId ?? item.id}</code>
                    </div>
                  ))}
                </div>
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
                <input type="hidden" name="project_id" value={activeProjectId} />
                <input type="hidden" name="actor" value={activeActorId} />
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
                          <form action={recordSelfImprovementCanaryAction} className="composer compact">
                            <input type="hidden" name="proposal_id" value={proposal.id} />
                            <input type="hidden" name="project_id" value={activeProjectId} />
                            <input type="hidden" name="actor" value={activeActorId} />
                            <input type="hidden" name="outcome" value="succeeded" />
                            <textarea
                              aria-label="Canary success summary"
                              className="textarea"
                              name="summary"
                              rows={2}
                              placeholder="Describe the successful canary outcome."
                              required
                            />
                            <textarea
                              aria-label="Canary success metrics JSON"
                              className="textarea"
                              name="metrics_json"
                              rows={3}
                              placeholder={'{"passed_checks": 3, "error_rate": 0}'}
                              required
                            />
                            <textarea
                              aria-label="Canary success evidence references"
                              className="textarea"
                              name="evidence_refs"
                              rows={2}
                              placeholder={"One evidence ref per line\nartifact:test-report-123"}
                              required
                            />
                            <button className="button secondary" type="submit">
                              Record canary success
                            </button>
                          </form>
                          <form action={recordSelfImprovementCanaryAction} className="composer compact">
                            <input type="hidden" name="proposal_id" value={proposal.id} />
                            <input type="hidden" name="project_id" value={activeProjectId} />
                            <input type="hidden" name="actor" value={activeActorId} />
                            <input type="hidden" name="outcome" value="failed" />
                            <textarea
                              aria-label="Canary failure summary"
                              className="textarea"
                              name="summary"
                              rows={2}
                              placeholder="Describe what failed in canary."
                              required
                            />
                            <textarea
                              aria-label="Canary failure metrics JSON"
                              className="textarea"
                              name="metrics_json"
                              rows={3}
                              placeholder={'{"failed_checks": 1, "rollback_required": 1}'}
                              required
                            />
                            <textarea
                              aria-label="Canary failure evidence references"
                              className="textarea"
                              name="evidence_refs"
                              rows={2}
                              placeholder={"One evidence ref per line\nartifact:canary-log-456"}
                              required
                            />
                            <button className="button secondary" type="submit">
                              Record canary failure
                            </button>
                          </form>
                        </div>
                      ) : proposal.status === "rollout_approved" ? (
                        <form action={recordSelfImprovementRolloutAction} className="composer compact">
                          <input type="hidden" name="proposal_id" value={proposal.id} />
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <textarea
                            aria-label="Rollout summary"
                            className="textarea"
                            name="summary"
                            rows={2}
                            placeholder="Describe what rolled out and what was validated."
                            required
                          />
                          <input
                            aria-label="Rollout external reference"
                            className="input"
                            name="external_ref"
                            placeholder="External reference (deploy URL, commit SHA)"
                          />
                          <textarea
                            aria-label="Rollout metrics JSON"
                            className="textarea"
                            name="metrics_json"
                            rows={3}
                            placeholder={'{"latency_p95_ms": 220, "error_rate": 0}'}
                            required
                          />
                          <textarea
                            aria-label="Rollout evidence references"
                            className="textarea"
                            name="evidence_refs"
                            rows={2}
                            placeholder={"One evidence ref per line\nartifact:rollout-report-789"}
                            required
                          />
                          <button className="button secondary" type="submit">
                            Record rollout
                          </button>
                        </form>
                      ) : proposal.status === "rolled_out" ? (
                        <form action={rollbackSelfImprovementProposalAction} className="composer compact">
                          <input type="hidden" name="proposal_id" value={proposal.id} />
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <textarea
                            aria-label="Rollback reason"
                            className="textarea"
                            name="reason"
                            rows={2}
                            placeholder="Describe why rollback was required."
                            required
                          />
                          <input
                            aria-label="Rollback external reference"
                            className="input"
                            name="external_ref"
                            placeholder="External reference (rollback PR, incident ticket)"
                          />
                          <textarea
                            aria-label="Rollback metrics JSON"
                            className="textarea"
                            name="metrics_json"
                            rows={3}
                            placeholder={'{"rollback_duration_minutes": 4, "customer_impact": 0}'}
                            required
                          />
                          <textarea
                            aria-label="Rollback evidence references"
                            className="textarea"
                            name="evidence_refs"
                            rows={2}
                            placeholder={"One evidence ref per line\nartifact:rollback-report-101"}
                            required
                          />
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
                <input type="hidden" name="project_id" value={activeProjectId} />
                <input type="hidden" name="actor" value={activeActorId} />
                <label className="field">
                  <span>Channel</span>
                  <input
                    name="channel"
                    className="input"
                    placeholder="slack:#ops or email:founder@example.com"
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
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <label className="field">
                            <span>Urgency</span>
                            <select
                              aria-label={`Delivery urgency for ${message.id}`}
                              className="input"
                              defaultValue="normal"
                              name="urgency"
                            >
                              <option value="low">Low</option>
                              <option value="normal">Normal</option>
                              <option value="urgent">Urgent</option>
                            </select>
                          </label>
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
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
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
                        {role.sourceModuleId ? (
                          <p className="detail">
                            Packaged from {role.sourceModuleId}
                            {role.sourceModuleVersion
                              ? ` v${role.sourceModuleVersion}`
                              : ""}
                          </p>
                        ) : null}
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
              <span className="eyebrow">Agent Marketplace</span>
              <h2>Packaged worker modules with contained prompts, skills, and runtime policy.</h2>
            </div>
            <p className="status-meta">
              Modules are installable catalog units that can own a role charter, prompt bundle,
              runtime job kinds, and configuration defaults.
            </p>
          </div>

          <article className="stack-card">
            <h3>Available agent modules</h3>
            {!agentModules.apiReachable ? (
              <p className="detail">
                {agentModules.blockedReason ?? "Agent module API is unavailable."}
              </p>
            ) : agentModules.items.length === 0 ? (
              <p className="detail">No agent modules are available.</p>
            ) : (
              <div className="feed-list">
                {agentModules.items.map((module) => (
                  <div className="feed-row" key={module.moduleId}>
                    <div>
                      <h4>{module.displayName}</h4>
                      <p className="detail">
                        {module.moduleId} / {module.publisher} / {module.category}
                      </p>
                      <p className="detail">{module.description}</p>
                      <p className="detail">
                        v{module.version} / {module.roleKey ?? "no role"} /{" "}
                        {module.jobKinds.join(", ")}
                      </p>
                      <p className="detail">
                        {module.priceModel ?? "custom pricing"}
                        {module.priceUsdMicros
                          ? ` / ${formatMicros(module.priceUsdMicros)}`
                          : ""}
                      </p>
                    </div>
                    {module.installed ? (
                      <span className="pill">
                        installed
                        {module.installedAt
                          ? ` ${formatDateTime(module.installedAt)}`
                          : ""}
                      </span>
                    ) : (
                      <form action={installAgentModuleAction}>
                        <input type="hidden" name="module_id" value={module.moduleId} />
                        <input type="hidden" name="project_id" value={activeProjectId} />
                        <input type="hidden" name="actor" value={activeActorId} />
                        <button className="button secondary" type="submit">
                          Install module
                        </button>
                      </form>
                    )}
                  </div>
                ))}
              </div>
            )}
          </article>
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
                        <p className="detail">{entitlementDetail(source)}</p>
                      </div>
                      <span
                        className={`pill${
                          source.effectiveStatus === "active" ? "" : " warn"
                        }`}
                      >
                        {entitlementBadge(source)}
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
                          {route.workloadClass} / {route.dataBoundary} /{" "}
                          {formatDateTime(route.createdAt)}
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
                        <p className="detail">
                          Owner {task.ownerRoleId ?? "unassigned"} / parent{" "}
                          {task.parentTaskId ?? "root"} / dependencies{" "}
                          {task.dependencyTaskIds.length}
                        </p>
                        {task.dueAt ? (
                          <p className="detail">Due {formatDateTime(task.dueAt)}</p>
                        ) : null}
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

        <section
          aria-labelledby="decision-inbox-title"
          className="panel-stack"
          id="decision-inbox"
          tabIndex={-1}
        >
          <div className="section-head">
            <div>
              <span className="eyebrow">Decision Inbox</span>
              <h2 id="decision-inbox-title">
                Explicit approvals before real effects.
              </h2>
            </div>
            <p className="status-meta">
              Decisions are served by the control plane API and expire to a safe
              default when their deadline passes.
            </p>
          </div>

          {decisions.apiReachable ? (
            <article className="decision-card">
              <div className="decision-head">
                <div>
                  <span className="pill">Digest</span>
                  <h3>Create decision digest</h3>
                </div>
                <p className="detail">
                  Creates a durable digest artifact. Optional channel creates an
                  outbox draft only; sending still requires a budgeted queue action.
                </p>
              </div>
              <form action={createDecisionDigestAction} className="decision-actions">
                <input type="hidden" name="project_id" value={activeProjectId} />
                <input type="hidden" name="actor" value={activeActorId} />
                <label className="field">
                  <span>Max items</span>
                  <input
                    className="input"
                    defaultValue={20}
                    max={100}
                    min={1}
                    name="max_items"
                    type="number"
                  />
                </label>
                <label className="field">
                  <span>Optional outbox channel</span>
                  <input
                    className="input"
                    name="channel"
                    placeholder="email:founder@example.com or slack:#ops"
                  />
                </label>
                <button className="button secondary" type="submit">
                  Create digest
                </button>
              </form>
            </article>
          ) : null}

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
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <textarea
                            aria-label={`Decision note for ${decision.id}`}
                            className="textarea decision-note"
                            name="note"
                            rows={3}
                            placeholder="Optional note or required revision request rationale."
                          />
                          <input
                            aria-label={`Delegate target for ${decision.id}`}
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
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <textarea
                            aria-label={`Execution note for ${decision.id}`}
                            className="textarea decision-note"
                            name="note"
                            rows={2}
                            placeholder="Record what was executed."
                          />
                          <input
                            aria-label={`Execution external reference for ${decision.id}`}
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
