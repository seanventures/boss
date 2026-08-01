import {
  applyChangeSetAction,
  approveChangeSetAction,
  completeEntitlementConnectionSessionAction,
  createChangeSetAction,
  createDeploymentTargetAction,
  createReplayBundleAction,
  createBreakGlassGrantAction,
  createComplianceEvidenceBundleAction,
  createDeadletterIncidentAction,
  createEntitlementConnectionSessionAction,
  createDecisionDigestAction,
  createOpsDigestAction,
  createDeploymentRunAction,
  completeAccountConnectAuthSessionAction,
  createAccountConnectBillingPortalSessionAction,
  createAccountConnectAuthSessionAction,
  createAccountConnectTeamInviteAction,
  createEnterpriseSupportCaseAction,
  createEnterpriseClusterValidationRunAction,
  installAgentModuleAction,
  installPackAction,
  linkAccountConnectAction,
  queueOutboxMessageAction,
  refreshHealthSnapshotAction,
  recordSelfImprovementCanaryAction,
  recordSelfImprovementRolloutAction,
  disconnectEntitlementSourceAction,
  revokeBreakGlassGrantAction,
  resolveDecisionAction,
  rollbackAgentModuleAction,
  rollbackSelfImprovementProposalAction,
  requeueDeadletterJobAction,
  runAutoscalerPlanAction,
  runAccountConnectHealthCheckAction,
  runDeploymentHealthCheckAction,
  runResourceJanitorAction,
  submitSelfImprovementProposalAction,
  submitOutboxDraftAction,
  submitCommandAction,
  syncAccountConnectAction,
  syncHostedMarketplaceAction,
  syncProviderResourcesAction,
  unlinkAccountConnectAction,
  uninstallAgentModuleAction,
  revokeAccountConnectTeamInviteAction,
  upsertDeploymentEnvironmentPolicyAction,
  upsertEnterpriseClusterAction,
  upsertEnterpriseIdentityProviderAction,
  upsertEnterpriseIdentityRolloutPolicyAction,
  upsertEnterpriseScimGroupAction,
  upsertEnterpriseScimUserAction,
  upsertCommsChannelPolicyAction,
  updateEnterpriseSupportCaseAction,
  upsertOrgMemberAction,
  upsertWorkerPoolAction,
} from "./actions";
import ConsoleShell from "./ConsoleShell";
import { AgentChat } from "./agent-ui";
import { WorkBoard, WorkflowHealth } from "./workflow-ui";
import { MissionAgentRoster, MissionTaskBoard } from "./mission-operations-ui";
import { ReportsView } from "./reports-ui";
import type { DataBoundary } from "@boss/api-client";
import { fetchAuditSummary } from "../lib/audit";
import { fetchArtifactFeed } from "../lib/artifacts";
import { fetchAgentModuleFeed } from "../lib/agent-modules";
import { fetchAgentStudioFeed } from "../lib/agent-studio";
import { AgentStudio } from "./agent-studio-ui";
import { fetchWorkOrchestrationFeed } from "../lib/work-orchestration";
import { WorkOrchestration } from "./work-orchestration-ui";
import { fetchAuthSession } from "../lib/auth-session";
import { fetchInboxFeed, fetchOutboxFeed } from "../lib/comms";
import { fetchBudgetAllocationFeed } from "../lib/budget-allocations";
import { fetchBudgetStatus, fetchBudgetIntensity } from "../lib/budget-status";
import { BudgetIntensitySlider } from "./budget-intensity-ui";
import { fetchLatestCanary } from "../lib/canaries";
import { CanaryCard } from "./canary-ui";
import { fetchDashboard } from "../lib/dashboard";
import { DashboardPanel } from "./dashboard-ui";
import { fetchPolicyEvals, fetchRunTrace } from "../lib/safety";
import { PolicyEvalList, AnomalyScanCard, RunTraceViewer } from "./safety-ui";
import { fetchLatestEval } from "../lib/evals";
import { fetchSkillInvocations } from "../lib/skill-invocations";
import {
  SelfImprovementRuntime,
  EvalHarnessCard,
  AutonomyEvaluateCard,
  RouterHealthCard,
  SkillInvokeCard,
} from "./phase2-ui";
import { fetchAckState } from "../lib/comms-ack";
import { fetchWipStatus } from "../lib/factory";
import {
  InboxAckCard,
  SlackToolsCard,
  MeetingConsensusCard,
  FactoryScaleCard,
} from "./phase3-ui";
import { fetchChaosRuns } from "../lib/chaos";
import { fetchCommitmentReconciliations } from "../lib/finops";
import { ChaosCard, FinopsCard, Soc2Card } from "./phase4-ui";
import { fetchPlans } from "../lib/plans";
import { PlansCard } from "./plans-ui";
import { fetchBreakGlassGrantFeed } from "../lib/break-glass";
import { fetchChangeJournalFeed } from "../lib/change-journal";
import { fetchCommercialFeed } from "../lib/commercial";
import { fetchDeadletterFeed } from "../lib/deadletters";
import { fetchDecisionFeed, type DecisionFeedItem } from "../lib/decisions";
import { fetchEntitlementFeed } from "../lib/entitlements";
import { fetchEnterpriseFeed } from "../lib/enterprise";
import { fetchActivityFeed } from "../lib/events";
import { fetchHealthSnapshotFeed } from "../lib/health-snapshots";
import { fetchInfrastructureResourceFeed } from "../lib/infrastructure";
import { fetchMemberFeed, fetchOrgMemberFeed } from "../lib/members";
import { fetchMeetingFeed } from "../lib/meetings";
import { fetchMetatronQuery } from "../lib/metatron";
import { fetchOpsQueueFeed } from "../lib/ops";
import { fetchWorkflowFeed } from "../lib/workflows";
import { WorkflowRegistryView } from "./workflow-registry-ui";
import { fetchReportSummary } from "../lib/reports";
import { fetchAgentRuns } from "../lib/agent-runs";
import { fetchTaskOutputs } from "../lib/outputs";
import { fetchCapabilities } from "../lib/capabilities";
import { CapabilitiesView } from "./capabilities-ui";
import { fetchMeetingRooms } from "../lib/meeting-rooms";
import { MeetingsHome } from "./meeting-room-ui";
import { fetchCodeTasksFeed, fetchPreviews } from "../lib/code-tasks";
import { CodeTasksHome } from "./code-tasks-ui";
import { fetchReliabilityFeed } from "../lib/reliability";
import { fetchCostInsights, fetchLlmSpend } from "../lib/budget-status";
import { CostInsightsCard, LlmSpendCard, BudgetThresholdsCard } from "./cost-governance-ui";
import { FoundationsScreen, ComponentsScreen } from "./design-system-showcase";
import { AgentOrb } from "./agent-orb";
import { buildAgentRoster } from "../lib/roster";
import { ReliabilityHome } from "./reliability-ui";
import { fetchDiscoveriesFeed } from "../lib/discoveries";
import { DiscoveriesHome } from "./discoveries-ui";
import { fetchPipelineData } from "../lib/pipeline-profiles";
import { PipelineView } from "./pipeline-ui";
import { fetchSandboxFeed } from "../lib/sandboxes";
import { SandboxesHome } from "./sandboxes-ui";
import { AreaLink } from "./area-link";
import { Suspense } from "react";
import { fetchOrgs } from "../lib/orgs";
import { OrgGate, OrgReconnect } from "./org-gate";
import { fetchApprovalRules } from "../lib/approval-rules";
import { ApprovalRulesView } from "./approval-rules-ui";
import { fetchMcpServers } from "../lib/mcp";
import { fetchPackFeed } from "../lib/packs";
import { fetchProviderOpsFeed } from "../lib/providerops";
import { ProviderConfigurationSuite } from "./provider-configuration-ui";
import { fetchGitHubOperationsFeed } from "../lib/github-operations";
import { GitHubOperations } from "./github-operations-ui";
import { fetchRepoActivity } from "../lib/repo-activity";
import { RepoActivityPanel } from "./repo-activity-ui";
import { DeploymentPromotionTimeline } from "./deployment-promotion-timeline";
import { fetchProjectFeed } from "../lib/projects";
import { fetchReceiptFeed } from "../lib/receipts";
import { fetchRoleFeed } from "../lib/roles";
import { fetchRoutingLogFeed } from "../lib/routing";
import { fetchSelfImprovementFeed } from "../lib/self-improvement";
import {
  extractSoftwareFactoryArtifactEvidence,
  extractSoftwareFactoryDecisionEvidence,
} from "../lib/software-factory";
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

function metadataString(
  metadata: Record<string, unknown>,
  key: string,
): string | undefined {
  const value = metadata[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

function formatRecommendation(decision: DecisionFeedItem): string {
  const r = decision.recommendation || {};
  const str = (k: string): string => (typeof r[k] === "string" ? (r[k] as string) : "");
  const action = str("action");
  const title = str("title");
  const pr = str("pull_request_url");
  const prNum = pr ? `#${pr.split("/").pop()}` : "";
  const vstatus = str("validation_status");
  const vsummary = str("validation_summary");

  // A code-change / PR review — spell out WHAT is being approved and its check status.
  if (action === "review_pull_request") {
    return [
      title ? `Approve the code change “${title}”.` : "Approve this code change.",
      pr ? `Pull request ${prNum} is open on GitHub with the full diff to review.` : "",
      vstatus
        ? `Automated checks: ${vstatus.replaceAll("_", " ")}${vsummary ? ` — ${vsummary}` : ""}.`
        : "",
      "Approving opens/merges the PR; rejecting keeps it blocked for changes.",
    ]
      .filter(Boolean)
      .join(" ");
  }
  // A gated agent tool (e.g. publishing the PRD and kicking off the build).
  if (action === "execute_agent_tool") {
    const tool = str("tool");
    if (tool === "publish_document") {
      return `Publish the drafted document${title ? ` “${title}”` : ""}${
        r["start_code_task"] ? " and start the build from it" : ""
      }.`;
    }
    return `Run the gated agent action${tool ? `: ${tool.replaceAll("_", " ")}` : ""}.`;
  }
  if (action === "review_plan_draft") {
    return `Review the drafted plan${title ? ` for “${title}”` : ""} before any code is written.`;
  }
  if (action === "review_deployment") {
    return `Approve deploying${title ? ` “${title}”` : " this change"} to its target environment.`;
  }
  if (action) return action.replaceAll("_", " ");
  return "review required";
}

// The approval rule that raised this decision (derived from recommendation.action).
function decisionRuleLabel(decision: DecisionFeedItem): string {
  const action = typeof decision.recommendation.action === "string" ? decision.recommendation.action : "";
  const tool = typeof decision.recommendation.tool === "string" ? ` (${decision.recommendation.tool})` : "";
  const map: Record<string, string> = {
    execute_agent_tool: `Agent tool requires approval${tool}`,
    resume_agent_run: `Agent action gated — deferred tool${tool}`,
    review_plan_draft: "Plan ready for review",
    review_pull_request: "Pull request review",
    review_deployment: "Deployment review",
    review_infra_provisioning: "Infrastructure provisioning review",
    "role.autonomy_change": "Role autonomy change",
  };
  if (map[action]) return map[action];
  if (action) return action.replaceAll("_", " ").replace(/\./g, " · ");
  return "governance gate";
}

// Plain-English description of what approving a decision actually triggers,
// keyed off recommendation.action (mirrors decisions._handle_approved_follow_on).
// Makes the otherwise-silent follow-on legible so "approve did nothing" is clear.
function approvalEffectLabel(recommendation: Record<string, unknown>): string {
  const action = typeof recommendation.action === "string" ? recommendation.action : "";
  // Meeting PRD publish also auto-starts a governed code task.
  if (
    action === "execute_agent_tool" &&
    (recommendation.start_code_task === true || recommendation.tool === "publish_document")
  ) {
    return "Approving publishes the PRD and starts a governed code task for this task.";
  }
  switch (action) {
    case "review_plan_draft": return "Approving queues a PR proposal (code work) for this task.";
    case "resume_code_task": return "Approving resumes the paused code task.";
    case "review_pull_request": return "Approving authorizes the pull-request merge.";
    case "review_deployment": return "Approving authorizes the deployment.";
    case "review_infra_provisioning": return "Approving authorizes infrastructure provisioning.";
    case "execute_agent_tool": return "Approving runs the deferred agent tool now.";
    case "resume_agent_run": return "Approving resumes the paused agent run.";
    case "role.autonomy_change": return "Approving applies the role autonomy change.";
    case "github.community.dispatch_plan": return "Approving dispatches the community plan into tasks.";
    case "github.community.merge_authorization": return "Approving records merge authorization (maintainer still merges).";
    case "self_improvement.begin_canary":
    case "self_improvement.rollout": return "Approving proceeds with the self-improvement rollout.";
    default: return "Approving records your decision — this type has no automated follow-on (no code task is started).";
  }
}

// Decision Queue (handoff format): category chip for what kind of effect the
// proposal is, colored by the signal palette.
function decisionImpact(decision: DecisionFeedItem): { label: string; tone: string } {
  const action =
    typeof decision.recommendation.action === "string" ? decision.recommendation.action : "";
  if (action === "resume_code_task" || action === "review_plan_draft") return { label: "plan", tone: "amber" };
  if (action === "review_pull_request" || action.startsWith("github.")) return { label: "merge", tone: "blue" };
  if (action === "review_deployment") return { label: "deploy", tone: "teal" };
  if (action === "review_infra_provisioning") return { label: "infra", tone: "amber" };
  if (action === "role.autonomy_change") return { label: "access", tone: "red" };
  if (action.startsWith("self_improvement")) return { label: "rollout", tone: "teal" };
  if (action === "execute_agent_tool") return { label: "publish", tone: "teal" };
  if (action === "resume_agent_run") return { label: "agent tool", tone: "teal" };
  if (action === "resume_code_task") return { label: "plan", tone: "amber" };
  const rule = decisionRuleLabel(decision);
  return { label: rule.length > 18 ? rule.slice(0, 18).trimEnd() + "…" : rule, tone: "muted" };
}

function roleInitials(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "CP";
  const initials =
    words.length === 1 ? words[0].slice(0, 3) : words.map((word) => word[0]).join("").slice(0, 3);
  return initials.toUpperCase();
}

function waitedLabel(createdAt: string): string {
  const created = Date.parse(createdAt);
  if (Number.isNaN(created)) return "";
  const minutes = Math.max(0, Math.floor((Date.now() - created) / 60_000));
  if (minutes < 60) return `waited ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `waited ${hours}h`;
  return `waited ${Math.floor(hours / 24)}d`;
}

// Command Center coordination graph: the handoff's node positions (percent of
// the stage) with their SVG line endpoints (520x340 viewBox) and staggered
// animation periods.
const CC_NODE_SPOTS = [
  { left: 23, top: 24, lineX: 120, lineY: 80, spinSeconds: 3, flowSeconds: 1.1 },
  { left: 77, top: 23, lineX: 400, lineY: 78, spinSeconds: 3.4, flowSeconds: 1.3 },
  { left: 17, top: 68, lineX: 90, lineY: 230, spinSeconds: 2.8, flowSeconds: 1.5 },
  { left: 83, top: 71, lineX: 430, lineY: 240, spinSeconds: 3.2, flowSeconds: 1.2 },
  { left: 38, top: 85, lineX: 200, lineY: 290, spinSeconds: 3.6, flowSeconds: 1.6 },
] as const;

function formatMsDuration(ms?: number): string {
  if (!ms || ms <= 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
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
    capacityState?: string;
  },
): string {
  if (
    source.capacityState === "exhausted" ||
    source.capacityState === "rate_limited" ||
    source.capacityState === "blocked"
  ) {
    return titleCaseToken(source.capacityState);
  }
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
    authMethod?: string;
    connectionStatus?: string;
    connectionRef?: string;
    capacityState?: string;
    capacityResetAt?: string;
  },
): string {
  const authMethod = titleCaseToken(source.authMethod ?? "unknown");
  const capacity = `Capacity ${titleCaseToken(source.capacityState ?? "unknown")}${
    source.capacityResetAt ? ` until ${formatDateTime(source.capacityResetAt)}` : ""
  }`;
  if (source.authMethod?.includes("oauth")) {
    if (source.connectionStatus === "connected" && source.credentialPresent) {
      return `${authMethod} connected through ${
        source.connectionRef ?? source.credentialRef ?? "official session"
      }. ${capacity}.`;
    }
    return `${authMethod} is available, but no active official session is connected. ${capacity}.`;
  }
  if (source.runtimeSupported && source.credentialPresent) {
    return `Adapter support is enabled and ${authMethod} credentials are present. ${capacity}.`;
  }
  if (source.runtimeSupported && !source.credentialPresent) {
    return source.credentialRef
      ? `Adapter support is enabled, but ${source.credentialRef} is not configured.`
      : "Adapter support is enabled, but credentials are missing.";
  }
  return "This source is currently registry inventory only and cannot execute work.";
}

function routingRejectionSummary(rejectedSources: Array<Record<string, unknown>>): string {
  const summary = rejectedSources
    .slice(0, 3)
    .map((source) => {
      const sourceId = typeof source.source_id === "string" ? source.source_id : "source";
      const reason = typeof source.reason === "string" ? source.reason : "rejected";
      const capacityState =
        typeof source.capacity_state === "string" ? ` / ${source.capacity_state}` : "";
      return `${sourceId}: ${reason}${capacityState}`;
    });
  return summary.length > 0 ? summary.join("; ") : "No rejected-source details recorded.";
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
  const requestedArea = getParam(resolvedSearchParams?.area);
  const requestedWorkTask = getParam(resolvedSearchParams?.task);
  const metatronQuestion = getParam(resolvedSearchParams?.q);
  const metatronTaskId = getParam(resolvedSearchParams?.task_id);
  const metatronDecisionId = getParam(resolvedSearchParams?.decision_id);
  const requestedActorId = getParam(resolvedSearchParams?.actor);
  const traceRunId = getParam(resolvedSearchParams?.trace_run);
  const routingProviderFilter = getParam(resolvedSearchParams?.route_provider);
  const routingBudgetFilter = getParam(resolvedSearchParams?.route_budget);
  const routingBoundaryParam = getParam(resolvedSearchParams?.route_boundary);
  const routingBoundaryFilter = (
    ["public", "internal", "confidential", "restricted", "local-only"].includes(
      routingBoundaryParam ?? "",
    )
      ? routingBoundaryParam
      : undefined
  ) as DataBoundary | undefined;
  const routingOutcomeParam = getParam(resolvedSearchParams?.route_outcome);
  const routingOutcomeFilter =
    routingOutcomeParam === "selected" || routingOutcomeParam === "denied"
      ? routingOutcomeParam
      : undefined;
  // ---- Organization gate ----------------------------------------------------
  // An org must be selected before entering the console; all data is scoped to
  // the selected org's project. No org in the URL → show the org picker.
  const requestedOrgId = getParam(resolvedSearchParams?.org);
  const orgs = await fetchOrgs();
  const selectedOrg = requestedOrgId
    ? orgs.items.find((o) => o.org_id === requestedOrgId)
    : undefined;
  if (!selectedOrg) {
    // A transient API failure must NOT dump an in-session operator back to the org
    // picker (it destroys their console state mid-work). URL names an org + the org
    // list didn't load => hold the session and retry, don't re-gate.
    if (requestedOrgId && !orgs.apiReachable) {
      return (
        <Suspense fallback={null}>
          <OrgReconnect orgId={requestedOrgId} reason={orgs.blockedReason} />
        </Suspense>
      );
    }
    return (
      <Suspense fallback={null}>
        <OrgGate orgs={orgs.items} />
      </Suspense>
    );
  }
  const activeOrgId = selectedOrg.org_id;
  const activeOrgName = selectedOrg.org_name;
  const activeProjectId = selectedOrg.project_id;
  const projects = await fetchProjectFeed(activeProjectId);
  const activeProject = projects.items.find(
    (project) => project.id === activeProjectId,
  );
  const [
    status,
    decisions,
    tasks,
    meetings,
    artifacts,
    events,
    healthSnapshots,
    opsQueue,
    deadletters,
    packs,
    roles,
    agentModules,
    commercial,
    enterprise,
    entitlements,
    routingLogs,
    skills,
    providerOps,
    githubOperations,
    repoActivity,
    receipts,
    infrastructureResources,
    budgetStatus,
    budgetIntensity,
    canaryRun,
    plansList,
    budgetAllocations,
    auditSummary,
    changeJournal,
    selfImprovement,
    outbox,
    inbox,
    members,
    orgMembers,
    metatron,
    authSession,
    breakGlassGrants,
    report,
    agentRuns,
    mcpServers,
    taskOutputs,
    capabilities,
    meetingRooms,
    approvalRules,
    codeTasksFeed,
    discoveryFeed,
    previewFeed,
    sandboxFeed,
    pipelineData,
    dashboardSnapshot,
    policyEvals,
    runTrace,
    latestEval,
    skillInvocations,
    ackState,
    wipStatus,
    chaosRuns,
    commitmentRecons,
    workflowFeed,
    agentStudioFeed,
    workOrchestrationFeed,
  ] =
    await Promise.all([
      fetchStatus(activeProjectId),
      fetchDecisionFeed(activeProjectId),
      fetchTaskFeed(activeProjectId, 50),
      fetchMeetingFeed(activeProjectId),
      fetchArtifactFeed(activeProjectId),
      fetchActivityFeed(activeProjectId, 40),
      fetchHealthSnapshotFeed(activeProjectId),
      fetchOpsQueueFeed(activeProjectId),
      fetchDeadletterFeed(activeProjectId),
      fetchPackFeed(activeProjectId),
      fetchRoleFeed(activeProjectId),
      fetchAgentModuleFeed(activeProjectId),
      fetchCommercialFeed(activeProjectId),
      fetchEnterpriseFeed(activeProjectId),
      fetchEntitlementFeed(activeProjectId),
      fetchRoutingLogFeed(activeProjectId, 8, {
        provider: routingProviderFilter,
        budget_state: routingBudgetFilter,
        data_boundary: routingBoundaryFilter,
        selected_only: routingOutcomeFilter === "selected" ? true : undefined,
        denied_only: routingOutcomeFilter === "denied" ? true : undefined,
      }),
      fetchSkillFeed(),
      fetchProviderOpsFeed(activeProjectId),
      fetchGitHubOperationsFeed(activeProjectId),
      fetchRepoActivity(activeProjectId, 30),
      fetchReceiptFeed(activeProjectId, 100),
      fetchInfrastructureResourceFeed(activeProjectId),
      fetchBudgetStatus(activeProjectId),
      fetchBudgetIntensity(activeProjectId),
      fetchLatestCanary(activeProjectId),
      fetchPlans(activeProjectId),
      fetchBudgetAllocationFeed(activeProjectId),
      fetchAuditSummary(activeProjectId),
      fetchChangeJournalFeed(activeProjectId),
      fetchSelfImprovementFeed(activeProjectId),
      fetchOutboxFeed(activeProjectId),
      fetchInboxFeed(activeProjectId),
      fetchMemberFeed(activeProjectId),
      fetchOrgMemberFeed(
        activeOrgId,
        activeProjectId,
        requestedActorId ?? "ceo",
      ),
      fetchMetatronQuery(
        metatronQuestion,
        metatronTaskId,
        metatronDecisionId,
        activeProjectId,
      ),
      fetchAuthSession(),
      fetchBreakGlassGrantFeed(activeProjectId),
      fetchReportSummary(activeProjectId),
      fetchAgentRuns(activeProjectId),
      fetchMcpServers(activeProjectId),
      fetchTaskOutputs(activeProjectId),
      fetchCapabilities(activeProjectId),
      fetchMeetingRooms(activeProjectId),
      fetchApprovalRules(activeProjectId),
      fetchCodeTasksFeed(activeProjectId),
      fetchDiscoveriesFeed(activeProjectId),
      fetchPreviews(activeProjectId),
      fetchSandboxFeed(),
      fetchPipelineData(activeProjectId),
      fetchDashboard(activeProjectId),
      fetchPolicyEvals(activeProjectId),
      fetchRunTrace(traceRunId),
      fetchLatestEval(activeProjectId),
      fetchSkillInvocations(activeProjectId),
      fetchAckState(activeProjectId),
      fetchWipStatus(activeProjectId),
      fetchChaosRuns(activeProjectId),
      fetchCommitmentReconciliations(activeProjectId),
      fetchWorkflowFeed(activeProjectId),
      fetchAgentStudioFeed(activeProjectId),
      fetchWorkOrchestrationFeed(activeProjectId),
    ]);
  const reliabilityFeed = await fetchReliabilityFeed(activeProjectId);
  const [costInsights, llmSpend] = await Promise.all([
    fetchCostInsights(activeProjectId),
    fetchLlmSpend(activeProjectId),
  ]);

  const deadletterCount = deadletters.items.length;
  const disableCommandEntry = !status.apiReachable || !status.databaseConnected;
  const installedCorePack = packs.items.find((pack) => pack.installed);
  const actorOptions = [
    ...members.items.map((member) => ({ ...member, source: "project" as const })),
    ...orgMembers.items
      .filter(
        (orgMember) =>
          !members.items.some((member) => member.memberId === orgMember.memberId),
      )
      .map((member) => ({ ...member, source: "org" as const })),
  ];
  const activeActorId =
    requestedActorId &&
    actorOptions.some((member) => member.memberId === requestedActorId)
      ? requestedActorId
      : actorOptions.some((member) => member.memberId === "executive_console")
        ? "executive_console"
        : actorOptions[0]?.memberId ?? "executive_console";
  const activeActor = actorOptions.find(
    (member) => member.memberId === activeActorId,
  );
  const canManageGitHub =
    activeActor?.status === "active" &&
    activeActor.permissions.includes("github_manage");
  const softwareFactoryDecisionEvidence = extractSoftwareFactoryDecisionEvidence(
    decisions.items,
  );
  const softwareFactoryArtifactEvidence = extractSoftwareFactoryArtifactEvidence(
    artifacts.items,
  );
  // Command Center (handoff format): live agent states for the coordination
  // graph — the busiest agents first so live work is what orbits the core.
  const ccRoster = buildAgentRoster(roles.items, tasks.items);
  const ccStateRank = { running: 0, blocked: 1, verified: 2, idle: 3 } as const;
  const ccGraphAgents = [...ccRoster]
    .sort((left, right) => ccStateRank[left.state] - ccStateRank[right.state])
    .slice(0, CC_NODE_SPOTS.length);
  const ccVerifiedToday = receipts.items.filter(
    (receipt) => Date.now() - Date.parse(receipt.createdAt) < 86_400_000,
  ).length;

  return (
    <ConsoleShell
      status={{
        apiReachable: status.apiReachable,
        budgetState: status.budgetState,
        deadletters: deadletterCount,
        pendingDecisions: status.pendingDecisions,
      }}
      scope={{
        project: activeProject?.name ?? activeProjectId,
        actor: activeActor?.displayName ?? activeActorId,
      }}
      org={{ id: activeOrgId, name: activeOrgName }}
      initialArea={requestedArea}
    >
      <div className="frame">
        <section className="panel-stack" data-area="agent" aria-label="BOSS conversational agent">
          <AgentChat projectId={activeProjectId} actor={activeActorId} initialTaskId={requestedWorkTask || undefined} />
        </section>
        <section className="panel-stack" data-area="dashboard" aria-label="Live dashboard">
          <div className="section-head">
            <div>
              <span className="eyebrow">Live Dashboard</span>
              <h2>Cross-phase control snapshot</h2>
            </div>
          </div>
          <DashboardPanel snapshot={dashboardSnapshot} />
        </section>

        <RepoActivityPanel repos={repoActivity} />

        <GitHubOperations
          feed={githubOperations}
          actor={activeActorId}
          canManage={canManageGitHub}
        />

        <section className="panel-stack mission-roster-section" data-area="org" aria-label="Agent roster">
          <div className="section-head">
            <div>
              <span className="eyebrow">Agent Roster</span>
              <h2>Operators and autonomous roles active in this project.</h2>
            </div>
            <span className="ar-meta">
              {ccRoster.length} specialized workers ·{" "}
              <span className="ar-meta-active">
                {ccRoster.filter((agent) => agent.state !== "idle").length} active
              </span>
            </span>
          </div>
          <MissionAgentRoster roles={roles.items} tasks={tasks.items} />
        </section>

        <section className="panel-stack" data-area="studio" aria-label="Agent Studio">
          <AgentStudio
            feed={agentStudioFeed}
            roles={roles.items}
            skills={skills.items}
          />
        </section>

        <section className="panel-stack" data-area="portfolio" aria-label="Work allocation">
          <WorkOrchestration
            feed={workOrchestrationFeed}
            actor={activeActorId}
          />
        </section>

        <section className="ov-cc" data-area="overview" aria-label="Command center">
          <form action={submitCommandAction} className="cc-composer">
            <input type="hidden" name="project_id" value={activeProjectId} />
            <input type="hidden" name="actor" value={activeActorId} />
            <span className="cc-spinner" aria-hidden />
            <input
              aria-label="Outcome command"
              className="cc-input"
              name="command"
              placeholder="Assign an outcome to your team…"
              disabled={disableCommandEntry}
              required
            />
            <span className="cc-kbd" aria-hidden>⌘K</span>
            <button className="cc-assign" disabled={disableCommandEntry} type="submit">
              Assign →
            </button>
          </form>

          <div className="cc-kpis">
            <AreaLink className="cc-kpi" area="work" orgId={activeOrgId}>
              <span className="cc-kpi-l">Outcomes in flight</span>
              <span className="cc-kpi-n">{status.openTasks}</span>
            </AreaLink>
            <AreaLink className="cc-kpi teal" area="org" orgId={activeOrgId}>
              <span className="cc-kpi-l">Agents active</span>
              <span className="cc-kpi-n">
                {ccRoster.filter((agent) => agent.state !== "idle").length}
                <small>/{ccRoster.length}</small>
              </span>
            </AreaLink>
            <AreaLink className="cc-kpi amber" area="decisions" orgId={activeOrgId}>
              <span className="cc-kpi-l">Awaiting you</span>
              <span className="cc-kpi-n">{status.pendingDecisions}</span>
            </AreaLink>
            <AreaLink className="cc-kpi green" area="reports" orgId={activeOrgId}>
              <span className="cc-kpi-l">Verified today</span>
              <span className="cc-kpi-n">{ccVerifiedToday}</span>
            </AreaLink>
          </div>

          <div className="cc-grid">
            <article className="cc-coord" aria-label="Live coordination">
              <div className="cc-panel-head">
                <strong>Live coordination</strong>
                <span className="cc-panel-meta">
                  {ccRoster.filter((agent) => agent.state === "running").length} running
                </span>
              </div>
              <div className="cc-stage">
                <svg className="cc-stage-lines" viewBox="0 0 520 340" aria-hidden>
                  <g strokeWidth="1.6" fill="none" strokeDasharray="4 6">
                    {CC_NODE_SPOTS.slice(0, ccGraphAgents.length).map((spot, index) => (
                      <line
                        key={index}
                        x1="260"
                        y1="170"
                        x2={spot.lineX}
                        y2={spot.lineY}
                        stroke={index % 2 === 0 ? "#2f6fe0" : "#2ec5c5"}
                        style={{ animation: `bpFlow ${spot.flowSeconds}s linear infinite` }}
                      />
                    ))}
                    {ccGraphAgents.length >= 2 ? (
                      <line
                        x1={CC_NODE_SPOTS[0].lineX}
                        y1={CC_NODE_SPOTS[0].lineY}
                        x2={CC_NODE_SPOTS[1].lineX}
                        y2={CC_NODE_SPOTS[1].lineY}
                        stroke="#26365c"
                        style={{ animation: "bpFlow 2s linear infinite" }}
                      />
                    ) : null}
                  </g>
                </svg>
                <div className="cc-core">
                  <div className="cc-core-tile">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src="/boss-mark.png" alt="" />
                  </div>
                  <span className="cc-core-label">BOSS core</span>
                </div>
                {ccGraphAgents.map((agent, index) => {
                  const spot = CC_NODE_SPOTS[index];
                  return (
                    <div
                      className="cc-node"
                      key={agent.id}
                      style={{ left: `${spot.left}%`, top: `${spot.top}%` }}
                    >
                      <AgentOrb
                        label={roleInitials(agent.name)}
                        state={agent.state}
                        spinSeconds={spot.spinSeconds}
                        title={`${agent.name} — ${agent.state}`}
                      />
                      <span className="cc-node-label">{agent.name}</span>
                    </div>
                  );
                })}
              </div>
            </article>

            <article className="cc-feed" aria-label="What changed">
              <div className="cc-panel-head">
                <span className="cc-live-dot" aria-hidden />
                <strong>What changed</strong>
                <span className="cc-feed-meta">live</span>
              </div>
              <div className="cc-feed-list">
                {events.items.slice(0, 8).map((event) => (
                  <div className="cc-feed-row" key={event.id}>
                    <span className="cc-feed-orb" aria-hidden>{roleInitials(event.actor)}</span>
                    <div className="cc-feed-main">
                      <div className="cc-feed-text">
                        <b>{event.actor}</b> {event.eventType.replaceAll("_", " ").replaceAll(".", " · ")}{" "}
                        <span>{event.entityType}</span>
                      </div>
                      <div className="cc-feed-sub">
                        <time dateTime={event.createdAt}>{formatDateTime(event.createdAt)}</time>
                      </div>
                    </div>
                  </div>
                ))}
                {events.items.length === 0 ? (
                  <p className="empty-state">No activity yet. Assign an outcome to start the event stream.</p>
                ) : null}
              </div>
            </article>
          </div>
          <div className="ov-actions">
            <span className="ov-actions-label">Quick actions</span>
            <AreaLink className="ov-action primary" area="meetings" orgId={activeOrgId}>Convene a panel</AreaLink>
            <AreaLink className="ov-action" area="code_tasks" orgId={activeOrgId}>Start a code task</AreaLink>
            <AreaLink className="ov-action" area="decisions" orgId={activeOrgId}>Review approvals</AreaLink>
            <AreaLink className="ov-action" area="reports" orgId={activeOrgId}>View evidence</AreaLink>
          </div>
        </section>

        <section className="stack-grid" data-area="overview">
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

        <section className="grid" data-area="overview">
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
                  ? authSession.authMethod ?? "API key"
                  : "Local"}
            </div>
            <p className="detail">
              {authSession.apiReachable
                ? authSession.message
                : authSession.blockedReason ?? "Auth session API unavailable."}
            </p>
            <p className="detail">
              Transport:{" "}
              {authSession.transportAuthenticated ? "authenticated" : "open"} /
              method {authSession.authMethod ?? "none"} / OIDC{" "}
              {authSession.oidcEnabled ? "enabled" : "disabled"}
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

        <section className="grid" data-area="overview">
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

        <section className="bx-overview" data-area="overview" aria-label="Overview summary">
          <div className="bx-overview-split">
            <article
              className="bx-card"
              style={{ padding: "22px", display: "grid", gap: "14px" }}
            >
              <div className="section-head">
                <div>
                  <span className="eyebrow">Needs attention</span>
                  <h2 style={{ fontSize: "1.18rem", margin: "4px 0 0" }}>
                    Pending decisions
                  </h2>
                </div>
                <span
                  className={`pill${status.pendingDecisions > 0 ? " warn" : ""}`}
                >
                  {status.pendingDecisions} open
                </span>
              </div>
              {decisions.items.filter(
                (decision) =>
                  decision.status === "pending" ||
                  decision.status === "delegated",
              ).length > 0 ? (
                <div className="bx-attention">
                  {decisions.items
                    .filter(
                      (decision) =>
                        decision.status === "pending" ||
                        decision.status === "delegated",
                    )
                    .slice(0, 4)
                    .map((decision) => (
                      <div className="bx-attention-item" key={decision.id}>
                        <span className="bx-tier">T{decision.riskTier}</span>
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontWeight: 600 }}>
                            {decision.question}
                          </div>
                          <p
                            className="detail"
                            style={{ fontSize: "0.84rem" }}
                          >
                            Recommends {formatRecommendation(decision)} /{" "}
                            {decision.status}
                          </p>
                        </div>
                      </div>
                    ))}
                </div>
              ) : (
                <p className="detail">
                  No decisions are waiting. The queue is clear.
                </p>
              )}
            </article>

            <article
              className="bx-card"
              style={{ padding: "22px", display: "grid", gap: "14px" }}
            >
              <div className="section-head">
                <div>
                  <span className="eyebrow">What changed</span>
                  <h2 style={{ fontSize: "1.18rem", margin: "4px 0 0" }}>
                    Recent activity
                  </h2>
                </div>
              </div>
              {events.apiReachable && events.items.length > 0 ? (
                <div className="feed-list">
                  {events.items.slice(0, 6).map((event) => (
                    <div className="feed-row" key={event.id}>
                      <div style={{ minWidth: 0 }}>
                        <h4 style={{ margin: 0 }}>
                          {titleCaseToken(event.eventType)}
                        </h4>
                        <p
                          className="detail"
                          style={{ fontSize: "0.82rem" }}
                        >
                          {event.actor} / {formatDateTime(event.createdAt)}
                        </p>
                      </div>
                      <code className="mono">{event.entityId}</code>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="detail">No activity recorded yet.</p>
              )}
            </article>
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="org">Org Delegation</span>
              <h2>Organization-wide actors with scoped project authorization.</h2>
            </div>
            <p className="status-meta">
              PRD 9.2 identity and access: delegated actors are explicit,
              permissioned, and audited through `/v1/org-members`.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Add or update org delegate</h3>
              <form action={upsertOrgMemberAction} className="composer compact">
                <input type="hidden" name="project_id" value={activeProjectId} />
                <input type="hidden" name="org_id" value={activeOrgId} />
                <input type="hidden" name="actor" value={activeActorId} />
                <div className="input-grid">
                  <label className="field">
                    <span>Member ID</span>
                    <input
                      className="input"
                      name="member_id"
                      placeholder="ops_delegate"
                      required
                    />
                  </label>
                  <label className="field">
                    <span>Display name</span>
                    <input
                      className="input"
                      name="display_name"
                      placeholder="Ops Delegate"
                      required
                    />
                  </label>
                </div>
                <div className="input-grid">
                  <label className="field">
                    <span>Member type</span>
                    <select className="input" name="member_type" defaultValue="human">
                      <option value="human">Human</option>
                      <option value="agent">Agent</option>
                      <option value="service">Service</option>
                    </select>
                  </label>
                  <label className="field">
                    <span>Roles</span>
                    <textarea
                      className="textarea"
                      name="role_keys"
                      rows={3}
                      placeholder={"One role per line\noperator\nprogram_manager"}
                    />
                  </label>
                </div>
                <label className="field">
                  <span>Permissions</span>
                  <textarea
                    className="textarea"
                    name="permissions"
                    rows={4}
                    placeholder={"One permission per line\ncommand_submit\ndecision_resolve\nmember_manage"}
                    required
                  />
                </label>
                <button className="button secondary" type="submit">
                  Save org delegate
                </button>
              </form>
            </article>

            <article className="stack-card">
              <h3>Delegates for {activeOrgId}</h3>
              {!orgMembers.apiReachable ? (
                <p className="detail">
                  {orgMembers.blockedReason ??
                    "Org-member API is unavailable for this actor."}
                </p>
              ) : orgMembers.items.length === 0 ? (
                <p className="detail">
                  No org-level delegates are configured for this organization.
                </p>
              ) : (
                <div className="feed-list">
                  {orgMembers.items.map((member) => (
                    <div className="feed-row" key={member.id}>
                      <div>
                        <h4>
                          {member.displayName} / {member.memberId}
                        </h4>
                        <p className="detail">
                          Scope {member.orgId}
                          {member.projectId ? ` / ${member.projectId}` : ""} /
                          status {member.status}
                        </p>
                        <p className="detail">
                          Roles: {member.roleKeys.join(", ") || "none"} /
                          permissions: {member.permissions.join(", ") || "none"}
                        </p>
                        <p className="detail">
                          Created by {member.createdBy} / updated{" "}
                          {formatDateTime(member.updatedAt)}
                        </p>
                      </div>
                      <code className="mono">org member</code>
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
              <span className="eyebrow" data-area="org">Break-Glass Access</span>
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
                    {actorOptions.map((member) => (
                      <option key={member.id} value={member.memberId}>
                        {member.displayName} ({member.memberId}, {member.source})
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
              <span className="eyebrow" data-area="budget">Budget Control Center</span>
              <h2>Live spend, forecast, and cap pressure.</h2>
            </div>
            <p className="status-meta">
              Source: `/v1/budget/status` for project {activeProjectId}.
            </p>
          </div>

          <BudgetIntensitySlider projectId={activeProjectId} current={budgetIntensity} />

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

        <section className="panel-stack" data-area="budget" aria-label="Cost governance">
          <div className="section-head">
            <div>
              <span className="eyebrow">Cost Governance</span>
              <h2>Real model spend, enforced thresholds, and the top cost saves.</h2>
            </div>
            <p className="status-meta">
              llm_calls is the ledger of record — per agent, per model, cache leverage.
            </p>
          </div>
          <div className="stack-grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}>
            <CostInsightsCard feed={costInsights} />
            <LlmSpendCard feed={llmSpend} />
            <BudgetThresholdsCard status={budgetStatus} />
          </div>
        </section>

        <section className="panel-stack" data-area="budget" aria-label="FinOps reconciler">
          <div className="section-head">
            <div>
              <span className="eyebrow">FinOps · Commitments &amp; Initiatives</span>
              <h2>Reconcile committed spend and gate big initiatives.</h2>
            </div>
            <p className="status-meta">
              Commitment utilization vs actual usage, and tier-2+ initiative budget approval.
            </p>
          </div>
          <div className="stack-grid">
            <FinopsCard projectId={activeProjectId} reconciliations={commitmentRecons} />
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="budget">Budget Allocations</span>
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

        <section className="panel-stack" data-area="infra" aria-label="MCP servers">
          <div className="section-head">
            <div>
              <span className="eyebrow">MCP Servers</span>
              <h2>Registered tool backends the agent can call — governed by policy, budget, and receipts.</h2>
            </div>
          </div>
          {mcpServers.items.length === 0 ? (
            <p className="detail">
              {mcpServers.apiReachable
                ? "No MCP servers registered. Register one via POST /v1/mcp/servers."
                : mcpServers.blockedReason ?? "MCP registry unavailable."}
            </p>
          ) : (
            <div className="feed-list">
              {mcpServers.items.map((server) => (
                <div className="feed-row" key={server.id}>
                  <div>
                    <h4 style={{ margin: "0 0 4px", display: "flex", gap: 8, alignItems: "center" }}>
                      {server.name}
                      <span className={`pill${server.enabled ? "" : " warn"}`}>
                        {server.enabled ? "enabled" : "disabled"}
                      </span>
                    </h4>
                    {server.description ? <p className="detail">{server.description}</p> : null}
                    <p className="detail">
                      <code className="mono">{server.url}</code> · {server.transport} · risk tier{" "}
                      {server.risk_tier} · {server.data_boundary}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="infra">ProviderOps</span>
              <h2>Elastic Resource Plane for real provider assets.</h2>
            </div>
            <p className="status-meta">
              Provider APIs stay behind Resource Plane gates: ChangeSet, policy,
              budget, approval, receipt, reconciliation.
            </p>
          </div>

          <ProviderConfigurationSuite
            feed={providerOps}
            projectId={activeProjectId}
            actor={activeActorId}
            canManage={
              activeActor?.status === "active" &&
              activeActor.permissions.includes("providerops_manage")
            }
            events={events.items}
          />

          {providerOps.apiReachable ? (
            <>
              <div className="stack-grid">
                <article className="stack-card" id="provider-resources">
                  <h3>Resource Explorer</h3>
                  <div className="button-stack">
                    <form action={syncProviderResourcesAction}>
                      <input type="hidden" name="project_id" value={activeProjectId} />
                      <input type="hidden" name="actor" value={activeActorId} />
                      <button className="button secondary" type="submit">
                        Sync all providers
                      </button>
                    </form>
                    <form action={runResourceJanitorAction} className="composer compact">
                      <input type="hidden" name="project_id" value={activeProjectId} />
                      <input type="hidden" name="actor" value={activeActorId} />
                      <label className="field">
                        <span>Janitor provider scope</span>
                        <select className="input" name="provider_account_id">
                          <option value="">All provider accounts</option>
                          {providerOps.providerAccounts.map((account) => (
                            <option key={account.id} value={account.id}>
                              {account.provider} / {account.accountLabel}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="field">
                        <span>Max cleanup candidates</span>
                        <input
                          className="input"
                          name="max_candidates"
                          type="number"
                          min={1}
                          max={100}
                          defaultValue={10}
                        />
                      </label>
                      <button className="button secondary" type="submit">
                        Run Resource Janitor
                      </button>
                      <p className="detail">
                        Creates a report artifact and approval-required cleanup
                        ChangeSets. It never deletes production resources or calls
                        providers directly.
                      </p>
                    </form>
                  </div>
                  {providerOps.resources.length === 0 ? (
                    <p className="detail">
                      No external resources have been synced for this project.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {providerOps.resources.slice(0, 8).map((resource) => (
                        <div className="feed-row" key={resource.id}>
                          <div>
                            <h4>{resource.name}</h4>
                            <p className="detail">
                              {resource.provider} {resource.resourceType} /{" "}
                              {resource.state} / {resource.ownership}
                            </p>
                            <p className="detail">
                              {resource.environment} / health{" "}
                              {resource.healthStatus} / cost{" "}
                              {resource.costConfidence}
                            </p>
                            <p className="detail">
                              Last seen {formatDateTime(resource.lastSeenAt)}
                            </p>
                          </div>
                          <code className="mono">{resource.externalId}</code>
                        </div>
                      ))}
                    </div>
                  )}
                </article>

                <article className="stack-card" id="provider-scaling">
                  <h3>Budget-Aware Autoscaler</h3>
                  <form action={upsertWorkerPoolAction} className="composer compact">
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <div className="input-grid">
                      <label className="field">
                        <span>Pool key</span>
                        <input
                          className="input"
                          name="pool_key"
                          placeholder="local-default"
                          required
                        />
                      </label>
                      <label className="field">
                        <span>Kind</span>
                        <select className="input" name="kind" required>
                          <option value="local">Local</option>
                          <option value="cloud">Cloud</option>
                        </select>
                      </label>
                    </div>
                    <label className="field">
                      <span>Provider account for cloud pools</span>
                      <select className="input" name="provider_account_id">
                        <option value="">No provider account</option>
                        {providerOps.providerAccounts.map((account) => (
                          <option key={account.id} value={account.id}>
                            {account.provider} / {account.accountLabel}
                          </option>
                        ))}
                      </select>
                    </label>
                    <div className="input-grid">
                      <label className="field">
                        <span>Current workers</span>
                        <input
                          className="input"
                          name="current_workers"
                          type="number"
                          min={0}
                          defaultValue={0}
                        />
                      </label>
                      <label className="field">
                        <span>Desired workers</span>
                        <input
                          className="input"
                          name="desired_workers"
                          type="number"
                          min={0}
                          defaultValue={0}
                        />
                      </label>
                    </div>
                    <div className="input-grid">
                      <label className="field">
                        <span>Min workers</span>
                        <input
                          className="input"
                          name="min_workers"
                          type="number"
                          min={0}
                          defaultValue={0}
                        />
                      </label>
                      <label className="field">
                        <span>Max workers</span>
                        <input
                          className="input"
                          name="max_workers"
                          type="number"
                          min={0}
                          defaultValue={2}
                        />
                      </label>
                    </div>
                    <div className="input-grid">
                      <label className="field">
                        <span>Queue depth per worker</span>
                        <input
                          className="input"
                          name="max_queue_depth_per_worker"
                          type="number"
                          min={1}
                          defaultValue={5}
                        />
                      </label>
                      <label className="field">
                        <span>Hourly cost micros</span>
                        <input
                          className="input"
                          name="cost_per_worker_hour_usd_micros"
                          type="number"
                          min={0}
                          defaultValue={0}
                        />
                      </label>
                    </div>
                    <label className="field">
                      <span>Capabilities</span>
                      <textarea
                        className="textarea"
                        name="capabilities"
                        rows={2}
                        placeholder={"One capability per line\nsoftware_factory.propose_pr"}
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Register worker pool
                    </button>
                  </form>

                  <form action={runAutoscalerPlanAction} className="composer compact">
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Worker pool</span>
                      <select className="input" name="worker_pool_id">
                        <option value="">Default local pool</option>
                        {providerOps.workerPools.map((pool) => (
                          <option key={pool.id} value={pool.id}>
                            {pool.kind} / {pool.poolKey} / desired{" "}
                            {pool.desiredWorkers}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field checkbox-field">
                      <span>Allow cloud spillover proposal</span>
                      <input
                        name="cloud_spillover_enabled"
                        type="checkbox"
                        value="true"
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Run autoscaler review
                    </button>
                    <p className="detail">
                      Local pool desired capacity may change immediately. Cloud
                      scale-up is held as a BudgetReservation-backed ChangeSet for
                      approval.
                    </p>
                  </form>

                  {providerOps.workerPools.length === 0 ? (
                    <p className="detail">
                      No worker pools registered yet. Running autoscaler review can
                      create the default local pool.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {providerOps.workerPools.slice(0, 4).map((pool) => (
                        <div className="feed-row" key={pool.id}>
                          <div>
                            <h4>{pool.poolKey}</h4>
                            <p className="detail">
                              {pool.kind} / current {pool.currentWorkers} /
                              desired {pool.desiredWorkers} / max{" "}
                              {pool.maxWorkers}
                            </p>
                            <p className="detail">
                              Queue per worker {pool.maxQueueDepthPerWorker} /
                              hourly cost{" "}
                              {formatMicros(pool.costPerWorkerHourUsdMicros)}
                            </p>
                          </div>
                          <code className="mono">{pool.provider ?? pool.status}</code>
                        </div>
                      ))}
                    </div>
                  )}

                  {providerOps.scalePlans.length > 0 ? (
                    <div className="feed-list">
                      {providerOps.scalePlans.slice(0, 4).map((plan) => (
                        <div className="feed-row" key={plan.id}>
                          <div>
                            <h4>
                              {plan.status} / {titleCaseToken(plan.triggerReason)}
                            </h4>
                            <p className="detail">
                              Queue {plan.queueDepth} / desired{" "}
                              {plan.desiredWorkersBefore} to{" "}
                              {plan.desiredWorkersAfter} / target{" "}
                              {plan.targetWorkers}
                            </p>
                            <p className="detail">
                              Budget {plan.budgetState} / hourly delta{" "}
                              {formatMicros(plan.estimatedHourlyDeltaUsdMicros)}
                            </p>
                          </div>
                          <code className="mono">
                            {plan.changesetId ?? plan.decisionId ?? plan.id}
                          </code>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </article>

                <article className="stack-card">
                  <h3>ChangeSet Review</h3>
                  <form action={createChangeSetAction} className="composer compact">
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Provider account</span>
                      <select className="input" name="provider_account_id">
                        <option value="">No provider account</option>
                        {providerOps.providerAccounts.map((account) => (
                          <option key={account.id} value={account.id}>
                            {account.provider} / {account.accountLabel}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field">
                      <span>Title</span>
                      <input
                        className="input"
                        name="title"
                        placeholder="Create staging DNS record"
                        required
                      />
                    </label>
                    <label className="field">
                      <span>Summary</span>
                      <textarea
                        className="textarea"
                        name="summary"
                        rows={2}
                        placeholder="Explain the provider change and expected impact."
                        required
                      />
                    </label>
                    <label className="field">
                      <span>Operations JSON</span>
                      <textarea
                        className="textarea"
                        name="operations_json"
                        rows={6}
                        placeholder={'[{"skill_id":"cloudflare.dns.record.upsert","action":"cloudflare.dns.record.upsert","resource_type":"dns_record","risk_tier":2,"production_affecting":false}]'}
                        required
                      />
                    </label>
                    <div className="input-grid">
                      <label className="field">
                        <span>Expected cost micros</span>
                        <input
                          className="input"
                          name="expected_cost_usd_micros"
                          type="number"
                          min={0}
                          defaultValue={0}
                        />
                      </label>
                      <label className="field">
                        <span>Risk tier</span>
                        <input
                          className="input"
                          name="risk_tier"
                          type="number"
                          min={1}
                          max={4}
                          defaultValue={2}
                        />
                      </label>
                    </div>
                    <label className="field">
                      <span>Rollback plan</span>
                      <textarea
                        className="textarea"
                        name="rollback_plan"
                        rows={3}
                        placeholder={"One rollback step per line\nDelete DNS record"}
                        required
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Create ChangeSet
                    </button>
                  </form>

                  {providerOps.changeSets.length === 0 ? (
                    <p className="detail">No Resource Plane ChangeSets yet.</p>
                  ) : (
                    <div className="feed-list">
                      {providerOps.changeSets.map((changeSet) => (
                        <div className="feed-row" key={changeSet.id}>
                          <div>
                            <h4>{changeSet.title}</h4>
                            <p className="detail">
                              {changeSet.status} / risk {changeSet.riskTier} /{" "}
                              {formatMicros(changeSet.expectedCostUsdMicros)}
                            </p>
                            <p className="detail">
                              {changeSet.operations.length} operation
                              {changeSet.operations.length === 1 ? "" : "s"} /{" "}
                              {changeSet.reservationId ?? "no reservation"}
                            </p>
                            {changeSet.failureReason ? (
                              <p className="detail">
                                Failure: {changeSet.failureReason}
                              </p>
                            ) : null}
                          </div>
                          <div className="button-stack">
                            {changeSet.status === "pending_approval" ? (
                              <form action={approveChangeSetAction}>
                                <input
                                  type="hidden"
                                  name="changeset_id"
                                  value={changeSet.id}
                                />
                                <input
                                  type="hidden"
                                  name="project_id"
                                  value={activeProjectId}
                                />
                                <input
                                  type="hidden"
                                  name="actor"
                                  value={activeActorId}
                                />
                                <button className="button secondary" type="submit">
                                  Approve
                                </button>
                              </form>
                            ) : null}
                            {changeSet.status === "approved" ? (
                              <form action={applyChangeSetAction}>
                                <input
                                  type="hidden"
                                  name="changeset_id"
                                  value={changeSet.id}
                                />
                                <input
                                  type="hidden"
                                  name="project_id"
                                  value={activeProjectId}
                                />
                                <input
                                  type="hidden"
                                  name="actor"
                                  value={activeActorId}
                                />
                                <button className="button secondary" type="submit">
                                  Apply
                                </button>
                              </form>
                            ) : (
                              <code className="mono">{changeSet.id}</code>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </article>

                <article className="stack-card">
                  <h3>Deployment Registry</h3>
                  <form
                    action={upsertDeploymentEnvironmentPolicyAction}
                    className="composer compact"
                  >
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <div className="input-grid">
                      <label className="field">
                        <span>Environment policy</span>
                        <select className="input" name="environment" required>
                          <option value="staging">Staging</option>
                          <option value="production">Production</option>
                          <option value="preview">Preview</option>
                        </select>
                      </label>
                      <label className="field">
                        <span>Runtime mode</span>
                        <select className="input" name="runtime_mode" required>
                          <option value="metadata_only">Metadata only</option>
                          <option value="preview_allowed">Preview allowed</option>
                          <option value="staging_allowed">Staging allowed</option>
                          <option value="production_allowed">
                            Production allowed
                          </option>
                          <option value="disabled">Disabled</option>
                        </select>
                      </label>
                    </div>
                    <label className="field">
                      <span>Allowed target kinds</span>
                      <textarea
                        className="textarea"
                        name="allowed_target_kinds"
                        rows={2}
                        placeholder={"One target kind per line\ndigitalocean_app"}
                      />
                    </label>
                    <div className="metric-row">
                      <label>
                        <input
                          type="checkbox"
                          name="requires_change_set"
                          defaultChecked
                        />{" "}
                        ChangeSet
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          name="requires_budget_reservation"
                          defaultChecked
                        />{" "}
                        BudgetReservation
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          name="requires_approval"
                          defaultChecked
                        />{" "}
                        Approval
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          name="requires_health_check"
                          defaultChecked
                        />{" "}
                        Health check
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          name="requires_rollback_plan"
                          defaultChecked
                        />{" "}
                        Rollback plan
                      </label>
                    </div>
                    <label className="field">
                      <span>Rollout notes</span>
                      <textarea
                        className="textarea"
                        name="rollout_notes"
                        rows={2}
                        placeholder="Why this environment is allowed to perform provider-backed rollouts."
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Save environment policy
                    </button>
                    <p className="detail">
                      Provider-backed staging and production runs stay blocked
                      until an active environment policy allows the runtime mode
                      and required gates.
                    </p>
                  </form>

                  {providerOps.deploymentPolicies.length > 0 ? (
                    <div className="feed-list">
                      {providerOps.deploymentPolicies.map((policy) => (
                        <div className="feed-row" key={policy.id}>
                          <div>
                            <h4>
                              {policy.environment} / {policy.runtimeMode}
                            </h4>
                            <p className="detail">
                              Target kinds:{" "}
                              {policy.allowedTargetKinds.length > 0
                                ? policy.allowedTargetKinds.join(", ")
                                : "all"}
                            </p>
                            <p className="detail">
                              Gates:{" "}
                              {[
                                policy.requiresChangeSet ? "ChangeSet" : null,
                                policy.requiresBudgetReservation
                                  ? "BudgetReservation"
                                  : null,
                                policy.requiresApproval ? "approval" : null,
                                policy.requiresHealthCheck ? "health" : null,
                                policy.requiresRollbackPlan ? "rollback" : null,
                              ]
                                .filter(Boolean)
                                .join(", ") || "none"}
                            </p>
                          </div>
                          <code className="mono">{policy.status}</code>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="detail">
                      No deployment environment policies configured yet.
                    </p>
                  )}

                  <form
                    action={createDeploymentTargetAction}
                    className="composer compact"
                  >
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <div className="input-grid">
                      <label className="field">
                        <span>App name</span>
                        <input
                          className="input"
                          name="app_name"
                          placeholder="BOSS"
                          required
                        />
                      </label>
                      <label className="field">
                        <span>Repo</span>
                        <input
                          className="input"
                          name="repo"
                          placeholder="seanheiney/boss"
                          required
                        />
                      </label>
                    </div>
                    <div className="input-grid">
                      <label className="field">
                        <span>Environment</span>
                        <select className="input" name="environment" required>
                          <option value="local">Local</option>
                          <option value="preview">Preview</option>
                          <option value="staging">Staging</option>
                          <option value="production">Production</option>
                        </select>
                      </label>
                      <label className="field">
                        <span>Provider account</span>
                        <select className="input" name="provider_account_id">
                          <option value="">No provider account</option>
                          {providerOps.providerAccounts.map((account) => (
                            <option key={account.id} value={account.id}>
                              {account.provider} / {account.accountLabel}
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>
                    <div className="input-grid">
                      <label className="field">
                        <span>Target kind</span>
                        <input
                          className="input"
                          name="target_kind"
                          placeholder="digitalocean_app"
                          required
                        />
                      </label>
                      <label className="field">
                        <span>Target ref</span>
                        <input
                          className="input"
                          name="target_ref"
                          placeholder="app-id-or-service-name"
                          required
                        />
                      </label>
                    </div>
                    <label className="field">
                      <span>Domains</span>
                      <textarea
                        className="textarea"
                        name="domains"
                        rows={2}
                        placeholder={"One domain per line\nstaging.example.com"}
                      />
                    </label>
                    <label className="field">
                      <span>Secret refs</span>
                      <textarea
                        className="textarea"
                        name="secret_refs"
                        rows={2}
                        placeholder={"One secret ref per line\nsec_database_url"}
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Register deployment target
                    </button>
                  </form>

                  <form action={createDeploymentRunAction} className="composer compact">
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Deployment target</span>
                      <select className="input" name="deployment_target_id" required>
                        <option value="">Select target</option>
                        {providerOps.deploymentTargets.map((target) => (
                          <option key={target.id} value={target.id}>
                            {target.appName} / {target.environment} /{" "}
                            {target.targetKind}
                          </option>
                        ))}
                      </select>
                    </label>
                    <div className="input-grid">
                      <label className="field">
                        <span>Commit/ref</span>
                        <input
                          className="input"
                          name="commit_ref"
                          placeholder="git sha or image tag"
                        />
                      </label>
                      <label className="field">
                        <span>Expected cost micros</span>
                        <input
                          className="input"
                          name="expected_cost_usd_micros"
                          type="number"
                          min={0}
                          defaultValue={0}
                        />
                      </label>
                    </div>
                    <label className="field">
                      <span>Budget reservation id</span>
                      <input
                        className="input"
                        name="reservation_id"
                        placeholder="Required for spendy deployment runs"
                      />
                    </label>
                    <label className="field">
                      <span>Release packet JSON</span>
                      <textarea
                        className="textarea"
                        name="release_packet_json"
                        rows={3}
                        placeholder='{"review_packet_id":"art_...","ci_status":"passed"}'
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Plan deployment run
                    </button>
                    <p className="detail">
                      Provider deployments are modeled as ChangeSets. Health checks
                      run only after the ChangeSet is applied, or immediately for
                      local targets with no provider write.
                    </p>
                  </form>

                  {providerOps.deploymentTargets.length === 0 ? (
                    <p className="detail">No deployment targets registered yet.</p>
                  ) : (
                    <div className="feed-list">
                      {providerOps.deploymentTargets.map((target) => (
                        <div className="feed-row" key={target.id}>
                          <div>
                            <h4>
                              {target.appName} / {target.environment}
                            </h4>
                            <p className="detail">
                              {target.repo} / {target.targetKind} / {target.status}
                            </p>
                            <p className="detail">
                              Domains:{" "}
                              {target.domains.length > 0
                                ? target.domains.join(", ")
                                : "none"}
                            </p>
                          </div>
                          <code className="mono">{target.targetRef}</code>
                        </div>
                      ))}
                    </div>
                  )}

                  {providerOps.deploymentRuns.length > 0 ? (
                    <div className="feed-list">
                      {providerOps.deploymentRuns.slice(0, 5).map((run) => (
                        <div className="feed-row" key={run.id}>
                          <div>
                            <h4>
                              {run.appName} / {run.environment} / {run.status}
                            </h4>
                            <p className="detail">
                              {run.repo} / {run.targetKind} /{" "}
                              {run.commitRef ?? "no ref"}
                            </p>
                            <p className="detail">
                              ChangeSet {run.changesetId ?? "none"} / health{" "}
                              {run.healthArtifactId ?? "not checked"}
                            </p>
                            {run.failureReason ? (
                              <p className="detail">
                                Failure: {run.failureReason}
                              </p>
                            ) : null}
                          </div>
                          <div className="button-stack">
                            <form action={runDeploymentHealthCheckAction}>
                              <input
                                type="hidden"
                                name="deployment_run_id"
                                value={run.id}
                              />
                              <input
                                type="hidden"
                                name="project_id"
                                value={activeProjectId}
                              />
                              <input
                                type="hidden"
                                name="actor"
                                value={activeActorId}
                              />
                              <button className="button secondary" type="submit">
                                Run health checks
                              </button>
                            </form>
                            <code className="mono">
                              {run.rollbackArtifactId ?? run.releaseArtifactId ?? run.id}
                            </code>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : null}

                  <div className="section-heading">
                    <div>
                      <h3>Promotion timeline</h3>
                      <p className="detail">
                        Pipeline verdict → staging → health gate → production → rollout.
                      </p>
                    </div>
                  </div>
                  <DeploymentPromotionTimeline
                    promotions={providerOps.deploymentPromotions}
                  />
                </article>

                <article className="stack-card" id="provider-health">
                  <h3>Health, Drift, and Receipts</h3>
                  <div className="feed-list">
                    {providerOps.providerHealth.slice(0, 4).map((health) => (
                      <div className="feed-row" key={health.id}>
                        <div>
                          <h4>{health.provider}</h4>
                          <p className="detail">
                            {health.status} / API{" "}
                            {health.apiAvailable ? "available" : "unavailable"}
                          </p>
                          <p className="detail">
                            Rate limit {health.rateLimitStatus} / cap{" "}
                            {health.usageCapStatus}
                          </p>
                        </div>
                        <code className="mono">
                          {formatDateTime(health.lastCheckedAt)}
                        </code>
                      </div>
                    ))}
                    {providerOps.driftReports.slice(0, 3).map((report) => (
                      <div className="feed-row" key={report.id}>
                        <div>
                          <h4>{titleCaseToken(report.classification)}</h4>
                          <p className="detail">{report.summary}</p>
                          <p className="detail">
                            unmanaged {report.unmanagedCount} / orphaned{" "}
                            {report.orphanedCount} / security{" "}
                            {report.securityImpactingCount}
                          </p>
                        </div>
                        <code className="mono">{report.id}</code>
                      </div>
                    ))}
                    {providerOps.resourceReceipts.slice(0, 4).map((receipt) => (
                      <div className="feed-row" key={receipt.id}>
                        <div>
                          <h4>{receipt.action}</h4>
                          <p className="detail">
                            {receipt.provider} / {receipt.resourceType} /{" "}
                            {receipt.status}
                          </p>
                          <p className="detail">
                            {receipt.providerRef ?? receipt.dedupeKey}
                          </p>
                        </div>
                        <code className="mono">
                          {receipt.changesetId ?? receipt.id}
                        </code>
                      </div>
                    ))}
                    {providerOps.providerHealth.length === 0 &&
                    providerOps.driftReports.length === 0 &&
                    providerOps.resourceReceipts.length === 0 ? (
                      <p className="detail">
                        No ProviderOps health, drift, or receipt records yet.
                      </p>
                    ) : null}
                  </div>
                </article>

                <article className="stack-card" id="provider-finops">
                  <h3>Cost and Reconciliation Ledger</h3>
                  <div className="feed-list">
                    {providerOps.costAnomalies.slice(0, 4).map((anomaly) => (
                      <div className="feed-row" key={anomaly.id}>
                        <div>
                          <h4>
                            {anomaly.provider} / {titleCaseToken(anomaly.severity)}{" "}
                            anomaly
                          </h4>
                          <p className="detail">{anomaly.summary}</p>
                          <p className="detail">
                            threshold {formatMicros(anomaly.thresholdUsdMicros)} /
                            observed {formatMicros(anomaly.amountUsdMicros)}
                            {anomaly.decisionId
                              ? ` / decision ${anomaly.decisionId}`
                              : ""}
                          </p>
                        </div>
                        <code className="mono">{anomaly.status}</code>
                      </div>
                    ))}
                    {providerOps.costs.slice(0, 4).map((cost) => (
                      <div className="feed-row" key={cost.id}>
                        <div>
                          <h4>
                            {cost.provider} / {cost.category}
                          </h4>
                          <p className="detail">
                            {formatMicros(cost.amountUsdMicros)} /{" "}
                            {cost.costLayer} / {cost.confidence}
                          </p>
                          <p className="detail">
                            {cost.sourceRef ?? formatDateTime(cost.createdAt)}
                          </p>
                        </div>
                        <code className="mono">{cost.id}</code>
                      </div>
                    ))}
                    {providerOps.reconciliationRuns.slice(0, 4).map((run) => (
                      <div className="feed-row" key={run.id}>
                        <div>
                          <h4>
                            {run.provider ?? "all providers"} / {run.status}
                          </h4>
                          <p className="detail">
                            {run.resourcesSeen} resources seen /{" "}
                            {run.resourcesChanged} changed / {run.costsSeen} costs
                          </p>
                          <p className="detail">
                            Started by {run.startedBy} at{" "}
                            {formatDateTime(run.startedAt)}
                          </p>
                          {run.inventorySnapshotArtifactId ? (
                            <p className="detail">
                              Inventory snapshot: {run.inventorySnapshotArtifactId}
                            </p>
                          ) : null}
                        </div>
                        <code className="mono">{run.id}</code>
                      </div>
                    ))}
                    {providerOps.costAnomalies.length === 0 &&
                    providerOps.costs.length === 0 &&
                    providerOps.reconciliationRuns.length === 0 ? (
                      <p className="detail">
                        No provider cost or reconciliation records yet.
                      </p>
                    ) : null}
                  </div>
                </article>
              </div>
            </>
          ) : null}
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="infra">Infrastructure Resources</span>
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

        <section className="panel-stack" data-area="ops" aria-label="Workflow health">
          <div className="section-head">
            <div>
              <span className="eyebrow">Workflow Health</span>
              <h2>Mission control — what is running, stuck, or failing.</h2>
            </div>
          </div>
          <CanaryCard projectId={activeProjectId} run={canaryRun} />
          <EvalHarnessCard projectId={activeProjectId} actor={activeActorId} run={latestEval} />
          <WorkflowHealth
            jobStatusCounts={opsQueue.jobStatusCounts}
            jobKindCounts={opsQueue.jobKindCounts}
            staleLeaseJobIds={opsQueue.staleLeaseJobIds}
            blockerCount={opsQueue.blockerCount}
            recommendedActions={opsQueue.recommendedActions}
            deadletters={deadletters.items}
            openTasks={status.openTasks}
            pendingDecisions={status.pendingDecisions}
          />
        </section>

        <section className="panel-stack" data-area="ops" aria-label="Reusable workflow registry">
          <div className="section-head">
            <div>
              <span className="eyebrow">Workflow Runtime</span>
              <h2>Mapped playbooks with pinned versions and visible execution.</h2>
            </div>
            <p className="status-meta">
              Definitions, work-class mappings, live frontiers, and audit evidence share one durable control plane.
            </p>
          </div>
          <WorkflowRegistryView
            apiReachable={workflowFeed.apiReachable}
            blockedReason={workflowFeed.blockedReason}
            definitions={workflowFeed.definitions}
            mappings={workflowFeed.mappings}
            runs={workflowFeed.runs}
            selectedRun={workflowFeed.selectedRun}
          />
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="ops">Health Snapshots</span>
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
              <h3>COO queue inspection</h3>
              {!opsQueue.apiReachable ? (
                <p className="detail">
                  {opsQueue.blockedReason ?? "Ops queue API is unavailable."}
                </p>
              ) : (
                <>
                  <p className="detail">
                    Budget {opsQueue.budgetState} / blockers{" "}
                    {opsQueue.blockerCount} / ready jobs{" "}
                    {opsQueue.readyJobIds.length} / stale leases{" "}
                    {opsQueue.staleLeaseJobIds.length}
                  </p>
                  <form action={createOpsDigestAction}>
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <input type="hidden" name="limit" value={25} />
                    <button className="button secondary" type="submit">
                      Create ops digest
                    </button>
                  </form>
                  <div className="feed-list">
                    {opsQueue.recommendedActions.length === 0 ? (
                      <p className="detail">
                        No deterministic queue actions are currently recommended.
                      </p>
                    ) : (
                      opsQueue.recommendedActions.slice(0, 4).map((action, index) => (
                        <div className="feed-row" key={`ops-action-${index}`}>
                          <div>
                            <h4>{action}</h4>
                            <p className="detail">
                              Queue {Number(opsQueue.queueState.queued_jobs ?? 0)} /
                              retryable{" "}
                              {Number(opsQueue.queueState.retryable_jobs ?? 0)} /
                              active {Number(opsQueue.queueState.leased_jobs ?? 0)}
                            </p>
                          </div>
                          <span className="pill">COO</span>
                        </div>
                      ))
                    )}
                    {opsQueue.jobs.slice(0, 3).map((job) => (
                      <div className="feed-row" key={job.id}>
                        <div>
                          <h4>{job.kind}</h4>
                          <p className="detail">
                            {job.status} / attempt {job.attempt}/{job.max_attempts} /
                            run {formatDateTime(job.run_after)}
                          </p>
                          {job.task_title ? (
                            <p className="detail">{job.task_title}</p>
                          ) : null}
                        </div>
                        <code className="mono">{job.id}</code>
                      </div>
                    ))}
                  </div>
                </>
              )}
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

        <section className="panel-stack" data-area="ops" aria-label="Safety & policy">
          <div className="section-head">
            <div>
              <span className="eyebrow">Safety &amp; Policy</span>
              <h2>The trust moat — every boundary decision, on the record.</h2>
            </div>
            <p className="status-meta">
              Policy-eval audit trail, cost-anomaly scanning with auto-lite, and run tracing.
            </p>
          </div>
          <div className="stack-grid">
            <PolicyEvalList items={policyEvals} />
            <AnomalyScanCard projectId={activeProjectId} actor={activeActorId} />
            <RunTraceViewer trace={runTrace} initialRunId={traceRunId} />
          </div>
        </section>

        <section className="panel-stack" data-area="ops" aria-label="Autonomy, router & skills">
          <div className="section-head">
            <div>
              <span className="eyebrow">Autonomy · Router · Skills</span>
              <h2>Earned autonomy and the model/skill runtime.</h2>
            </div>
            <p className="status-meta">
              Evidence-based autonomy evaluation, router health + rationale, and mock skill invocation.
            </p>
          </div>
          <div className="stack-grid">
            <AutonomyEvaluateCard projectId={activeProjectId} actor={activeActorId} />
            <RouterHealthCard
              projectId={activeProjectId}
              actor={activeActorId}
              providerHealth={dashboardSnapshot?.providerHealth}
            />
            <SkillInvokeCard projectId={activeProjectId} invocations={skillInvocations} />
          </div>
        </section>

        <section className="panel-stack" data-area="ops" aria-label="Resilience">
          <div className="section-head">
            <div>
              <span className="eyebrow">Resilience</span>
              <h2>Chaos &amp; load — prove recovery, not assume it.</h2>
            </div>
            <p className="status-meta">
              Inject controlled faults against the live primitives and record how the system recovers.
            </p>
          </div>
          <div className="stack-grid">
            <ChaosCard projectId={activeProjectId} runs={chaosRuns} />
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="ops">Audit & Replay</span>
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
                        {(() => {
                          const t = `${item.artifactType} ${item.changeKind}`.toLowerCase();
                          const area = t.includes("self_improvement")
                            ? "ops"
                            : t.includes("module")
                              ? "org"
                              : null;
                          return area ? (
                            <p className="detail">
                              <AreaLink
                                area={area}
                                orgId={activeOrgId}
                                title="Open the rollback control for this change"
                              >
                                ↩ Roll back
                              </AreaLink>
                            </p>
                          ) : null;
                        })()}
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
              <span className="eyebrow" data-area="ops">Self-Improvement</span>
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
                      <SelfImprovementRuntime
                        proposal={proposal}
                        projectId={activeProjectId}
                        actor={activeActorId}
                      />
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
              <span className="eyebrow" data-area="comms">Communications</span>
              <h2>Outbox discipline before external sends.</h2>
            </div>
            <p className="status-meta">
              Messages are drafted and queued through the API first. Real Slack delivery stays policy-gated and receipt-backed.
            </p>
          </div>

          <div className="stack-grid">
            <InboxAckCard items={ackState} />
            <SlackToolsCard projectId={activeProjectId} />
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
              <h3>Channel rollout policies</h3>
              {!outbox.apiReachable ? (
                <p className="detail">
                  {outbox.blockedReason ?? "Outbox API is unavailable."}
                </p>
              ) : (
                <>
                  {outbox.policies.length === 0 ? (
                    <p className="detail">
                      No real-send policy exists. Slack and email delivery will block
                      after writing a receipt until a policy is approved.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {outbox.policies.map((policy) => (
                        <div className="feed-row" key={policy.id}>
                          <div>
                            <h4>{titleCaseToken(policy.provider)}</h4>
                            <p className="detail">
                              {titleCaseToken(policy.deliveryMode)} / boundaries{" "}
                              {policy.allowedDataBoundaries.join(", ") || "none"}
                            </p>
                            <p className="detail">
                              Updated by {policy.createdBy} at{" "}
                              {formatDateTime(policy.updatedAt)}
                            </p>
                            {policy.rolloutNotes ? (
                              <p className="detail">{policy.rolloutNotes}</p>
                            ) : null}
                          </div>
                          <span
                            className={`pill${
                              policy.deliveryMode === "real_send_allowed"
                                ? ""
                                : " warn"
                            }`}
                          >
                            {titleCaseToken(policy.deliveryMode)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  <form
                    action={upsertCommsChannelPolicyAction}
                    className="composer compact"
                  >
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Provider</span>
                      <select className="input" name="provider" defaultValue="slack">
                        <option value="slack">Slack</option>
                        <option value="email">Email</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Delivery mode</span>
                      <select
                        className="input"
                        name="delivery_mode"
                        defaultValue="draft_only"
                      >
                        <option value="disabled">Disabled</option>
                        <option value="draft_only">Draft only</option>
                        <option value="real_send_allowed">Real send allowed</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Allowed data boundaries</span>
                      <textarea
                        className="textarea"
                        name="allowed_data_boundaries"
                        defaultValue={"public\ninternal"}
                      />
                    </label>
                    <label className="field">
                      <span>Rollout notes</span>
                      <textarea
                        className="textarea"
                        name="rollout_notes"
                        placeholder="Approval, environment, and credential rollout notes."
                      />
                    </label>
                    <label className="field">
                      <span>Metadata JSON</span>
                      <textarea
                        className="textarea"
                        name="metadata_json"
                        defaultValue={`{"source":"mission_control"}`}
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Save channel policy
                    </button>
                  </form>
                </>
              )}
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
              <span className="eyebrow" data-area="org">Company Pack</span>
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

        <section className="panel-stack" data-area="enterprise" aria-label="SOC2 evidence">
          <div className="section-head">
            <div>
              <span className="eyebrow">Compliance · SOC2</span>
              <h2>Auditor-ready evidence, generated from live data.</h2>
            </div>
            <p className="status-meta">
              Trust Services Common-Criteria controls mapped to BOSS’s audit, policy-eval,
              break-glass, change-management, and verification records.
            </p>
          </div>
          <div className="stack-grid">
            <Soc2Card projectId={activeProjectId} />
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="enterprise">Commercial Trust Layer</span>
              <h2>Edition, Account Connect, Enterprise, and SCIM governance.</h2>
            </div>
            <p className="status-meta">
              Community stays local-first. Cloud fails closed until Account Connect is
              linked, and Enterprise trust controls are tracked as auditable runtime
              records.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Edition and Account Connect</h3>
              {!commercial.apiReachable ? (
                <p className="detail">
                  {commercial.blockedReason ?? "Commercial API is unavailable."}
                </p>
              ) : !commercial.edition ? (
                <p className="detail">No edition status is available.</p>
              ) : (
                <div className="feed-list">
                  <div className="feed-row">
                    <div>
                      <h4>{commercial.edition.displayName}</h4>
                      <p className="detail">
                        {commercial.edition.editionId} / {commercial.edition.status} /{" "}
                        {commercial.edition.connectionMode}
                      </p>
                      <p className="detail">
                        Account Connect {commercial.edition.accountConnectStatus}
                        {commercial.edition.accountConnectRequired
                          ? " / required"
                          : " / optional"}
                        {commercial.edition.localAuthAllowed
                          ? " / local auth allowed"
                          : " / hosted auth required"}
                      </p>
                      <p className="detail">
                        License posture{" "}
                        {commercial.edition.licenseCandidate ?? "unclassified"}
                        {commercial.edition.legalReviewRequired
                          ? " / legal review required"
                          : ""}
                      </p>
                      {commercial.edition.blockedReason ? (
                        <p className="detail">
                          Blocked: {commercial.edition.blockedReason}
                        </p>
                      ) : null}
                    </div>
                    <span
                      className={`pill${
                        commercial.edition.status === "active" ? "" : " warn"
                      }`}
                    >
                      {titleCaseToken(commercial.edition.status)}
                    </span>
                  </div>

                  {commercial.edition.accountConnectOrg ? (
                    <div className="feed-row">
                      <div>
                        <h4>{commercial.edition.accountConnectOrg.hostedOrgId}</h4>
                        <p className="detail">
                          Linked by {commercial.edition.accountConnectOrg.linkedBy}
                          {commercial.edition.accountConnectOrg.linkedAt
                            ? ` at ${formatDateTime(
                                commercial.edition.accountConnectOrg.linkedAt,
                              )}`
                            : ""}
                        </p>
                        <p className="detail">
                          Billing{" "}
                          {commercial.edition.accountConnectOrg.billingPortalRef ??
                            "not connected"}{" "}
                          / connector refs{" "}
                          {
                            commercial.edition.accountConnectOrg
                              .connectorAuthorizationRefs.length
                          }
                        </p>
                        <div className="composer compact">
                          <form action={syncAccountConnectAction}>
                            <input
                              type="hidden"
                              name="project_id"
                              value={activeProjectId}
                            />
                            <input type="hidden" name="actor" value={activeActorId} />
                            <input
                              type="hidden"
                              name="hosted_org_id"
                              value={commercial.edition.accountConnectOrg.hostedOrgId}
                            />
                            <button className="button secondary" type="submit">
                              Sync entitlements
                            </button>
                          </form>
                          <form action={unlinkAccountConnectAction}>
                            <input
                              type="hidden"
                              name="project_id"
                              value={activeProjectId}
                            />
                            <input type="hidden" name="actor" value={activeActorId} />
                            <input
                              type="hidden"
                              name="hosted_org_id"
                              value={commercial.edition.accountConnectOrg.hostedOrgId}
                            />
                            <input
                              type="hidden"
                              name="reason"
                              value="Operator unlinked Account Connect from Mission Control."
                            />
                            <button className="button secondary" type="submit">
                              Unlink
                            </button>
                          </form>
                        </div>
                      </div>
                      <span
                        className={`pill${
                          commercial.edition.accountConnectOrg.status === "linked"
                            ? ""
                            : " warn"
                        }`}
                      >
                        {titleCaseToken(commercial.edition.accountConnectOrg.status)}
                      </span>
                    </div>
                  ) : (
                    <form action={linkAccountConnectAction} className="composer">
                      <input type="hidden" name="project_id" value={activeProjectId} />
                      <input type="hidden" name="actor" value={activeActorId} />
                      <label className="field">
                        <span>Hosted org ID</span>
                        <input
                          className="input"
                          name="hosted_org_id"
                          placeholder="acct_org_..."
                          required
                        />
                      </label>
                      <label className="field">
                        <span>Connection SecretRef</span>
                        <input
                          className="input"
                          name="connection_ref"
                          placeholder="secretref:boss-account-connect"
                        />
                      </label>
                      <label className="field">
                        <span>Marketplace identity JSON</span>
                        <textarea
                          className="input"
                          name="marketplace_identity_json"
                          defaultValue={`{"publisher":"seanheiney"}`}
                        />
                      </label>
                      <label className="field">
                        <span>Connector authorization refs</span>
                        <textarea
                          className="input"
                          name="connector_authorization_refs"
                          placeholder="github:installation:..."
                        />
                      </label>
                      <label className="field">
                        <span>Billing portal ref</span>
                        <input
                          className="input"
                          name="billing_portal_ref"
                          placeholder="stripe:portal:..."
                        />
                      </label>
                      <label className="check-row">
                        <input
                          type="hidden"
                          name="optional_telemetry_enabled"
                          value="false"
                        />
                        <input
                          type="checkbox"
                          name="optional_telemetry_enabled"
                          value="true"
                          defaultChecked
                        />
                        <span>Enable optional hosted telemetry</span>
                      </label>
                      <button className="button secondary" type="submit">
                        Link Account Connect
                      </button>
                    </form>
                  )}
                </div>
              )}
            </article>

            <article className="stack-card">
              <h3>Account Connect health</h3>
              {!commercial.apiReachable ? (
                <p className="detail">
                  {commercial.blockedReason ?? "Commercial API is unavailable."}
                </p>
              ) : (
                <>
                  {commercial.healthChecks.length === 0 ? (
                    <p className="detail">
                      No hosted Account Connect health checks have been recorded.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {commercial.healthChecks.slice(0, 5).map((check) => (
                        <div className="feed-row" key={check.id}>
                          <div>
                            <h4>{titleCaseToken(check.status)}</h4>
                            <p className="detail">
                              {check.checkKind} / hosted org{" "}
                              {check.hostedOrgId ?? "not linked"} / latency{" "}
                              {check.latencyMs ?? 0}ms
                            </p>
                            <p className="detail">
                              Checked by {check.checkedBy} at{" "}
                              {formatDateTime(check.createdAt)}
                              {check.providerRef ? ` / ref ${check.providerRef}` : ""}
                            </p>
                            {check.readinessPacketArtifactId ? (
                              <p className="detail">
                                Readiness packet {check.readinessPacketArtifactId}
                              </p>
                            ) : null}
                            {check.error ? (
                              <p className="detail">
                                Error{" "}
                                {String(check.error.reason ?? "unknown")}:{" "}
                                {String(check.error.message ?? "no message")}
                              </p>
                            ) : null}
                          </div>
                          <span
                            className={`pill${
                              check.status === "healthy" ? "" : " warn"
                            }`}
                          >
                            {titleCaseToken(check.status)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  <form
                    action={runAccountConnectHealthCheckAction}
                    className="composer"
                  >
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Hosted org ID (optional)</span>
                      <input
                        className="input"
                        name="hosted_org_id"
                        defaultValue={
                          commercial.edition?.accountConnectOrg?.hostedOrgId ?? ""
                        }
                        placeholder="acct_org_..."
                      />
                    </label>
                    <label className="field">
                      <span>Check kind</span>
                      <input
                        className="input"
                        name="check_kind"
                        defaultValue="hosted_service"
                      />
                    </label>
                    <label className="field">
                      <span>Metadata JSON</span>
                      <textarea
                        className="input"
                        name="metadata_json"
                        defaultValue={`{"source":"mission_control"}`}
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Run hosted readiness check
                    </button>
                  </form>
                </>
              )}
            </article>

            <article className="stack-card">
              <h3>Hosted auth sessions</h3>
              {!commercial.apiReachable ? (
                <p className="detail">
                  {commercial.blockedReason ?? "Commercial API is unavailable."}
                </p>
              ) : (
                <>
                  {commercial.authSessions.length === 0 ? (
                    <p className="detail">
                      No hosted Account Connect auth sessions have been requested.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {commercial.authSessions.slice(0, 6).map((session) => (
                        <div className="feed-row" key={session.id}>
                          <div>
                            <h4>{session.providerRef ?? session.id}</h4>
                            <p className="detail">
                              {titleCaseToken(session.status)} / receipt{" "}
                              {session.receiptId ?? "not opened"} / hosted org{" "}
                              {session.hostedOrgId ?? "pending"}
                            </p>
                            <p className="detail">
                              Return URL {session.returnUrl ?? "not set"}
                              {session.expiresAt
                                ? ` / expires ${formatDateTime(session.expiresAt)}`
                                : ""}
                              {session.completedAt
                                ? ` / completed ${formatDateTime(session.completedAt)}`
                                : ""}
                            </p>
                            {session.authorizationUrl ? (
                              <p className="detail">
                                <a href={session.authorizationUrl} rel="noreferrer">
                                  Open hosted Account Connect login
                                </a>
                              </p>
                            ) : (
                              <p className="detail">
                                No hosted login URL was issued; check status and receipt.
                              </p>
                            )}
                            {session.status === "ready" ? (
                              <form
                                action={completeAccountConnectAuthSessionAction}
                                className="composer compact"
                              >
                                <input
                                  type="hidden"
                                  name="project_id"
                                  value={activeProjectId}
                                />
                                <input
                                  type="hidden"
                                  name="actor"
                                  value={activeActorId}
                                />
                                <input
                                  type="hidden"
                                  name="session_id"
                                  value={session.id}
                                />
                                <input
                                  type="hidden"
                                  name="state"
                                  value={session.state}
                                />
                                <input
                                  className="input"
                                  name="code"
                                  type="password"
                                  aria-label="Hosted callback code"
                                  placeholder="Hosted callback code"
                                />
                                <button className="button secondary" type="submit">
                                  Complete hosted login
                                </button>
                              </form>
                            ) : null}
                          </div>
                          <span
                            className={`pill${
                              session.status === "ready" ||
                              session.status === "completed"
                                ? ""
                                : " warn"
                            }`}
                          >
                            {titleCaseToken(session.status)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  <form
                    action={createAccountConnectAuthSessionAction}
                    className="composer"
                  >
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Hosted org ID (optional)</span>
                      <input
                        className="input"
                        name="hosted_org_id"
                        placeholder="acct_org_..."
                      />
                    </label>
                    <label className="field">
                      <span>Return URL</span>
                      <input
                        className="input"
                        name="return_url"
                        placeholder="https://boss.local/account-connect/callback"
                      />
                    </label>
                    <label className="field">
                      <span>Metadata JSON</span>
                      <textarea
                        className="input"
                        name="metadata_json"
                        defaultValue={`{"source":"mission_control"}`}
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Start hosted Account Connect login
                    </button>
                  </form>
                </>
              )}
            </article>

            <article className="stack-card">
              <h3>License entitlements</h3>
              {!commercial.apiReachable ? (
                <p className="detail">
                  {commercial.blockedReason ?? "Commercial API is unavailable."}
                </p>
              ) : commercial.entitlements.length === 0 ? (
                <p className="detail">
                  No hosted license entitlements are active for this project.
                </p>
              ) : (
                <div className="feed-list">
                  {commercial.entitlements.slice(0, 8).map((entitlement) => (
                    <div className="feed-row" key={entitlement.id}>
                      <div>
                        <h4>{entitlement.capabilityKey}</h4>
                        <p className="detail">
                          {entitlement.editionId} / {entitlement.status} /{" "}
                          {entitlement.source}
                        </p>
                        <p className="detail">
                          {entitlement.capabilities.join(", ") || "No capabilities"}
                          {entitlement.seats ? ` / ${entitlement.seats} seats` : ""}
                        </p>
                        <p className="detail">
                          Scope {entitlement.scopeType}:{entitlement.scopeId}
                          {entitlement.expiresAt
                            ? ` / expires ${formatDateTime(entitlement.expiresAt)}`
                            : ""}
                        </p>
                      </div>
                      <span
                        className={`pill${
                          entitlement.status === "active" ? "" : " warn"
                        }`}
                      >
                        {titleCaseToken(entitlement.status)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </article>

            <article className="stack-card">
              <h3>Team invites</h3>
              {!commercial.apiReachable ? (
                <p className="detail">
                  {commercial.blockedReason ?? "Commercial API is unavailable."}
                </p>
              ) : (
                <>
                  {commercial.teamInvites.length === 0 ? (
                    <p className="detail">
                      No Account Connect team invites have been requested.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {commercial.teamInvites.slice(0, 6).map((invite) => (
                        <div className="feed-row" key={invite.id}>
                          <div>
                            <h4>{invite.displayName ?? invite.email}</h4>
                            <p className="detail">
                              {invite.email} / {titleCaseToken(invite.status)} / delivery{" "}
                              {titleCaseToken(invite.deliveryStatus)}
                            </p>
                            <p className="detail">
                              Roles {invite.roleKeys.join(", ") || "none"} / permissions{" "}
                              {invite.permissions.length}
                            </p>
                            <p className="detail">
                              Receipt {invite.receiptId ?? "not opened"} / hosted ref{" "}
                              {invite.hostedInviteRef ?? "none"}
                            </p>
                            {invite.status !== "revoked" ? (
                              <form
                                action={revokeAccountConnectTeamInviteAction}
                                className="composer compact"
                              >
                                <input
                                  type="hidden"
                                  name="project_id"
                                  value={activeProjectId}
                                />
                                <input
                                  type="hidden"
                                  name="actor"
                                  value={activeActorId}
                                />
                                <input
                                  type="hidden"
                                  name="invite_id"
                                  value={invite.id}
                                />
                                <input
                                  className="input"
                                  name="reason"
                                  aria-label="Revoke invite reason"
                                  placeholder="Reason for revoke"
                                />
                                <button className="button secondary" type="submit">
                                  Revoke invite
                                </button>
                              </form>
                            ) : null}
                          </div>
                          <span
                            className={`pill${
                              invite.status === "pending_acceptance" ||
                              invite.status === "revoked"
                                ? ""
                                : " warn"
                            }`}
                          >
                            {titleCaseToken(invite.status)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  <form action={createAccountConnectTeamInviteAction} className="composer">
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Email</span>
                      <input
                        className="input"
                        name="email"
                        type="email"
                        placeholder="operator@example.com"
                        required
                      />
                    </label>
                    <label className="field">
                      <span>Display name</span>
                      <input className="input" name="display_name" />
                    </label>
                    <label className="field">
                      <span>Role keys</span>
                      <textarea
                        className="input"
                        name="role_keys"
                        placeholder="operator&#10;providerops_manager"
                      />
                    </label>
                    <label className="field">
                      <span>Permissions</span>
                      <textarea
                        className="input"
                        name="permissions"
                        placeholder="command_submit&#10;providerops_manage"
                      />
                    </label>
                    <label className="field">
                      <span>Metadata JSON</span>
                      <textarea
                        className="input"
                        name="metadata_json"
                        defaultValue={`{"source":"mission_control"}`}
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Create team invite
                    </button>
                  </form>
                </>
              )}
            </article>

            <article className="stack-card">
              <h3>Billing portal sessions</h3>
              {!commercial.apiReachable ? (
                <p className="detail">
                  {commercial.blockedReason ?? "Commercial API is unavailable."}
                </p>
              ) : (
                <>
                  {commercial.billingPortalSessions.length === 0 ? (
                    <p className="detail">
                      No billing portal sessions have been requested.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {commercial.billingPortalSessions.slice(0, 6).map((session) => (
                        <div className="feed-row" key={session.id}>
                          <div>
                            <h4>{session.providerRef ?? session.id}</h4>
                            <p className="detail">
                              {titleCaseToken(session.status)} / receipt{" "}
                              {session.receiptId ?? "not opened"} / hosted org{" "}
                              {session.hostedOrgId}
                            </p>
                            <p className="detail">
                              Return URL {session.returnUrl ?? "not set"}
                              {session.expiresAt
                                ? ` / expires ${formatDateTime(session.expiresAt)}`
                                : ""}
                            </p>
                            {session.portalUrl ? (
                              <p className="detail">
                                <a href={session.portalUrl} rel="noreferrer">
                                  Open hosted billing portal
                                </a>
                              </p>
                            ) : (
                              <p className="detail">
                                No portal URL was issued; check status and receipt.
                              </p>
                            )}
                          </div>
                          <span
                            className={`pill${
                              session.status === "ready" ? "" : " warn"
                            }`}
                          >
                            {titleCaseToken(session.status)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  <form
                    action={createAccountConnectBillingPortalSessionAction}
                    className="composer"
                  >
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Return URL</span>
                      <input
                        className="input"
                        name="return_url"
                        placeholder="https://boss.local/settings"
                      />
                    </label>
                    <label className="field">
                      <span>Metadata JSON</span>
                      <textarea
                        className="input"
                        name="metadata_json"
                        defaultValue={`{"source":"mission_control"}`}
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Create billing portal session
                    </button>
                  </form>
                </>
              )}
            </article>

            <article className="stack-card">
              <h3>Enterprise cluster records</h3>
              {!enterprise.apiReachable ? (
                <p className="detail">
                  {enterprise.blockedReason ?? "Enterprise API is unavailable."}
                </p>
              ) : (
                <>
                  <div className="metric-row">
                    <span className="pill">
                      {enterprise.summary.clusterCount} cluster
                      {enterprise.summary.clusterCount === 1 ? "" : "s"}
                    </span>
                    <span
                      className={`pill${
                        enterprise.summary.blockingGapCount > 0 ? " warn" : ""
                      }`}
                    >
                      {enterprise.summary.blockingGapCount} blocking gap
                      {enterprise.summary.blockingGapCount === 1 ? "" : "s"}
                    </span>
                    <span className="pill">
                      {enterprise.summary.activeIdentityProviders}/
                      {enterprise.summary.identityProviderCount} active IdP
                    </span>
                    <span className="pill">
                      {enterprise.summary.activeScimUsers} active SCIM user
                      {enterprise.summary.activeScimUsers === 1 ? "" : "s"}
                    </span>
                    <span
                      className={`pill${
                        enterprise.summary.blockedSupportCases > 0 ? " warn" : ""
                      }`}
                    >
                      {enterprise.summary.openSupportCases} open support case
                      {enterprise.summary.openSupportCases === 1 ? "" : "s"}
                    </span>
                  </div>
                  {enterprise.clusters.length === 0 ? (
                    <p className="detail">No enterprise cluster record exists yet.</p>
                  ) : (
                    <div className="feed-list">
                      {enterprise.clusters.map((cluster) => (
                        <div className="feed-row" key={cluster.id}>
                          <div>
                            <h4>{titleCaseToken(cluster.deploymentMode)}</h4>
                            <p className="detail">
                              {cluster.accountConnectMode} / {cluster.workerPoolStrategy}
                            </p>
                            <p className="detail">
                              {cluster.haProfile} / {cluster.dataResidency} / support{" "}
                              {cluster.supportTier}
                            </p>
                            <p className="detail">
                              Compliance{" "}
                              {cluster.compliancePacks.join(", ") || "not configured"}
                            </p>
                            {cluster.blockingGaps.length > 0 ? (
                              <p className="detail">
                                Gaps: {cluster.blockingGaps.join(", ")}
                              </p>
                            ) : null}
                          </div>
                          <span
                            className={`pill${
                              cluster.status === "active" ? "" : " warn"
                            }`}
                          >
                            {titleCaseToken(cluster.status)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                  <form action={upsertEnterpriseClusterAction} className="composer">
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Deployment mode</span>
                      <select className="input" name="deployment_mode" defaultValue="enterprise_cloud">
                        <option value="enterprise_cloud">Enterprise Cloud</option>
                        <option value="self_hosted">Self-hosted</option>
                        <option value="private_cloud">Private Cloud</option>
                        <option value="air_gapped">Air-gapped</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Account Connect mode</span>
                      <select className="input" name="account_connect_mode" defaultValue="attached">
                        <option value="attached">Attached</option>
                        <option value="detached">Detached</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Worker pool strategy</span>
                      <input
                        className="input"
                        name="worker_pool_strategy"
                        defaultValue="dedicated_managed_workers"
                      />
                    </label>
                    <label className="field">
                      <span>HA profile</span>
                      <input className="input" name="ha_profile" defaultValue="regional_ha" />
                    </label>
                    <label className="field">
                      <span>Data residency</span>
                      <input className="input" name="data_residency" defaultValue="us" />
                    </label>
                    <label className="field">
                      <span>Identity controls JSON</span>
                      <textarea
                        className="input"
                        name="identity_controls_json"
                        defaultValue={`{"sso_oidc_configured":false,"scim_configured":false}`}
                      />
                    </label>
                    <label className="field">
                      <span>Governance controls JSON</span>
                      <textarea
                        className="input"
                        name="governance_controls_json"
                        defaultValue={`{"budget_governor_precedence":true,"agents_receive_raw_provider_secrets":false}`}
                      />
                    </label>
                    <label className="field">
                      <span>Audit controls JSON</span>
                      <textarea
                        className="input"
                        name="audit_controls_json"
                        defaultValue={`{"audit_export_enabled":true,"retention_days":365}`}
                      />
                    </label>
                    <label className="field">
                      <span>Private worker pools JSON</span>
                      <textarea
                        className="input"
                        name="private_worker_pools_json"
                        defaultValue={`[]`}
                      />
                    </label>
                    <label className="field">
                      <span>Compliance packs</span>
                      <textarea
                        className="input"
                        name="compliance_packs"
                        placeholder="soc2_evidence&#10;enterprise_audit"
                      />
                    </label>
                    <label className="field">
                      <span>Support tier</span>
                      <input className="input" name="support_tier" defaultValue="enterprise" />
                    </label>
                    <label className="field">
                      <span>Support SLA JSON</span>
                      <textarea
                        className="input"
                        name="support_sla_json"
                        defaultValue={`{"response_hours":4,"uptime_target":"99.9"}`}
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Save enterprise record
                    </button>
                  </form>
                </>
              )}
            </article>

            <article className="stack-card">
              <h3>Cluster validation evidence</h3>
              {!enterprise.apiReachable ? (
                <p className="detail">
                  {enterprise.blockedReason ?? "Enterprise API is unavailable."}
                </p>
              ) : (
                <>
                  {enterprise.clusterValidationRuns.length === 0 ? (
                    <p className="detail">
                      No enterprise cluster validation runs have been recorded.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {enterprise.clusterValidationRuns.slice(0, 6).map((run) => {
                        const validationPacketArtifactId = metadataString(
                          run.metadata,
                          "validation_packet_artifact_id",
                        );
                        const blockingDecisionId = metadataString(
                          run.metadata,
                          "blocking_decision_id",
                        );
                        return (
                          <div className="feed-row" key={run.id}>
                            <div>
                              <h4>{titleCaseToken(run.validationKind)}</h4>
                              <p className="detail">
                                Cluster {run.enterpriseClusterId} / validated by{" "}
                                {run.validatedBy} / {formatDateTime(run.createdAt)}
                              </p>
                              <p className="detail">
                                Checks {run.checks.length} / evidence refs{" "}
                                {run.evidenceRefs.length}
                              </p>
                              {validationPacketArtifactId ? (
                                <p className="detail">
                                  Validation packet: {validationPacketArtifactId}
                                </p>
                              ) : null}
                              {blockingDecisionId ? (
                                <p className="detail">
                                  Blocking decision: {blockingDecisionId}
                                </p>
                              ) : null}
                              {run.metadata.live_rollout_claim === false ? (
                                <p className="detail">
                                  Readiness evidence only; no live rollout claim.
                                </p>
                              ) : null}
                              {run.blockingGaps.length > 0 ? (
                                <p className="detail">
                                  Gaps: {run.blockingGaps.join(", ")}
                                </p>
                              ) : null}
                            </div>
                            <span
                              className={`pill${
                                run.status === "passed" ? "" : " warn"
                              }`}
                            >
                              {titleCaseToken(run.status)}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  <form
                    action={createEnterpriseClusterValidationRunAction}
                    className="composer"
                  >
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Enterprise cluster ID</span>
                      <input
                        className="input"
                        name="enterprise_cluster_id"
                        placeholder="entcluster_..."
                        defaultValue={enterprise.clusters[0]?.id ?? ""}
                        required
                      />
                    </label>
                    <label className="field">
                      <span>Validation kind</span>
                      <input
                        className="input"
                        name="validation_kind"
                        defaultValue="multi_node_readiness"
                      />
                    </label>
                    <label className="field">
                      <span>Status</span>
                      <select className="input" name="status" defaultValue="passed">
                        <option value="passed">Passed</option>
                        <option value="warning">Warning</option>
                        <option value="failed">Failed</option>
                        <option value="blocked">Blocked</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Checks JSON</span>
                      <textarea
                        className="input"
                        name="checks_json"
                        defaultValue={`[{"name":"kubernetes_manifests","status":"passed","required":true,"message":"make k8s-validate passed"}]`}
                      />
                    </label>
                    <label className="field">
                      <span>Evidence refs</span>
                      <textarea
                        className="input"
                        name="evidence_refs"
                        placeholder="artifact:release_packet_..."
                      />
                    </label>
                    <label className="field">
                      <span>Metadata JSON</span>
                      <textarea
                        className="input"
                        name="metadata_json"
                        defaultValue={`{"source":"mission_control"}`}
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Record validation run
                    </button>
                  </form>
                </>
              )}
            </article>

            <article className="stack-card">
              <h3>Enterprise support and SLA</h3>
              {!enterprise.apiReachable ? (
                <p className="detail">
                  {enterprise.blockedReason ?? "Enterprise API is unavailable."}
                </p>
              ) : (
                <>
                  {enterprise.supportCases.length === 0 ? (
                    <p className="detail">
                      No enterprise support cases have been recorded.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {enterprise.supportCases.slice(0, 6).map((supportCase) => (
                        <div className="feed-row" key={supportCase.id}>
                          <div>
                            <h4>{supportCase.title}</h4>
                            <p className="detail">
                              {titleCaseToken(supportCase.severity)} /{" "}
                              {supportCase.category} / due{" "}
                              {formatDateTime(supportCase.slaDueAt)}
                            </p>
                            <p className="detail">
                              Assigned {supportCase.assignedTo ?? "unassigned"} / receipt{" "}
                              {supportCase.receiptId ?? "local case"} / hosted{" "}
                              {supportCase.hostedCaseRef ?? "not escalated"}
                            </p>
                            {supportCase.supportPacketArtifactId ? (
                              <p className="detail">
                                Support packet {supportCase.supportPacketArtifactId}
                              </p>
                            ) : null}
                            {supportCase.enterpriseClusterId ? (
                              <p className="detail">
                                Cluster {supportCase.enterpriseClusterId} / Account Connect{" "}
                                {supportCase.accountConnectOrgId ?? "not linked"}
                              </p>
                            ) : null}
                            {supportCase.metadata.blocked_reason ? (
                              <p className="detail">
                                Blocked reason:{" "}
                                {String(supportCase.metadata.blocked_reason)}
                              </p>
                            ) : null}
                            <form
                              action={updateEnterpriseSupportCaseAction}
                              className="inline-actions"
                            >
                              <input
                                type="hidden"
                                name="project_id"
                                value={activeProjectId}
                              />
                              <input
                                type="hidden"
                                name="actor"
                                value={activeActorId}
                              />
                              <input
                                type="hidden"
                                name="case_id"
                                value={supportCase.id}
                              />
                              <label className="field compact">
                                <span>Status</span>
                                <select
                                  className="input"
                                  name="status"
                                  defaultValue={supportCase.status}
                                >
                                  <option value="open">Open</option>
                                  <option value="in_progress">In progress</option>
                                  <option value="resolved">Resolved</option>
                                  <option value="closed">Closed</option>
                                  <option value="failed">Failed</option>
                                </select>
                              </label>
                              <label className="field compact">
                                <span>Assigned to</span>
                                <input
                                  className="input"
                                  name="assigned_to"
                                  defaultValue={supportCase.assignedTo ?? ""}
                                  placeholder="cto"
                                />
                              </label>
                              <label className="field compact">
                                <span>Metadata JSON</span>
                                <textarea
                                  className="input"
                                  name="metadata_json"
                                  defaultValue={`{"updated_from":"mission_control"}`}
                                />
                              </label>
                              <button className="button ghost" type="submit">
                                Update support case
                              </button>
                            </form>
                          </div>
                          <span
                            className={`pill${
                              supportCase.status === "open" ||
                              supportCase.status === "in_progress"
                                ? " warn"
                                : ""
                            }`}
                          >
                            {titleCaseToken(supportCase.status)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  <form
                    action={createEnterpriseSupportCaseAction}
                    className="composer"
                  >
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Title</span>
                      <input
                        className="input"
                        name="title"
                        required
                        placeholder="Review production readiness blockers"
                      />
                    </label>
                    <label className="field">
                      <span>Description</span>
                      <textarea
                        className="input"
                        name="description"
                        required
                        placeholder="Describe the enterprise support issue or SLA request."
                      />
                    </label>
                    <label className="field">
                      <span>Severity</span>
                      <select className="input" name="severity" defaultValue="normal">
                        <option value="critical">Critical</option>
                        <option value="high">High</option>
                        <option value="normal">Normal</option>
                        <option value="low">Low</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Category</span>
                      <input
                        className="input"
                        name="category"
                        defaultValue="operational_trust"
                      />
                    </label>
                    <label className="field">
                      <span>Enterprise cluster ID</span>
                      <input
                        className="input"
                        name="enterprise_cluster_id"
                        placeholder="optional cluster id"
                      />
                    </label>
                    <label className="field">
                      <span>Metadata JSON</span>
                      <textarea
                        className="input"
                        name="metadata_json"
                        defaultValue={`{"source":"mission_control"}`}
                      />
                    </label>
                    <label className="check-row">
                      <input type="hidden" name="escalate_to_hosted" value="false" />
                      <input type="checkbox" name="escalate_to_hosted" value="true" />
                      <span>
                        Escalate to hosted Account Connect support when configured
                      </span>
                    </label>
                    <button className="button secondary" type="submit">
                      Create support case
                    </button>
                  </form>
                </>
              )}
            </article>

            <article className="stack-card">
              <h3>Enterprise identity providers</h3>
              {!enterprise.apiReachable ? (
                <p className="detail">
                  {enterprise.blockedReason ?? "Enterprise API is unavailable."}
                </p>
              ) : (
                <>
                  {enterprise.identityProviders.length === 0 ? (
                    <p className="detail">
                      No OIDC, SAML, or SCIM identity provider metadata has been
                      registered.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {enterprise.identityProviders.slice(0, 6).map((provider) => (
                        <div className="feed-row" key={provider.id}>
                          <div>
                            <h4>{provider.displayName}</h4>
                            <p className="detail">
                              {provider.providerKey} /{" "}
                              {titleCaseToken(provider.providerType)} /{" "}
                              {provider.testStatus}
                            </p>
                            <p className="detail">
                              Ref{" "}
                              {provider.issuer ??
                                provider.entityId ??
                                provider.scimBaseUrl ??
                                "not configured"}{" "}
                              / SecretRef {provider.secretRef ?? "not required"}
                            </p>
                            {provider.blockingGaps.length > 0 ? (
                              <p className="detail">
                                Gaps: {provider.blockingGaps.join(", ")}
                              </p>
                            ) : null}
                          </div>
                          <span
                            className={`pill${
                              provider.status === "active" ? "" : " warn"
                            }`}
                          >
                            {titleCaseToken(provider.status)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  <form
                    action={upsertEnterpriseIdentityProviderAction}
                    className="composer"
                  >
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Provider key</span>
                      <input className="input" name="provider_key" required />
                    </label>
                    <label className="field">
                      <span>Provider type</span>
                      <select className="input" name="provider_type" defaultValue="oidc">
                        <option value="oidc">OIDC</option>
                        <option value="saml">SAML</option>
                        <option value="scim">SCIM</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Display name</span>
                      <input className="input" name="display_name" required />
                    </label>
                    <label className="field">
                      <span>Issuer</span>
                      <input className="input" name="issuer" placeholder="https://idp.example" />
                    </label>
                    <label className="field">
                      <span>Audience</span>
                      <input className="input" name="audience" placeholder="boss-api" />
                    </label>
                    <label className="field">
                      <span>JWKS URL</span>
                      <input className="input" name="jwks_url" placeholder="https://idp.example/.well-known/jwks.json" />
                    </label>
                    <label className="field">
                      <span>SAML entity ID</span>
                      <input className="input" name="entity_id" />
                    </label>
                    <label className="field">
                      <span>SAML SSO URL</span>
                      <input className="input" name="sso_url" />
                    </label>
                    <label className="field">
                      <span>SAML certificate fingerprint</span>
                      <input className="input" name="certificate_fingerprint" />
                    </label>
                    <label className="field">
                      <span>SCIM base URL</span>
                      <input className="input" name="scim_base_url" />
                    </label>
                    <label className="field">
                      <span>SecretRef</span>
                      <input className="input" name="secret_ref" placeholder="secretref:idp-token" />
                    </label>
                    <label className="field">
                      <span>Status</span>
                      <select className="input" name="status" defaultValue="planned">
                        <option value="planned">Planned</option>
                        <option value="active">Active</option>
                        <option value="disabled">Disabled</option>
                      </select>
                    </label>
                    <button className="button secondary" type="submit">
                      Save identity provider
                    </button>
                  </form>
                </>
              )}
            </article>

            <article className="stack-card">
              <h3>Identity rollout policies</h3>
              {!enterprise.apiReachable ? (
                <p className="detail">
                  {enterprise.blockedReason ?? "Enterprise API is unavailable."}
                </p>
              ) : (
                <>
                  {enterprise.identityRolloutPolicies.length === 0 ? (
                    <p className="detail">
                      No identity rollout policy is configured. SAML login and SCIM
                      runtime provisioning stay blocked until a policy is approved.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {enterprise.identityRolloutPolicies.map((policy) => (
                        <div className="feed-row" key={policy.id}>
                          <div>
                            <h4>{titleCaseToken(policy.providerType)}</h4>
                            <p className="detail">
                              {titleCaseToken(policy.runtimeMode)} / domains{" "}
                              {policy.allowedDomains.join(", ") || "not restricted"}
                            </p>
                            <p className="detail">
                              Updated by {policy.createdBy} at{" "}
                              {formatDateTime(policy.updatedAt)}
                            </p>
                            {policy.rolloutNotes ? (
                              <p className="detail">{policy.rolloutNotes}</p>
                            ) : null}
                          </div>
                          <span
                            className={`pill${
                              policy.runtimeMode === "auth_allowed" ||
                              policy.runtimeMode === "provisioning_allowed"
                                ? ""
                                : " warn"
                            }`}
                          >
                            {titleCaseToken(policy.runtimeMode)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  <form
                    action={upsertEnterpriseIdentityRolloutPolicyAction}
                    className="composer compact"
                  >
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Provider type</span>
                      <select className="input" name="provider_type" defaultValue="saml">
                        <option value="oidc">OIDC</option>
                        <option value="saml">SAML</option>
                        <option value="scim">SCIM</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Runtime mode</span>
                      <select className="input" name="runtime_mode" defaultValue="disabled">
                        <option value="disabled">Disabled</option>
                        <option value="metadata_only">Metadata only</option>
                        <option value="auth_allowed">Auth allowed</option>
                        <option value="provisioning_allowed">Provisioning allowed</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Allowed domains</span>
                      <textarea
                        className="textarea"
                        name="allowed_domains"
                        placeholder="example.com"
                      />
                    </label>
                    <label className="field">
                      <span>Rollout notes</span>
                      <textarea
                        className="textarea"
                        name="rollout_notes"
                        placeholder="Approval, IdP environment, and rollback notes."
                      />
                    </label>
                    <label className="field">
                      <span>Metadata JSON</span>
                      <textarea
                        className="textarea"
                        name="metadata_json"
                        defaultValue={`{"source":"mission_control"}`}
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Save rollout policy
                    </button>
                  </form>
                </>
              )}
            </article>

            <article className="stack-card">
              <h3>SCIM provisioning</h3>
              {!enterprise.apiReachable ? (
                <p className="detail">
                  {enterprise.blockedReason ?? "Enterprise API is unavailable."}
                </p>
              ) : (
                <>
                  <div className="feed-list">
                    {enterprise.scimGroups.length === 0 ? (
                      <p className="detail">No SCIM groups have been provisioned.</p>
                    ) : (
                      enterprise.scimGroups.slice(0, 4).map((group) => (
                        <div className="feed-row" key={group.id}>
                          <div>
                            <h4>{group.displayName}</h4>
                            <p className="detail">
                              {group.externalId ?? "no external id"} / {group.status}
                            </p>
                            <p className="detail">
                              Roles {group.roleKeys.join(", ") || "none"} / permissions{" "}
                              {group.permissions.length}
                            </p>
                          </div>
                          <span className="pill">{titleCaseToken(group.status)}</span>
                        </div>
                      ))
                    )}
                    {enterprise.scimUsers.length === 0 ? (
                      <p className="detail">No SCIM users have been provisioned.</p>
                    ) : (
                      enterprise.scimUsers.slice(0, 4).map((user) => (
                        <div className="feed-row" key={user.id}>
                          <div>
                            <h4>{user.displayName}</h4>
                            <p className="detail">
                              {user.userName} / member {user.memberId} /{" "}
                              {user.active ? "active" : "inactive"}
                            </p>
                            <p className="detail">
                              Roles {user.roleKeys.join(", ") || "none"} / groups{" "}
                              {user.groups.join(", ") || "none"}
                            </p>
                          </div>
                          <span className={`pill${user.active ? "" : " warn"}`}>
                            {user.active ? "active" : "inactive"}
                          </span>
                        </div>
                      ))
                    )}
                  </div>

                  <form action={upsertEnterpriseScimGroupAction} className="composer">
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>Group display name</span>
                      <input className="input" name="display_name" required />
                    </label>
                    <label className="field">
                      <span>External group ID</span>
                      <input className="input" name="external_id" />
                    </label>
                    <label className="field">
                      <span>Role keys</span>
                      <textarea className="input" name="role_keys" placeholder="operator" />
                    </label>
                    <label className="field">
                      <span>Permissions</span>
                      <textarea
                        className="input"
                        name="permissions"
                        placeholder="command_submit&#10;enterprise_manage"
                      />
                    </label>
                    <button className="button secondary" type="submit">
                      Upsert SCIM group
                    </button>
                  </form>

                  <form action={upsertEnterpriseScimUserAction} className="composer">
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <label className="field">
                      <span>User name</span>
                      <input className="input" name="user_name" required />
                    </label>
                    <label className="field">
                      <span>Display name</span>
                      <input className="input" name="display_name" required />
                    </label>
                    <label className="field">
                      <span>External user ID</span>
                      <input className="input" name="external_id" />
                    </label>
                    <label className="field">
                      <span>Role keys</span>
                      <textarea className="input" name="role_keys" placeholder="operator" />
                    </label>
                    <label className="field">
                      <span>Permissions</span>
                      <textarea
                        className="input"
                        name="permissions"
                        placeholder="command_submit&#10;enterprise_manage"
                      />
                    </label>
                    <label className="field">
                      <span>Groups</span>
                      <textarea className="input" name="groups" placeholder="ops" />
                    </label>
                    <label className="check-row">
                      <input type="hidden" name="active" value="false" />
                      <input type="checkbox" name="active" value="true" defaultChecked />
                      <span>Active user grants org-member access</span>
                    </label>
                    <button className="button secondary" type="submit">
                      Upsert SCIM user
                    </button>
                  </form>
                </>
              )}
            </article>
          </div>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="org">Agent Marketplace</span>
              <h2>Packaged worker modules with contained prompts, skills, and runtime policy.</h2>
            </div>
            <p className="status-meta">
              Modules are installable catalog units that can own a role charter, prompt bundle,
              runtime job kinds, and configuration defaults.
            </p>
          </div>

          <article className="stack-card">
            <h3>Hosted marketplace catalog</h3>
            {!agentModules.apiReachable ? (
              <p className="detail">
                {agentModules.blockedReason ?? "Agent module API is unavailable."}
              </p>
            ) : (
              <>
                <form action={syncHostedMarketplaceAction} className="composer">
                  <input type="hidden" name="project_id" value={activeProjectId} />
                  <input type="hidden" name="actor" value={activeActorId} />
                  <label className="field">
                    <span>Hosted org ID</span>
                    <input
                      className="input"
                      name="hosted_org_id"
                      defaultValue={
                        commercial.edition?.accountConnectOrg?.hostedOrgId ?? ""
                      }
                      placeholder="acct_org_..."
                    />
                  </label>
                  <label className="field">
                    <span>Metadata JSON</span>
                    <textarea
                      className="input"
                      name="metadata_json"
                      defaultValue={`{"source":"mission_control"}`}
                    />
                  </label>
                  <button className="button secondary" type="submit">
                    Sync hosted catalog
                  </button>
                </form>

                {agentModules.marketplaceSyncRuns.length === 0 ? (
                  <p className="detail">
                    No hosted marketplace sync runs have been recorded.
                  </p>
                ) : (
                  <div className="feed-list">
                    {agentModules.marketplaceSyncRuns.slice(0, 3).map((run) => (
                      <div className="feed-row" key={run.id}>
                        <div>
                          <h4>{titleCaseToken(run.status)}</h4>
                          <p className="detail">
                            {run.listingCount} listing(s) / hosted org{" "}
                            {run.hostedOrgId ?? "not linked"} /{" "}
                            {formatDateTime(run.createdAt)}
                          </p>
                          {run.error ? (
                            <p className="detail">
                              Error {String(run.error.reason ?? "unknown")}:{" "}
                              {String(run.error.message ?? "no message")}
                            </p>
                          ) : null}
                          {run.syncPacketArtifactId ? (
                            <p className="detail">
                              Sync packet artifact: {run.syncPacketArtifactId}
                            </p>
                          ) : null}
                        </div>
                        <span className={`pill${run.status === "synced" ? "" : " warn"}`}>
                          {titleCaseToken(run.status)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}

                {agentModules.hostedListings.length === 0 ? (
                  <p className="detail">
                    No hosted marketplace listings have been synced. Local modules
                    remain available below.
                  </p>
                ) : (
                  <div className="feed-list">
                    {agentModules.hostedListings.slice(0, 6).map((listing) => (
                      <div className="feed-row" key={listing.id}>
                        <div>
                          <h4>{listing.displayName}</h4>
                          <p className="detail">
                            {listing.packId} v{listing.version} /{" "}
                            {listing.publisher} / {listing.category}
                          </p>
                          <p className="detail">
                            {listing.description ?? "No hosted description provided."}
                          </p>
                          <p className="detail">
                            {listing.priceModel ?? "custom pricing"}
                            {listing.priceUsdMicros
                              ? ` / ${formatMicros(listing.priceUsdMicros)}`
                              : ""}{" "}
                            / {titleCaseToken(listing.status)}
                          </p>
                          <p className="detail">
                            Required entitlements{" "}
                            {listing.requiredEntitlements.length > 0
                              ? listing.requiredEntitlements.join(", ")
                              : "none"}
                          </p>
                        </div>
                        <span
                          className={`pill${
                            listing.status === "active" ||
                            listing.status === "available"
                              ? ""
                              : " warn"
                          }`}
                        >
                          Hosted
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </article>

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
                      <p className="detail">
                        Required entitlements{" "}
                        {module.requiredEntitlements.length > 0
                          ? module.requiredEntitlements.join(", ")
                          : "none"}{" "}
                        / {titleCaseToken(module.entitlementStatus)}
                      </p>
                    </div>
                    {module.installed ? (
                      <div className="button-stack">
                        <span className="pill">
                          installed
                          {module.installedAt
                            ? ` ${formatDateTime(module.installedAt)}`
                            : ""}
                        </span>
                        <form action={uninstallAgentModuleAction}>
                          <input type="hidden" name="module_id" value={module.moduleId} />
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <input
                            type="hidden"
                            name="reason"
                            value="Disabled from Mission Control marketplace."
                          />
                          <button className="button ghost" type="submit">
                            Uninstall
                          </button>
                        </form>
                        <form action={rollbackAgentModuleAction}>
                          <input type="hidden" name="module_id" value={module.moduleId} />
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <input
                            type="hidden"
                            name="reason"
                            value="Rollback requested from Mission Control marketplace."
                          />
                          <button className="button secondary" type="submit">
                            Roll back
                          </button>
                        </form>
                      </div>
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

          <article className="stack-card">
            <h3>Module install records</h3>
            {!agentModules.apiReachable ? (
              <p className="detail">
                {agentModules.blockedReason ?? "Agent module API is unavailable."}
              </p>
            ) : agentModules.installRecords.length === 0 ? (
              <p className="detail">
                No module install or denial records have been recorded yet.
              </p>
            ) : (
              <div className="feed-list">
                {agentModules.installRecords.slice(0, 8).map((record) => (
                  <div className="feed-row" key={record.id}>
                    <div>
                      <h4>{record.moduleId}</h4>
                      <p className="detail">
                        {record.action} / {record.status} / actor {record.actor}
                      </p>
                      <p className="detail">
                        {record.fromVersion ?? "none"} {"->"}{" "}
                        {record.toVersion ?? "none"} /{" "}
                        {formatDateTime(record.createdAt)}
                      </p>
                      <p className="detail">
                        Entitlement result{" "}
                        {typeof record.entitlementResult.status === "string"
                          ? titleCaseToken(record.entitlementResult.status)
                          : "recorded"}
                        {typeof record.entitlementResult.reason === "string"
                          ? ` / ${record.entitlementResult.reason}`
                          : ""}
                      </p>
                      {metadataString(record.metadata, "module_integrity_artifact_id") ? (
                        <p className="detail">
                          Integrity packet:{" "}
                          {metadataString(
                            record.metadata,
                            "module_integrity_artifact_id",
                          )}
                        </p>
                      ) : null}
                    </div>
                    <span
                      className={`pill${record.status === "applied" ? "" : " warn"}`}
                    >
                      {titleCaseToken(record.status)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </article>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="infra">AI Capacity</span>
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
                  {entitlements.items.map((source) => {
                    const sourceConnectionSessions =
                      entitlements.connectionSessions.filter(
                        (session) => session.sourceId === source.sourceId,
                      );
                    const supportedConnectionModes = Array.isArray(
                      source.connectionMetadata.supported_connection_modes,
                    )
                      ? source.connectionMetadata.supported_connection_modes.filter(
                          (mode): mode is string => typeof mode === "string",
                        )
                      : [];
                    return (
                    <div className="feed-row" key={source.id}>
                      <div>
                        <h4>{source.sourceId}</h4>
                        <p className="detail">
                          {source.provider} / {titleCaseToken(source.sourceKind)} /{" "}
                          {source.effectiveStatus}
                        </p>
                        <p className="detail">
                          Auth {titleCaseToken(source.authMethod)} / connection{" "}
                          {source.connectionStatus}
                          {source.oauthScopes.length > 0
                            ? ` / scopes ${source.oauthScopes.join(", ")}`
                            : ""}
                        </p>
                        <p className="detail">
                          Capacity {titleCaseToken(source.capacityState)}
                          {source.capacityResetAt
                            ? ` / resets ${formatDateTime(source.capacityResetAt)}`
                            : ""}
                          {source.observedAt
                            ? ` / observed ${formatDateTime(source.observedAt)}`
                            : ""}
                        </p>
                        <p className="detail">
                          {source.supportedModels.length > 0
                            ? source.supportedModels.join(", ")
                            : "No runtime-backed models"}
                        </p>
                        <p className="detail">{entitlementDetail(source)}</p>
                        {source.authMethod.includes("oauth") ? (
                          source.connectionStatus === "connected" ? (
                            <form
                              action={disconnectEntitlementSourceAction}
                              className="composer compact"
                            >
                              <input
                                type="hidden"
                                name="project_id"
                                value={activeProjectId}
                              />
                              <input type="hidden" name="actor" value={activeActorId} />
                              <input
                                type="hidden"
                                name="source_id"
                                value={source.sourceId}
                              />
                              <input
                                type="hidden"
                                name="reason"
                                value="Operator disconnected official session reference from ProviderOps."
                              />
                              <button className="button secondary" type="submit">
                                Disconnect session
                              </button>
                            </form>
                          ) : (
                            <div className="feed-list">
                              <form
                                action={createEntitlementConnectionSessionAction}
                                className="composer compact"
                              >
                                <input
                                  type="hidden"
                                  name="project_id"
                                  value={activeProjectId}
                                />
                                <input
                                  type="hidden"
                                  name="actor"
                                  value={activeActorId}
                                />
                                <input
                                  type="hidden"
                                  name="source_id"
                                  value={source.sourceId}
                                />
                                <label className="field">
                                  <span>Connection mode</span>
                                  <select
                                    className="input"
                                    name="connection_mode"
                                    defaultValue={supportedConnectionModes[0] ?? ""}
                                  >
                                    {supportedConnectionModes.length === 0 ? (
                                      <option value="">Official provider login</option>
                                    ) : (
                                      supportedConnectionModes.map((mode) => (
                                        <option key={mode} value={mode}>
                                          {titleCaseToken(mode)}
                                        </option>
                                      ))
                                    )}
                                  </select>
                                </label>
                                <textarea
                                  className="input"
                                  name="requested_scopes"
                                  aria-label="Requested OAuth scopes"
                                  defaultValue={source.oauthScopes.join("\n")}
                                  placeholder="One OAuth scope per line"
                                />
                                <button className="button secondary" type="submit">
                                  Start official session
                                </button>
                              </form>

                              {sourceConnectionSessions.length === 0 ? (
                                <p className="detail">
                                  Start a session to get official login instructions
                                  before recording a local session reference.
                                </p>
                              ) : (
                                sourceConnectionSessions.map((session) => (
                                  <div className="feed-row" key={session.id}>
                                    <div>
                                      <h4>{session.connectionMode}</h4>
                                      <p className="detail">
                                        {titleCaseToken(session.status)} / requested by{" "}
                                        {session.requestedBy} /{" "}
                                        {formatDateTime(session.createdAt)}
                                      </p>
                                      <p className="detail">
                                        Login command:{" "}
                                        <code className="mono">
                                          {session.loginCommand ?? "official provider login"}
                                        </code>
                                      </p>
                                      {typeof session.instructions.operator_action ===
                                      "string" ? (
                                        <p className="detail">
                                          {session.instructions.operator_action}
                                        </p>
                                      ) : null}
                                      {session.status ===
                                      "operator_action_required" ? (
                                        <form
                                          action={
                                            completeEntitlementConnectionSessionAction
                                          }
                                          className="composer compact"
                                        >
                                          <input
                                            type="hidden"
                                            name="project_id"
                                            value={activeProjectId}
                                          />
                                          <input
                                            type="hidden"
                                            name="actor"
                                            value={activeActorId}
                                          />
                                          <input
                                            type="hidden"
                                            name="session_id"
                                            value={session.id}
                                          />
                                          <input
                                            className="input"
                                            name="connection_ref"
                                            aria-label="Official session connection reference"
                                            placeholder="SecretRef or local session ref"
                                            required
                                          />
                                          <textarea
                                            className="input"
                                            name="oauth_scopes"
                                            aria-label="OAuth scopes"
                                            defaultValue={session.requestedScopes.join(
                                              "\n",
                                            )}
                                            placeholder="One OAuth scope per line"
                                          />
                                          <textarea
                                            className="input"
                                            name="observed_capacity_json"
                                            aria-label="Observed capacity JSON"
                                            defaultValue={`{"capacity_state":"available"}`}
                                            placeholder="Observed capacity JSON"
                                          />
                                          <button
                                            className="button secondary"
                                            type="submit"
                                          >
                                            Complete session
                                          </button>
                                        </form>
                                      ) : null}
                                    </div>
                                    <code className="mono">{session.id}</code>
                                  </div>
                                ))
                              )}
                            </div>
                          )
                        ) : null}
                      </div>
                      <span
                        className={`pill${
                          source.effectiveStatus === "active" ? "" : " warn"
                        }`}
                      >
                        {entitlementBadge(source)}
                      </span>
                    </div>
                    );
                  })}
                </div>
              )}
            </article>

            <article className="stack-card">
              <h3>Model route decisions</h3>
              <form className="input-grid" method="get">
                <input name="project_id" type="hidden" value={activeProjectId} />
                <label className="field">
                  <span>Provider</span>
                  <select
                    className="input"
                    defaultValue={routingProviderFilter ?? ""}
                    name="route_provider"
                  >
                    <option value="">All providers</option>
                    {Array.from(
                      new Set(entitlements.items.map((source) => source.provider)),
                    )
                      .sort()
                      .map((provider) => (
                        <option key={provider} value={provider}>
                          {titleCaseToken(provider)}
                        </option>
                      ))}
                  </select>
                </label>
                <label className="field">
                  <span>Budget state</span>
                  <select
                    className="input"
                    defaultValue={routingBudgetFilter ?? ""}
                    name="route_budget"
                  >
                    <option value="">All budget states</option>
                    {["green", "yellow", "orange", "red", "frozen"].map((state) => (
                      <option key={state} value={state}>
                        {titleCaseToken(state)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Data boundary</span>
                  <select
                    className="input"
                    defaultValue={routingBoundaryFilter ?? ""}
                    name="route_boundary"
                  >
                    <option value="">All boundaries</option>
                    {["public", "internal", "confidential", "restricted", "local-only"].map(
                      (boundary) => (
                        <option key={boundary} value={boundary}>
                          {titleCaseToken(boundary)}
                        </option>
                      ),
                    )}
                  </select>
                </label>
                <label className="field">
                  <span>Outcome</span>
                  <select
                    className="input"
                    defaultValue={routingOutcomeFilter ?? ""}
                    name="route_outcome"
                  >
                    <option value="">Selected and denied</option>
                    <option value="selected">Selected only</option>
                    <option value="denied">Denied only</option>
                  </select>
                </label>
                <button className="button secondary" type="submit">
                  Filter decisions
                </button>
              </form>
              {!routingLogs.apiReachable ? (
                <p className="detail">
                  {routingLogs.blockedReason ?? "Route decision API is unavailable."}
                </p>
              ) : routingLogs.items.length === 0 ? (
                <p className="detail">No model route decisions match these filters.</p>
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
                          {titleCaseToken(route.budgetState)} budget /{" "}
                          {formatDateTime(route.createdAt)}
                        </p>
                        <p className="detail">
                          {route.selectedSourceId
                            ? `Auth ${
                                route.selectedSource &&
                                typeof route.selectedSource.auth_method === "string"
                                  ? titleCaseToken(route.selectedSource.auth_method)
                                  : "Unknown"
                              } / capacity ${
                                route.selectedSource &&
                                typeof route.selectedSource.capacity_state === "string"
                                  ? titleCaseToken(route.selectedSource.capacity_state)
                                  : "Unknown"
                              } / ${titleCaseToken(route.routingMode)}`
                            : routingRejectionSummary(route.rejectedSources)}
                        </p>
                        <p className="detail">
                          Policy {route.policyAllowed ? "allowed" : "denied"} /{" "}
                          {route.reservationRequired
                            ? "BudgetReservation required"
                            : "No reservation required by skill policy"}
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

        <section className="panel-stack" data-area="reports" aria-label="Reports">
          <div className="section-head">
            <div>
              <span className="eyebrow">Reports</span>
              <h2>Holistic view — throughput, cost, and agent performance.</h2>
            </div>
          </div>
          {(() => {
            const receipt = receipts.items[0];
            // Prefer a governed code-task receipt: it carries the real changes /
            // tests / cost / files behind the effect. Match the latest receipt to
            // its run when possible, else fall back to any run with evidence.
            const codeTask =
              (receipt
                ? codeTasksFeed.items.find(
                    (ct) =>
                      (receipt.createdByJobId?.includes(ct.id) ?? false) ||
                      receipt.dedupeKey.includes(ct.id) ||
                      (ct.pull_request_url != null && receipt.providerRef === ct.pull_request_url),
                  )
                : undefined) ??
              codeTasksFeed.items.find(
                (ct) =>
                  ct.state &&
                  (ct.state.additions != null ||
                    ct.state.files_changed != null ||
                    (ct.state.tests?.length ?? 0) > 0),
              );
            if (codeTask) {
              const st = codeTask.state ?? {};
              const tests = st.tests ?? [];
              const testTotal = tests.length;
              const testPass = tests.filter((t) => t.exit_code === 0).length;
              const testDurationMs = tests.reduce((acc, t) => acc + (t.duration_ms ?? 0), 0);
              const costMicros = st.cost_usd_micros ?? codeTask.usage?.cost_usd_micros;
              const testsGreen = testTotal > 0 ? testPass === testTotal : st.tests_passed === true;
              const verified = codeTask.status === "completed" && testsGreen;
              const failed = codeTask.status === "failed" || codeTask.status === "blocked";
              const modelName = st.model ?? codeTask.usage?.model;
              const paths = st.paths ?? [];
              const checks: { ok: boolean; label: string }[] = [];
              if (st.tests_passed != null || testTotal > 0)
                checks.push({
                  ok: testsGreen,
                  label: `Tests ${testTotal > 0 ? `pass (${testPass}/${testTotal})` : st.tests_passed ? "pass" : "did not pass"}`,
                });
              if (st.security_verdict)
                checks.push({ ok: /pass|clean|ok|none/i.test(st.security_verdict), label: `Security scan — ${st.security_verdict}` });
              if (st.reviewer_independent != null)
                checks.push({ ok: st.reviewer_independent, label: "Independent reviewer signed off" });
              if (st.regressions_ok != null)
                checks.push({ ok: st.regressions_ok, label: "No regressions detected" });
              if (st.consistency_verdict)
                checks.push({ ok: /pass|ok|consistent/i.test(st.consistency_verdict), label: `Consistency — ${st.consistency_verdict}` });
              if (checks.length === 0)
                checks.push({ ok: verified, label: verified ? "Governed pipeline completed" : `Status — ${codeTask.status}` });
              return (
                <article className="mission-evidence-receipt">
                  <header>
                    <span className="mission-receipt-avatar">CT</span>
                    <span>
                      <strong>{codeTask.title}</strong>
                      <small>
                        {codeTask.id}
                        {modelName ? ` · ${modelName}` : ""} · {codeTask.phase}
                      </small>
                    </span>
                    <span className={`status-text ${verified ? "verified" : failed ? "blocked" : "running"}`}>
                      <span className={`dot ${verified ? "green" : failed ? "red" : "teal"}`} />
                      {verified ? "verified" : codeTask.status}
                    </span>
                  </header>
                  <div className="mission-receipt-metrics mission-receipt-metrics--four">
                    <span>
                      <small>Changes</small>
                      <strong className="mission-receipt-changes">
                        {st.additions != null || st.deletions != null ? (
                          <>
                            <span className="add">+{st.additions ?? 0}</span>{" "}
                            <span className="del">−{st.deletions ?? 0}</span>
                          </>
                        ) : (
                          "—"
                        )}
                      </strong>
                    </span>
                    <span>
                      <small>Tests</small>
                      <strong>
                        {testTotal > 0
                          ? `${testPass}/${testTotal} passed`
                          : st.tests_passed != null
                            ? st.tests_passed
                              ? "pass"
                              : "fail"
                            : "—"}
                      </strong>
                    </span>
                    <span><small>Cost</small><strong>{costMicros != null ? formatMicros(costMicros) : "—"}</strong></span>
                    <span><small>Test duration</small><strong>{formatMsDuration(testDurationMs)}</strong></span>
                  </div>
                  <div className="mission-receipt-verification">
                    <span className="eyebrow">Verification</span>
                    {checks.map((c, i) => (
                      <p key={i}>
                        <span className={`dot ${c.ok ? "green" : "red"}`} aria-hidden /> {c.label}
                      </p>
                    ))}
                  </div>
                  <div className="mission-receipt-files">
                    <span className="eyebrow">
                      Files changed{st.files_changed != null ? ` · ${st.files_changed}` : ""}
                    </span>
                    {paths.length > 0 ? (
                      paths.slice(0, 14).map((p, i) => <code key={i}>{p}</code>)
                    ) : (
                      <code>{st.summary ?? "No file manifest recorded"}</code>
                    )}
                    {codeTask.pull_request_url ? (
                      <a href={codeTask.pull_request_url} target="_blank" rel="noreferrer">
                        → {codeTask.pull_request_url}
                      </a>
                    ) : null}
                  </div>
                </article>
              );
            }
            if (receipt) {
              return (
                <article className="mission-evidence-receipt">
                  <header>
                    <span className="mission-receipt-avatar">RC</span>
                    <span>
                      <strong>{titleCaseToken(receipt.effectKind)}</strong>
                      <small>{receipt.id} · {formatDateTime(receipt.createdAt)}</small>
                    </span>
                    <span className="status-text verified"><span className="dot green" />{receipt.status}</span>
                  </header>
                  <div className="mission-receipt-metrics">
                    <span><small>Provider ref</small><strong>{receipt.providerRef ?? "Local control plane"}</strong></span>
                    <span><small>Job lineage</small><strong>{receipt.createdByJobId ?? "Canonical event"}</strong></span>
                    <span><small>Dedupe key</small><strong>{receipt.dedupeKey}</strong></span>
                  </div>
                  <div className="mission-receipt-verification">
                    <span className="eyebrow">Verification</span>
                    <p><span className="dot green" aria-hidden /> Durable receipt recorded in the control plane</p>
                    <p><span className="dot green" aria-hidden /> Unique dedupe key prevents duplicate effects</p>
                    <p><span className="dot green" aria-hidden /> Provider and job lineage remain auditable</p>
                  </div>
                </article>
              );
            }
            return (
              <p className="empty-state">No effect receipts have been recorded for this project yet.</p>
            );
          })()}
          <ReportsView report={report} />
        </section>

        <section className="panel-stack" data-area="capabilities" aria-label="Capabilities">
          <div className="section-head">
            <div>
              <span className="eyebrow">Capabilities</span>
              <h2>Agent tools &amp; MCP servers — discover, audit, extend.</h2>
            </div>
          </div>
          <CapabilitiesView
            projectId={activeProjectId}
            nativeTools={capabilities.nativeTools}
            skills={capabilities.skills}
            mcpServers={capabilities.mcpServers}
            apiReachable={capabilities.apiReachable}
            engineLabel={capabilities.engineLabel}
            engineModel={capabilities.engineModel}
          />
        </section>

        <section className="panel-stack" data-area="meetings" aria-label="Meeting room">
          <div className="section-head">
            <div>
              <span className="eyebrow">Meeting Room</span>
              <h2>Cross-functional agent panels — watch them debate to a PRD.</h2>
            </div>
          </div>
          <MeetingsHome
            projectId={activeProjectId}
            orgId={activeOrgId}
            meetings={meetingRooms.items}
            roles={meetingRooms.roles}
            tasks={tasks.items.map((t) => ({ id: t.id, title: t.title }))}
            apiReachable={meetingRooms.apiReachable}
          />
          <div className="stack-grid" style={{ marginTop: 14 }}>
            <MeetingConsensusCard projectId={activeProjectId} actor={activeActorId} />
          </div>
        </section>

        <section className="panel-stack" data-area="code_tasks" aria-label="Code tasks">
          <div className="section-head">
            <div>
              <span className="eyebrow">Code Tasks</span>
              <h2>An agent clones the repo, edits code, runs your tests — then opens a gated PR.</h2>
            </div>
          </div>
          <CodeTasksHome
            projectId={activeProjectId}
            orgId={activeOrgId}
            codeTasks={codeTasksFeed.items}
            apiReachable={codeTasksFeed.apiReachable}
            previews={previewFeed}
          />
        </section>

        <section data-area="foundations" aria-label="Design foundations">
          <FoundationsScreen />
        </section>

        <section data-area="components" aria-label="Component library">
          <ComponentsScreen />
        </section>

        <section className="panel-stack" data-area="reliability" aria-label="Reliability">
          <div className="section-head">
            <div>
              <span className="eyebrow">Reliability</span>
              <h2>AI SRE — anomalies become incidents, incidents become governed fix PRs, and the loop learns.</h2>
            </div>
          </div>
          <ReliabilityHome feed={reliabilityFeed} />
        </section>

        <section className="panel-stack" data-area="discovery" aria-label="Discovery">
          <div className="section-head">
            <div>
              <span className="eyebrow">Discovery</span>
              <h2>Feed a signal — BOSS researches the market, vets the thesis, and proposes evidence‑backed outcomes.</h2>
            </div>
          </div>
          <DiscoveriesHome
            projectId={activeProjectId}
            orgId={activeOrgId}
            discoveries={discoveryFeed.items}
            apiReachable={discoveryFeed.apiReachable}
          />
        </section>

        <section className="panel-stack" data-area="pipeline" aria-label="Coding pipeline">
          <div className="section-head">
            <div>
              <span className="eyebrow">Coding Pipeline · VAG</span>
              <h2>Verification-Amplified Generation — cheap models write, frontier models verify. Tune cost vs. rigor per tier and compare run costs.</h2>
            </div>
          </div>
          <PipelineView data={pipelineData} />
          <div className="stack-grid" style={{ marginTop: 14 }}>
            <FactoryScaleCard wip={wipStatus} />
          </div>
        </section>

        <section className="panel-stack" data-area="sandboxes" aria-label="Sandboxes">
          <div className="section-head">
            <div>
              <span className="eyebrow">Execution Plane</span>
              <h2>Inspect live sandboxes — browse the working tree and run a governed console.</h2>
            </div>
          </div>
          <SandboxesHome
            sandboxes={sandboxFeed.items}
            apiReachable={sandboxFeed.apiReachable}
            unavailable={sandboxFeed.unavailable}
            host={sandboxFeed.host}
          />
        </section>

        <section className="panel-stack" data-area="work" aria-label="Task runs">
          <div className="section-head">
            <div>
              <span className="eyebrow">Task Runs</span>
              <h2>Workflow transitions — click a task to trace its run.</h2>
            </div>
          </div>
          <MissionTaskBoard
            tasks={tasks.items}
            jobs={opsQueue.jobs}
            receipts={receipts.items}
          />
          <PlansCard projectId={activeProjectId} plans={plansList} />
          <WorkBoard
            tasks={tasks.items}
            jobs={opsQueue.jobs}
            events={events.items}
            artifacts={artifacts.items}
            decisions={decisions.items}
            agentRuns={agentRuns.items}
            outputs={taskOutputs.items}
            initialTaskId={requestedWorkTask}
          />
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="work">Company Workspace</span>
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
              <h3>Meeting timeline</h3>
              {!meetings.apiReachable ? (
                <p className="detail">
                  {meetings.blockedReason ?? "Meeting API is unavailable."}
                </p>
              ) : meetings.items.length === 0 ? (
                <p className="detail">
                  No structured meetings have been scheduled or run yet.
                </p>
              ) : (
                <div className="feed-list">
                  {meetings.items.map((meeting) => (
                    <div className="feed-row" key={meeting.id}>
                      <div>
                        <h4>{meeting.title}</h4>
                        <p className="detail">
                          {meeting.status} / participants{" "}
                          {meeting.participants.length} / agenda{" "}
                          {meeting.agenda.length}
                        </p>
                        <p className="detail">
                          Actions {meeting.actionTaskIds.length} / decisions{" "}
                          {meeting.decisions.length} / dissent{" "}
                          {meeting.dissentRecords.length}
                        </p>
                        {meeting.blockedState ? (
                          <p className="detail">
                            Blocked:{" "}
                            {String(
                              meeting.blockedState.reason ??
                                meeting.blockedState.next_action ??
                                "review required",
                            )}
                          </p>
                        ) : null}
                        {meeting.minutesArtifactId ? (
                          <p className="detail">
                            Minutes {meeting.minutesArtifactId} / transcript{" "}
                            {meeting.transcriptArtifactId ?? "not linked"}
                          </p>
                        ) : (
                          <p className="detail">{meeting.purpose}</p>
                        )}
                      </div>
                      <code className="mono">{meeting.id}</code>
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

            <article className="stack-card">
              <h3>Software Factory evidence</h3>
              {!decisions.apiReachable || !artifacts.apiReachable ? (
                <p className="detail">
                  {decisions.blockedReason ??
                    artifacts.blockedReason ??
                    "Decision or artifact evidence is unavailable."}
                </p>
              ) : softwareFactoryDecisionEvidence.length === 0 &&
                softwareFactoryArtifactEvidence.length === 0 ? (
                <p className="detail">
                  No Software Factory release evidence has been recorded yet.
                </p>
              ) : (
                <div className="feed-list">
                  {softwareFactoryDecisionEvidence.map((evidence) => (
                    <div className="feed-row" key={`decision:${evidence.decisionId}`}>
                      <div>
                        <h4>{evidence.title}</h4>
                        <p className="detail">
                          Release gate{" "}
                          {titleCaseToken(evidence.releaseGoNoGoStatus ?? "unknown")}{" "}
                          / validation{" "}
                          {titleCaseToken(evidence.validationStatus ?? "unknown")}
                        </p>
                        <p className="detail">
                          Review {evidence.reviewPacketArtifactId ?? "not linked"} / test{" "}
                          {evidence.testReportArtifactId ?? "not linked"}
                        </p>
                        <p className="detail">
                          Security {evidence.securityReviewArtifactId ?? "not linked"} /{" "}
                          go-no-go {evidence.releaseGoNoGoArtifactId ?? "not linked"}
                        </p>
                        {evidence.localValidationReceiptId ? (
                          <p className="detail">
                            Local validation receipt {evidence.localValidationReceiptId}
                          </p>
                        ) : null}
                        {evidence.pullRequestUrl ? (
                          <a href={evidence.pullRequestUrl} rel="noreferrer" target="_blank">
                            Open sandbox PR
                          </a>
                        ) : null}
                      </div>
                      <code className="mono">{evidence.decisionId}</code>
                    </div>
                  ))}
                  {softwareFactoryArtifactEvidence.map((artifact) => (
                    <div className="feed-row" key={`artifact:${artifact.id}`}>
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
              <span className="eyebrow" data-area="work">What Changed</span>
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
                          {deadletter.sourceKind} / {deadletter.status} /{" "}
                          {formatDateTime(deadletter.createdAt)}
                        </p>
                        {deadletter.incidentArtifactId ? (
                          <p className="detail">
                            Incident {deadletter.incidentArtifactId} / task{" "}
                            {deadletter.incidentTaskId ?? "not linked"}
                          </p>
                        ) : null}
                        {deadletter.requeuedJobId ? (
                          <p className="detail">
                            Requeued as {deadletter.requeuedJobId} by{" "}
                            {deadletter.resolvedBy ?? "unknown"}
                          </p>
                        ) : null}
                        {deadletter.relatedEntityId ? (
                          <p className="muted">
                            Related {deadletter.relatedEntityType ?? "entity"}: {" "}
                            <code className="mono">{deadletter.relatedEntityId}</code>
                            {deadletter.relatedStatus
                              ? ` / ${deadletter.relatedStatus}`
                              : ""}
                          </p>
                        ) : null}
                        <div className="inline-actions">
                          <form action={createDeadletterIncidentAction}>
                            <input
                              type="hidden"
                              name="deadletter_id"
                              value={deadletter.id}
                            />
                            <input
                              type="hidden"
                              name="project_id"
                              value={activeProjectId}
                            />
                            <input
                              type="hidden"
                              name="actor"
                              value={activeActorId}
                            />
                            <input type="hidden" name="severity" value="high" />
                            <button className="button secondary" type="submit">
                              Incident packet
                            </button>
                          </form>
                          {deadletter.sourceKind === "job" &&
                          !deadletter.requeuedJobId ? (
                            <form action={requeueDeadletterJobAction}>
                              <input
                                type="hidden"
                                name="deadletter_id"
                                value={deadletter.id}
                              />
                              <input
                                type="hidden"
                                name="project_id"
                                value={activeProjectId}
                              />
                              <input
                                type="hidden"
                                name="actor"
                                value={activeActorId}
                              />
                              <input
                                type="hidden"
                                name="reason"
                                value="operator_requested_requeue"
                              />
                              <button className="button secondary" type="submit">
                                Requeue job
                              </button>
                            </form>
                          ) : null}
                        </div>
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
          className="panel-stack decision-inbox-section"
          data-area="decisions"
          id="decision-inbox"
          tabIndex={-1}
        >
          <div className="section-head">
            <div>
              <span className="eyebrow">Decision Queue</span>
              <h2 id="decision-inbox-title">
                Agents proposed — <span className="dq-you-decide">you decide</span>.
              </h2>
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
            <div className="dq">
              {decisions.items.map((decision) => {
                const estimatedCost =
                  typeof decision.costSummary.estimated_usd_micros === "number"
                    ? decision.costSummary.estimated_usd_micros
                    : undefined;
                const isPending =
                  decision.status === "pending" || decision.status === "delegated";
                const isApproved = decision.status === "approved";
                const linkedTask = decision.taskId
                  ? tasks.items.find((task) => task.id === decision.taskId)
                  : undefined;
                const ownerRole = linkedTask?.ownerRoleId
                  ? roles.items.find((role) => role.id === linkedTask.ownerRoleId)
                  : undefined;
                const who = ownerRole?.displayName ?? "Control plane";
                const impact = decisionImpact(decision);
                const rec = decision.recommendation;
                const why = typeof rec.why === "string" ? rec.why : "";
                const plan = typeof rec.plan === "string" ? rec.plan : "";
                const review = typeof rec.plan_review === "string" ? rec.plan_review : "";

                return (
                  <article className="dq-row" key={decision.id}>
                    <div className="dq-body">
                      <div className="dq-orb" aria-hidden>{roleInitials(who)}</div>
                      <div className="dq-main">
                        <div className="dq-propose">
                          <span className="dq-who"><b>{who}</b> proposes</span>
                          <span className={`dq-impact ${impact.tone}`}>{impact.label}</span>
                        </div>
                        <h3 className="dq-title">{decision.question}</h3>
                        <p className="dq-rationale">
                          {why || formatRecommendation(decision)}
                        </p>
                        {plan || review ? (
                          <details className="decision-plan-details">
                            <summary>Read the technical plan{review ? " & automated review" : ""}</summary>
                            {plan ? (
                              <div className="decision-plan-sec">
                                <h4>Technical plan</h4>
                                <pre>{plan}</pre>
                              </div>
                            ) : null}
                            {review ? (
                              <div className="decision-plan-sec">
                                <h4>Automated plan review</h4>
                                <pre>{review}</pre>
                              </div>
                            ) : null}
                          </details>
                        ) : null}
                        <div className="dq-evidence">
                          <span className="dq-ev-label">evidence</span>
                          {linkedTask ? (
                            <span className="dq-ev">
                              T-{decision.taskId?.slice(-4)} · {linkedTask.title.slice(0, 44)}
                              {linkedTask.title.length > 44 ? "…" : ""}
                            </span>
                          ) : null}
                          <span className="dq-ev">risk tier {decision.riskTier}</span>
                          <span className="dq-ev">default {decision.defaultAction ?? "hold"}</span>
                          {estimatedCost != null ? (
                            <span className="dq-ev">{formatMicros(estimatedCost)}</span>
                          ) : null}
                          <span className="dq-ev">{formatDeadline(decision.deadlineAt)}</span>
                        </div>
                        {isPending ? (
                          <p className="dq-effect">{approvalEffectLabel(decision.recommendation)}</p>
                        ) : null}
                      </div>
                    </div>
                    <div className="dq-foot">
                      {isPending ? (
                        <form action={resolveDecisionAction} className="dq-actions">
                          <input type="hidden" name="decision_id" value={decision.id} />
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <button className="dq-btn approve" name="action" value="approve">
                            Approve
                          </button>
                          <button className="dq-btn neutral" name="action" value="revise">
                            Request changes
                          </button>
                          <button className="dq-btn reject" name="action" value="reject">
                            Reject
                          </button>
                          <details className="dq-more">
                            <summary>Note · delegate</summary>
                            <div className="dq-more-body">
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
                              <button className="dq-btn neutral" name="action" value="delegate">
                                Delegate
                              </button>
                            </div>
                          </details>
                          <span className="dq-waited">{waitedLabel(decision.createdAt)}</span>
                        </form>
                      ) : (
                        <>
                          <span
                            className={`dq-verdict ${
                              isApproved || decision.status === "executed"
                                ? "green"
                                : decision.status === "rejected"
                                  ? "red"
                                  : "muted"
                            }`}
                          >
                            {decision.status.toUpperCase()}
                          </span>
                          <span className="dq-note">{decisionStatusDetail(decision)}</span>
                          {isApproved ? (
                            <details className="dq-more">
                              <summary>Mark executed</summary>
                              <form action={resolveDecisionAction} className="dq-more-body">
                                <input type="hidden" name="decision_id" value={decision.id} />
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
                                <button className="dq-btn approve" name="action" value="execute">
                                  Mark executed
                                </button>
                              </form>
                            </details>
                          ) : null}
                          <span className="dq-waited">{waitedLabel(decision.createdAt)}</span>
                        </>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>
          )}

          {decisions.apiReachable ? (
            <details className="dq-digest">
              <summary>Create decision digest</summary>
              <div className="dq-digest-body">
                <p className="detail">
                  Creates a durable digest artifact. Optional channel creates an
                  outbox draft only; sending still requires a budgeted queue action.
                </p>
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
              </div>
            </details>
          ) : null}
        </section>

        <section className="panel-stack approval-rules-section" aria-label="Approval rules">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="decisions">Approval Rules</span>
              <h2>What requires your approval — and why.</h2>
            </div>
          </div>
          <ApprovalRulesView rules={approvalRules.rules} apiReachable={approvalRules.apiReachable} />
        </section>
      </div>
    </ConsoleShell>
  );
}
