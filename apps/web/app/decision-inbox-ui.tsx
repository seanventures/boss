"use client";

import { WhyAsked } from "./skills/why-asked";
import { ReasonChips, rejectKind, sendReasons } from "./improve/rate-it";
import React, { useEffect, useMemo, useState } from "react";

import { BasedOn } from "./knowledge-console";
import { createDecisionDigestAction } from "./actions";
import { identityKey } from "./app-identity";
import { ClientRelativeTime } from "./client-relative-time";
import { ActionForm, DecisionResolveForm } from "./client-forms";
import { ActionPreview, actionPreviewOf, previewLead } from "./action-preview";
import { ArchitectureDoc } from "./architecture-doc";
import { ChecksFailing, checksFailingOf } from "./checks-failing";
import { architectureDocOf } from "../lib/architecture";
import { AreaLink } from "./area-link";
import { linkTo } from "./agents/live-util";
import { isAreaId } from "../lib/area-ids";
import { displayTitle } from "../lib/directive-subject";
import { SubmitButton } from "./submit-button";
import { formatDateTime as formatDateTimeShared } from "../lib/datetime";
import {
  DEFAULT_DECISION_INBOX_FILTER,
  DECISION_INBOX_EMPTY_HOWTO,
  DECISION_INBOX_EMPTY_TITLE,
  filterDecisionInbox,
  decisionAttention,
  isAnswerableDecision,
  offeredActions,
  parseDecisionInboxFilter,
  type DecisionFeed,
  type DecisionFeedItem,
  type DecisionInboxFilter,
} from "../lib/decisions";
import type { RoleFeedItem } from "../lib/roles";
import type { TaskFeedItem } from "../lib/tasks";
import { ChangeSetApproval, isChangeSetDecision } from "./multirepo/change-set-approval";
import { SelfFixCard, cardOf, isSelfFixDecision } from "./self-fix/approval-card";
import { MonitoringPackCard, monitoringPackOf } from "./workflows/monitoring-pack";

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

function formatDeadline(deadlineAt?: string): string {
  if (!deadlineAt) {
    return "No deadline";
  }
  return formatDateTimeShared(deadlineAt, "No deadline");
}

function roleInitials(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "CP";
  const initials =
    words.length === 1 ? words[0].slice(0, 3) : words.map((word) => word[0]).join("").slice(0, 3);
  return initials.toUpperCase();
}

export function formatRecommendation(decision: DecisionFeedItem): string {
  const r = decision.recommendation || {};
  const str = (k: string): string => (typeof r[k] === "string" ? (r[k] as string) : "");
  const action = str("action");
  const title = str("title");
  const pr = str("pull_request_url");
  const prNum = pr ? `#${pr.split("/").pop()}` : "";
  const vstatus = str("validation_status");
  const vsummary = str("validation_summary");

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
  if (action === "deploy_host") {
    // A governed host deployment (governed_host_deploy.py): where, which commit, the rollback.
    const commit = str("commit_ref");
    const rollback = Array.isArray(r["rollback"]) ? String((r["rollback"] as unknown[])[0] ?? "") : "";
    return [
      `Deploy ${str("app_name") || "this app"} to ${str("host") || "its host"}`
        + `${str("environment") ? ` · ${str("environment")}` : ""}`
        + `${commit ? ` at ${commit.slice(0, 12)}` : ""}.`,
      rollback,
      "Your own host: no provider cost.",
    ].filter(Boolean).join(" ");
  }
  if (action === "resume_workflow") {
    const gate = workflowGateCard(r);
    return gate ? workflowGateSubtitle(gate) : "Workflow decision";
  }
  if (action) return action.replaceAll("_", " ");
  // A WORKFLOW GATE, WHICH CARRIES NO `action`. It rendered as the bare words "review
  // required" — which names the obligation and nothing about the thing being reviewed.
  const step = str("step_label") || str("capability").replaceAll("_", " ");
  if (str("workflow_run_id") && step) {
    const app = str("app_name");
    const env = str("environment");
    return `The “${step}” step of this workflow is held for a person`
      + (app ? `, for ${app.slice(0, 60)}${app.length > 60 ? "…" : ""}` : "")
      + (env && ["deploy", "redeploy", "monitor"].includes(str("capability"))
          ? ` in ${env}` : "")
      + ". It does not run until this is answered.";
  }
  return "review required";
}

function decisionRuleLabel(decision: DecisionFeedItem): string {
  const action = typeof decision.recommendation.action === "string" ? decision.recommendation.action : "";
  const tool = typeof decision.recommendation.tool === "string" ? ` (${decision.recommendation.tool})` : "";
  const map: Record<string, string> = {
    execute_agent_tool: `Agent tool requires approval${tool}`,
    resume_agent_run: `Agent action gated — deferred tool${tool}`,
    review_plan_draft: "Plan ready for review",
    review_pull_request: "Pull request review",
    review_deployment: "Deployment review",
    deploy_host: "Host deployment",
    review_infra_provisioning: "Infrastructure provisioning review",
    "role.autonomy_change": "Role autonomy change",
  };
  if (map[action]) return map[action];
  if (action) return action.replaceAll("_", " ").replace(/\./g, " · ");
  return "governance gate";
}

/**
 * WHAT APPROVING A WORKFLOW GATE ACTUALLY DOES.
 *
 * A gate raised by the workflow runner carries no `action` — its recommendation is
 * `{capability, step_id, workflow_run_id, approver_role}` — so every one of them fell through
 * `approvalEffectLabel`'s switch to the default, and the Approvals page told the approver:
 *
 *     Approving records your decision — this type has no automated follow-on (no project is
 *     started).
 *
 * over a Deploy gate whose approval builds a container and runs it on a host. The operator's
 * words: "it is not relevant, we need to explain what the approver is going to do, in this case
 * he is going to approve to be deployed in the container which is not clear". The sentence was
 * not merely unhelpful, it was the opposite of true, and it is the last thing read before the
 * green button.
 *
 * `gate_completes_step` is the distinction that matters: on a gate capability the approval IS
 * the work, and on an ordinary step held at manual the approval only unblocks it — the step
 * then runs and does the thing.
 */
export function workflowGateEffect(r: Record<string, unknown>): string {
  const str = (k: string): string => (typeof r[k] === "string" ? (r[k] as string) : "");
  if (!str("workflow_run_id")) return "";
  const capability = str("capability");
  const label = str("step_label") || capability.replaceAll("_", " ") || str("step_id");
  const app = str("app_name");
  const named = app ? `“${app.slice(0, 60)}${app.length > 60 ? "…" : ""}”` : "this app";
  const env = str("environment") || "staging";
  switch (capability) {
    case "deploy":
    case "redeploy":
      return `Approving ships it: BOSS builds a container image from ${named}, runs it on the `
        + `bound host in ${env}, and the workflow moves on to monitoring. Nothing is sent to `
        + "any host before this is approved. If no deployment target is set, this step will "
        + "fail rather than guess a destination.";
    case "build":
      return `Approving starts the build of ${named} — an agent writes code, tests it and `
        + "opens a pull request. It spends model budget.";
    case "preview":
      return `Approving starts a throwaway preview of ${named} on the host so it can be looked `
        + "at before anything is shipped for real.";
    case "spec_review":
      return "Approving publishes the spec and releases the build that follows it. Nothing is "
        + "written until it is approved.";
    case "architecture":
      return `Approving signs off this architecture for ${named}: the step completes and every `
        + "build after it receives the document in its brief. Request changes sends it back to "
        + "the architect with your note; a new version comes back here.";
    case "monitor":
      return `Approving sets up health probes against ${named} in ${env}.`;
    case "notify":
      return "Approving sends the notification this step was configured with.";
    case "teardown_preview":
      return "Approving stops the preview and frees its port. The deployed app is untouched.";
    default:
      break;
  }
  // Unknown capability, but still a workflow gate: never claim there is no follow-on.
  return r.gate_completes_step
    ? `Approving completes the “${label}” step and the workflow continues from it.`
    : `Approving releases the “${label}” step — it then runs, and the workflow continues.`;
}

/**
 * WHERE IT WOULD ACTUALLY GO — read now, not recorded when the gate opened.
 *
 * "Approve the deployment" is not a decision anybody can make well without knowing the
 * destination, and the destination is usually chosen AFTER the gate opens (that is the whole
 * shape of the Deploy step: bind a target, then approve). So this is resolved at read time
 * against the live target list rather than frozen into the recommendation.
 *
 * The unbound case is the one worth showing: approving with no target does not ship anywhere,
 * it fails the step. An approver about to press a green button should be told that here, not
 * discover it in the run log afterwards.
 */
function DeployDestination({ recommendation, projectId }: {
  recommendation: Record<string, unknown>; projectId: string;
}) {
  const str = (k: string): string =>
    (typeof recommendation[k] === "string" ? (recommendation[k] as string) : "");
  const capability = str("capability");
  const appName = str("app_name");
  const isDeploy = !!str("workflow_run_id") && ["deploy", "redeploy"].includes(capability);
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "bound"; where: string; env: string }
    | { kind: "unbound" } | { kind: "unknown" }>({ kind: "loading" });

  useEffect(() => {
    if (!isDeploy || !appName) { setState({ kind: "unknown" }); return; }
    let alive = true;
    (async () => {
      try {
        const r = await fetch(
          `/api/deployment-targets?project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (!r.ok) { if (alive) setState({ kind: "unknown" }); return; }
        const body = await r.json();
        const wanted = identityKey(appName);
        // Identity, not string equality: the stored names carry U+2010 hyphens and trailing
        // punctuation from the directive they were made out of.
        const hit = ((body.items ?? []) as Array<Record<string, unknown>>).find(
          (t) => (t.status ?? "active") === "active"
                 && identityKey(String(t.app_name ?? "")) === wanted);
        if (!alive) return;
        if (!hit) { setState({ kind: "unbound" }); return; }
        const domains = Array.isArray(hit.domains) ? (hit.domains as string[]) : [];
        setState({
          kind: "bound",
          where: domains.find(Boolean) || String(hit.id ?? ""),
          env: String(hit.environment ?? "staging"),
        });
      } catch { if (alive) setState({ kind: "unknown" }); }
    })();
    return () => { alive = false; };
  }, [isDeploy, appName, projectId]);

  if (!isDeploy || state.kind === "unknown" || state.kind === "loading") return null;
  if (state.kind === "unbound") {
    return (
      <p className="dq-destination bad" data-testid="decision-destination-unbound">
        <strong>Nowhere to ship yet.</strong> “{appName}” has no active deployment target, so
        approving this would fail at the deploy rather than release anything. Set the target on
        the Deploy step in Workflow first.
      </p>
    );
  }
  return (
    <p className="dq-destination" data-testid="decision-destination">
      <strong>Goes to</strong> {state.where} · {state.env} — a container built from this app
      and started there, replacing whatever is running under that name.
    </p>
  );
}

/** The engine's sentence for a PRD its workflow did not wait for (engine spec_gate.py): the
 *  build is already running, so approving starts nothing. */
export function specGateSkipped(recommendation: Record<string, unknown>): string {
  const skipped = recommendation.spec_gate_skipped as { said?: unknown } | undefined;
  return skipped && typeof skipped.said === "string" ? skipped.said : "";
}

function approvalEffectLabel(recommendation: Record<string, unknown>): string {
  const action = typeof recommendation.action === "string" ? recommendation.action : "";
  // A SPEC THE WORKFLOW DID NOT WAIT FOR: "starts a governed project" would promise a second
  // build of work already building.
  const skipped = specGateSkipped(recommendation);
  if (skipped) return skipped;
  // A workflow gate has no `action`; before this it landed on the default below, which says
  // the opposite of what approving a Deploy gate does.
  const gate = workflowGateEffect(recommendation);
  if (gate && !action) return gate;
  if (
    action === "execute_agent_tool" &&
    (recommendation.start_code_task === true || recommendation.tool === "publish_document")
  ) {
    return "Approving publishes the PRD and starts a governed project for this task.";
  }
  switch (action) {
    case "review_plan_draft": return "Approving queues a PR proposal (code work) for this task.";
    case "resume_code_task": return "Approving resumes the paused project.";
    case "review_pull_request": return "Approving authorizes the pull-request merge.";
    case "review_deployment": return "Approving authorizes the deployment.";
    case "deploy_host": return "Approving deploys it now: BOSS builds, ships and health-checks it, and rolls back if the check fails.";
    case "review_infra_provisioning": return "Approving authorizes infrastructure provisioning.";
    case "execute_agent_tool": return "Approving runs the deferred agent tool now.";
    case "resume_agent_run": return "Approving resumes the paused agent run.";
    case "role.autonomy_change": return "Approving applies the role autonomy change.";
    // A workflow's own decision step: the runner reads the answer like any gate's. The default
    // below said "no automated follow-on" above a button that releases the step.
    case "resume_workflow": return "Approving releases the step and the workflow continues from it.";
    // An architecture a person signs off; the gate's own sentence says what follows.
    case "review_architecture": return gate || "Approving signs off the architecture; builds after it receive it.";
    case "github.community.dispatch_plan": return "Approving dispatches the community plan into tasks.";
    case "github.community.merge_authorization": return "Approving records merge authorization (maintainer still merges).";
    case "self_improvement.begin_canary":
    case "self_improvement.rollout": return "Approving proceeds with the self-improvement rollout.";
    default: return "Approving records your decision — this type has no automated follow-on (no project is started).";
  }
}

function decisionImpact(decision: DecisionFeedItem): { label: string; tone: string } {
  const action =
    typeof decision.recommendation.action === "string" ? decision.recommendation.action : "";
  if (action === "resume_code_task" || action === "review_plan_draft") return { label: "plan", tone: "amber" };
  if (action === "review_pull_request" || action.startsWith("github.")) return { label: "merge", tone: "blue" };
  if (action === "review_deployment" || action === "deploy_host") return { label: "deploy", tone: "teal" };
  if (action === "review_infra_provisioning") return { label: "infra", tone: "amber" };
  if (action === "role.autonomy_change") return { label: "access", tone: "red" };
  if (action.startsWith("self_improvement")) return { label: "rollout", tone: "teal" };
  if (action === "execute_agent_tool") return { label: "publish", tone: "teal" };
  if (action === "resume_agent_run") return { label: "agent tool", tone: "teal" };
  if (action === "resume_workflow") return { label: "workflow decision", tone: "blue" };
  if (action === "review_architecture") return { label: "architecture", tone: "blue" };
  if (action.includes("budget") || action.includes("reservation")) return { label: "budget", tone: "amber" };
  if (action === "deploy_stack") return { label: "deploy", tone: "teal" };
  return { label: impactWord(decisionRuleLabel(decision)), tone: "muted" };
}

/** A chip's word: whole words that fit, never a word cut in half ("CREATE BUDGET RESE…"). */
export function impactWord(rule: string): string {
  const words = rule.trim().split(/\s+/).filter(Boolean);
  let out = "";
  for (const w of words) {
    const next = out ? `${out} ${w}` : w;
    if (next.length > 18) break;
    out = next;
  }
  return out || (words[0] ?? "").slice(0, 18);
}

/** Who approved a decision, as a person reads it: their name, or a role word, never an id. */
export function approverName(decision: Pick<DecisionFeedItem, "approvedBy" | "approvedByName">): string {
  if (decision.approvedByName) return decision.approvedByName;
  const by = String(decision.approvedBy ?? "");
  if (by === "ceo") return "the owner";
  return /^[a-z]{2,6}_[A-Za-z0-9]+$/.test(by) ? "a person" : by;
}

/** The approval delegate who answered a decision, by name: "their approval delegate" without one. */
export function delegateName(decision: Pick<DecisionFeedItem, "viaDelegateName">): string {
  return decision.viaDelegateName || "their approval delegate";
}

/** Reason codes a decision carries, in words. A bare code (`host_deploy_requires_approval`,
 *  `missing_reservation`) is the machine's; the card says what it means. */
const REASON_WORDS: Record<string, string> = {
  host_deploy_requires_approval: "a deploy to a host needs a person's approval",
  stack_deploy_requires_approval: "a deploy to a host needs a person's approval",
  missing_reservation: "no budget was reserved for this work",
  missing_active_reservation: "no budget was reserved for this work",
  budget_exceeded: "it would go over the budget",
  live_secret_requires_approval: "a live key needs a person's approval",
};
/** The sentences a workflow gate wrote before it said where it was in words (engine
 *  workflows._gate_reason): a run id and step keys, which a card that is still open shows. */
const GATE_ID_SENTENCES = [
  /\s*Workflow wfr_[0-9A-Za-z]+ is waiting at decision step [\w.-]+\.?/g,
  /\s*It was produced by step '[^']*'\.?/g,
];
export function reasonInWords(reason: string | null | undefined): string {
  let r = String(reason ?? "").trim();
  for (const pattern of GATE_ID_SENTENCES) r = r.replace(pattern, "").trim();
  if (!r) return "";
  if (!/^[a-z0-9_.:-]+$/.test(r)) return r;
  return REASON_WORDS[r] ?? r.replace(/[_.:-]+/g, " ");
}

/** What a workflow gate's card says about itself (engine `recommendation.gate`): which case,
 *  which workflow, what the step before produced and who produced it. Null for any other card. */
export type WorkflowGateCard = {
  subject: string; workflowName: string; summary: string;
  producedByAgent: string; producedByTitle: string; runId: string;
};
export function workflowGateCard(rec: Record<string, unknown>): WorkflowGateCard | null {
  if (rec.action !== "resume_workflow") return null;
  const gate = (rec.gate && typeof rec.gate === "object" ? rec.gate : {}) as Record<string, unknown>;
  const s = (v: unknown): string => (typeof v === "string" ? v.trim() : "");
  return {
    subject: s(gate.subject), workflowName: s(gate.workflow_name), summary: s(gate.summary),
    producedByAgent: s(gate.produced_by_agent), producedByTitle: s(gate.produced_by_title),
    runId: s(rec.workflow_run_id),
  };
}

/** A gate card's subtitle: the workflow by name and the case it is about, never "resume
 *  workflow". */
export function workflowGateSubtitle(gate: WorkflowGateCard): string {
  return [gate.workflowName || "Workflow decision", gate.subject].filter(Boolean).join(" · ");
}

export function decisionStatusDetail(decision: DecisionFeedItem): string {
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
  if (decision.status === "withdrawn") {
    // Its run was stopped, removed or finished before anybody answered: there is no step left
    // for an answer to release, so the card has no buttons and says why.
    return decision.resolutionNote || "Withdrawn: the run it belonged to is over.";
  }
  if (decision.status === "executed") {
    return decision.externalRef
      ? `Executed with evidence: ${decision.externalRef}`
      : "This decision has been marked executed.";
  }
  if (decision.approvedBy === "auto_approval") {
    // Not a person. Saying "approved by auto_approval" invites the reader to look for who
    // that is; saying a rule fired is what actually happened.
    return `Approved automatically by the approval gate at ${formatDeadline(
      decision.approvedAt,
    )} — no human reviewed this.`;
  }
  if (decision.approvedBy?.startsWith("policy:")) {
    return `Approved by this environment's deploy rule at ${formatDeadline(decision.approvedAt)}.`;
  }
  if (decision.viaDelegate) {
    // AN APPROVAL DELEGATE answered it FOR the person (IDENTITY.md §6.15): both are named.
    const verb = decision.status === "rejected" ? "Rejected" : "Approved";
    const when = decision.approvedAt ? ` at ${formatDeadline(decision.approvedAt)}` : "";
    const person = decision.viaForName || (decision.approvedBy ? approverName(decision) : "the person who delegated it");
    return `${verb} by ${delegateName(decision)}, for ${person}${when}.`;
  }
  if (decision.approvedBy) {
    // A NAME, NEVER AN ID: "Approved by usr_kdzh…" and "Approved by ceo" were what cards said.
    return `Approved by ${approverName(decision)} at ${formatDeadline(
      decision.approvedAt,
    )}.`;
  }
  return "This card remains explicit until it is approved, rejected, delegated, revised, or safely expired.";
}

function readFilterFromLocation(): DecisionInboxFilter {
  if (typeof window === "undefined") {
    return DEFAULT_DECISION_INBOX_FILTER;
  }
  return parseDecisionInboxFilter(
    new URLSearchParams(window.location.search).get("decision_filter"),
  );
}

function writeFilterToLocation(next: DecisionInboxFilter, orgId?: string): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (orgId) url.searchParams.set("org", orgId);
  url.searchParams.set("area", "decisions");
  url.searchParams.set("decision_filter", next);
  if (url.toString() !== window.location.href) {
    window.history.pushState(null, "", url.toString());
  }
}

export type DecisionInboxProps = {
  feed: DecisionFeed;
  tasks: TaskFeedItem[];
  roles: RoleFeedItem[];
  projectId: string;
  actorId: string;
  orgId?: string;
  initialFilter?: DecisionInboxFilter;
};

export function DecisionInbox({
  feed,
  tasks,
  roles,
  projectId,
  actorId,
  orgId,
  initialFilter = DEFAULT_DECISION_INBOX_FILTER,
}: DecisionInboxProps) {
  const [filter, setFilter] = useState<DecisionInboxFilter>(initialFilter);

  useEffect(() => {
    const sync = () => setFilter(readFilterFromLocation());
    sync();
    window.addEventListener("popstate", sync);
    window.addEventListener("boss:navigate", sync);
    return () => {
      window.removeEventListener("popstate", sync);
      window.removeEventListener("boss:navigate", sync);
    };
  }, []);

  // WHICH DECISION SOMEBODY WAS SENT HERE FOR. A workflow gate links here naming it; read on
  // navigation as well as on mount, because the console moves between areas without reloading.
  const [wanted, setWanted] = useState<string | null>(null);
  useEffect(() => {
    const read = () =>
      setWanted(new URLSearchParams(window.location.search).get("decision"));
    read();
    window.addEventListener("popstate", read);
    window.addEventListener("boss:navigate", read);
    return () => {
      window.removeEventListener("popstate", read);
      window.removeEventListener("boss:navigate", read);
    };
  }, []);

  const visible = useMemo(
    () => filterDecisionInbox(feed.items, filter),
    [feed.items, filter],
  );
  const actionableCount = useMemo(
    () => filterDecisionInbox(feed.items, "needs_action").length,
    [feed.items],
  );
  const resolvedCount = useMemo(
    () => filterDecisionInbox(feed.items, "resolved").length,
    [feed.items],
  );
  const expiredCount = useMemo(
    () => filterDecisionInbox(feed.items, "expired").length,
    [feed.items],
  );

  const applyFilter = (next: DecisionInboxFilter) => {
    setFilter(next);
    writeFilterToLocation(next, orgId);
  };

  return (
    <section
      aria-labelledby="decision-inbox-title"
      className="panel-stack decision-inbox-section"
      data-area="decisions"
      data-decision-filter={filter}
      data-testid="decision-inbox"
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
          Needs action is what is waiting on you now, the same count the Command
          Center shows. A card that passes its deadline expires to its default;
          one whose default is to hold applied nothing and is kept under Expired,
          where it can still be answered.
        </p>
      </div>

      <div
        className="dq-filter"
        role="tablist"
        aria-label="Decision inbox filter"
        data-testid="decision-inbox-filter"
      >
        <button
          type="button"
          role="tab"
          aria-selected={filter === "needs_action"}
          className={`dq-filter-btn${filter === "needs_action" ? " active" : ""}`}
          data-testid="decision-filter-needs-action"
          onClick={() => applyFilter("needs_action")}
        >
          Needs action
          <span className="dq-filter-count">{actionableCount}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={filter === "expired"}
          className={`dq-filter-btn${filter === "expired" ? " active" : ""}`}
          data-testid="decision-filter-expired"
          onClick={() => applyFilter("expired")}
        >
          Expired, still answerable
          <span className="dq-filter-count">{expiredCount}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={filter === "resolved"}
          className={`dq-filter-btn${filter === "resolved" ? " active" : ""}`}
          data-testid="decision-filter-resolved"
          onClick={() => applyFilter("resolved")}
        >
          Resolved
          <span className="dq-filter-count">{resolvedCount}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={filter === "all"}
          className={`dq-filter-btn${filter === "all" ? " active" : ""}`}
          data-testid="decision-filter-all"
          onClick={() => applyFilter("all")}
        >
          All
          <span className="dq-filter-count">{feed.items.length}</span>
        </button>
      </div>

      {!feed.apiReachable ? (
        <article
          className="decision-card empty"
          data-empty-state="api-unavailable"
          data-testid="decision-inbox-unavailable"
        >
          <h3>Decision API unavailable</h3>
          <p className="detail">
            {feed.blockedReason ??
              "The executive console could not reach the decision inbox."}
          </p>
        </article>
      ) : visible.length === 0 ? (
        <DecisionInboxEmpty filter={filter} orgId={orgId} />
      ) : (
        <div className="dq" data-testid="decision-inbox-list">
          {/* THE ONE SOMEBODY WAS SENT HERE TO DECIDE. A workflow gate links here with the
              decision named; without this the operator arrives at a list and has to find it. */}
          {visible.map((decision) => (
            <DecisionInboxRow
              key={decision.id}
              highlighted={decision.id === wanted}
              decision={decision}
              tasks={tasks}
              roles={roles}
              projectId={projectId}
              actorId={actorId}
              orgId={orgId}
            />
          ))}
        </div>
      )}

      {feed.apiReachable ? (
        <details className="dq-digest">
          <summary>Create decision digest</summary>
          <div className="dq-digest-body">
            <p className="detail">
              Creates a durable digest artifact. Optional channel creates an
              outbox draft only; sending still requires a budgeted queue action.
            </p>
            <ActionForm permission="system:operate" action={createDecisionDigestAction} className="decision-actions">
              <input type="hidden" name="project_id" value={projectId} />
              <input type="hidden" name="actor" value={actorId} />
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
              <SubmitButton className="button secondary" type="submit">
                Create digest
              </SubmitButton>
            </ActionForm>
          </div>
        </details>
      ) : null}
    </section>
  );
}

export function DecisionInboxEmpty({
  filter,
  orgId,
}: {
  filter: DecisionInboxFilter;
  orgId?: string;
}) {
  if (filter === "expired") {
    return (
      <article
        className="decision-card empty"
        data-empty-state="no-expired"
        data-testid="decision-inbox-empty"
      >
        <h3>No expired decisions to answer</h3>
        <p className="detail">
          A decision whose deadline passes with a default of hold applies nothing and
          waits here, so you can still answer it if it still matters.
        </p>
      </article>
    );
  }
  if (filter === "resolved") {
    return (
      <article
        className="decision-card empty"
        data-empty-state="no-resolved"
        data-testid="decision-inbox-empty"
      >
        <h3>No resolved decisions</h3>
        <p className="detail">
          Approved, rejected, executed, and expired cards land here after you act
          or a deadline expires to its safe default.
        </p>
      </article>
    );
  }

  return (
    <article
      className="decision-card empty"
      data-empty-state="queue-clear"
      data-testid="decision-inbox-empty"
    >
      <h3>{DECISION_INBOX_EMPTY_TITLE}</h3>
      <p className="detail">
        Nothing needs your signature. Decisions appear here when work hits a
        human gate — they are not a history dump of already-resolved cards.
      </p>
      <ul className="dq-empty-how">
        {DECISION_INBOX_EMPTY_HOWTO.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      {filter !== "all" && orgId ? (
        <p className="detail">
          <button
            type="button"
            className="linklike"
            onClick={() => {
              // RESOLVED, as the button says — it opened "all". And a popstate, which the inbox
              // reads, rather than a boss:navigate carrying an object the shell ignores.
              writeFilterToLocation("resolved", orgId);
              window.dispatchEvent(new PopStateEvent("popstate"));
            }}
          >
            View resolved history
          </button>
        </p>
      ) : null}
    </article>
  );
}

function DecisionInboxRow({
  decision,
  tasks,
  roles,
  projectId,
  actorId,
  orgId,
  highlighted = false,
}: {
  /** Sent here by a workflow gate that named this decision. */
  highlighted?: boolean;
  decision: DecisionFeedItem;
  tasks: TaskFeedItem[];
  roles: RoleFeedItem[];
  projectId: string;
  actorId: string;
  orgId?: string;
}) {
  // A notice, not a question: BOSS refused to spend and the step has already failed. Its fix
  // is elsewhere (Govern, Repo and models), and the engine offers only Dismiss.
  const refusal = decision.kind === "refusal";
  const guidance = decision.guidance ?? null;
  const estimatedCost =
    typeof decision.costSummary.estimated_usd_micros === "number"
      ? decision.costSummary.estimated_usd_micros
      : undefined;
  // Answerable, not only pending: see isAnswerableDecision. A PR parked in a dead-end
  // "Request changes" is answerable again, and so is an expired hold decision; the card
  // draws their buttons.
  const isPending = isAnswerableDecision(decision);
  const expired = decisionAttention(decision) === "expired";
  const isApproved = decision.status === "approved";
  // Who actually decided this. A gate set to auto-approve resolves these without a person,
  // but the card said "approved" with no author, so the queue read as though someone had sat
  // and reviewed each one. Naming the approver is the difference between a record of a human
  // decision and a record of a policy firing.
  const autoApproved = isApproved && decision.approvedBy === "auto_approval";
  const linkedTask = decision.taskId
    ? tasks.find((task) => task.id === decision.taskId)
    : undefined;
  const ownerRole = linkedTask?.ownerRoleId
    ? roles.find((role) => role.id === linkedTask.ownerRoleId)
    : undefined;
  const who = ownerRole?.displayName ?? "Control plane";
  const impact = decisionImpact(decision);
  const rec = decision.recommendation;
  // How many times the same cause has re-raised while this stayed open. Each occurrence used
  // to insert its own row -- thirty identical "Model capacity exhausted" items -- so severity
  // now lives on ONE decision instead of by repetition.
  const occurrences = typeof rec.occurrences === "number" ? rec.occurrences : 0;
  const why = typeof rec.why === "string" ? rec.why : "";
  const plan = typeof rec.plan === "string" ? rec.plan : "";
  const review = typeof rec.plan_review === "string" ? rec.plan_review : "";
  const recArgs =
    rec.args && typeof rec.args === "object"
      ? (rec.args as Record<string, unknown>)
      : {};
  const subject =
    typeof recArgs.markdown === "string"
      ? recArgs.markdown
      : typeof recArgs.content === "string"
        ? recArgs.content
        : "";
  const subjectTitle =
    typeof recArgs.title === "string" ? recArgs.title : "Document to publish";
  // WHAT APPROVING WILL DO, as the thing itself (action-preview.tsx). When it is there it
  // replaces the sentence about the step: the email is the explanation.
  const preview = actionPreviewOf(rec.action_preview);
  // A WORKFLOW GATE says which case it is about and shows what it is deciding (engine
  // recommendation.gate): two alerts at one gate read identically, and its guidance said the
  // recommendation was "above" with nothing above it.
  const gateCard = workflowGateCard(rec);

  // ONE APPROVAL FOR A MULTI-REPO CHANGE: its own card (every PR, review, cross-repo check, cost,
  // rollback) and the three answers the engine offers for a set; no per-PR approval.
  if (isChangeSetDecision(decision)) {
    return (
      <article className="dq-row dq-row-changeset" data-testid="decision-row" data-decision-status={decision.status}
               data-highlighted={highlighted || undefined} id={`decision-${decision.id}`}
               ref={(el) => { if (el && highlighted) el.scrollIntoView({ block: "center" }); }}>
        <ChangeSetApproval decision={decision} projectId={projectId} actorId={actorId} pending={isPending} />
      </article>
    );
  }

  // A BOSS SELF-FIX: its own card (what broke, the change, the red→green test, the reviewers,
  // the tier) above the inbox's own answers, so the content hash still binds the approval.
  if (isSelfFixDecision(decision) && cardOf(rec)) {
    return (
      <article className="dq-row dq-row-selffix" data-testid="decision-row" data-decision-status={decision.status}
               data-highlighted={highlighted || undefined} id={`decision-${decision.id}`}
               ref={(el) => { if (el && highlighted) el.scrollIntoView({ block: "center" }); }}>
        <SelfFixCard card={cardOf(rec)!} projectId={projectId}
                     selfFixId={typeof rec.self_fix_id === "string" ? rec.self_fix_id : undefined} />
        <div className="dq-foot">
          {isPending ? (
            <DecisionActions decision={decision} projectId={projectId} actorId={actorId} />
          ) : (
            <>
              <span className={`dq-verdict ${isApproved ? "green" : decision.status === "rejected" ? "red" : "muted"}`}>
                {decision.status.toUpperCase()}
              </span>
              <span className="dq-note">{decisionStatusDetail(decision)}</span>
            </>
          )}
        </div>
      </article>
    );
  }

  return (
    <article
      className="dq-row"
      data-testid="decision-row"
      data-decision-status={decision.status}
      // Sent here by a workflow gate that named this one: marked, and brought into view, so
      // the operator does not arrive at a list and have to find what they were sent to decide.
      data-highlighted={highlighted || undefined}
      ref={(el) => { if (el && highlighted) el.scrollIntoView({ block: "center" }); }}
      id={`decision-${decision.id}`}
    >
      <div className="dq-body">
        <div className="dq-orb" aria-hidden>{roleInitials(who)}</div>
        <div className="dq-main">
          <div className="dq-propose">
            {refusal ? (
              <span className="dq-who"><b>BOSS</b> refused to spend</span>
            ) : (
              <span className="dq-who"><b>{who}</b> proposes</span>
            )}
            <span className={`dq-impact ${impact.tone}`}>{refusal ? "notice" : impact.label}</span>
          </div>
          {/* A refusal's question is internal plumbing ("Model capacity refused typed decision
              setup:typesafe-key/key-proof-fa84…"); the guidance says it in words. */}
          <h3 className="dq-title">
            {refusal && guidance ? guidance.what_happened : decision.question}
          </h3>
          {/* A PR that opened with its checks failing says so FIRST, security first: its merge
              is the person's stated decision, never a click (engine checks_failing.py). */}
          {checksFailingOf(rec) ? <ChecksFailing record={checksFailingOf(rec)!} /> : null}
          {/* An expired card says so before its buttons: "Approve and merge" on a pull request
              from weeks ago is a different act from the same button on one raised today. */}
          {expired ? (
            <p className="dq-rationale" data-testid="decision-expired-note">
              Expired{" "}
              <ClientRelativeTime iso={decision.deadlineAt ?? decision.updatedAt} empty=""
                as="span" />{" "}
              without an answer, so nothing was applied. Answer it only if it still matters;
              the work it was about may have moved on.
            </p>
          ) : null}
          {refusal ? null : (
            <p className="dq-rationale" data-testid="decision-subtitle">
              {why || (preview ? previewLead(preview) : formatRecommendation(decision))}
            </p>
          )}
          {gateCard && (gateCard.summary || gateCard.runId) ? (
            <div className="dq-gate-summary" data-testid="decision-gate-summary">
              {gateCard.summary ? (
                <>
                  <span className="dq-gate-summary-label">
                    {gateCard.producedByAgent
                      ? `What the ${gateCard.producedByAgent} produced`
                      : gateCard.producedByTitle
                        ? `From “${gateCard.producedByTitle}”`
                        : "What the run produced"}
                  </span>
                  <p>{gateCard.summary}</p>
                </>
              ) : null}
              {gateCard.runId ? (
                <a className="dq-gate-run" data-testid="decision-open-run"
                   href={linkTo({ area: "delivery_workflows", wfr: gateCard.runId }, orgId)}>
                  Open the run
                </a>
              ) : null}
            </div>
          ) : null}
          {preview && !refusal ? <ActionPreview preview={preview} /> : null}
          {/* THE RELEASE'S MONITORING PACK (engine monitoring_pack): approved with it. */}
          {monitoringPackOf(rec.monitoring_pack) ? <MonitoringPackCard pack={monitoringPackOf(rec.monitoring_pack)!} onCard /> : null}
          {subject ? (
            <details className="decision-plan-details">
              <summary>
                Read the full document before approving
                <span className="dq-doc-size">
                  {" · "}
                  {subject.length.toLocaleString("en-US")} characters
                </span>
              </summary>
              <div className="decision-plan-sec">
                <h4>{subjectTitle}</h4>
                <pre className="decision-doc">{subject}</pre>
              </div>
            </details>
          ) : null}
          {architectureDocOf(rec) ? <ArchitectureDoc doc={architectureDocOf(rec)!} /> : null}
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
                {/* The ask, not a panel's prompt scaffolding (lib/directive-subject.ts). */}
                T-{decision.taskId?.slice(-4)} · {displayTitle(linkedTask.title, "task").slice(0, 44)}
                {displayTitle(linkedTask.title, "task").length > 44 ? "…" : ""}
              </span>
            ) : null}
            <span className="dq-ev">risk tier {decision.riskTier}</span>
            <span className="dq-ev" data-testid="decision-default-action">
              default {decision.defaultAction ?? "hold"}
            </span>
            <BasedOn
              factIds={Array.isArray(rec.cited_fact_ids)
                ? rec.cited_fact_ids.filter((item): item is string => typeof item === "string")
                : []}
            />
            {estimatedCost != null ? (
              <span className="dq-ev">{formatMicros(estimatedCost)}</span>
            ) : null}
            <span className="dq-ev">{formatDeadline(decision.deadlineAt)}</span>
            {refusal ? (
              <span className="dq-ev" title={decision.question}>
                {decision.question.slice(0, 60)}{decision.question.length > 60 ? "…" : ""}
              </span>
            ) : null}
            {/* On the PENDING card, where it matters: thirty re-raises of one cause is the
                difference between a blip and an outage. It used to show only once resolved. */}
            {isPending && occurrences > 1 ? (
              <span className="cap-badge tone-amber" data-testid="decision-occurrences"
                    title="The same cause re-raised this while it stayed open">
                seen {occurrences}×
              </span>
            ) : null}
          </div>
          {/* Prose in the body font: a paragraph of guidance set as a mono chip read as a log. */}
          {reasonInWords(decision.reason) ? (
            <p className="dq-why" data-testid="decision-reason">
              <span className="dq-why-label">Why</span> {reasonInWords(decision.reason)}
            </p>
          ) : null}
          {isPending && refusal && guidance ? (
            <div className="dq-guidance" data-testid="decision-guidance">
              <p><strong>What to do:</strong> {guidance.what_to_do}</p>
              {/* The engine names the area as a string; only an area the console has is a link
                  (console-b: a link to an area that is gone did nothing when clicked). */}
              {isAreaId(guidance.area) ? (
                <AreaLink className="button secondary" area={guidance.area} orgId={orgId}>
                  {guidance.link_label}
                </AreaLink>
              ) : null}
            </div>
          ) : null}
          {isPending && !refusal && !preview ? (
            <>
              <p className="dq-effect">{approvalEffectLabel(decision.recommendation)}</p>
              <DeployDestination recommendation={decision.recommendation}
                                 projectId={projectId} />
            </>
          ) : null}
          {isPending ? <WhyAsked recommendation={decision.recommendation} projectId={projectId} /> : null}
        </div>
      </div>
      <div className="dq-foot">
        {isPending ? (
          <DecisionActions decision={decision} projectId={projectId} actorId={actorId} />
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
              {autoApproved ? "AUTO-APPROVED" : decision.status.toUpperCase()}
            </span>
            <span className="dq-note">{decisionStatusDetail(decision)}</span>
            {occurrences > 1 ? (
              <span className="cap-badge tone-amber"
                    title="The same cause re-raised this while it stayed open">
                seen {occurrences}×
              </span>
            ) : null}
            {isApproved && offeredActions(decision).some((a) => a.action === "execute") ? (
              <details className="dq-more">
                <summary>Mark executed</summary>
                <DecisionResolveForm className="dq-more-body">
                  <input type="hidden" name="decision_id" value={decision.id} />
                  <input type="hidden" name="project_id" value={projectId} />
                  <input type="hidden" name="actor" value={actorId} />
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
                  <SubmitButton permission="decision:execute" className="dq-btn approve" name="action" value="execute"
                                data-testid="decision-execute">
                    Mark executed
                  </SubmitButton>
                </DecisionResolveForm>
              </details>
            ) : null}
            <span className="dq-waited">
              <ClientRelativeTime
                iso={decision.createdAt}
                empty=""
                as="span"
              />
            </span>
          </>
        )}
      </div>
    </article>
  );
}

/**
 * THE BUTTONS THE ENGINE SAYS WORK, AND NO OTHERS.
 *
 * Every pending card used to get Approve, Reject, Escalate and Request changes. Escalate posted
 * an action the server never accepted, so it always failed; Approve on a budget or capacity
 * refusal recorded an answer nothing reads; Request changes on a workflow gate parked the run
 * for ever. The engine now decides the list (services/decision_actions.py) and refuses
 * anything else, so this draws exactly `allowedActions` — the primary ones as buttons, the
 * ones that need a note or a name behind "More".
 */
export function DecisionActions({ decision, projectId, actorId }: {
  decision: DecisionFeedItem; projectId: string; actorId: string;
}) {
  const offers = offeredActions(decision).filter((a) => a.action !== "execute");
  const direct = offers.filter((a) => !a.needs);
  // AN APPROVAL THAT NEEDS A REASON (a merge with checks failing): drawn open, never behind
  // "More", with the reason box; sent as the forced merge the engine requires.
  const reasoned = offers.find((a) => a.action === "approve" && a.needs === "note");
  // …or a release that ships commits from rejected or unreviewed runs (engine release_scope).
  const shipsUnreviewed = Boolean(
    (decision.recommendation.release_commits as { needs_reason?: unknown } | undefined)
      ?.needs_reason);
  // …or a workflow gate whose answer is a person's stated determination (the AML analyst
  // decision): the step says what to write.
  const reasonPrompt = typeof decision.recommendation.reason_prompt === "string"
    ? decision.recommendation.reason_prompt : "";
  const revise = offers.find((a) => a.action === "revise");
  const delegate = offers.find((a) => a.action === "delegate");
  const runId = typeof decision.recommendation.workflow_run_id === "string"
    ? decision.recommendation.workflow_run_id : "";
  const startsWork = !specGateSkipped(decision.recommendation)
    && (decision.recommendation.start_code_task === true
        || decision.recommendation.tool === "publish_document");
  const dismiss = direct.find((a) => a.action === "reject")?.label === "Dismiss";
  // THE CONTENT THIS APPROVAL IS OF. The engine refuses the approval if the preview has moved
  // on since this card was drawn, so nobody approves an email they did not read.
  // A preview without a drawable kind can still bind the approval (a self-fix card's preview is
  // its hash of the commit, tree, diff, tests and review): its hash is sent all the same.
  const rawPreview = decision.recommendation.action_preview as { content_hash?: unknown } | undefined;
  const contentHash = actionPreviewOf(decision.recommendation.action_preview)?.content_hash
    ?? (typeof rawPreview?.content_hash === "string" ? rawPreview.content_hash : "");
  // Why it is a no: optional chips, sent when Reject is pressed (the press itself is unchanged).
  const [why, setWhy] = useState<string[]>([]);
  const onPress = (e: React.MouseEvent) => {
    const pressed = (e.target as HTMLElement).closest?.("[data-testid='decision-reject']");
    if (!pressed || !why.length) return;
    void sendReasons(projectId, actorId, "reject", decision.id, why,
                     rejectKind(null, null, null, decision.recommendation));
  };
  return (
    // A layout-free wrapper: it only hears which button was pressed.
    <div className="im-contents" onClickCapture={onPress}>
    <DecisionResolveForm className="dq-actions">
      <input type="hidden" name="decision_id" value={decision.id} />
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="actor" value={actorId} />
      {runId ? <input type="hidden" name="workflow_run_id" value={runId} /> : null}
      {startsWork ? <input type="hidden" name="starts_work" value="1" /> : null}
      {dismiss ? <input type="hidden" name="reject_label" value="Dismiss" /> : null}
      {contentHash ? <input type="hidden" name="content_hash" value={contentHash} /> : null}
      {reasoned ? (
        <div className="dq-reasoned" data-testid="decision-reasoned-approve">
          <input type="hidden" name="force" value="true" />
          <textarea
            aria-label={reasonPrompt ? `Your reason for ${decision.id}` : shipsUnreviewed
              ? `Why ship ${decision.id} with commits from rejected or unreviewed runs`
              : `Why merge ${decision.id} with its checks failing`}
            className="textarea decision-note"
            name="note"
            rows={2}
            placeholder={reasonPrompt || (shipsUnreviewed
              ? "Why ship commits from rejected or unreviewed runs? Recorded with the release."
              : "Why merge it with its checks failing? Recorded with the merge.")}
            data-testid="decision-reason"
          />
          <SubmitButton permission="decision:approve" className="dq-btn reject" name="action"
                        value="approve" title={reasoned.effect || undefined}
                        data-testid="decision-approve">
            {reasoned.label}
          </SubmitButton>
        </div>
      ) : null}
      {direct.map((offer) => (
        <SubmitButton permission="decision:approve"
          key={offer.action}
          className={`dq-btn ${offer.tone === "neutral" ? "neutral" : offer.tone}`}
          name="action"
          value={offer.action}
          title={offer.effect || undefined}
          data-testid={`decision-${offer.action}`}
        >
          {offer.label}
        </SubmitButton>
      ))}
      {direct.some((a) => a.action === "reject") && !dismiss ? (
        <details className="dq-more dq-why" data-testid="decision-why">
          <summary title="Optional: tell BOSS why, before you reject">Why no?</summary>
          <div className="dq-more-body"><ReasonChips value={why} onChange={setWhy} /></div>
        </details>
      ) : null}
      {revise || delegate ? (
        <details className="dq-more">
          <summary>{[revise ? "Request changes" : "", delegate ? "hand to someone else" : ""]
            .filter(Boolean).join(" · ").replace(/^h/, "H")}</summary>
          <div className="dq-more-body">
            {revise ? (
              <>
                <textarea
                  aria-label={`What to change, for ${decision.id}`}
                  className="textarea decision-note"
                  name="note"
                  rows={3}
                  placeholder="What should change? This is sent back with the decision."
                />
                <SubmitButton permission="decision:approve" className="dq-btn neutral" name="action" value="revise"
                              data-testid="decision-revise" title={revise.effect}>
                  {revise.label}
                </SubmitButton>
              </>
            ) : null}
            {delegate ? (
              <>
                <label className="dq-escalate-field">
                  <span className="sr-only">Hand {decision.id} to</span>
                  <input
                    aria-label={`Who to hand ${decision.id} to`}
                    className="input dq-escalate-input"
                    name="delegate_to"
                    placeholder="A person or role, e.g. release_manager"
                    data-testid="decision-delegate-target"
                  />
                </label>
                <SubmitButton permission="decision:delegate" className="dq-btn escalate" name="action" value="delegate"
                              data-testid="decision-delegate" title={delegate.effect}>
                  {delegate.label}
                </SubmitButton>
              </>
            ) : null}
          </div>
        </details>
      ) : null}
      <span className="dq-waited">
        <ClientRelativeTime iso={decision.createdAt} empty="" as="span" />
      </span>
    </DecisionResolveForm>
    </div>
  );
}


