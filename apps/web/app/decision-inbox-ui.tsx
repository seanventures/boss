"use client";

import React, { useEffect, useMemo, useState } from "react";

import { BasedOn } from "./knowledge-console";
import { createDecisionDigestAction } from "./actions";
import { identityKey } from "./app-identity";
import { ClientRelativeTime } from "./client-relative-time";
import { DecisionResolveForm } from "./client-forms";
import { SubmitButton } from "./submit-button";
import { formatDateTime as formatDateTimeShared } from "../lib/datetime";
import {
  DEFAULT_DECISION_INBOX_FILTER,
  DECISION_INBOX_EMPTY_HOWTO,
  DECISION_INBOX_EMPTY_TITLE,
  filterDecisionInbox,
  parseDecisionInboxFilter,
  type DecisionFeed,
  type DecisionFeedItem,
  type DecisionInboxFilter,
} from "../lib/decisions";
import type { RoleFeedItem } from "../lib/roles";
import type { TaskFeedItem } from "../lib/tasks";

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

function approvalEffectLabel(recommendation: Record<string, unknown>): string {
  const action = typeof recommendation.action === "string" ? recommendation.action : "";
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
    case "review_infra_provisioning": return "Approving authorizes infrastructure provisioning.";
    case "execute_agent_tool": return "Approving runs the deferred agent tool now.";
    case "resume_agent_run": return "Approving resumes the paused agent run.";
    case "role.autonomy_change": return "Approving applies the role autonomy change.";
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
  if (action === "review_deployment") return { label: "deploy", tone: "teal" };
  if (action === "review_infra_provisioning") return { label: "infra", tone: "amber" };
  if (action === "role.autonomy_change") return { label: "access", tone: "red" };
  if (action.startsWith("self_improvement")) return { label: "rollout", tone: "teal" };
  if (action === "execute_agent_tool") return { label: "publish", tone: "teal" };
  if (action === "resume_agent_run") return { label: "agent tool", tone: "teal" };
  const rule = decisionRuleLabel(decision);
  return { label: rule.length > 18 ? rule.slice(0, 18).trimEnd() + "…" : rule, tone: "muted" };
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
  if (decision.approvedBy === "auto_approval") {
    // Not a person. Saying "approved by auto_approval" invites the reader to look for who
    // that is; saying a rule fired is what actually happened.
    return `Approved automatically by the approval gate at ${formatDeadline(
      decision.approvedAt,
    )} — no human reviewed this.`;
  }
  if (decision.approvedBy) {
    return `Approved by ${decision.approvedBy} at ${formatDeadline(
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
          Default view is the actionable queue (pending and delegated). Resolved
          history is one click away. Cards expire to a safe default when their
          deadline passes.
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
            <form action={createDecisionDigestAction} className="decision-actions">
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
            </form>
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
            className="linkish"
            onClick={() => {
              writeFilterToLocation("all", orgId);
              window.dispatchEvent(
                new CustomEvent("boss:navigate", {
                  detail: { area: "decisions", params: { decision_filter: "all" } },
                }),
              );
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
  highlighted = false,
}: {
  /** Sent here by a workflow gate that named this decision. */
  highlighted?: boolean;
  decision: DecisionFeedItem;
  tasks: TaskFeedItem[];
  roles: RoleFeedItem[];
  projectId: string;
  actorId: string;
}) {
  const estimatedCost =
    typeof decision.costSummary.estimated_usd_micros === "number"
      ? decision.costSummary.estimated_usd_micros
      : undefined;
  const isPending =
    decision.status === "pending" || decision.status === "delegated";
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
            <span className="dq-who"><b>{who}</b> proposes</span>
            <span className={`dq-impact ${impact.tone}`}>{impact.label}</span>
          </div>
          <h3 className="dq-title">{decision.question}</h3>
          <p className="dq-rationale">
            {why || formatRecommendation(decision)}
          </p>
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
            <span className="dq-ev" data-testid="decision-default-action">
              default {decision.defaultAction ?? "hold"}
            </span>
            {decision.reason ? (
              <span className="dq-ev" data-testid="decision-reason">
                reason {decision.reason}
              </span>
            ) : null}
            <BasedOn
              factIds={Array.isArray(rec.cited_fact_ids)
                ? rec.cited_fact_ids.filter((item): item is string => typeof item === "string")
                : []}
            />
            {estimatedCost != null ? (
              <span className="dq-ev">{formatMicros(estimatedCost)}</span>
            ) : null}
            <span className="dq-ev">{formatDeadline(decision.deadlineAt)}</span>
          </div>
          {isPending ? (
            <>
              <p className="dq-effect">{approvalEffectLabel(decision.recommendation)}</p>
              <DeployDestination recommendation={decision.recommendation}
                                 projectId={projectId} />
            </>
          ) : null}
        </div>
      </div>
      <div className="dq-foot">
        {isPending ? (
          <DecisionResolveForm className="dq-actions">
            <input type="hidden" name="decision_id" value={decision.id} />
            <input type="hidden" name="project_id" value={projectId} />
            <input type="hidden" name="actor" value={actorId} />
            <SubmitButton
              className="dq-btn approve"
              name="action"
              value="approve"
              data-testid="decision-approve"
            >
              Approve
            </SubmitButton>
            <SubmitButton
              className="dq-btn reject"
              name="action"
              value="reject"
              data-testid="decision-reject"
            >
              Reject
            </SubmitButton>
            <label className="dq-escalate-field">
              <span className="sr-only">Escalate {decision.id} to</span>
              <input
                aria-label={`Escalate target for ${decision.id}`}
                className="input dq-escalate-input"
                name="escalate_to"
                placeholder="Escalate to role or approver"
                data-testid="decision-escalate-target"
              />
            </label>
            <SubmitButton
              className="dq-btn escalate"
              name="action"
              value="escalate"
              data-testid="decision-escalate"
            >
              Escalate
            </SubmitButton>
            <details className="dq-more">
              <summary>Note · request changes</summary>
              <div className="dq-more-body">
                <textarea
                  aria-label={`Decision note for ${decision.id}`}
                  className="textarea decision-note"
                  name="note"
                  rows={3}
                  placeholder="Optional note or required revision request rationale."
                />
                <SubmitButton className="dq-btn neutral" name="action" value="revise">
                  Request changes
                </SubmitButton>
              </div>
            </details>
            <span className="dq-waited">
              <ClientRelativeTime
                iso={decision.createdAt}
                empty=""
                as="span"
              />
            </span>
          </DecisionResolveForm>
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
            {isApproved ? (
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
                  <SubmitButton className="dq-btn approve" name="action" value="execute">
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


