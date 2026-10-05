
import { consoleAuthConfig } from "../lib/console-session";
import { cookies, headers } from "next/headers";
import {
  areasToRender,
  isFullDocumentRequest,
  LOADED_AREAS_COOKIE,
} from "../lib/area-loading";
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
  createDeadletterRemediationAction,
  createEntitlementConnectionSessionAction,
  createOpsDigestAction,
  createDeploymentRunAction,
  installAgentModuleAction,
  installPackAction,
  dryRunOutboxMessageAction,
  queueOutboxMessageAction,
  refreshHealthSnapshotAction,
  disconnectEntitlementSourceAction,
  revokeBreakGlassGrantAction,
  rollbackAgentModuleAction,
  runAutoscalerPlanAction,
  runDeploymentHealthCheckAction,
  runResourceJanitorAction,
  submitOutboxDraftAction,
  submitCommandAction,
  syncProviderResourcesAction,
  uninstallAgentModuleAction,
  upsertDeploymentEnvironmentPolicyAction,
  upsertEnterpriseIdentityProviderAction,
  upsertEnterpriseIdentityRolloutPolicyAction,
  upsertEnterpriseScimGroupAction,
  upsertEnterpriseScimUserAction,
  upsertCommsChannelPolicyAction,
  upsertOrgMemberAction,
  upsertWorkerPoolAction,
  uploadModelCredentialAction,
} from "./actions";
import ConsoleShell from "./ConsoleShell";
import {
  CommunityIncludedBadge,
  SurfaceHeading,
} from "./edition-ui";
import { OnboardingHelpCard } from "./onboarding-help-card";
import { BossAgentHome } from "./boss-agent-home";
import { WorkBoard, WorkflowHealth } from "./workflow-ui";
import { MissionAgentRoster, MissionTaskBoard } from "./mission-operations-ui";
import { ReportsView } from "./reports-ui";
import type { DataBoundary } from "@boss/api-client";
import { formatDateTime as formatDateTimeShared } from "../lib/datetime";
import { fetchAuditSummary } from "../lib/audit";
import { fetchArtifactFeed } from "../lib/artifacts";
import { fetchAgentModuleFeed } from "../lib/agent-modules";
import { fetchAgentStudioFeed } from "../lib/agent-studio";
import { AgentProcesses } from "./agent-process-ui";
import { ProcessManager } from "./process-manager-ui";
import { ActivityLog } from "./activity-log-ui";
import { DeliveryWorkflows } from "./delivery-workflows-ui";
import { fetchWorkOrchestrationFeed } from "../lib/work-orchestration";
import { WorkOrchestration } from "./work-orchestration-ui";
import { PackExportButton, PackImportForm } from "./packs-import-ui";
import { DecisionInbox } from "./decision-inbox-ui";
import { fetchAuthSession } from "../lib/auth-session";
import { fetchInboxFeed, fetchOutboxFeed } from "../lib/comms";
import { fetchBudgetAllocationFeed } from "../lib/budget-allocations";
import {
  loadAssuranceAreaData,
  loadBudgetAreaData,
  loadInfraAreaData,
  ASSURANCE_AREA_IDS,
  BUDGET_AREA_IDS,
  INFRA_AREA_IDS,
} from "../lib/area-data";
import { fetchBudgetStatus, fetchBudgetIntensity, periodWords } from "../lib/budget-status";
import { BudgetIntensitySlider } from "./budget-intensity-ui";
import { SettingsKnobs } from "./settings-knobs-ui";
import { fetchLatestCanary } from "../lib/canaries";
import { CanaryCard } from "./canary-ui";
import { fetchDashboard } from "../lib/dashboard";
import { DashboardPanel } from "./dashboard-ui";
import { fetchPolicyEvals, fetchRunTrace } from "../lib/safety";
import { PolicyEvalList, AnomalyScanCard, RunTraceViewer } from "./safety-ui";
import { fetchLatestEval } from "../lib/evals";
import { fetchSkillInvocations } from "../lib/skill-invocations";
import {
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
import { fetchCommitmentReconciliations, fetchFinopsSummary } from "../lib/finops";
import { ChaosCard, FinopsCard } from "./phase4-ui";
import { fetchPlans } from "../lib/plans";
import { PlansCard } from "./plans-ui";
import { fetchBreakGlassGrantFeed } from "../lib/break-glass";
import { fetchChangeJournalFeed } from "../lib/change-journal";
import { fetchDeadletterFeed } from "../lib/deadletters";
import { fetchDecisionFeed, parseDecisionInboxFilter, type DecisionFeedItem } from "../lib/decisions";
import { fetchEntitlementFeed } from "../lib/entitlements";
import { fetchActivityFeed } from "../lib/events";
import { fetchHealthSnapshotFeed } from "../lib/health-snapshots";
import { fetchInfrastructureResourceFeed } from "../lib/infrastructure";
import { fetchMemberFeed, fetchOrgMemberFeed } from "../lib/members";
import { fetchMeetingFeed } from "../lib/meetings";
import { fetchMetatronQuery } from "../lib/metatron";
import { fetchOpsQueueFeed } from "../lib/ops";
import { fetchLiveJobsFeed } from "../lib/live-jobs";
import { LiveJobsCard } from "./live-jobs-ui";
import { fetchQaScorecardFeed } from "../lib/qa-scorecard";
import { QaScorecardIsland } from "./qa-scorecard-ui";
import { fetchWorkflowFeed } from "../lib/workflows";
import { WorkflowRegistryView } from "./workflow-registry-ui";
import { fetchReportSummary } from "../lib/reports";
import { fetchAgentRuns } from "../lib/agent-runs";
import { fetchTaskOutputs } from "../lib/outputs";
import { fetchCapabilities } from "../lib/capabilities";
import { CapabilitiesView } from "./capabilities-ui";
import { fetchMeetingRooms } from "../lib/meeting-rooms";
import { MeetingsHome } from "./meeting-room-ui";
import { fetchCodeTasksFeed, fetchPreviews, fetchQaBenchmarkApps, structuredCodeTaskTests } from "../lib/code-tasks";
import { appIdentity } from "./app-identity";
import { CodeTasksHome } from "./code-tasks-ui";
import { WorkforceArea } from "./workforce-ui";
import { resolveLegacyArea, searchParamsFrom } from "./legacy-areas";
import { fetchOrgChart, fetchWorkforce } from "../lib/v2/workforce";
import { ShepherdConsole } from "./shepherd-ui";
import { DeliveryPipeline } from "./delivery-ui";
import { HardwareManager } from "./hardware-manager-ui";
import { MonitoringArea } from "./monitoring-ui";
import { getShepherdOverviewAction, getDeliveryPipelineAction, getDeploymentHealthAction, listSecretsAction, getObservabilityAction,
         listDeploymentsAction } from "./actions";
import { fetchReliabilityFeed } from "../lib/reliability";
import { fetchAiSreCapabilities } from "../lib/ai-sre";
import { AiSreHome } from "./ai-sre-ui";
import { fetchCostInsights, fetchLlmSpend } from "../lib/budget-status";
import { CostInsightsCard, LlmSpendCard, BudgetThresholdsCard } from "./cost-governance-ui";
import { FoundationsScreen, ComponentsScreen } from "./design-system-showcase";
import { buildAgentRoster } from "../lib/roster";
import { TZ_COOKIE, fetchCommandCenterApps, fetchCommandCenterView } from "../lib/command-center-view-server";
import { CommandCenter } from "./command-center-ui";
import { ReliabilityHome } from "./reliability-ui";
import { AssuranceCenter } from "./assurance-ui";
import { fetchAssuranceFeed } from "../lib/assurance";
import { fetchDiscoveriesFeed } from "../lib/discoveries";
import { DiscoveriesHome } from "./discoveries-ui";
import { SetupBoard } from "./setup/setup-board";
import { fetchSetupSummary, landingArea } from "../lib/setup-server";
import { KitHome } from "./skills/kit-home";
import { MemoryHome } from "./memory-ui";
import { DeploymentManagerPanel } from "./deployment-manager-ui";
import { fetchPipelineData } from "../lib/pipeline-profiles";
import { PipelineView } from "./pipeline-ui";
import { fetchSandboxFeed } from "../lib/sandboxes";
import { SandboxesHome } from "./sandboxes-ui";
import { AreaLink } from "./area-link";
import { Suspense } from "react";
import { redirect } from "next/navigation";
import { fetchOrgs } from "../lib/orgs";
import { OrgGate, OrgReconnect } from "./org-gate";
import { fetchApprovalRules } from "../lib/approval-rules";
import { ApprovalRulesView } from "./approval-rules-ui";
import { fetchMcpServers } from "../lib/mcp";
import { fetchPackFeed } from "../lib/packs";
import { describeSpendHonesty, fetchProviderOpsFeed } from "../lib/providerops";
import { ProviderConfigurationSuite } from "./provider-configuration-ui";
import { fetchInfraFabricFeed } from "../lib/infra-fabric";
import { InfraFabricPanel } from "./infra-fabric-ui";
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
import { ImprovementProgramsPanel } from "./improvement-programs-ui";
import {
  extractSoftwareFactoryArtifactEvidence,
  extractSoftwareFactoryDecisionEvidence,
} from "../lib/software-factory";
import { fetchSkillFeed } from "../lib/skills";
import { fetchStatus } from "../lib/status";
import { fetchTaskFeed } from "../lib/tasks";
import { API_URL, PROJECT_ID } from "../lib/client";
import { SubmitButton } from "./submit-button";
import { fetchIdentityProvidersFeed } from "../lib/identity-providers";
import { fetchIdentityRegistryFeed } from "../lib/identity-registry";
import { IdentityProvidersCatalogView } from "./identity-providers-ui";
import { fetchOncallSreStatus } from "../lib/oncall-sre";
import { OncallSreHome } from "./oncall-sre-ui";
import {
  fetchLatestSecurityScan,
  fetchSecurityScanners,
} from "../lib/security-scan";
import { SecurityScanCard } from "./security-scan-ui";
import {
  fetchReleaseBoard,
  fetchReleaseStatus,
  fetchReleases,
} from "../lib/release-planning";
import { ReleasePlanningPanel } from "./release-planning-ui";
import {
  fetchWorkerAutoscalingHistory,
  fetchWorkerAutoscalingStatus,
} from "../lib/worker-autoscaling";
import { WorkerAutoscalingPanel } from "./worker-autoscaling-ui";
import {
  fetchTokenConsumption,
  fetchTokenPolicies,
  fetchTokenRecommendations,
  fetchTokenScorecard,
} from "../lib/token-efficiency";
import { TokenEfficiencyPanel } from "./token-efficiency-ui";
import { fetchObservabilitySuiteFeed } from "../lib/observability-suite";
import { ObservabilitySuitePanel } from "./observability-suite-ui";
import { fetchKnowledgeGraphFeed } from "../lib/knowledge-graph";
import { KnowledgeGraphPanel } from "./knowledge-graph-ui";
import { fetchCICDCatalog, fetchCICDStatus } from "../lib/cicd-pipelines";
import { CICDPipelinesPanel } from "./cicd-pipelines-ui";

import { AskBossHero } from "./ask-boss-hero";
import { ActionForm } from "./client-forms";
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

function roleInitials(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "CP";
  const initials =
    words.length === 1 ? words[0].slice(0, 3) : words.map((word) => word[0]).join("").slice(0, 3);
  return initials.toUpperCase();
}

// Relative "waited Xm" labels use ClientRelativeTime (absolute UTC on SSR,
// relative after mount) so decision-queue rows never trip React #418.

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

function formatDateTime(value?: string): string {
  return formatDateTimeShared(value, "Unknown time");
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

  // START INDEPENDENT WORK TOGETHER.
  //
  // These were seven sequential awaits, each a full round-trip before the next began. Measured,
  // that is where the load time lived: cold TTFB 22s, but with every feed served from memory
  // the SAME page renders in 1.9s — so ~20 of the 22 seconds was waiting, not working. And the
  // waiting was not the API's fault: it serves 82 concurrent requests in 1.6s, and Node issues
  // 82 in 0.8s. The page simply asked for them one at a time.
  //
  // None of these depends on another except observability, which needs a host id from the
  // delivery pipeline — so it stays behind, and everything else starts at once.
  //
  // .catch(() => null) is preserved per-feed: one dead panel must never take the console with
  // it, and Promise.all would reject the batch if any of them threw.
  const [
    shepherdOverview,
    deliveryPipeline,
    deploymentHealth,
    deliverySecrets,
    recentDeployments,
    resolvedSearchParams,
  ] = await Promise.all([
    getShepherdOverviewAction().catch(() => null),
    getDeliveryPipelineAction().catch(() => null),
    getDeploymentHealthAction().catch(() => null),
    listSecretsAction().catch(() => null),
    listDeploymentsAction(10).catch(() => null),
    searchParams ? Promise.resolve(searchParams).then((v) => v) : Promise.resolve(undefined),
  ]);
  // The host target is whichever active target we can borrow a host+credential from; the stack
  // is one per host, not one per app. This is the one genuine dependency in the group.
  const obsHost = (deliveryPipeline?.targets || []).find((t: any) => t.status === "active")?.id;
  // Started here, awaited later WITH fetchOrgs. observability needs obsHost so it cannot join
  // the batch above, but nothing makes it block the org lookup either — and a promise created
  // now is already in flight while the code in between runs.
  const observabilityPromise = getObservabilityAction(obsHost).catch(() => null);
  // A RETIRED SURFACE NEVER RENDERS. Agent Studio, Team structure and the Workflow area's old
  // tabs are rewritten to where they live now before anything is fetched for them; one table,
  // shared with the shell's link handler (app/legacy-areas.ts).
  const legacy = resolveLegacyArea(searchParamsFrom(resolvedSearchParams));
  if (legacy) redirect(`/?${legacy}`);
  const requestedProjectId =
    getParam(resolvedSearchParams?.project_id) ?? PROJECT_ID;
  const requestedArea = getParam(resolvedSearchParams?.area);
  // RENDER THE AREA THE OPERATOR ASKED FOR, AND NOT THE OTHER THIRTY-NINE.
  //
  // Measured on the live box: 8.3 MB of HTML and ~9s of server render per load, 78% of it an
  // RSC payload carrying every area at once — Discovery 949 KB, Task Shepherd 431 KB,
  // Decisions 326 KB — all of it parsed and hydrated by the operator's machine, all but one
  // hidden by a CSS rule. The data was never the problem: every feed answers in 83–235 ms.
  //
  // This was only possible once the sidebar started navigating (see ConsoleShell.go): while it
  // switched areas with pushState and no server round-trip, whichever area the server happened
  // to render for decided what data existed for the session, so nothing could be left out.
  //
  // The default is `overview`, matching the rail's own default, so a URL with no `?area=` still
  // lands somewhere real.
  const shownArea = requestedArea && requestedArea.trim() ? requestedArea.trim() : "overview";

  // A CLICK ON THE RAIL DOES NO WORK. THAT IS THE REQUIREMENT, AND IT OUTRANKS PAYLOAD SIZE.
  //
  // Rendering one area per request cut this page from 8.3 MB to 25-235 KB, and it made every
  // menu click cost a server round trip AND a full React teardown — ~50 components unmounted
  // and ~50 mounted, every time. Reported over and over, in escalating terms, ending at "it is
  // taking really really horrible time to load". Three separate attempts to buy the latency
  // back (keeping the outgoing area painted, hover prefetch, a client router cache) each fixed
  // part of it and none fixed the cost that matters, because the cost that matters is React
  // rebuilding the screen — and no amount of caching the payload avoids that.
  //
  // Before the split, all forty areas were in the DOM and `main[data-active=X]` revealed one
  // with CSS. A click did no fetching, no unmounting and no mounting. It was instant, and the
  // operator says plainly that it was fine.
  //
  // So it goes back. `shows`/`needs` stay as the single seam this decision lives behind: this
  // is two lines to flip, not a rewrite to undo, if the trade is ever worth revisiting.
  //
  // What it costs, stated honestly rather than discovered later: the first load is ~8 MB again
  // and takes several seconds, and every area's feeds are fetched whether or not you open it.
  // That is one slow load per session instead of a slow response to every click.
  //
  // AND THEN IT WENT LAZY, WITHOUT GIVING THE CLICK BACK. Both ends of this trade were reported
  // as unusable, and neither is necessary (see lib/area-loading.ts). A browser load renders the
  // requested area alone, so the first paint is one area's worth instead of 6-9 seconds. The
  // first time the operator opens another area, the shell adds it to a session cookie, shows a
  // real loading state and refreshes; from then on this renders every area the session has
  // opened, at the same tree positions, so the ones on screen update rather than remount and a
  // second click on them is the old pure CSS reveal.
  const [requestHeaders, requestCookies] = await Promise.all([headers(), cookies()]);
  const loadedAreas = areasToRender({
    requested: shownArea,
    cookie: requestCookies.get(LOADED_AREAS_COOKIE)?.value,
    fullDocument: isFullDocumentRequest((name) => requestHeaders.get(name)),
  });
  const shows = (area: string) => loadedAreas.has(area);
  const needs = (...areas: string[]) => areas.some((area) => loadedAreas.has(area));
  const requestedDecisionFilter = parseDecisionInboxFilter(
    getParam(resolvedSearchParams?.decision_filter),
  );
  const requestedWorkTask = getParam(resolvedSearchParams?.task);
  // WHO THE OPERATOR CLICKED "ASK" ON. Without this the Agents area answers as BOSS in
  // general, however specific the name on the card was.
  const requestedRole = getParam(resolvedSearchParams?.role);
  // WHICH DOSSIER, not the shelf of dossiers. The research step links here knowing its
  // discovery id, but the page kept the open dossier in local state alone, so every link
  // landed on the list and the operator had to go and find the run they had just clicked.
  // `run` as well as `discovery`, because that is the name the workflow step already sends.
  const requestedDiscovery =
    getParam(resolvedSearchParams?.discovery) ?? getParam(resolvedSearchParams?.run);
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
  // ACCEPT BOTH SPELLINGS.
  //
  // The console reads ?org=, but links and bookmarks carrying ?org_id= exist (one shipped from
  // the Ask BOSS panel). The failure is nasty out of proportion to the typo: the org resolves
  // to nothing, the operator is dropped on the picker mid-session, and OrgGate's stored-org
  // effect redirects back a second later — so it reads as the console randomly logging you out
  // and recovering. Treating org_id as an alias makes that class of link harmless.
  const requestedOrgId =
    getParam(resolvedSearchParams?.org) || getParam(resolvedSearchParams?.org_id);
  const [orgs, observability] = await Promise.all([fetchOrgs(), observabilityPromise]);
  const selectedOrg = requestedOrgId
    ? orgs.items.find((o) => o.org_id === requestedOrgId)
    : undefined;
  if (!selectedOrg) {
    // A transient API failure must NOT dump an in-session operator back to the org
    // picker (it destroys their console state mid-work). URL names an org + the org
    // list didn't load => hold the session and retry, don't re-gate.
    // AN EMPTY ORG LIST IS NOT NEWS THAT YOUR ORG IS GONE.
    //
    // fetchOrgs reports apiReachable:true whenever the call returns at all, so a degraded read
    // that yields zero orgs looks identical to a genuinely empty account. OrgGate then opens
    // the ONBOARDING form, and an operator working mid-session is shown "Onboard a new
    // organization" where their workspace used to be. That is the screenshot, and it reads as
    // data loss rather than as a blip.
    //
    // If the URL names an org and the list came back EMPTY, the list is wrong, not the URL:
    // hold the session and retry. A non-empty list that simply lacks this org is a real
    // mismatch and still falls through to the picker, so a bad link cannot retry forever.
    if (requestedOrgId && (!orgs.apiReachable || orgs.items.length === 0)) {
      return (
        <Suspense fallback={null}>
          <OrgReconnect
            orgId={requestedOrgId}
            reason={orgs.blockedReason ?? (orgs.items.length === 0 ? "the org list came back empty" : undefined)}
          />
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
  // FIRST SIGN-IN LANDS WHERE THE WORK IS. With no `area` asked for and setup not ready, the
  // console opens on Repo and models; once ready the default is the Command Center as before.
  // The same read feeds the rail badge, so the two cannot disagree.
  const setupSummary = await fetchSetupSummary(activeProjectId);
  const landing = landingArea(requestedArea, setupSummary);
  if (landing) {
    const next = searchParamsFrom(resolvedSearchParams);
    next.set("area", landing);
    redirect(`/?${next}`);
  }
  // LOAD ONLY WHAT THIS AREA NEEDS.
  //
  // Every area paid for every other: one render fetched ~70 feeds and shipped 8.38 MB in
  // 20-25s on a QUIET box, and the console's own fetches timing out is what produced several
  // "API unreachable" reports about a healthy API.
  //
  // These loaders were written in August and never wired in; the test asserting the wiring has
  // been red ever since. Substituted BY POSITION into the fan-out below rather than lifted out
  // of it: that array has 79 entries destructured positionally, and re-ordering it would
  // silently swap two feeds — a far worse failure than the one being fixed.
  // THE PACKS FOLLOW `needs`, NOT THE AREA THE SERVER WAS ASKED FOR.
  //
  // They were gated on `?area=` while every area renders and the rail switches client-side, so
  // the area you LANDED on decided what Govern and System Configuration could show for the whole
  // session. Seen live on boss-1: land on Skills and MCP, open Govern, "FinOps summary
  // unavailable"; land on Govern, open System Configuration, "MCP registry unavailable." while
  // Skills listed three servers. Data that was simply not requested rendered as a failure.
  // `needs` is the one seam that decides what a render fetches, so the packs ask it too.
  const budgetActive = needs(...BUDGET_AREA_IDS);
  const infraActive = needs(...INFRA_AREA_IDS);
  const assuranceActive = needs(...ASSURANCE_AREA_IDS);
  // ALWAYS FETCHED, deliberately.
  //
  // The sidebar switches area with history.pushState and NO server round-trip, so whichever
  // area the server happened to render for decides what data exists for the whole session.
  // Any conditional here means: land on Org Chart, click Workforce, and the panel says "Open
  // Workforce to load it" until you hard-reload. I tried gating on the requested area and then
  // on isAreaActive, and both left that hole open from a different starting point.
  //
  // These are two bounded single calls (~0.8s each, measured) running inside the same
  // Promise.all as the other ~30 feeds, so the cost is a rounding error against the render
  // they join. Correctness beats an optimisation nobody can see.
  // EVERYTHING BELOW NEEDS activeProjectId AND NOTHING ELSE, SO IT ALL STARTS NOW.
  //
  // Measured: 6.5 seconds to first byte, and the feeds themselves total only ~0.8 MB and under
  // a second each. The time was not fetch volume — it was EIGHT sequential `await Promise.all`
  // batches, each waiting for the slowest member of the one before it, when the only real
  // dependency in the chain is searchParams -> orgs -> activeProjectId -> everything.
  //
  // These are started here, together, and awaited where their values are used. The destructuring
  // below is unchanged; what changes is that the requests are in flight concurrently instead of
  // in four separate round trips. Nothing is reordered and nothing is dropped — a batch that
  // genuinely depends on an earlier result (costInsights, the assurance-derived feeds) still
  // waits for it.
  const workforceP = fetchWorkforce(activeProjectId);
  const orgChartP = fetchOrgChart(activeProjectId);
  const budgetP = loadBudgetAreaData({ projectId: activeProjectId, active: budgetActive });
  const infraP = loadInfraAreaData({ projectId: activeProjectId, active: infraActive });
  const assuranceP = loadAssuranceAreaData({ projectId: activeProjectId, active: assuranceActive });
  const projectsP = fetchProjectFeed(activeProjectId);
  // Behind the 66-feed wave for no reason: these need the project id and nothing that wave
  // produces, so they were a whole round trip spent waiting.
  const ccZone = (() => {
    const raw = requestCookies.get(TZ_COOKIE)?.value;
    try { return raw ? decodeURIComponent(raw) : "UTC"; } catch { return "UTC"; }
  })();
  const reliabilityWaveP = Promise.all([
    fetchReliabilityFeed(activeProjectId),
    fetchAiSreCapabilities(activeProjectId),
    // Was the delivery strip's feed. THIS WEEK reads the same numbers, relabelled honestly,
    // from the view below (command-center spec §4.12), so the position holds a settled value
    // rather than a second request.
    Promise.resolve(null),
    // The Command Center's ONE call (command-center spec §6.2), in this wave rather than after
    // it: an await on its own is a round trip every other feed waits behind. The zone is the
    // viewer's (the cookie the page writes), so the first paint's day is their day.
    fetchCommandCenterView(activeProjectId, ccZone, undefined, needs("overview")),
    // The Factory's apps for the hero's numbers and the shelf, in the same wave (cc v2).
    fetchCommandCenterApps(activeProjectId, needs("overview")),
  ] as const);

  const [workforceData, orgChartData] = await Promise.all([workforceP, orgChartP]);

  const [budgetArea, infraArea, assuranceArea] = await Promise.all([
    budgetP, infraP, assuranceP,
  ]);
  const assuranceFeed = assuranceArea.assuranceFeed;
  // The platform-ops feeds. They need the project id and `assuranceArea`, both of which exist
  // here — so waiting until after the 66-feed wave cost a third round trip for nothing.
  // Each of these 17 feeds is read by exactly one area's JSX (verified: every one has zero
  // uses outside a `shows(...)` block). `shows()` renders one area per request, so fetching
  // the other 16 is pure latency — and firing ~85 requests at once is what made a click cost
  // seconds: individually these endpoints answer in 0.2-0.8s, but they queue behind each
  // other in the API's worker pool. Each fetcher returns the same empty its unreachable path
  // already produced, so the positions below are unchanged and nothing is reordered.
  const platformOpsWaveP = Promise.all([
    fetchIdentityProvidersFeed(activeProjectId, needs("enterprise")),
    fetchIdentityRegistryFeed(activeProjectId, needs("enterprise")),
    fetchOncallSreStatus(activeProjectId, needs("reliability")),
    Promise.resolve(assuranceArea.securityScanners),
    Promise.resolve(assuranceArea.latestSecurityScan),
    fetchReleaseStatus(activeProjectId, needs("portfolio")),
    fetchReleaseBoard(activeProjectId, needs("portfolio")),
    fetchReleases(activeProjectId, needs("portfolio")),
    fetchWorkerAutoscalingStatus(activeProjectId, needs("infra")),
    fetchWorkerAutoscalingHistory(activeProjectId, 20, needs("infra")),
    fetchTokenScorecard(activeProjectId, undefined, needs("budget")),
    fetchTokenConsumption(activeProjectId, 24, needs("budget")),
    fetchTokenPolicies(activeProjectId, needs("budget")),
    fetchTokenRecommendations(activeProjectId, undefined, needs("budget")),
    fetchObservabilitySuiteFeed(activeProjectId, obsHost, needs("monitoring")),
    fetchKnowledgeGraphFeed(activeProjectId, needs("reliability")),
    fetchCICDCatalog(needs("delivery")),
    fetchCICDStatus(activeProjectId, needs("delivery")),
    fetchLiveJobsFeed(activeProjectId, 12, needs("work")),
    fetchQaScorecardFeed(activeProjectId, needs("reports")),
  ]);
  const projects = await projectsP;
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
    entitlements,
    routingLogs,
    skills,
    providerOps,
    infraFabric,
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
    qaBenchmarkApps,
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
    finopsSummary,
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
      fetchHealthSnapshotFeed(activeProjectId, needs("ops")),
      fetchOpsQueueFeed(activeProjectId, 8, needs("ops", "work")),
      fetchDeadletterFeed(activeProjectId),
      fetchPackFeed(activeProjectId),
      fetchRoleFeed(activeProjectId),
      fetchAgentModuleFeed(activeProjectId, needs("org")),
      fetchEntitlementFeed(activeProjectId),
      fetchRoutingLogFeed(activeProjectId, 8, {
        provider: routingProviderFilter,
        budget_state: routingBudgetFilter,
        data_boundary: routingBoundaryFilter,
        selected_only: routingOutcomeFilter === "selected" ? true : undefined,
        denied_only: routingOutcomeFilter === "denied" ? true : undefined,
      }, needs("infra")),
      fetchSkillFeed(activeProjectId),
      Promise.resolve(infraArea.providerOps),
      fetchInfraFabricFeed(activeProjectId, needs("infra")),
      // Twelve and thirty sub-requests respectively, both read only by data-area="github".
      fetchGitHubOperationsFeed(activeProjectId, needs("github")),
      fetchRepoActivity(activeProjectId, 30, needs("github")),
      fetchReceiptFeed(activeProjectId, 100),
      Promise.resolve(infraArea.infrastructureResources),
      Promise.resolve(budgetArea.budgetStatus),
      Promise.resolve(budgetArea.budgetIntensity),
      fetchLatestCanary(activeProjectId, needs("ops")),
      fetchPlans(activeProjectId, needs("work")),
      Promise.resolve(budgetArea.budgetAllocations),
      // Was the single biggest cost on this page, for eight numbers: it called
      // /v1/audit/export, which serialises the entire audit trail — 192 MB in 62 seconds on
      // this project — so the Ops screen could display eight counts. It now asks
      // /v1/audit/summary, which counts in SQL and answers in well under a second, so this is
      // an ordinary area gate like the rest.
      fetchAuditSummary(activeProjectId, needs("ops")),
      fetchChangeJournalFeed(activeProjectId, 8, needs("ops")),
      fetchSelfImprovementFeed(activeProjectId),
      fetchOutboxFeed(activeProjectId, 8, needs("comms")),
      fetchInboxFeed(activeProjectId),
      fetchMemberFeed(activeProjectId),
      fetchOrgMemberFeed(
        activeOrgId,
        activeProjectId,
        requestedActorId ?? "ceo",
        needs("org"),
      ),
      fetchMetatronQuery(
        metatronQuestion,
        metatronTaskId,
        metatronDecisionId,
        activeProjectId,
      ),
      fetchAuthSession(),
      fetchBreakGlassGrantFeed(activeProjectId, needs("org")),
      fetchReportSummary(activeProjectId, needs("infra", "ops", "reports")),
      fetchAgentRuns(activeProjectId, needs("work")),
      Promise.resolve(infraArea.mcpServers),
      fetchTaskOutputs(activeProjectId, needs("work")),
      fetchCapabilities(activeProjectId),
      fetchMeetingRooms(activeProjectId, needs("code_tasks", "meetings")),
      fetchApprovalRules(activeProjectId, needs("decisions")),
      fetchCodeTasksFeed(
        activeProjectId,
        requestedArea,
        needs("activity", "code_tasks", "reports"),
      ),
      fetchDiscoveriesFeed(activeProjectId, needs("discovery")),
      fetchPreviews(activeProjectId, needs("code_tasks")),
      fetchQaBenchmarkApps(activeProjectId, needs("code_tasks")),
      fetchSandboxFeed(needs("sandboxes")),
      fetchPipelineData(activeProjectId, needs("pipeline")),
      fetchDashboard(activeProjectId, needs("dashboard", "ops")),
      fetchPolicyEvals(activeProjectId, 25, needs("ops")),
      // Already self-gating: returns null unless the URL carries a run to trace.
      fetchRunTrace(traceRunId),
      fetchLatestEval(activeProjectId, needs("ops")),
      fetchSkillInvocations(activeProjectId, 15, needs("ops")),
      fetchAckState(activeProjectId, 25, false, needs("comms")),
      fetchWipStatus(activeProjectId, 5, needs("code_tasks", "pipeline")),
      fetchChaosRuns(activeProjectId, 15, needs("ops")),
      Promise.resolve(budgetArea.commitmentRecons),
      Promise.resolve(budgetArea.finopsSummary),
      fetchWorkflowFeed(activeProjectId, needs("ops")),
      // The Agents area reads it: Profile on an agent, Panels, and orphan definitions offered
      // as templates in New agent.
      fetchAgentStudioFeed(activeProjectId, needs("workforce")),
      fetchWorkOrchestrationFeed(activeProjectId, needs("portfolio")),
    ]);
  // These were two sequential awaits in the middle of an otherwise parallel render, so the
  // page paid for them one after the other: reliability ~0.8s THEN ai-sre ~4.5s. Nothing
  // depends on the first, and /v1/ai-sre/capabilities is the slowest endpoint the console
  // calls, so serialising it was pure waste on every single render.
  const [reliabilityFeed, aiSreFeed, , commandCenter, commandCenterApps] = await reliabilityWaveP;
  const [costInsights, llmSpend] = await Promise.all([
    Promise.resolve(budgetArea.costInsights),
    Promise.resolve(budgetArea.llmSpend),
  ]);
  // Platform ops panels nested into existing Mission Control areas (no new nav).
  const [
    identityProvidersFeed,
    identityRegistry,
    oncallSreStatus,
    securityScanners,
    latestSecurityScan,
    releaseStatus,
    releaseBoard,
    releasePlans,
    workerAutoscalingStatus,
    workerAutoscalingHistory,
    tokenScorecard,
    tokenConsumption,
    tokenPolicies,
    tokenRecommendations,
    observabilitySuiteFeed,
    knowledgeGraphFeed,
    cicdCatalog,
    cicdStatus,
    liveJobs,
    qaScorecard,
  ] = await platformOpsWaveP;


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
  const editionId = "community" as const;
  return (
    <ConsoleShell
      status={{
        apiReachable: status.apiReachable,
        budgetState: status.budgetState,
        deadletters: deadletterCount,
        pendingDecisions: status.pendingDecisions,
        setup: setupSummary?.badge ?? null,
        // Read here, on the server, because this is the URL the page's fetches actually use.
        controlPlane: API_URL,
      }}
      scope={{
        project: activeProject?.name ?? activeProjectId,
        actor: activeActor?.displayName ?? activeActorId,
        // The header shows names; anything that CALLS the API needs ids. The agent dock was
        // handed the display name ("BOSS dogfood bootstrap") and every request came back
        // "project not found" — a label and an identifier are not interchangeable just
        // because both are strings.
        projectId: activeProjectId,
        actorId: activeActorId,
      }}
      org={{ id: activeOrgId, name: activeOrgName }}
      editionId={editionId}
      initialArea={requestedArea}
      // Sign out only means something when the console has a password; without one the button
      // led to a sign-in page that could only say authentication is not configured.
      signOutAvailable={consoleAuthConfig() !== null}
      loadedAreas={[...loadedAreas]}
    >
      <div className="frame">
        {/* ONE AGENT SURFACE.
            There were three: this chat area, a Metatron Captain page, and the Task Shepherd
            autopilot. The operator used one of them and did not like it — "I never used
            Metatron and Agent chat is not good" — while praising the SAME agent everywhere
            else, because those screens hand it their facts before the first word. So the chat
            keeps its component and gains the whole factory in front of it; Metatron's page is
            gone (its `metatron_sessions` table does not exist in this database, so it was a
            shell); and the Shepherd's taxonomy survives as the diagnosis on each row, offered
            as a button rather than run unattended. */}
        {shows("agent") ? (
        <section className="panel-stack" data-area="agent" aria-label="BOSS agent">
          <BossAgentHome projectId={activeProjectId} actor={activeActorId}
                         initialTaskId={requestedWorkTask || undefined}
                         roleName={requestedRole || undefined} />
          {/* WHAT BOSS KNOWS — lessons and rules it keeps between sessions — beside the agent
              that uses them (skills-mcp spec D-1; it used to sit inside Skills and MCP). */}
          <MemoryHome projectId={activeProjectId} actor={activeActorId} />
        </section>
        ) : null}
        {shows("dashboard") ? (
        <section className="panel-stack" data-area="dashboard" aria-label="Live dashboard">
          <div className="section-head">
            <div>
              <span className="eyebrow">Live Dashboard</span>
              <h2>Cross-phase control snapshot</h2>
            </div>
          </div>
          <DashboardPanel feed={dashboardSnapshot} />
        </section>
        ) : null}

        {shows("assurance") ? (
        <section className="panel-stack" data-area="assurance" aria-label="Assurance Center">
          <AssuranceCenter feed={assuranceFeed} />
        </section>
        ) : null}

        {shows("assurance") ? (
        <section className="panel-stack" data-area="assurance" aria-label="Security scanning">
          <div className="section-head">
            <div>
              <span className="eyebrow">Security scan</span>
              <h2>OSS vulnerability scanning with a governed go/no-go gate.</h2>
            </div>
            <p className="status-meta">
              Semgrep · Trivy · OSV · Gitleaks — receipted runs for project{" "}
              {activeProjectId}.
            </p>
          </div>
          <SecurityScanCard
            projectId={activeProjectId}
            initialRun={latestSecurityScan}
            scanners={securityScanners}
          />
        </section>
        ) : null}

        {/* THESE RENDER THEIR OWN `<section data-area>`, so the gate has to go on the mount.
            Missed on the first pass and caught by measuring: a Discovery render still carried a
            GitHub panel and a Govern panel, because the marker the CSS keys on lives inside the
            component rather than in this file. */}
        {shows("github") ? <RepoActivityPanel repos={repoActivity} /> : null}

        {shows("github") ? (
          <GitHubOperations
            feed={githubOperations}
            actor={activeActorId}
            canManage={canManageGitHub}
          />
        ) : null}

        {shows("org") ? (
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
        ) : null}

        {shows("portfolio") ? (
        <section className="panel-stack" data-area="portfolio" aria-label="Work allocation">
          <WorkOrchestration
            feed={workOrchestrationFeed}
            actor={activeActorId}
          />
        </section>
        ) : null}

        {shows("portfolio") ? (
        <section className="panel-stack" data-area="portfolio" aria-label="Release planning">
          <div className="section-head">
            <div>
              <span className="eyebrow">Release planning</span>
              <h2>Built / building / planned / blocked — release board and plans.</h2>
            </div>
            <p className="status-meta">
              From BOSS&apos;s release board and plans for project {activeProjectId}.
            </p>
          </div>
          <ReleasePlanningPanel
            snapshot={releaseStatus}
            board={releaseBoard}
            plans={releasePlans}
          />
        </section>
        ) : null}

        {/* THE BRIDGE (command-center spec §4): one section, one read. Ask BOSS is still FIRST
            and still the agent that can act — the old entry point was a Metatron "Status Query"
            form far below the fold that could only answer — laid out as one line until used.
            The delivery strip is THIS WEEK now, counting workflow runs (it counted code tasks
            beside tiles that counted runs), and the help card is a `?` on the status line,
            inline before the first run. See command-center-ui.tsx. */}
        {shows("overview") ? (
        <section className="ov-cc" data-area="overview" aria-label="Command center">
          <CommandCenter key={`${activeOrgId}:${activeProjectId}`} projectId={activeProjectId}
            orgId={activeOrgId} actor={activeActorId} viewer={activeActorId}
            initial={null} initialWire={commandCenter.wire} initialError={commandCenter.error} tz={ccZone}
            initialApps={commandCenterApps}
            composer={
              <AskBossHero projectId={activeProjectId} actor={activeActorId}
                orgId={activeOrgId} deploymentAvailable={false} layout="hero"
                ideasInline={commandCenter.everRan === false} />
            }
            help={<OnboardingHelpCard />} />
        </section>
        ) : null}

        {/* THE DIAGNOSTIC GRID IS GONE, DELIBERATELY.
            Eleven tiles: Project, Database "Connected", Auth Session "Ceo", Budget State,
            Pending Decisions, Active Jobs, Open Tasks, Blocking State "Clear", Artifacts,
            Events, Deadletters. Almost all of it answered a question an operator never asks —
            whether Postgres is reachable and which auth method the console used are things
            they find out by the console working at all. Three were already duplicated by the
            KPI row above (pending decisions, open tasks, active jobs).

            The one line worth keeping was Deadletters, because a non-zero count is work that
            FAILED and is sitting unseen — so it survives as an alert that only appears when
            there is something to act on, rather than as a tile reading 0 forever. */}
        {/* The deadletter alert is gone too. "25 items failed and are waiting" floating
            above the fold says a number and nothing else — not what failed, not when, not
            whether it matters now. A count with no context is an anxiety generator, not a
            signal; failures belong next to the work they came from, which is Monitoring. */}

        {shows("org") ? (
        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="org">Org Delegation</span>
              <h2>Organization-wide actors with scoped project authorization.</h2>
            </div>
            <p className="status-meta">
              Delegated actors are explicit, scoped to the projects they may act on,
              and every change to them is recorded in the audit log.
            </p>
          </div>

          <div className="stack-grid">
            <article className="stack-card">
              <h3>Add or update org delegate</h3>
              <ActionForm action={upsertOrgMemberAction} className="composer compact">
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
                <SubmitButton className="button secondary" type="submit">
                  Save org delegate
                </SubmitButton>
              </ActionForm>
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
        ) : null}

        {shows("org") ? (
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
              <ActionForm action={createBreakGlassGrantAction} className="composer compact">
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
                <SubmitButton className="button secondary" type="submit">
                  Grant break-glass access
                </SubmitButton>
              </ActionForm>
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
                        <ActionForm
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
                          <SubmitButton className="button secondary" type="submit">
                            Revoke
                          </SubmitButton>
                        </ActionForm>
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
        ) : null}

        {shows("skills") ? (
        <section className="panel-stack" data-area="skills" aria-label="Skills & MCP">
          {/* WHAT YOUR CREW CAN DO (skills-mcp spec §5): the Kit of abilities, Who can, and the
              Library of every skill and MCP server. Email, tickets, team chat, web search and
              YouTube are connected here; the GitHub repo stays in Setup and the Kit links to it. */}
          <KitHome projectId={activeProjectId} actor={activeActorId} />
        </section>
        ) : null}

        {/* REPO AND MODELS: where BOSS writes code and what it thinks with — one board over
            one server view (app/setup/setup-board.tsx). Outgoing email lives in Skills and MCP. */}
        {shows("setup") ? (
        <section className="panel-stack" data-area="setup" aria-label="Repo and models">
          <SetupBoard projectId={activeProjectId} actor={activeActorId} />
        </section>
        ) : null}

        {shows("budget") ? (
        <section className="panel-stack" data-area="budget" aria-label="Govern">
          <div className="panel">
            <span className="eyebrow" data-area="budget">Govern</span>
            <h2>How hard BOSS runs</h2>
            <p className="detail">
              These lived in environment variables, so changing one meant an ops action and a
              restart. Each says where its value comes from; a project value overrides the
              deployment default.
            </p>
            {/* Numbers only. Which model fills which role is asked in Setup, next to the
                step that connects one — not here beside concurrency and spend. */}
            <SettingsKnobs projectId={activeProjectId} actor={activeActorId} only="number" />
          </div>
        </section>
        ) : null}
        {shows("budget") ? (
        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="budget">Budget Control Center</span>
              <h2>Live spend, forecast, and cap pressure.</h2>
            </div>
            <p className="status-meta">
              From the budget ledger for project {activeProjectId}: reservations, actual spend and the cap.
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
                    <h2>Spent {periodWords(budgetStatus.period).when}</h2>
                    <div className="metric">
                      {formatMicros(budgetStatus.actualUsdMicros)}
                    </div>
                    <p className="detail" data-testid="budget-period">
                      Money settled {periodWords(budgetStatus.period).when}. The{" "}
                      {periodWords(budgetStatus.period).cap} resets at{" "}
                      {budgetStatus.resetsAtLabel}.
                    </p>
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
                      {periodWords(budgetStatus.period).whose}{" "}
                      {formatMicros(budgetStatus.capUsdMicros)} with{" "}
                      {Math.round(budgetStatus.utilizationRatio * 100)}%
                      utilization. It resets at {budgetStatus.resetsAtLabel}.
                    </p>
                    {/* Each cap measures its own period; the one that does not bind is shown
                        too, so a nearly-spent month is not hidden behind a quiet day. */}
                    {budgetStatus.periods
                      .filter((p) => p.period !== budgetStatus.period)
                      .map((p) => (
                        <p className="detail" key={p.period} data-testid={`budget-period-${p.period}`}>
                          {periodWords(p.period).whose[0].toUpperCase()}
                          {periodWords(p.period).whose.slice(1)} budget:{" "}
                          {formatMicros(p.committedUsdMicros)} of{" "}
                          {formatMicros(p.capUsdMicros)} (
                          {Math.round(p.utilizationRatio * 100)}%), resets at {p.resetsAtLabel}.
                        </p>
                      ))}
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
                    {/* NOT MONEY, NEVER DROPPED. What subscription calls would have cost at
                        API list price: shown, and counted against no cap (included_spend.py). */}
                    {budgetStatus.includedInSubscriptionUsdMicros > 0 ? (
                      <p className="detail" data-testid="budget-included-in-subscription">
                        Included in your subscription:{" "}
                        {formatMicros(budgetStatus.includedInSubscriptionUsdMicros)} at API list
                        price — not spent, and not counted against the cap.
                      </p>
                    ) : null}
                  </div>
                </div>
              </>
            )}
          </article>
        </section>
        ) : null}

        {shows("budget") ? (
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
        ) : null}

        {shows("budget") ? (
          <TokenEfficiencyPanel
            scorecard={tokenScorecard}
            consumption={tokenConsumption}
            policies={tokenPolicies}
            recommendations={tokenRecommendations}
          />
        ) : null}

        {shows("budget") ? (
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
            <FinopsCard
              projectId={activeProjectId}
              reconciliations={commitmentRecons}
              summary={finopsSummary}
            />
          </div>
        </section>
        ) : null}

        {shows("budget") ? (
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
        ) : null}

        {shows("infra") ? (
        <section className="panel-stack" data-area="infra" aria-label="MCP servers">
          <div className="section-head">
            <div>
              <span className="eyebrow">MCP Servers</span>
              <h2>Registered tool backends the agent can call — governed by policy, budget, and receipts.</h2>
            </div>
          </div>
          {/* THE SHARED MCP PANEL, in summary: status from the same registry every screen
              reads, and a link to where servers are managed. This said "Register one via POST
              /v1/mcp/servers" — an API call, on an operator's screen — and showed the three
              seeded servers as enabled while none of them answered. */}
          {/* ONE HOME FOR MCP SERVERS: Skills & MCP › Library. This is a link to it. */}
          {mcpServers.apiReachable ? (
            <a className="kit-link" href="?area=skills&tab=library&lib=mcp" data-testid="infra-mcp-link">
              MCP servers · {(mcpServers.items as Array<{ enabled?: boolean; reachable?: boolean | null;
                                                         trusted?: boolean }>)
                .filter((s) => s.enabled !== false && s.reachable === true && s.trusted).length} ready ↗
            </a>
          ) : (
            <p className="detail">{mcpServers.blockedReason ?? "MCP registry unavailable."}</p>
          )}
        </section>
        ) : null}

        {shows("infra") ? (
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

          <InfraFabricPanel feed={infraFabric} />

          <WorkerAutoscalingPanel
            feed={workerAutoscalingStatus}
            history={workerAutoscalingHistory}
          />

          {providerOps.apiReachable ? (
            <>
              <div className="stack-grid">
                <article className="stack-card" id="provider-resources">
                  <h3>Resource Explorer</h3>
                  <div className="button-stack">
                    <ActionForm action={syncProviderResourcesAction}>
                      <input type="hidden" name="project_id" value={activeProjectId} />
                      <input type="hidden" name="actor" value={activeActorId} />
                      <SubmitButton className="button secondary" type="submit">
                        Sync all providers
                      </SubmitButton>
                    </ActionForm>
                    <ActionForm action={runResourceJanitorAction} className="composer compact">
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
                      <SubmitButton className="button secondary" type="submit">
                        Run Resource Janitor
                      </SubmitButton>
                      <p className="detail">
                        Creates a report artifact and approval-required cleanup
                        ChangeSets. It never deletes production resources or calls
                        providers directly.
                      </p>
                    </ActionForm>
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
                  <ActionForm action={upsertWorkerPoolAction} className="composer compact">
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
                    <SubmitButton className="button secondary" type="submit">
                      Register worker pool
                    </SubmitButton>
                  </ActionForm>

                  <ActionForm action={runAutoscalerPlanAction} className="composer compact">
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
                    <SubmitButton className="button secondary" type="submit">
                      Run autoscaler review
                    </SubmitButton>
                    <p className="detail">
                      Local pool desired capacity may change immediately. Cloud
                      scale-up is held as a BudgetReservation-backed ChangeSet for
                      approval.
                    </p>
                  </ActionForm>

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
                  <ActionForm action={createChangeSetAction} className="composer compact">
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
                    <SubmitButton className="button secondary" type="submit">
                      Create ChangeSet
                    </SubmitButton>
                  </ActionForm>

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
                              <ActionForm action={approveChangeSetAction}>
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
                                <SubmitButton className="button secondary" type="submit">
                                  Approve
                                </SubmitButton>
                              </ActionForm>
                            ) : null}
                            {changeSet.status === "approved" ? (
                              <ActionForm action={applyChangeSetAction}>
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
                                <SubmitButton className="button secondary" type="submit">
                                  Apply
                                </SubmitButton>
                              </ActionForm>
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
                  <ActionForm
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
                    <SubmitButton className="button secondary" type="submit">
                      Save environment policy
                    </SubmitButton>
                    <p className="detail">
                      Provider-backed staging and production runs stay blocked
                      until an active environment policy allows the runtime mode
                      and required gates.
                    </p>
                  </ActionForm>

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

                  <ActionForm
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
                          placeholder="owner/repository"
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
                    <SubmitButton className="button secondary" type="submit">
                      Register deployment target
                    </SubmitButton>
                  </ActionForm>

                  <ActionForm action={createDeploymentRunAction} className="composer compact">
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
                    <SubmitButton className="button secondary" type="submit">
                      Plan deployment run
                    </SubmitButton>
                    <p className="detail">
                      Provider deployments are modeled as ChangeSets. Health checks
                      run only after the ChangeSet is applied, or immediately for
                      local targets with no provider write.
                    </p>
                  </ActionForm>

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
                            <ActionForm action={runDeploymentHealthCheckAction}>
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
                              <SubmitButton className="button secondary" type="submit">
                                Run health checks
                              </SubmitButton>
                            </ActionForm>
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
                          <p className="detail">
                            Billing {health.billingStatus ?? "unknown"} /
                            confidence {health.billingConfidence ?? "unknown"}
                            {health.billingMessage
                              ? ` — ${health.billingMessage}`
                              : ""}
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
                    {providerOps.resourceReceipts.length === 0 ? (
                      <p className="detail">
                        No resource receipts yet. Receipts prove provider
                        actions; they are not invoices and do not imply $0.00
                        spend.
                      </p>
                    ) : null}
                    {providerOps.providerHealth.length === 0 &&
                    providerOps.driftReports.length === 0 &&
                    providerOps.resourceReceipts.length === 0 ? (
                      <p className="detail">
                        No ProviderOps health or drift records yet.
                      </p>
                    ) : null}
                  </div>
                </article>

                <article className="stack-card" id="provider-finops">
                  <h3>Cost and Reconciliation Ledger</h3>
                  {(() => {
                    const honesty = describeSpendHonesty({
                      costs: providerOps.costs,
                      health: providerOps.providerHealth,
                      runs: providerOps.reconciliationRuns,
                    });
                    return (
                      <p className="detail" data-testid="provider-ledger-honesty">
                        {honesty.label}
                        {honesty.amountUsdMicros == null
                          ? " — "
                          : ` ${formatMicros(honesty.amountUsdMicros)} — `}
                        {honesty.helper}
                      </p>
                    );
                  })()}
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
                            {cost.confidence === "unknown"
                              ? "amount unknown"
                              : formatMicros(cost.amountUsdMicros)}{" "}
                            / {cost.costLayer} / {cost.confidence}
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
                            {run.resourcesChanged} changed / {run.costsSeen} cost
                            events / billing {run.billingStatus ?? "unknown"}
                          </p>
                          <p className="detail">
                            Started by {run.startedBy} at{" "}
                            {formatDateTime(run.startedAt)}
                          </p>
                          {run.billingMessage ? (
                            <p className="detail">{run.billingMessage}</p>
                          ) : null}
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
                        No provider cost or reconciliation records yet. Empty is
                        not $0.00.
                      </p>
                    ) : null}
                  </div>
                </article>
              </div>
            </>
          ) : null}
        </section>
        ) : null}

        {shows("infra") ? (
        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="infra">Infrastructure Resources</span>
              <h2>Receipt-backed droplets with TTL and janitor visibility.</h2>
            </div>
            <p className="status-meta">
              From BOSS&apos;s resource ledger for project {activeProjectId} — every row has a receipt.
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
        ) : null}

        {shows("ops") ? (
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
        ) : null}

        {shows("ops") ? (
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
        ) : null}

        {shows("ops") ? (
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
              <ActionForm action={refreshHealthSnapshotAction}>
                <input type="hidden" name="project_id" value={activeProjectId} />
                <input type="hidden" name="actor" value={activeActorId} />
                <SubmitButton className="button secondary" type="submit">
                  Refresh snapshot
                </SubmitButton>
              </ActionForm>
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
                  <ActionForm action={createOpsDigestAction}>
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <input type="hidden" name="limit" value={25} />
                    <SubmitButton className="button secondary" type="submit">
                      Create ops digest
                    </SubmitButton>
                  </ActionForm>
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
        ) : null}

        {shows("ops") ? (
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
        ) : null}

        {shows("ops") ? (
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
              providerHealth={dashboardSnapshot.snapshot?.providerHealth}
            />
            <SkillInvokeCard projectId={activeProjectId} invocations={skillInvocations} />
          </div>
        </section>
        ) : null}

        {shows("ops") ? (
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
        ) : null}

        {shows("ops") ? (
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
                  <ActionForm action={createReplayBundleAction}>
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <SubmitButton className="button secondary" type="submit">
                      Create replay bundle
                    </SubmitButton>
                  </ActionForm>
                  <ActionForm action={createComplianceEvidenceBundleAction}>
                    <input type="hidden" name="project_id" value={activeProjectId} />
                    <input type="hidden" name="actor" value={activeActorId} />
                    <SubmitButton className="button secondary" type="submit">
                      Create compliance evidence
                    </SubmitButton>
                  </ActionForm>
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
        ) : null}

        {shows("ops") ? (
        <section className="panel-stack" data-area="ops" aria-label="Improvement programs">
          <ImprovementProgramsPanel
            programs={selfImprovement.improvementPrograms}
            programsApiReachable={selfImprovement.programsApiReachable}
            programsBlockedReason={selfImprovement.programsBlockedReason}
            projectId={activeProjectId}
            actor={activeActorId}
          />
        </section>
        ) : null}

        {shows("comms") ? (
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
              <ActionForm action={submitOutboxDraftAction} className="composer compact">
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
                <SubmitButton className="button secondary" type="submit">
                  Create outbox draft
                </SubmitButton>
              </ActionForm>
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

                  <ActionForm
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
                    <SubmitButton className="button secondary" type="submit">
                      Save channel policy
                    </SubmitButton>
                  </ActionForm>
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
                          {message.status === "dry_run"
                            ? "dry-run — no provider send"
                            : (message.receiptId ?? message.body)}
                        </p>
                      </div>
                      {message.status === "draft" || message.status === "dry_run" ? (
                        <div>
                          {message.status === "dry_run" ? (
                            <p className="detail">dry-run — no provider send</p>
                          ) : null}
                          <ActionForm action={dryRunOutboxMessageAction}>
                            <input type="hidden" name="message_id" value={message.id} />
                            <input type="hidden" name="project_id" value={activeProjectId} />
                            <input type="hidden" name="actor" value={activeActorId} />
                            <SubmitButton className="button secondary" type="submit">
                              Dry-run deliver
                            </SubmitButton>
                          </ActionForm>
                          <ActionForm action={queueOutboxMessageAction}>
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
                            <SubmitButton className="button secondary" type="submit">
                              Queue send
                            </SubmitButton>
                          </ActionForm>
                        </div>
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
        ) : null}

        {shows("org") ? (
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
                        <div>
                          <span className="pill">installed</span>
                          <PackExportButton
                            projectId={activeProjectId}
                            packId={pack.packId}
                            actor={activeActorId}
                          />
                        </div>
                      ) : (
                        <ActionForm action={installPackAction}>
                          <input type="hidden" name="pack_id" value={pack.packId} />
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <SubmitButton className="button secondary" type="submit">
                            Install
                          </SubmitButton>
                        </ActionForm>
                      )}
                    </div>
                  ))}
                </div>
              )}
              <PackImportForm projectId={activeProjectId} actor={activeActorId} />
            </article>

            <article className="stack-card">
              <h3>Skill YAML</h3>
              {/* ONE HOME: importing and exporting skills is Skills & MCP › Library › ⋯. */}
              <a className="kit-link" href="?area=skills&tab=library" data-testid="org-skills-link">
                Import or export in Skills &amp; MCP ↗
              </a>
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
        ) : null}

        {shows("enterprise") ? (
        <section
          className="panel-stack"
          data-area="enterprise"
          aria-label="Identity providers"
          data-testid="sso-identity-section"
          data-community-included="true"
        >
          <div className="section-head">
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span className="eyebrow">Identity providers</span>
              <CommunityIncludedBadge label="SSO · Community" />
            </div>
            <h2>Identity providers BOSS can record — and what sign-in uses today.</h2>
            <p className="status-meta">
              Provider records for project {activeProjectId}. Secrets are referenced by name,
              never shown. Included in Community.
            </p>
          </div>
          <IdentityProvidersCatalogView
            feed={identityProvidersFeed}
            projectId={activeProjectId}
          />
        </section>
        ) : null}

        {shows("enterprise") ? (
        <section className="panel-stack" data-area="enterprise" aria-label="Self-hosted SSO management">
          <div className="stack-grid">
            <article
              className="stack-card"
              data-testid="sso-enterprise-identity-card"
              data-community-included="true"
            >
              <SurfaceHeading
                title="Self-hosted identity providers"
                editionId={editionId}
                communityIncluded
              />
              <p className="detail" style={{ marginTop: 8 }}>
                Self-hosted OIDC, SAML, and SCIM IdP registry — included in Community.
              </p>
              {!identityRegistry.apiReachable ? (
                <p className="detail">
                  {identityRegistry.blockedReason ?? "Identity provider API is unavailable."}
                </p>
              ) : (
                <>
                  {identityRegistry.identityProviders.length === 0 ? (
                    <p className="detail">
                      No OIDC, SAML, or SCIM identity provider metadata has been
                      registered.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {identityRegistry.identityProviders.slice(0, 6).map((provider) => (
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

                  <ActionForm
                    action={upsertEnterpriseIdentityProviderAction}
                    className="composer"
                    id="identity-provider-form"
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
                    <SubmitButton className="button secondary" type="submit">
                      Save identity provider
                    </SubmitButton>
                  </ActionForm>
                </>
              )}
            </article>

            <article
              className="stack-card"
              data-testid="sso-identity-rollout-card"
              data-community-included="true"
            >
              <SurfaceHeading
                title="Identity rollout policies"
                editionId={editionId}
                communityIncluded
              />
              {!identityRegistry.apiReachable ? (
                <p className="detail">
                  {identityRegistry.blockedReason ?? "Enterprise API is unavailable."}
                </p>
              ) : (
                <>
                  {identityRegistry.identityRolloutPolicies.length === 0 ? (
                    <p className="detail">
                      No identity rollout policy is configured. SAML login and SCIM
                      runtime provisioning stay blocked until a policy is approved.
                    </p>
                  ) : (
                    <div className="feed-list">
                      {identityRegistry.identityRolloutPolicies.map((policy) => (
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

                  <ActionForm
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
                    <SubmitButton className="button secondary" type="submit">
                      Save rollout policy
                    </SubmitButton>
                  </ActionForm>
                </>
              )}
            </article>

            <article
              className="stack-card"
              data-testid="sso-scim-card"
              data-community-included="true"
            >
              <SurfaceHeading
                title="SCIM provisioning"
                editionId={editionId}
                communityIncluded
              />
              {!identityRegistry.apiReachable ? (
                <p className="detail">
                  {identityRegistry.blockedReason ?? "Enterprise API is unavailable."}
                </p>
              ) : (
                <>
                  <div className="feed-list">
                    {identityRegistry.scimGroups.length === 0 ? (
                      <p className="detail">No SCIM groups have been provisioned.</p>
                    ) : (
                      identityRegistry.scimGroups.slice(0, 4).map((group) => (
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
                    {identityRegistry.scimUsers.length === 0 ? (
                      <p className="detail">No SCIM users have been provisioned.</p>
                    ) : (
                      identityRegistry.scimUsers.slice(0, 4).map((user) => (
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

                  <ActionForm action={upsertEnterpriseScimGroupAction} className="composer">
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
                    <SubmitButton className="button secondary" type="submit">
                      Upsert SCIM group
                    </SubmitButton>
                  </ActionForm>

                  <ActionForm action={upsertEnterpriseScimUserAction} className="composer">
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
                    <SubmitButton className="button secondary" type="submit">
                      Upsert SCIM user
                    </SubmitButton>
                  </ActionForm>
                </>
              )}
            </article>
          </div>
        </section>
        ) : null}

        {shows("org") ? (
        <section className="panel-stack" data-area="org" aria-label="Agent marketplace">
          <div className="section-head">
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span className="eyebrow" data-area="org">Agent Marketplace</span>
            </div>
            <h2>Packaged worker modules with contained prompts, skills, and runtime policy.</h2>
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
                        <ActionForm action={uninstallAgentModuleAction}>
                          <input type="hidden" name="module_id" value={module.moduleId} />
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <input
                            type="hidden"
                            name="reason"
                            value="Disabled from Mission Control marketplace."
                          />
                          <SubmitButton className="button ghost" type="submit">
                            Uninstall
                          </SubmitButton>
                        </ActionForm>
                        <ActionForm action={rollbackAgentModuleAction}>
                          <input type="hidden" name="module_id" value={module.moduleId} />
                          <input type="hidden" name="project_id" value={activeProjectId} />
                          <input type="hidden" name="actor" value={activeActorId} />
                          <input
                            type="hidden"
                            name="reason"
                            value="Rollback requested from Mission Control marketplace."
                          />
                          <SubmitButton className="button secondary" type="submit">
                            Roll back
                          </SubmitButton>
                        </ActionForm>
                      </div>
                    ) : (
                      <ActionForm action={installAgentModuleAction}>
                        <input type="hidden" name="module_id" value={module.moduleId} />
                        <input type="hidden" name="project_id" value={activeProjectId} />
                        <input type="hidden" name="actor" value={activeActorId} />
                        <SubmitButton className="button secondary" type="submit">
                          Install module
                        </SubmitButton>
                      </ActionForm>
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
        ) : null}

        {shows("infra") ? (
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
                        {["api_key", "gateway_token"].includes(
                          source.authMethod,
                        ) ? (
                          <ActionForm
                            action={uploadModelCredentialAction}
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
                            <label className="field">
                              <span>
                                {source.credentialPresent
                                  ? "Replace configured credential"
                                  : "API or service credential"}
                              </span>
                              <input
                                className="input"
                                type="password"
                                name="credential_value"
                                autoComplete="new-password"
                                placeholder="Write-only; never shown again"
                                required
                              />
                            </label>
                            <label className="field">
                              <span>Secret backend</span>
                              <select
                                className="input"
                                name="backend"
                                defaultValue="platform_secret_store"
                              >
                                <option value="platform_secret_store">
                                  Platform secret store
                                </option>
                                <option value="vault">Vault</option>
                                <option value="local_runner">Local runner</option>
                              </select>
                            </label>
                            <p className="detail">
                              {source.multiTenantAllowed
                                ? "Governed project capacity; jobs receive leases, never this value."
                                : "Owner/single-tenant capacity; this value is never pooled."}
                            </p>
                            <SubmitButton className="button secondary" type="submit">
                              {source.credentialPresent
                                ? "Rotate credential"
                                : "Store credential"}
                            </SubmitButton>
                          </ActionForm>
                        ) : null}
                        {source.authMethod.includes("oauth") ? (
                          source.connectionStatus === "connected" ? (
                            <ActionForm
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
                              <SubmitButton className="button secondary" type="submit">
                                Disconnect session
                              </SubmitButton>
                            </ActionForm>
                          ) : (
                            <div className="feed-list">
                              <ActionForm
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
                                <SubmitButton className="button secondary" type="submit">
                                  Start official session
                                </SubmitButton>
                              </ActionForm>

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
                                        <ActionForm
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
                                          <SubmitButton
                                            className="button secondary"
                                            type="submit"
                                          >
                                            Complete session
                                          </SubmitButton>
                                        </ActionForm>
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
        ) : null}

        {shows("reports") ? (
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
              const tests = structuredCodeTaskTests(st);
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
                        ) : st.files_changed != null ? (
                          // Not "—" beside "Files changed · 53": the files are known, the
                          // line counts are what was not recorded (console walk, 2026-10-02).
                          `${st.files_changed} files`
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
                      <code>
                        {st.summary
                          ?? (st.files_changed
                            ? `${st.files_changed} files changed; this run did not record their paths — the pull request lists them.`
                            : "No file list recorded for this run.")}
                      </code>
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
          <QaScorecardIsland feed={qaScorecard} />
          <ReportsView report={report} />
        </section>
        ) : null}

        {shows("capabilities") ? (
        <section className="panel-stack" data-area="capabilities" aria-label="Capabilities">
          <div className="section-head">
            <div>
              <span className="eyebrow">Capabilities</span>
              <h2>Agent tools &amp; MCP servers — discover, audit, extend.</h2>
            </div>
          </div>
          <CapabilitiesView
            projectId={activeProjectId}
            actor={activeActorId}
            nativeTools={capabilities.nativeTools}
            skills={capabilities.skills}
            mcpServers={capabilities.mcpServers}
            apiReachable={capabilities.apiReachable}
            engineLabel={capabilities.engineLabel}
            engineModel={capabilities.engineModel}
          />
        </section>
        ) : null}

        {shows("meetings") ? (
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
            actor={activeActorId}
            meetings={meetingRooms.items}
            roles={meetingRooms.roles}
            tasks={tasks.items.map((t) => ({ id: t.id, title: t.title }))}
            apiReachable={meetingRooms.apiReachable}
          />
          <div className="stack-grid" style={{ marginTop: 14 }}>
            <MeetingConsensusCard
              projectId={activeProjectId}
              actor={activeActorId}
              meetings={meetingRooms.items.map((item) => ({ id: item.id, title: item.title }))}
            />
          </div>
        </section>
        ) : null}

        {shows("workforce") ? (
        <section className="panel-stack" data-area="workforce" aria-label="Agent Studio">
          <WorkforceArea workforce={workforceData} chart={orgChartData} studio={agentStudioFeed}
                         actor={activeActorId} />
        </section>
        ) : null}

        {shows("processes") ? (
        <section className="panel-stack" data-area="processes" aria-label="Agent processes">
          <AgentProcesses projectId={activeProjectId} actor={activeActorId} />
          <ProcessManager projectId={activeProjectId} actor={activeActorId} />
        </section>
        ) : null}

        {shows("delivery") ? (
        <section className="panel-stack" data-area="delivery" aria-label="Deployment Manager">
          <div className="section-head">
            <div>
              <span className="eyebrow">Deployment Manager</span>
              <h2>Ask the agent that owns hosts, releases and rollback readiness.</h2>
            </div>
          </div>
          <DeploymentManagerPanel projectId={activeProjectId} />
        </section>
        ) : null}

        {shows("delivery") ? (
        <section className="panel-stack" id="hardware-manager" data-area="delivery" aria-label="Where apps install">
          <HardwareManager projectId={activeProjectId} actor={activeActorId} />
        </section>
        ) : null}

        {shows("delivery") ? (
        <section className="panel-stack" data-area="delivery" aria-label="Infra">
          <div className="section-head">
            <div>
              <span className="eyebrow">Infra</span>
              <h2>
                {deliveryPipeline && !deliveryPipeline.error && !deliveryPipeline.configured
                  ? "Nothing deploys yet — add a VM, then deploy a repository to it."
                  : "VMs, stack targets, and governed deployments from your repositories."}
              </h2>
            </div>
          </div>
          <DeliveryPipeline initial={deliveryPipeline} secrets={deliverySecrets}
                            deployments={recentDeployments} communityEdition
                            actor={activeActorId} />
        </section>
        ) : null}

        {shows("delivery") ? (
        <section className="panel-stack" data-area="delivery" aria-label="Pipeline generators">
          <div className="section-head">
            <div>
              <span className="eyebrow">Pipeline generators</span>
              <h2>Generate and validate CI config for GitHub Actions and peers.</h2>
            </div>
            <p className="status-meta">
              Generated and validated here; nothing is pushed to your repository until you copy it.
            </p>
          </div>
          <CICDPipelinesPanel
            projectId={activeProjectId}
            catalog={cicdCatalog}
            status={cicdStatus}
            initialRuns={cicdStatus.recent_runs ?? []}
          />
        </section>
        ) : null}

        {/* Monitoring is its own area: "did it ship" and "is it working" are different
            questions asked at different times, and one page answering both buries whichever
            you did not come for. */}
        {shows("monitoring") ? (
        <section className="panel-stack" data-area="monitoring" aria-label="Observability">
          <div className="section-head">
            <div>
              <span className="eyebrow">Observability</span>
              <h2>
                {(!deploymentHealth?.targets?.length && !observability?.deployed)
                  ? "Not configured — health and dashboards appear after an app is onboarded in Infra."
                  : "Health of what BOSS has deployed, and the dashboards behind it."}
              </h2>
            </div>
          </div>
          <MonitoringArea health={deploymentHealth} observability={observability}
                          hostTargetId={obsHost} />
        </section>
        ) : null}

        {shows("monitoring") ? (
        <section className="panel-stack" data-area="monitoring" aria-label="Observability suite">
          <div className="section-head">
            <div>
              <span className="eyebrow">Observability suite</span>
              <h2>Recommended OSS stack, gaps, and source health checks.</h2>
            </div>
            <p className="status-meta">
              Metrics · logs · traces · visualization — project {activeProjectId}.
            </p>
          </div>
          <ObservabilitySuitePanel feed={observabilitySuiteFeed} />
        </section>
        ) : null}

        {shows("shepherd") ? (
        <section className="panel-stack" data-area="shepherd" aria-label="Task Shepherd">
          <div className="section-head">
            <div>
              <span className="eyebrow">Task Shepherd</span>
              <h2>An internal AI developer that owns a task until it ships — or asks you one clear question.</h2>
            </div>
          </div>
          <ShepherdConsole initial={shepherdOverview} />
        </section>
        ) : null}

        {/* Apps — the object the console never had.
            A run is a job; an app is the thing a person has in mind. Its spec is under
            Meetings, its runs under Code Tasks, its preview under CI/CD and its health under
            Monitoring, so "where is my ApproveIt?" had no answer and was reconstructed by
            hand. This groups the runs into apps and puts each one's life on a single row. */}
        {shows("code_tasks") ? (
        <section className="panel-stack" data-area="code_tasks" aria-label="Factory">
          <CodeTasksHome
            projectId={activeProjectId}
            orgId={activeOrgId}
            codeTasks={codeTasksFeed.items}
            codeTaskTotal={codeTasksFeed.total}
            // A panel that is RUNNING has no code run yet, so Factory -- which lists runs --
            // could not show it at all. The work existed and the page that tracks work said
            // nothing, which is why a convened meeting looked like a dead button.
            meetings={meetingRooms.items}
            apiReachable={codeTasksFeed.apiReachable}
            previews={previewFeed.items}
            previewBlockedReason={previewFeed.blockedReason}
            benchmarks={qaBenchmarkApps}
            actor={activeActorId}
            githubPlane={wipStatus?.githubPlane}
            githubPlaneLabel={wipStatus?.githubPlaneReason}
            githubFullName={wipStatus?.githubFullName}
          />
        </section>
        ) : null}

        {shows("foundations") ? (
        <section data-area="foundations" aria-label="Design foundations">
          <FoundationsScreen />
        </section>
        ) : null}

        {shows("components") ? (
        <section data-area="components" aria-label="Component library">
          <ComponentsScreen />
        </section>
        ) : null}

        {shows("reliability") ? (
        <section className="panel-stack" data-area="reliability" aria-label="On-call SRE">
          <div className="section-head">
            <div>
              <span className="eyebrow">On-call</span>
              <h2>Schedules, escalation policies, and open pages for this project.</h2>
            </div>
            <p className="status-meta">
              Notify adapters default to dry-run until a token or webhook is configured.
            </p>
          </div>
          <OncallSreHome status={oncallSreStatus} />
        </section>
        ) : null}

        {shows("reliability") ? (
        <section className="panel-stack" data-area="reliability" aria-label="Reliability">
          <div className="section-head">
            <div>
              <span className="eyebrow">Reliability</span>
              <h2>AI SRE — anomalies become incidents, incidents become governed fix PRs, and the loop learns.</h2>
            </div>
          </div>
          <ReliabilityHome feed={reliabilityFeed} />
        </section>
        ) : null}

        {shows("ai-sre") ? (
        <section className="panel-stack" data-area="ai-sre" aria-label="AI SRE">
          <div className="section-head">
            <div>
              <span className="eyebrow">AI SRE</span>
              <h2>Capability ledger — category, status, and the remaining gap. Missing stays missing.</h2>
            </div>
            <p className="status-meta">
              A coverage ledger kept by BOSS. Not public GA.
            </p>
          </div>
          <AiSreHome feed={aiSreFeed} />
        </section>
        ) : null}

        {shows("reliability") ? (
        <section className="panel-stack" data-area="reliability" aria-label="Knowledge graph">
          <div className="section-head">
            <div>
              <span className="eyebrow">Knowledge graph</span>
              <h2>Service topology, known causes, and RCA context for reliability.</h2>
            </div>
            <p className="status-meta">
              Seed from infra fabric; query known causes per service.
            </p>
          </div>
          <KnowledgeGraphPanel feed={knowledgeGraphFeed} />
        </section>
        ) : null}

        {shows("discovery") ? (
        <section className="panel-stack" data-area="discovery" aria-label="Discovery">
          <div className="section-head">
            <div>
              <span className="eyebrow">Discovery</span>
              <h2>Review saved Discovery dossiers and the outcomes they identified.</h2>
            </div>
          </div>
          <DiscoveriesHome
            projectId={activeProjectId}
            orgId={activeOrgId}
            discoveries={discoveryFeed.items}
            apiReachable={discoveryFeed.apiReachable}
            initialDiscoveryId={requestedDiscovery}
          />
        </section>
        ) : null}

        {shows("pipeline") ? (
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
        ) : null}

        {shows("sandboxes") ? (
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
        ) : null}

        {shows("delivery_workflows") ? (
        <section className="panel-stack" data-area="delivery_workflows" aria-label="Workflows">
          <DeliveryWorkflows projectId={activeProjectId} actor={activeActorId} />
        </section>
        ) : null}

        {shows("activity") ? (
        <section className="panel-stack" data-area="activity" aria-label="Activity log">
          {/* NO WORK BOARD HERE. The Factory flow board ("where official work is, and why it
              stalled") used to sit above this log. It is a status board, not a log: it answers
              where work stands, while this screen exists to answer what the machinery actually
              emitted — BOSS's own records, the workflow's steps, the deploys, and the
              containers' output. Mixing the two made the log look like a dashboard and pushed
              the first log line below the fold. */}
          {/* The viewer reads `?run=` and `?wfr=` itself. Neither can be passed from here: this is a server
              component, and the Factory links across with pushState — a client-side navigation
              that never re-runs it. Threading it through props would have worked on a reload
              and silently done nothing on the click that matters. */}
          <ActivityLog
            projectId={activeProjectId} actor={activeActorId}
            // NAMES, NOT IDS. Arriving here from a run, the scope banner said
            // "Scoped to one work task crun_2f0a15b2…" — which is the truth and tells the
            // reader nothing about WHICH app they are looking at. The page already has every
            // run's title, so no extra fetch is needed to say it.
            runTitles={Object.fromEntries(codeTasksFeed.items.map((t) => [
              t.id, appIdentity({ title: t.title ?? "" }).name || t.id,
            ]))}
          />
        </section>
        ) : null}

        {shows("work") ? (
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
          <LiveJobsCard feed={liveJobs} />
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
        ) : null}

        {shows("work") ? (
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
        ) : null}

        {shows("work") ? (
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
                        {deadletter.latestRemediation ? (
                          <p className="detail">
                            Last remediation {deadletter.latestRemediation.action} /{" "}
                            {deadletter.latestRemediation.status}
                            {deadletter.latestRemediation.error
                              ? ` — ${deadletter.latestRemediation.error}`
                              : ""}
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
                          <ActionForm action={createDeadletterIncidentAction}>
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
                            <SubmitButton className="button secondary" type="submit">
                              Incident packet
                            </SubmitButton>
                          </ActionForm>
                          {deadletter.sourceKind === "job" &&
                          !deadletter.requeuedJobId ? (
                            <ActionForm action={createDeadletterRemediationAction}>
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
                                name="remediation_action"
                                value="retry"
                              />
                              <input
                                type="hidden"
                                name="reason"
                                value="operator_requested_retry"
                              />
                              <SubmitButton className="button secondary" type="submit">
                                Retry
                              </SubmitButton>
                            </ActionForm>
                          ) : null}
                          <ActionForm action={createDeadletterRemediationAction}>
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
                              name="remediation_action"
                              value="escalate"
                            />
                            <input
                              type="hidden"
                              name="reason"
                              value="operator_requested_escalate"
                            />
                            <SubmitButton className="button secondary" type="submit">
                              Escalate
                            </SubmitButton>
                          </ActionForm>
                          <ActionForm action={createDeadletterRemediationAction}>
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
                              name="remediation_action"
                              value="hold"
                            />
                            <input
                              type="hidden"
                              name="reason"
                              value="operator_requested_hold"
                            />
                            <SubmitButton className="button secondary" type="submit">
                              Hold
                            </SubmitButton>
                          </ActionForm>
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
        ) : null}

        {shows("decisions") ? (
        <section data-area="decisions" aria-label="Decision inbox">
          <DecisionInbox
            feed={decisions}
            tasks={tasks.items}
            roles={roles.items}
            projectId={activeProjectId}
            actorId={activeActorId}
            orgId={activeOrgId}
            initialFilter={requestedDecisionFilter}
          />
        </section>
        ) : null}

        {shows("decisions") ? (
        <section className="panel-stack approval-rules-section" aria-label="Approval rules">
          <div className="section-head">
            <div>
              <span className="eyebrow" data-area="decisions">Approval Rules</span>
              <h2>What requires your approval — and why.</h2>
            </div>
          </div>
          <ApprovalRulesView rules={approvalRules.rules} apiReachable={approvalRules.apiReachable} />
        </section>
        ) : null}
      </div>
    </ConsoleShell>
  );
}
