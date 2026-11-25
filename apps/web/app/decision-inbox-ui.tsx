"use client";

import { changeHref, changeRef, isLocalChangeUrl, linkTarget, prWhere, reviewChangeWords } from "../lib/repo-links";
import { WhyAsked } from "./skills/why-asked";
import { ReasonChips, rejectKind, sendReasons } from "./improve/rate-it";
import React, { useEffect, useMemo, useState } from "react";

import { BasedOn } from "./knowledge-console";
import { createDecisionDigestAction } from "./actions";
import { identityKey } from "./app-identity";
import { ClientRelativeTime } from "./client-relative-time";
import { ActionForm, DecisionResolveForm } from "./client-forms";
import { ActionPreview, actionPreviewOf, previewLead } from "./action-preview";
import { HeldArguments } from "./held-arguments";
import { ArchitectureDoc } from "./architecture-doc";
import { automatedChecksLine, ChecksFailing, checksFailingOf, failingGateLabels, FollowUps, followUpsOf, isAdvisory, MustHaves, mustHavesOf, OldTestsUnknown, splitChecksFailing } from "./checks-failing";
import { architectureDocOf } from "../lib/architecture";
import { AreaLink } from "./area-link";
import { linkTo, navigate } from "./agents/live-util";
import { reviewPatch } from "./agents/presence-pill";
import { useAttention } from "../lib/agents-live";
import { PX_PATHS, approvalCountsOf, parseSummary, usePxRead, type ProjectsSummary } from "../lib/project-experience";
import { isAreaId } from "../lib/area-ids";
import { displayTitle } from "../lib/directive-subject";
import { PrdRecordView } from "./prd-record";
import { TypedAnswer, TypedDecisionForm } from "./typed-form-ui";
import { StorageDeleteFromDecision } from "./infra/storage-delete-decision";
import { DecisionOptions, optionsOf } from "./decision-options-ui";
import { AlwaysAllowOption, alwaysAllowOffer } from "./always-allow-ui";
import { parseForm } from "../lib/typed-form";
import { SubmitButton } from "./submit-button";
import { formatDateTime as formatDateTimeShared } from "../lib/datetime";
import { sentenceCut } from "../lib/decision-excerpt";
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
  type DecisionActionOffer,
  type DecisionInboxFilter,
} from "../lib/decisions";
import type { RoleFeedItem } from "../lib/roles";
import type { TaskFeedItem } from "../lib/tasks";
import { ChangeSetApproval, isChangeSetDecision } from "./multirepo/change-set-approval";
import { SelfFixCard, cardOf, isSelfFixDecision } from "./self-fix/approval-card";
import { GrantAccessCard, grantOf } from "./skills/grant-card";
import { SecretProposalCard, secretProposalOf } from "./secret-proposal-card";
import { MonitoringPackCard, monitoringPackOf } from "./workflows/monitoring-pack";
import { plainSentence, plainTitle } from "../lib/plain-words";
import { blockingRows, isReviewFinding, lineWords, packetOf, packetTally, rerunPending, rootTitle, SEVS, type ReviewPacket } from "../lib/validation-review";
import { RebuildPacket, ReviewPacketView, packetHold, packetLinks, packetTitle } from "./validation/review-packet";
import { usd } from "../lib/self-fix";

/**
 * A decision-card excerpt (output/evidence/why) that never cuts mid-word or mid-sentence
 * (AML video polish [7]): short text renders whole, long text cuts at a sentence boundary with
 * an in-place "Show all" control that toggles the full text and its own aria-expanded state.
 */
export function DecisionCardExcerpt({ text, testid, as: As = "p" }: {
  text: string; testid: string; as?: "p" | "span";
}) {
  const [expanded, setExpanded] = useState(false);
  const { text: excerpt, cut } = sentenceCut(text, 600);
  return (
    <>
      <As data-testid={testid}>{expanded || !cut ? text : excerpt}</As>
      {cut ? (
        <button type="button" className="dq-excerpt-toggle" data-testid={`${testid}-toggle`}
                aria-expanded={expanded} onClick={() => setExpanded((prev) => !prev)}>
          {expanded ? "Show less" : "Show all"}
        </button>
      ) : null}
    </>
  );
}

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

/** The card's deadline, labelled (a bare future date in the evidence row read as an unexplained
 *  timestamp): "Decide by …" before it, "Deadline passed …" after, nothing when there is none. */
export function deadlineLabel(deadlineAt?: string | null, now: number = Date.now()): string {
  if (!deadlineAt) return "";
  const at = Date.parse(deadlineAt);
  const when = formatDateTimeShared(deadlineAt, "");
  if (!when || Number.isNaN(at)) return "";
  return at >= now ? `Decide by ${when}` : `Deadline passed ${when}`;
}

function roleInitials(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "CP";
  const initials =
    words.length === 1 ? words[0].slice(0, 3) : words.map((word) => word[0]).join("").slice(0, 3);
  return initials.toUpperCase();
}

/** What the card proposes, ONE FACT PER LINE (validation-review SPEC §0 #1): a merge card used to
 *  read as one long sentence, four fragments joined with spaces. The card draws each line on its
 *  own row; the title is the work's root title, never a chain of "Follow-up: … — " prefixes. */
export function recommendationLines(decision: DecisionFeedItem): string[] {
  const r = decision.recommendation || {};
  const str = (k: string): string => (typeof r[k] === "string" ? (r[k] as string) : "");
  const action = str("action");
  const title = rootTitle(plainTitle(str("title")));
  const pr = str("pull_request_url");
  const prNum = pr ? `#${pr.split("/").pop()}` : "";

  if (action === "review_pull_request") {
    return [
      title ? `Approve the code change “${title}”.` : "Approve this code change.",
      pr ? `${isLocalChangeUrl(pr) ? "Change set" : "Pull request"} ${prNum} is open ${prWhere(pr)} with the full diff to review.` : "",
      automatedChecksLine(r),
      "Approving opens/merges the PR; rejecting keeps it blocked for changes.",
    ].filter(Boolean);
  }
  return [formatOne(decision)];
}

/** The lines as text, one per line (tests and plain-text surfaces). */
/**
 * A BLOCKED PACKET (an open P0/P1 from a validator or a seat) is merged only when a person forces
 * it with a reason — the engine refuses a plain approve with `packet_blocked`. So its Approve is
 * the reasoned forced-merge form "Merge with checks failing" uses, naming the open rows. An
 * invalid packet and a pending re-run never reach here: they stay fully disabled (packetHold).
 */
export function blockedPacketDecision(decision: DecisionFeedItem, packet: ReviewPacket): DecisionFeedItem {
  if (packet.verdict.state !== "blocked") return decision;
  // The same rows the change page's "Merge anyway" lists (one helper: lib/validation-review).
  const rows = blockingRows(packet);
  const open = rows;
  const offers = offeredActions(decision).map((o) => (o.action === "approve"
    ? { ...o, needs: "note" as const, label: "Merge anyway", tone: "reject" as const,
        effect: `Merges with ${open.length || "its"} open P0/P1 finding${open.length === 1 ? "" : "s"}; your reason is recorded with the merge.` }
    : o));
  return {
    ...decision, allowedActions: offers,
    recommendation: { ...decision.recommendation, blocked_rows: rows,
      reason_prompt: "Why merge with these open? Recorded with the merge." },
  };
}

/**
 * A MERGE WHOSE ONLY "FAILING" CHECK IS OLD TESTS NOBODY CAN NAME: the engine still wants the
 * merge forced with a reason, but the button and the prompt never say "checks failing" — they say
 * what is true, old tests unknown. Any other failing gate keeps the card as it is.
 */
export function oldTestsUnknownDecision(decision: DecisionFeedItem, split: { real: unknown; oldTestsUnknown: string | null }): DecisionFeedItem {
  if (split.real || !split.oldTestsUnknown) return decision;
  const offers = offeredActions(decision).map((o) => (o.action === "approve" && o.needs === "note" && /checks failing/i.test(o.label)
    ? { ...o, label: "Merge" } : o));
  return { ...decision, allowedActions: offers,
    recommendation: { ...decision.recommendation,
      reason_prompt: typeof decision.recommendation.reason_prompt === "string" ? decision.recommendation.reason_prompt
        : "Old tests could not be named. Why merge? Recorded with the merge." } };
}

export function formatRecommendation(decision: DecisionFeedItem): string {
  return recommendationLines(decision).join("\n");
}

function formatOne(decision: DecisionFeedItem): string {
  const r = decision.recommendation || {};
  const str = (k: string): string => (typeof r[k] === "string" ? (r[k] as string) : "");
  const action = str("action");
  const title = plainTitle(str("title"));
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
        + `${str("environment") ? `, ${str("environment")}` : ""}`
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
  // required", which names the obligation and nothing about the thing being reviewed.
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
    resume_agent_run: `Agent action gated, deferred tool${tool}`,
    review_plan_draft: "Plan ready for review",
    review_pull_request: "Pull request review",
    review_deployment: "Deployment review",
    deploy_host: "Host deployment",
    review_infra_provisioning: "Infrastructure provisioning review",
    "role.autonomy_change": "Role autonomy change",
  };
  if (map[action]) return map[action];
  if (action) return action.replaceAll("_", " ").replace(/\./g, ", ");
  return "governance gate";
}

/**
 * WHAT APPROVING A WORKFLOW GATE ACTUALLY DOES.
 *
 * A gate raised by the workflow runner carries no `action`, its recommendation is
 * `{capability, step_id, workflow_run_id, approver_role}`, so every one of them fell through
 * `approvalEffectLabel`'s switch to the default, and the Approvals page told the approver:
 *
 *     Approving records your decision, this type has no automated follow-on (no project is
 *     started).
 *
 * over a Deploy gate whose approval builds a container and runs it on a host. The operator's
 * words: "it is not relevant, we need to explain what the approver is going to do, in this case
 * he is going to approve to be deployed in the container which is not clear". The sentence was
 * not merely unhelpful, it was the opposite of true, and it is the last thing read before the
 * green button.
 *
 * `gate_completes_step` is the distinction that matters: on a gate capability the approval IS
 * the work, and on an ordinary step held at manual the approval only unblocks it, the step
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
      return `Approving starts the build of ${named}, an agent writes code, tests it and `
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
    : `Approving releases the “${label}” step, it then runs, and the workflow continues.`;
}

/**
 * WHERE IT WOULD ACTUALLY GO, read now, not recorded when the gate opened.
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
function DeployDestination({ recommendation, projectId, inline = false }: {
  recommendation: Record<string, unknown>; projectId: string;
  /** One phrase of the card's status line (" · goes to …"), not a paragraph. */
  inline?: boolean;
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
  if (inline) {
    return state.kind === "unbound" ? (
      <span className="dq-phrase bad" data-testid="decision-destination-unbound-phrase">
        {" · "}nowhere to ship yet: no deployment target set
      </span>
    ) : (
      <span className="dq-phrase" data-testid="decision-destination-phrase">
        {" · "}goes to {state.where}
      </span>
    );
  }
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
      <strong>Goes to</strong> {state.where}, {state.env}, a container built from this app
      and started there, replacing whatever is running under that name.
    </p>
  );
}

/** The engine's sentence for a PRD its workflow did not wait for (engine spec_gate.py): the
 *  build is already running, so approving starts nothing. */
/** A review of a LOCAL change set (BOSSAPP-919): "Review change #n ›" opens BOSS's change-set
 *  page in this tab, where the diff, the review and Merge are. */
function LocalChangeLink({ recommendation }: { recommendation: Record<string, unknown> }) {
  const url = typeof recommendation.pull_request_url === "string" ? recommendation.pull_request_url : "";
  const words = recommendation.action === "review_pull_request" ? reviewChangeWords(url) : null;
  if (!words) return null;
  return <p className="dq-effect"><a href={changeHref(url)} data-testid="decision-review-change">{words}</a></p>;
}

export function specGateSkipped(recommendation: Record<string, unknown>): string {
  const skipped = recommendation.spec_gate_skipped as { said?: unknown } | undefined;
  return skipped && typeof skipped.said === "string" ? skipped.said : "";
}

/** What the engine says its Approve button does (services/decision_actions.py), or "". The
 *  engine names every follow-on it starts there; this card used to keep its own per-action list
 *  and fell to "no automated follow-on" for any action that list had not heard of, above a
 *  "Fix these" that starts a build (BOSSAPP-1354). */
export function serverApproveEffect(offers: { action: string; effect?: string }[] | undefined): string {
  const approve = (offers ?? []).find((o) => o.action === "approve");
  return approve && typeof approve.effect === "string" ? approve.effect.trim() : "";
}

export function approvalEffectLabel(recommendation: Record<string, unknown>,
                                    offers?: { action: string; effect?: string }[]): string {
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
    return "Approving publishes the PRD and starts the build from it.";
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
    // A re-check's Fix card (engine change_recheck): pressing it starts the follow-up build.
    case "start_recheck_followup":
      return serverApproveEffect(offers)
        || "Approving starts a follow-up build that fixes the re-check's findings.";
    default: {
      // THE ENGINE'S OWN WORDS FOR ITS BUTTON before any claim that nothing follows: only an
      // action whose Approve the engine describes with nothing may say so.
      const said = serverApproveEffect(offers);
      if (said) return said;
      return "Approving records your decision, this type has no automated follow-on (no project is started).";
    }
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
  if (action === "grant_tool_access") return { label: "tool access", tone: "amber" };
  if (action === "store_proposed_secret") return { label: "secret", tone: "amber" };
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
  meeting_prd_ready_to_publish: "the panel finished the PRD; it needs your go-ahead to publish",
  plan_ready_for_review: "the plan is ready for your review",
  merge_decision_required: "merging needs a person's decision",
  ambient_needs_you: "always-on agents need your answer",
  legacy_model_outcome_unknown: "BOSS could not tell whether some earlier model calls were charged",
  model_outcome_unknown: "BOSS could not tell whether a model call was charged",
  budget_outcome_unknown: "BOSS could not tell whether a budget hold was spent",
  budget_denied: "it would go over the budget",
  model_capacity_denied: "no model had room to take this work",
  missing_github_credentials: "BOSS has no GitHub access for this repository",
  no_worker: "no worker is taking work from the queue",
  agent_skill_requires_approval: "an agent's new skill needs a person's approval",
  provider_changeset_requires_approval: "a provider change needs a person's approval",
  connection_reconciliation: "a connection's records disagree and need a person to settle them",
  architecture_reconciliation: "the architecture records disagree and need a person to settle them",
  conversation_reconciliation: "a conversation's records disagree and need a person to settle them",
  orange_defers_non_critical_work: "the system is under strain, so non-critical work waits",
  change_sync: "a change needs syncing with its repository",
  quiet: "the work stopped moving",
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
  return [gate.workflowName || "Workflow decision", gate.subject].filter(Boolean).join(", ");
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
    )}, no human reviewed this.`;
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

/** What waits on you in the OTHER projects (stranger test r2): the rail said "+4" and the bell
 *  "4" while this page said "Queue is clear 0" with no pointer anywhere. From the same summary
 *  the rail's org total is counted from (orgNeedsOf), so the three numbers agree. */
export type ElsewhereWaiting = { total: number; projects: { id: string; name: string; count: number; href: string }[] };
export function waitingElsewhere(summary: ProjectsSummary | null | undefined, projectId: string, orgId?: string): ElsewhereWaiting {
  const projects = (summary?.items ?? [])
    .filter((c) => !c.archived && c.id !== projectId && (c.needs_you || 0) > 0)
    .map((c) => ({ id: c.id, name: c.name || c.id, count: c.needs_you,
                   href: linkTo({ area: "decisions", project_id: c.id, decision_filter: "needs_action" }, orgId) }));
  // The same number the rail's "+N" and the bell count beyond this project (approvalCountsOf).
  return { total: summary ? approvalCountsOf(null, summary, projectId).elsewhere : 0, projects };
}

/** The paragraph that headed Approvals, now the Expired tab's tooltip (approvals wireframe B). */
export const EXPIRED_TAB_TIP = "A card that passes its deadline expires to its default. One whose "
  + "default is to hold applied nothing and is kept here, where it can still be answered.";

/** The projects that wait, by name, most waiting first; the top few, then "+ K more". */
export const ALSO_WAITING_SHOWN = 5;
export function alsoWaitingList(waiting: ElsewhereWaiting): ElsewhereWaiting["projects"] {
  return [...waiting.projects].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

/**
 * "Also waiting: 67 in 21 other projects ›" (approvals wireframe B). One line under the tabs,
 * the same number as the rail's "+N" and the bell beyond this project (stranger tests r2, r3);
 * it opens a short list by project name, with counts, most waiting first, each one a link to
 * that project's approvals. It replaced the "+67" in two tabs and a wall of links.
 */
export function WaitingElsewhere({ waiting }: { waiting: ElsewhereWaiting }) {
  const [open, setOpen] = useState(false);
  const [all, setAll] = useState(false);
  if (!waiting.total) return null;
  const sorted = alsoWaitingList(waiting);
  const shown = all ? sorted : sorted.slice(0, ALSO_WAITING_SHOWN);
  const more = sorted.length - shown.length;
  const n = waiting.projects.length;
  return (
    <div className="dq-also" data-testid="decision-inbox-elsewhere">
      <span>Also waiting:</span>{" "}
      <button type="button" className="linklike" aria-expanded={open} onClick={() => setOpen((v) => !v)}
              data-testid="decision-also-toggle">
        {waiting.total} in {n ? `${n} other project${n === 1 ? "" : "s"}` : "other projects"} {open ? "‹" : "›"}
      </button>
      {open ? (
        <ul className="dq-also-list" data-testid="decision-also-list">
          {shown.map((p) => (
            <li key={p.id}>
              <a href={p.href} data-testid={`decision-inbox-elsewhere-${p.id}`}>{p.name}</a>
              <span className="dq-also-count"> · {p.count}</span>
            </li>
          ))}
          {more > 0 ? (
            <li>
              <button type="button" className="linklike dq-also-more" onClick={() => setAll(true)}
                      data-testid="decision-also-more">+ {more} more</button>
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}

/**
 * "To set up: Pick a prod host ›" — what only a person can set up, counted in Needs you (X-14),
 * as ONE quiet line in the "Also waiting" line's scale, never a box. Each item it knows is a link
 * to where it is set; the poll lists only the first five things, so the rest (the count says
 * they exist, the list was full of decisions) is one link to the project page, whose Needs you
 * lists every one of them, never an orphan "+1 more".
 */
export function SetupLine({ items, count, projectId }: {
  items: { id: string; title: string; href?: string; kind?: string; roleKey: string | null; decideInline: boolean }[];
  count: number;
  projectId: string;
}) {
  const rest = Math.max(0, count - items.length);
  if (!items.length && !rest) return null;
  return (
    <p className="dq-also" data-testid="decision-inbox-setup"
       title="Only you can set these up. They wait here and on the bell until they are set.">
      <span>To set up<span className="sr-only">, only you can do these</span>:</span>
      {items.map((i) => (
        <React.Fragment key={i.id}>
          {" "}
          <button type="button" className="linklike" data-testid={`decision-setup-${i.id}`}
                  onClick={() => navigate(reviewPatch(i))}>{i.title} <span aria-hidden>›</span></button>
        </React.Fragment>
      ))}
      {rest ? (
        <>
          {" "}
          <button type="button" className="linklike" data-testid="decision-setup-more"
                  onClick={() => navigate({ area: "project", project_id: projectId, tab: null, view: null,
                                            focus: null, decision: null })}>
            {items.length ? `+${rest} more` : `${rest} on the project page`} <span aria-hidden>›</span>
          </button>
        </>
      ) : null}
    </p>
  );
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

  // "all" (an old link) opens Needs you: there is no All tab any more. A link that names a
  // decision opens the tab that decision is under, so it is never hidden by the tab.
  const wantedTab = useMemo(() => {
    const hit = wanted ? feed.items.find((d) => d.id === wanted) : undefined;
    return hit ? decisionAttention(hit) : null;
  }, [feed.items, wanted]);
  const [tabChosen, setTabChosen] = useState(false);
  useEffect(() => { setTabChosen(false); }, [wanted]);
  const tab: Exclude<DecisionInboxFilter, "all"> = !tabChosen && wantedTab ? wantedTab
    : filter === "all" ? "needs_action" : filter;
  const visible = useMemo(
    () => filterDecisionInbox(feed.items, tab),
    [feed.items, tab],
  );
  // ONE COUNT WITH THE BELL (X-14): the bell counts decisions AND what only a person can set up
  // on this project (a prod host, a value nothing else sets — the project's Needs you), from the
  // one poll (/v1/agents/live `attention`). Needs action counts and lists the same things, so the
  // bell never says 1 over a queue that says 0.
  const attention = useAttention(projectId);
  const setupItems = useMemo(() => (attention?.items ?? []).filter((i) => i.kind === "setup"), [attention]);
  const setupCount = attention?.setup ?? setupItems.length;
  const actionableCount = useMemo(
    () => filterDecisionInbox(feed.items, "needs_action").length,
    [feed.items],
  ) + setupCount;
  const summary = usePxRead<ProjectsSummary>(PX_PATHS.summary(), parseSummary, { everyMs: 30_000 });
  const elsewhere = useMemo(
    () => waitingElsewhere(summary.state === "ready" ? summary.data : null, projectId, orgId),
    [summary, projectId, orgId],
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
    setTabChosen(true);
    setFilter(next);
    writeFilterToLocation(next, orgId);
  };

  return (
    <section
      aria-labelledby="decision-inbox-title"
      className="panel-stack decision-inbox-section"
      data-area="decisions"
      data-decision-filter={tab}
      data-testid="decision-inbox"
      id="decision-inbox"
      tabIndex={-1}
    >
      <div className="section-head">
        <div>
          <span className="area-name">Decision Queue</span>
          <h2 id="decision-inbox-title">
            Agents proposed, you decide.
          </h2>
        </div>
      </div>

      {/* THREE TABS (approvals wireframe B): what waits on you, what expired and can still be
          answered, what is done. No "All" (it mixed the three), and no "+67" inside a tab: what
          waits in other projects is its own line below. The paragraph on deadlines that headed
          the page is the Expired tab's tooltip. */}
      <div
        className="dq-filter"
        role="tablist"
        aria-label="Decision inbox filter"
        data-testid="decision-inbox-filter"
      >
        <button
          type="button"
          role="tab"
          aria-selected={tab === "needs_action"}
          className={`dq-filter-btn${tab === "needs_action" ? " active" : ""}`}
          data-testid="decision-filter-needs-action"
          title="Waiting on you now: the same count as the bell and the Command Center."
          onClick={() => applyFilter("needs_action")}
        >
          Needs you
          <span className="dq-filter-count">{actionableCount}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "expired"}
          className={`dq-filter-btn${tab === "expired" ? " active" : ""}`}
          data-testid="decision-filter-expired"
          title={EXPIRED_TAB_TIP}
          onClick={() => applyFilter("expired")}
        >
          Expired
          <span className="dq-filter-count">{expiredCount}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "resolved"}
          className={`dq-filter-btn${tab === "resolved" ? " active" : ""}`}
          data-testid="decision-filter-resolved"
          onClick={() => applyFilter("resolved")}
        >
          Resolved
          <span className="dq-filter-count">{resolvedCount}</span>
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
      ) : null}
      {feed.apiReachable ? <WaitingElsewhere waiting={elsewhere} /> : null}
      {feed.apiReachable && tab === "needs_action" && setupCount > 0 ? (
        <SetupLine items={setupItems} count={setupCount} projectId={projectId} />
      ) : null}
      {!feed.apiReachable ? null : visible.length === 0 ? (
        tab === "needs_action" && setupCount > 0 ? null : <DecisionInboxEmpty filter={tab} orgId={orgId} />
      ) : (
        <div className="dq" data-testid="decision-inbox-list">
          {/* THE ONE SOMEBODY WAS SENT HERE TO DECIDE. A workflow gate links here with the
              decision named; without this the operator arrives at a list and has to find it. */}
          {visible.map((decision) => (
            <DecisionCard
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
        human gate, they are not a history dump of already-resolved cards.
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
              // RESOLVED, as the button says, it opened "all". And a popstate, which the inbox
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

/* ================================================================================================
 * THE DECISION CARD: THREE PLAIN LINES (approvals wireframe B).
 *
 * The operator, on Approvals: "Do you think these things are really readable ... if not why do we
 * even display like this?" A card was an eyebrow ("Control plane proposes · merge"), a commit sha,
 * a build id, five severity boxes, a rationale, an evidence row and an effect paragraph before the
 * buttons. Now every card says three things:
 *
 *   1. what deciding does ("Merge change #24 · Lumen Pay tenant onboarding")    7 min ago
 *   2. one plain status line ("✓ Ready · 9 issues found, all fixed · Checks ›")
 *   3. the answer (primary + one secondary, the rest behind "…")    Workflow #27  Change #24
 *
 * Everything the card said before is kept, behind Checks / Details: nothing is lost, it is folded.
 * The same card sits on the Workflow run screen at the step that waits (ReviewAndDecide).
 * ============================================================================================== */

export type CardVariant = "merge_ready" | "merge_waiting" | "merge_blocked" | "merge_invalid" | "deploy" | "spec" | "plan"
  | "architecture" | "workflow_gate" | "refusal" | "self_fix" | "general";
export type CardLink = { label: string; href: string; testid: string };
export type CardLines = {
  variant: CardVariant;
  /** Line 1: what deciding does, never "review required". */
  title: string;
  /** Line 2's colour: ok (green), bad (red: something must be fixed first) or plain. */
  tone: "ok" | "bad" | "plain";
  /** Line 2, one phrase each. */
  phrases: string[];
  /** Line 2's last word: a page (href) or, with no href, the card's own Details. */
  more: { label: string; href: string | null };
  /** Line 3's right side: where the work is. */
  links: CardLink[];
};

const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

/** A question's subject and the rest: "Background job · Docs update: No GitHub credentials…"
 *  is titled by what comes before the first colon, when that is a short name. */
export function splitQuestion(q: string): { title: string; rest: string } {
  const text = String(q ?? "").trim();
  // The first ": " that ends a name: never inside quotes or brackets ("on “Hot file: x.go”"),
  // and followed by a sentence, not the rest of the name ("gwlint: automate tenant onboarding").
  let depth = 0;
  let quoted = false;
  for (let i = 0; i < text.length - 2 && i <= 140; i += 1) {
    const ch = text[i];
    if ("“([{".includes(ch)) depth += 1;
    else if ("”)]}".includes(ch)) depth = Math.max(0, depth - 1);
    else if (ch === '"') quoted = !quoted;
    if (depth === 0 && !quoted && ch === ":" && text[i + 1] === " " && i >= 8 && /^[A-Z0-9“"'‘]/.test(text.slice(i + 2))) {
      return { title: shortPhrase(text.slice(0, i), 100), rest: text.slice(i + 2).trim() };
    }
  }
  return { title: shortPhrase(text, 110), rest: "" };
}

/** CI that passed, in any of the words the engine uses for it. */
export const ciPassed = (status: string): boolean => /^(passed|success|succeeded|green|ok)$/i.test(status.trim());

/** A CI hold in words: "CI could not be read (403)", never the HTTP client's error and its URL. */
export function heldWords(reason: string): string {
  const r = String(reason ?? "").trim();
  const head = r.split(/:\s/)[0].replace(/[.\s]+$/, "");
  const code = /\b([45]\d\d)\b/.exec(r.slice(head.length))?.[1];
  return shortPhrase(code && head !== r ? `${head} (${code})` : head || r, 80);
}

/** A reason the card can say: words, or a code it knows; an unknown code says nothing. */
function knownReasonWords(reason: string | null | undefined): string {
  const r = String(reason ?? "").trim();
  // A code, with or without spaces ("failure_needs_person", "failure owner.retry").
  if (/^[a-z0-9_.: -]+$/.test(r) && /[_.]/.test(r) && !REASON_WORDS[r]) return "";
  return reasonInWords(r);
}

/** One phrase of the status line: whole words, no trailing full stop, never a paragraph. */
export function shortPhrase(text: string, max = 110): string {
  const t = String(text ?? "").replace(/\s+/g, " ").trim().replace(/[.:;,]+$/, "");
  if (t.length <= max) return t;
  const cut = t.slice(0, max);
  const at = cut.lastIndexOf(" ");
  return `${(at > max / 2 ? cut.slice(0, at) : cut).replace(/[,;:·\s-]+$/, "")}…`;
}

const recText = (r: Record<string, unknown>, k: string): string => (typeof r[k] === "string" ? (r[k] as string).trim() : "");

function prNumberOf(url: string): string {
  const m = /\/pull\/(\d+)/.exec(url);
  return m ? m[1] : (url.split("/").pop() ?? "");
}

function isDeployDecision(r: Record<string, unknown>): boolean {
  const action = recText(r, "action");
  if (["review_deployment", "deploy_host", "deploy_stack"].includes(action)) return true;
  return !action && !!recText(r, "workflow_run_id") && ["deploy", "redeploy"].includes(recText(r, "capability"));
}

function isSpecDecision(decision: DecisionFeedItem): boolean {
  const r = decision.recommendation;
  if (decision.prd) return true;
  if (recText(r, "action") === "execute_agent_tool" && (r.start_code_task === true || r.tool === "publish_document")) return true;
  return !recText(r, "action") && recText(r, "capability") === "spec_review";
}

/** The open must-fix findings of a blocked packet, in words: what the validator said, not a rule id. */
export function mustFixWords(p: ReviewPacket): string[] {
  return p.findings
    .filter((f) => (f.status === "open" || f.status === "reverted")
      && (f.blocks || (!f.followup && !isReviewFinding(f) && (f.severity === "P0" || f.severity === "P1"))))
    // The validator's own message; else the rule, in words ("no inlined secrets"); never the
    // id alone. A rulebook's "why" is a paragraph, so it is the last resort.
    .map((f) => shortPhrase(plainSentence(f.title
      || (f.source.rule_id || f.source.name).replace(/[-_]+/g, " ").trim() || f.why || f.id), 60));
}

/** Line 2 of a merge with a review packet: the verdict in counts and words; P0–P3 behind Checks. */
function packetPhrases(p: ReviewPacket, rec: Record<string, unknown>): { tone: CardLines["tone"]; phrases: string[] } {
  const t = packetTally(p);
  if (p.verdict.state === "invalid") {
    return { tone: "bad", phrases: [`Packet invalid: ${plural(p.verdict.reasons.length, "problem")}`, "rebuild it before merging"] };
  }
  if (p.verdict.state === "blocked") {
    const words = mustFixWords(p);
    if (words.length) {
      const shown = words.slice(0, 3).join(", ") + (words.length > 3 ? `, +${words.length - 3} more` : "");
      return { tone: "bad", phrases: [`${plural(words.length, "must-fix issue")} open: ${shown}`] };
    }
    const why = rerunPending(p) ? "re-checking after your change"
      : p.verdict.reasons.includes("rerun_failed") ? "the re-run failed, rebuild the packet"
      : p.verdict.reasons.includes("stale_evidence") ? "the checks ran on an older commit"
      : p.verdict.reasons.some((r) => r.startsWith("unexplained_hunk")) ? "an unexplained change in the original file"
      : "not ready";
    return { tone: "bad", phrases: [`Blocked: ${why}`] };
  }
  // A review finding BOSS moved to a follow-up is said as a follow-up, not as an issue found.
  const found = SEVS.reduce((n, s) => n + t[s].found, 0) + t.review.found - t.review.deferred;
  const open = SEVS.reduce((n, s) => n + t[s].open, 0) + t.review.open - t.review.deferred;
  // A review finding suppressed or waived is said with the waived ones, never as fixed.
  const waived = SEVS.reduce((n, s) => n + t[s].waived + t[s].overridden, 0)
    + p.findings.filter((f) => isReviewFinding(f) && (f.status === "suppressed" || f.status === "waived")).length;
  // ONE count for follow-ups: the engine's (the number its own question says), else the packet's.
  const followups = followUpsOf(rec)?.count ?? t.review.deferred;
  return {
    tone: "ok",
    phrases: [
      "✓ Ready",
      !found ? "no issues found"
        : open > 0 ? `${plural(found, "issue")} found, ${open} open, none blocking`
        : `${plural(found, "issue")} found, all fixed${waived ? " or waived" : ""}`,
      followups ? `${plural(followups, "follow-up")} for later` : "",
    ].filter(Boolean),
  };
}

/** Line 2 of a merge without a packet: failing checks, must-haves, CI, follow-ups. */
function mergePhrases(rec: Record<string, unknown>): { tone: CardLines["tone"]; phrases: string[] } {
  const split = splitChecksFailing(checksFailingOf(rec));
  const oldTests = split.oldTestsUnknown ? "old tests: unknown" : "";
  if (split.real) {
    return { tone: "bad", phrases: [
      `Checks failing: ${failingGateLabels(rec) || "see Checks"}`,
      split.real.security ? "security finding open" : "",
      oldTests,
      "merges only with your reason",
    ].filter(Boolean) };
  }
  const mh = mustHavesOf(rec);
  const unmet = mh ? mh.items.filter((i) => i.status !== "met" && !isAdvisory(i)).length : 0;
  const mustHaves = !mh ? ""
    : mh.verdict === "not_checked" || mh.verdict === "not_run" || !mh.items.length ? "must-haves not checked"
    : unmet ? `${unmet} of ${mh.items.length} must-haves not met` : "";
  const ci = recText(rec, "validation_status").replaceAll("_", " ");
  // "✓ Ready" only when nothing says otherwise: CI that timed out or failed is said instead.
  const ciOk = !ci || ciPassed(ci);
  const fu = followUpsOf(rec);
  const ready = !mustHaves && ciOk;
  return {
    tone: ready ? "ok" : "plain",
    phrases: [
      ready ? "✓ Ready" : "",
      mustHaves,
      ci ? `CI ${ci}` : "",
      oldTests,
      fu ? `${plural(fu.count, "follow-up")} for later` : "",
    ].filter(Boolean),
  };
}

/**
 * WHAT A CARD SAYS, IN THREE LINES. Pure: the card component draws it; tests read it directly.
 * The engine's own records (packet, checks_failing, followups, acceptance, prd, gate) are the
 * only sources; nothing is invented here.
 */
export function decisionCardLines(decision: DecisionFeedItem, projectId: string, orgId?: string): CardLines {
  const rec = decision.recommendation || {};
  const action = recText(rec, "action");
  // The work's name, never a paragraph: a brief pasted as a title is cut at a word.
  const title = shortPhrase(rootTitle(plainTitle(recText(rec, "title"))), 70);
  const prUrl = recText(rec, "pull_request_url");
  const runId = recText(rec, "workflow_run_id");
  const runNo = typeof rec.run_no === "number" ? rec.run_no
    : typeof rec.workflow_run_no === "number" ? rec.workflow_run_no
    : typeof rec.run_number === "number" ? rec.run_number : null;
  const app = recText(rec, "app_name");
  const env = recText(rec, "environment");
  const packet = packetOf(rec);
  const links: CardLink[] = [];
  const addLink = (l: CardLink) => { if (l.href && !links.some((x) => x.href === l.href || x.testid === l.testid)) links.push(l); };
  const details = { label: "Details", href: null as string | null };

  let variant: CardVariant = "general";
  const asked = splitQuestion(plainSentence(decision.question));
  let cardTitle = asked.title;
  let tone: CardLines["tone"] = "plain";
  let phrases: string[] = [];
  let more = details;

  if (decision.kind === "refusal") {
    variant = "refusal";
    const g = decision.guidance;
    cardTitle = g?.what_happened ? plainSentence(g.what_happened) : cardTitle;
    tone = "bad";
    phrases = [g?.what_to_do ? shortPhrase(g.what_to_do, 140) : shortPhrase(reasonInWords(decision.reason) || "BOSS refused to spend")];
    if (g && isAreaId(g.area)) addLink({ label: g.link_label || "Fix it", href: linkTo({ area: g.area }, orgId), testid: "decision-link-fix" });
  } else if (isSelfFixDecision(decision) && cardOf(rec)) {
    variant = "self_fix";
    const c = cardOf(rec)!;
    cardTitle = `Install a BOSS self-fix${c.title ? ` · ${shortPhrase(c.title, 70)}` : ""}`;
    tone = c.tests?.red_on_base && c.tests?.green_with_fix ? "ok" : "plain";
    phrases = [
      c.tests?.red_on_base && c.tests?.green_with_fix ? "✓ Failing test now passes" : "",
      typeof c.tier === "number" ? `tier ${c.tier}` : "",
      c.confidence?.level ? `${c.confidence.level} confidence` : "",
      typeof c.cost_usd === "number" ? usd(c.cost_usd) : "",
    ].filter(Boolean);
  } else if (packet) {
    const links0 = packetLinks(projectId, packet);
    const said = packetPhrases(packet, rec);
    const split = splitChecksFailing(checksFailingOf(rec));
    variant = packet.verdict.state === "invalid" ? "merge_invalid"
      : packet.verdict.state === "blocked" || split.real ? "merge_blocked" : "merge_ready";
    cardTitle = packetTitle(packet, recText(rec, "title"));
    if (split.real && packet.verdict.state === "ready") {
      const m = mergePhrases(rec);
      tone = m.tone; phrases = m.phrases;
    } else {
      tone = said.tone; phrases = said.phrases;
      if (split.oldTestsUnknown) phrases.push("old tests: unknown");
    }
    more = links0.change ? { label: "Checks", href: `${links0.change}?tab=checks` } : { label: "Checks", href: null };
    if (links0.run) addLink({ label: `Workflow${packet.work.run_no != null ? ` #${packet.work.run_no}` : ""}`, href: links0.run, testid: "decision-link-run" });
    if (links0.change) addLink({ label: `Change #${packet.work.change}`, href: links0.change, testid: "decision-link-change" });
  } else if (action === "review_pull_request" || action === "github.community.merge_authorization") {
    const n = prUrl ? prNumberOf(prUrl) : "";
    const m = mergePhrases(rec);
    variant = m.tone === "bad" ? "merge_blocked" : m.tone === "ok" ? "merge_ready" : "merge_waiting";
    cardTitle = `Merge ${changeRef(prUrl, n, true)}${title ? ` · ${title}` : ""}`;
    tone = m.tone; phrases = m.phrases;
    const page = prUrl ? changeHref(prUrl) : null;
    // A GitHub PR has no BOSS Checks page: line 2 opens this card's Details, and says so.
    more = page && isLocalChangeUrl(prUrl) ? { label: "Checks", href: `${page}?tab=checks` } : { label: "Details", href: null };
  } else if (isDeployDecision(rec)) {
    variant = "deploy";
    const host = recText(rec, "host");
    const what = app || title || "this change";
    cardTitle = `Deploy ${what}${env ? ` to ${env}` : host ? ` to ${host}` : ""}`;
    const ci = recText(rec, "validation_status");
    tone = ciPassed(ci) ? "ok" : "plain";
    phrases = [
      tone === "ok" ? "✓ Tests passed" : "",
      host && env ? host : "",
      action === "deploy_host" ? "your own host, no provider cost" : "",
    ].filter(Boolean);
  } else if (isSpecDecision(decision)) {
    variant = "spec";
    const prd = decision.prd;
    const named = prd?.title && prd.title !== "PRD" ? shortPhrase(prd.title, 70) : title;
    cardTitle = `Approve the spec${named ? ` · ${named}` : ""}`;
    const m = prd?.meeting;
    const skipped = specGateSkipped(rec);
    phrases = [
      m ? (m.dissent.length
        ? `Panel: ${Math.max(0, m.positions.length - m.dissent.length)} for, ${m.dissent.length} dissented`
        : "Panel agreed") : "",
      m && m.positions.length && !m.dissent.length ? plural(m.positions.length, "agent") : "",
      skipped ? shortPhrase(skipped, 90)
        : rec.start_code_task === true || rec.tool === "publish_document" || recText(rec, "capability") === "spec_review"
          ? "build starts when you approve" : "",
    ].filter(Boolean);
    more = { label: "Read spec", href: null };
    if (m?.href) addLink({ label: "Meeting", href: m.href, testid: "decision-link-meeting" });
  } else if (action === "review_plan_draft" || action === "resume_code_task") {
    variant = "plan";
    const subject = title || shortPhrase(asked.rest, 70);
    cardTitle = action === "resume_code_task" ? `Resume the work${subject ? ` · ${subject}` : ""}` : `Review the plan${subject ? ` · ${subject}` : ""}`;
    phrases = [action === "resume_code_task" ? "paused, resumes when you approve" : "code starts when you approve"];
    more = { label: action === "resume_code_task" ? "Details" : "Read plan", href: null };
  } else if (action === "review_architecture" || (!action && recText(rec, "capability") === "architecture")) {
    variant = "architecture";
    cardTitle = `Approve the architecture${app ? ` · ${app}` : title ? ` · ${title}` : ""}`;
    phrases = ["every build after it gets the document"];
    more = { label: "Read it", href: null };
  } else if (action === "resume_workflow") {
    variant = "workflow_gate";
    const gate = workflowGateCard(rec)!;
    cardTitle = workflowGateSubtitle(gate);
    phrases = [
      gate.producedByAgent ? `from the ${gate.producedByAgent}` : gate.producedByTitle ? `from “${gate.producedByTitle}”` : "",
      gate.summary ? shortPhrase(gate.summary, 100) : "the workflow waits for your answer",
    ].filter(Boolean);
  } else if (!action && runId) {
    variant = "workflow_gate";
    const step = recText(rec, "step_label") || recText(rec, "capability").replaceAll("_", " ") || "this";
    // The engine's question names the step and its case ("Email the newsletter — Weekly
    // newsletter"); only a bare one is replaced by the step's own name.
    const q = plainSentence(decision.question);
    cardTitle = q && !/^review required$/i.test(q) && q.length <= 120 ? q
      : `Approve the “${step}” step${app ? ` · ${app}` : ""}`;
    phrases = ["the workflow waits for this", env && ["monitor", "preview"].includes(recText(rec, "capability")) ? env : ""].filter(Boolean);
  } else {
    // ANY OTHER QUESTION: its subject is the title ("Background job · Docs update"), what follows
    // it the status line; else the reason, in words the card can say.
    const line = asked.rest || knownReasonWords(decision.reason) || (recommendationLines(decision)[0] ?? "");
    phrases = line && !/^review required$/i.test(line) ? [shortPhrase(plainSentence(line), 120)] : [];
  }

  // A RELEASE THAT SHIPS MORE THAN ITS OWN COMMITS (engine release_scope): said in counts; red
  // when approving it needs a reason.
  const release = rec.release_commits && typeof rec.release_commits === "object"
    ? rec.release_commits as { known?: boolean; commits?: Array<{ own?: boolean; reviewed?: boolean; approval?: string }>; needs_reason?: boolean }
    : null;
  if (release) {
    const commits = Array.isArray(release.commits) ? release.commits : [];
    const unreviewed = commits.filter((c) => !c.own && !c.reviewed).length;
    const rejected = commits.filter((c) => c.approval === "rejected").length;
    phrases.push(...(release.known === false ? ["what it ships could not be listed"] : [
      plural(commits.length, "commit"),
      rejected ? `${rejected} from rejected runs` : "",
      unreviewed ? `${unreviewed} never reviewed` : "",
    ].filter(Boolean)));
    if (release.needs_reason) tone = "bad";
  }

  // ONE PHRASE EACH (wireframe B): the cost note, the CI hold, the re-raises.
  const estimated = decision.costSummary?.estimated_usd_micros;
  if (typeof estimated === "number" && estimated > 0 && variant !== "self_fix") phrases.push(`est. ${formatMicros(estimated)}`);
  if (decision.held?.reason) {
    // HELD BY CI: never "✓ Ready" beside "Checks not read"; the hold is what the line says.
    // Nor "CI passed" beside "CI could not be read": the hold is the one word on CI.
    phrases = phrases.filter((p) => p !== "✓ Ready" && !/^CI /.test(p));
    if (tone === "ok") tone = "plain";
    if (variant === "merge_ready") variant = "merge_waiting";
    phrases.push(heldWords(decision.held.reason));
  }
  const occurrences = typeof rec.occurrences === "number" ? rec.occurrences : 0;
  if (occurrences > 1) phrases.push(`same cause re-raised ${occurrences}×`);

  // WHERE THE WORK IS: the run, the change (or PR), the meeting.
  if (runId) addLink({ label: `Workflow${runNo != null ? ` #${runNo}` : ""}`, href: linkTo({ area: "delivery_workflows", wfr: runId }, orgId), testid: "decision-link-run" });
  if (prUrl && !links.some((l) => l.testid === "decision-link-change")) {
    const n = prNumberOf(prUrl);
    addLink({ label: changeRef(prUrl, n), href: changeHref(prUrl), testid: isLocalChangeUrl(prUrl) ? "decision-link-change" : "decision-link-pr" });
  }
  return { variant, title: cardTitle, tone, phrases, more, links };
}

/** Where a deploy would go, read now (DeployDestination), said as one phrase of line 2. */
function DeployPhrase({ recommendation, projectId }: { recommendation: Record<string, unknown>; projectId: string }) {
  return <DeployDestination recommendation={recommendation} projectId={projectId} inline />;
}

function CardLinks({ links, detailsToggle }: { links: CardLink[]; detailsToggle?: React.ReactNode }) {
  if (!links.length && !detailsToggle) return null;
  return (
    <span className="dq-links" data-testid="decision-links">
      {links.map((l) => (
        <a key={l.testid + l.href} href={l.href} data-testid={l.testid} {...linkTarget(l.href)}>{l.label}</a>
      ))}
      {detailsToggle}
    </span>
  );
}

export type DecisionCardProps = {
  decision: DecisionFeedItem;
  tasks?: TaskFeedItem[];
  roles?: RoleFeedItem[];
  projectId: string;
  actorId: string;
  orgId?: string;
  /** Sent here by a workflow gate that named this decision. */
  highlighted?: boolean;
  /** Drawn on the run it belongs to: no "Workflow #N" link back to the same page. */
  hideRunLink?: boolean;
};

/** One decision, as three lines: the Approvals list and the Workflow run screen draw this. */
export function DecisionCard({
  decision,
  tasks = [],
  roles = [],
  projectId,
  actorId,
  orgId,
  highlighted = false,
  hideRunLink = false,
}: DecisionCardProps) {
  const [open, setOpen] = useState(false);
  const rec = decision.recommendation;
  // Answerable, not only pending: see isAnswerableDecision. A PR parked in a dead-end
  // "Request changes" is answerable again, and so is an expired hold decision; the card
  // draws their buttons.
  const isPending = isAnswerableDecision(decision);
  const isApproved = decision.status === "approved";
  const article = {
    "data-testid": "decision-row",
    "data-decision-status": decision.status,
    // Sent here by a workflow gate that named this one: marked, and brought into view, so
    // the operator does not arrive at a list and have to find what they were sent to decide.
    "data-highlighted": highlighted || undefined,
    id: `decision-${decision.id}`,
    ref: (el: HTMLElement | null) => { if (el && highlighted) el.scrollIntoView({ block: "center" }); },
  };

  // ONE APPROVAL FOR A MULTI-REPO CHANGE: its own card (every PR, review, cross-repo check, cost,
  // rollback) and the three answers the engine offers for a set; no per-PR approval.
  if (isChangeSetDecision(decision)) {
    return (
      <article className="dq-row dq-row-changeset" {...article}>
        <ChangeSetApproval decision={decision} projectId={projectId} actorId={actorId} pending={isPending} />
      </article>
    );
  }

  const drawn = decisionCardLines(decision, projectId, orgId);
  const lines = hideRunLink ? { ...drawn, links: drawn.links.filter((l) => l.testid !== "decision-link-run") } : drawn;
  const packet = packetOf(rec);
  const detailsId = `decision-details-${decision.id}`;
  const toggle = () => setOpen((v) => !v);
  const detailsButton = (label: string, testid: string) => (
    <button type="button" className="linklike dq-details-toggle" aria-expanded={open} aria-controls={detailsId}
            onClick={toggle} data-testid={testid}>{label}</button>
  );
  // Line 2's last word opens the page it names (a change's Checks) or this card's Details; when
  // it leaves the page, line 3 keeps a Details toggle so the folded facts are one press away.
  const moreControl = lines.more.href
    ? <a href={lines.more.href} data-testid="decision-status-more">{lines.more.label} ›</a>
    : detailsButton(`${lines.more.label} ›`, "decision-status-more");
  const detailsInLinks = lines.more.href ? detailsButton(open ? "Hide details" : "Details", "decision-details-toggle") : null;
  const when = deadlineLabel(decision.deadlineAt);
  const occurrences = typeof rec.occurrences === "number" ? rec.occurrences : 0;
  const autoApproved = isApproved && decision.approvedBy === "auto_approval";

  const body = (
    <DecisionCardDetails decision={decision} tasks={tasks} roles={roles} projectId={projectId} orgId={orgId}
                         isPending={isPending} />
  );

  if (!isPending) {
    // RESOLVED: one line, who answered and when; the card's facts behind Details.
    return (
      <article className="dq-row dq-card dq-card-resolved" data-variant={lines.variant} {...article}>
        <div className="dq-card-head">
          <h3 className="dq-title" data-testid="decision-title">{lines.title}</h3>
          <span className={`dq-verdict ${isApproved || decision.status === "executed" ? "green" : decision.status === "rejected" ? "red" : "muted"}`}>
            {autoApproved ? "AUTO-APPROVED" : decision.status.toUpperCase()}
          </span>
          <span className="dq-note" data-testid="decision-resolved-note">{decisionStatusDetail(decision)}</span>
          {occurrences > 1 ? (
            <span className="cap-badge tone-amber" title="The same cause re-raised this while it stayed open">
              seen {occurrences}×
            </span>
          ) : null}
          <CardLinks links={lines.links} detailsToggle={detailsButton(open ? "Hide details" : "Details", "decision-details-toggle")} />
          <span className="dq-when"><ClientRelativeTime iso={decision.createdAt} empty="" as="span" /></span>
        </div>
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
        <div id={detailsId} className="dq-details" hidden={!open} data-testid="decision-details">{body}</div>
      </article>
    );
  }

  // A MERGE WITH A REVIEW PACKET: an invalid packet holds Approve (its problems are in Checks);
  // a blocked one merges only as the reasoned "Merge anyway"; old tests nobody can name read so.
  const hold = packet ? packetHold(packet) ?? decision.held ?? null : null;
  const answerable = packet
    ? (hold ? { ...decision, held: hold }
      : oldTestsUnknownDecision(blockedPacketDecision(decision, packet), splitChecksFailing(checksFailingOf(rec))))
    : decision;

  return (
    <article className={`dq-row dq-card${lines.tone === "bad" ? " dq-card-bad" : ""}${packet ? " dq-row-packet" : ""}${isSelfFixDecision(decision) ? " dq-row-selffix" : ""}`}
             data-variant={lines.variant} {...article}>
      <div className="dq-card-head">
        <h3 className="dq-title" data-testid="decision-title">{lines.title}</h3>
        {when ? (
          <span className={`dq-when${decisionAttention(decision) === "expired" ? " late" : ""}`} data-testid="decision-deadline">{when}</span>
        ) : (
          <span className="dq-when" data-testid="decision-when"><ClientRelativeTime iso={decision.createdAt} empty="" as="span" /></span>
        )}
      </div>
      <p className={`dq-status ${lines.tone}`} data-testid="decision-status">
        {lines.phrases.map((p, i) => (
          <React.Fragment key={i}>{i ? " · " : null}<span className="dq-phrase">{p}</span></React.Fragment>
        ))}
        {lines.variant === "deploy" ? <DeployPhrase recommendation={rec} projectId={projectId} /> : null}
        {lines.phrases.length ? " · " : null}{moreControl}
      </p>
      <div className="dq-card-act">
        {packet && packet.verdict.state === "invalid" ? <RebuildPacket projectId={projectId} change={packet.work.change} /> : null}
        <DecisionActions decision={answerable} projectId={projectId} actorId={actorId} compact
                         reviseFirst={["spec", "plan", "architecture"].includes(lines.variant)} />
        <CardLinks links={lines.links} detailsToggle={detailsInLinks} />
      </div>
      <div id={detailsId} className="dq-details" hidden={!open} data-testid="decision-details">{body}</div>
    </article>
  );
}

/** Everything a card said before the three lines, kept: behind Checks / Details. */
function DecisionCardDetails({
  decision, tasks, roles, projectId, orgId, isPending,
}: {
  decision: DecisionFeedItem; tasks: TaskFeedItem[]; roles: RoleFeedItem[]; projectId: string; orgId?: string;
  isPending: boolean;
}) {
  // A notice, not a question: BOSS refused to spend and the step has already failed. Its fix
  // is elsewhere (Govern, Setup), and the engine offers only Dismiss.
  const refusal = decision.kind === "refusal";
  const guidance = decision.guidance ?? null;
  const estimatedCost =
    typeof decision.costSummary.estimated_usd_micros === "number"
      ? decision.costSummary.estimated_usd_micros
      : undefined;
  const expired = decisionAttention(decision) === "expired";
  const linkedTask = decision.taskId
    ? tasks.find((task) => task.id === decision.taskId)
    : undefined;
  const ownerRole = linkedTask?.ownerRoleId
    ? roles.find((role) => role.id === linkedTask.ownerRoleId)
    : undefined;
  const who = ownerRole?.displayName ?? "";
  const impact = decisionImpact(decision);
  const rec = decision.recommendation;
  // How many times the same cause has re-raised while this stayed open. Each occurrence used
  // to insert its own row, thirty identical "Model capacity exhausted" items, so severity
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
  // AML VIDEO POLISH [7]: scoped to the AML gate card, other workflows' excerpts are unchanged.
  const isAmlGateCard = gateCard?.workflowName === "AML Alert Investigation";
  const packet = packetOf(rec);
  const split = splitChecksFailing(checksFailingOf(rec));

  // A BOSS SELF-FIX: its own card (what broke, the change, the red→green test, the reviewers,
  // the tier); the answer is the card's line 3, so the content hash still binds the approval.
  if (isSelfFixDecision(decision) && cardOf(rec)) {
    return (
      <SelfFixCard card={cardOf(rec)!} projectId={projectId}
                   selfFixId={typeof rec.self_fix_id === "string" ? rec.self_fix_id : undefined} />
    );
  }

  // THE ENGINE'S OWN QUESTION, kept word for word when line 1 says it differently.
  const asked = plainSentence(decision.question);
  const askedShown = !refusal && asked && asked !== decisionCardLines(decision, projectId, orgId).title;
  return (
    <div className="dq-main">
      {askedShown ? <p className="dq-rationale dq-asked" data-testid="decision-question">Asked: {asked}</p> : null}
      {packet ? (
        // THE PACKET, FOLDED (validation-review SPEC §1.4–§1.5): verdict, P0–P3 and review counts,
        // the head commit, and the run · build · change links (the Build link lives only here).
        <ReviewPacketView packet={packet} projectId={projectId} fallbackTitle={typeof rec.title === "string" ? rec.title : ""}
                          compact bare failing={split.real ? failingGateLabels(rec) : ""} />
      ) : null}
      {/* A PR that opened with its checks failing: every gate and finding, security first. */}
      {split.real ? <ChecksFailing record={split.real} /> : null}
      {split.oldTestsUnknown ? <OldTestsUnknown why={split.oldTestsUnknown} /> : null}
      {/* Follow-ups only: a normal ready card, the list folded (engine quality_bar.py). */}
      {!packet && followUpsOf(rec) ? <FollowUps record={followUpsOf(rec)!} /> : null}
      {mustHavesOf(rec) ? <MustHaves data={mustHavesOf(rec)!} /> : null}
      {/* An expired card says so: "Approve and merge" on a pull request from weeks ago is a
          different act from the same button on one raised today. */}
      {expired ? (
        <p className="dq-rationale" data-testid="decision-expired-note">
          Waiting since{" "}
          <ClientRelativeTime iso={decision.createdAt ?? decision.deadlineAt ?? decision.updatedAt}
            empty="" as="span" />. Its deadline passed with nothing applied, so you can
          still answer it; the work it was about may have moved on.
        </p>
      ) : null}
      {refusal || packet ? null : isAmlGateCard && why ? (
        <div className="dq-rationale" data-testid="decision-subtitle">
          <DecisionCardExcerpt text={why} testid="dq-evidence-excerpt" />
        </div>
      ) : (
        <p className="dq-rationale" data-testid="decision-subtitle">
          {why || preview ? plainSentence(why || previewLead(preview!))
            : recommendationLines(decision).map((line, i) => (
              <span key={i} className="dq-line" data-testid="decision-subtitle-line">{plainSentence(line)}</span>))}
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
              {isAmlGateCard ? (
                <DecisionCardExcerpt text={gateCard.summary} testid="dq-output-excerpt" />
              ) : (
                <p>{gateCard.summary}</p>
              )}
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
      <HeldArguments recommendation={rec} />
      {/* AN AGENT'S TOOL REQUEST (BOSSAPP-848): exactly the tools, their tiers and why. */}
      {grantOf(rec) ? <GrantAccessCard card={grantOf(rec)!} /> : null}
      {/* THE RELEASE'S MONITORING PACK (engine monitoring_pack): approved with it. */}
      {monitoringPackOf(rec.monitoring_pack) ? <MonitoringPackCard pack={monitoringPackOf(rec.monitoring_pack)!} onCard /> : null}
      {decision.prd ? (
        // A MEETING'S PRD (BOSSAPP-929): rendered, with its meeting and its file in the repo.
        <PrdRecordView prd={decision.prd} testid="decision-prd" />
      ) : subject ? (
        <details className="decision-plan-details">
          <summary>
            Read the full document before approving
            <span className="dq-doc-size">
              {", "}
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
        <span className={`dq-impact ${impact.tone}`}>{refusal ? "notice" : impact.label}</span>
        {who ? <span className="dq-ev">proposed by {who}</span> : null}
        {linkedTask ? (
          <span className="dq-ev">
            {/* The ask, not a panel's prompt scaffolding (lib/directive-subject.ts). */}
            T-{decision.taskId?.slice(-4)}, {displayTitle(linkedTask.title, "task").slice(0, 44)}
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
        {refusal ? (
          <span className="dq-ev" title={decision.question}>
            {decision.question.slice(0, 60)}{decision.question.length > 60 ? "…" : ""}
          </span>
        ) : null}
        {/* On the PENDING card, where it matters: thirty re-raises of one cause is the
            difference between a blip and an outage. */}
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
          <span className="dq-why-label">Why</span>{" "}
          {isAmlGateCard ? (
            <DecisionCardExcerpt text={reasonInWords(decision.reason)} testid="dq-why-excerpt" as="span" />
          ) : (
            reasonInWords(decision.reason)
          )}
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
          <p className="dq-effect">{approvalEffectLabel(decision.recommendation, offeredActions(decision))}</p>
          <LocalChangeLink recommendation={decision.recommendation} />
          <DeployDestination recommendation={decision.recommendation}
                             projectId={projectId} />
        </>
      ) : null}
      {isPending ? <WhyAsked recommendation={decision.recommendation} projectId={projectId} /> : null}
    </div>
  );
}

/**
 * THE BUTTONS THE ENGINE SAYS WORK, AND NO OTHERS.
 *
 * Every pending card used to get Approve, Reject, Escalate and Request changes. Escalate posted
 * an action the server never accepted, so it always failed; Approve on a budget or capacity
 * refusal recorded an answer nothing reads; Request changes on a workflow gate parked the run
 * for ever. The engine now decides the list (services/decision_actions.py) and refuses
 * anything else, so this draws exactly `allowedActions`, the primary ones as buttons, the
 * ones that need a note or a name behind "More".
 */
export function DecisionActions({ decision, projectId, actorId, compact = false, reviseFirst = false }: {
  decision: DecisionFeedItem; projectId: string; actorId: string;
  /** THE CARD'S LINE 3 (approvals wireframe B): a primary and one secondary answer drawn; the
   *  rest behind "…", each with the same reason prompt and the same form fields as before. */
  compact?: boolean;
  /** A spec, plan or architecture: "Request changes…" is the secondary answer (wireframe B). */
  reviseFirst?: boolean;
}) {
  const offers = offeredActions(decision).filter((a) => a.action !== "execute");
  // Two settles share the action name. Each button carries its own settle_at, or they
  // would submit the same value and React would reuse one key.
  const settles = offers.filter((a) => a.action === "settle" && a.settle_at);
  const direct = offers.filter((a) => !a.needs && a.action !== "settle");
  // AN APPROVAL THAT NEEDS A REASON (a merge with checks failing): drawn open, never behind
  // "More", with the reason box; sent as the forced merge the engine requires.
  const reasoned = offers.find((a) => a.action === "approve" && a.needs === "note");
  // …or a release that ships commits from rejected or unreviewed runs (engine release_scope).
  const shipsUnreviewed = Boolean(
    (decision.recommendation.release_commits as { needs_reason?: unknown } | undefined)
      ?.needs_reason);
  // …or a workflow gate whose answer is a person's stated determination (the AML analyst
  // decision): the step says what to write.
  // …or a work item's review stage that asks for a comment (execution_policy, BOSSAPP-831): the
  // note is the reviewer's record, not an override, so it is not sent forced.
  const stageComment = decision.recommendation.action === "execution_stage";
  const reasonPrompt = typeof decision.recommendation.reason_prompt === "string"
    ? decision.recommendation.reason_prompt
    : stageComment ? "What did you check? Your comment is recorded on the stage." : "";
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
  // A TYPED QUESTION (BOSSAPP-832): the answer is its form, not Approve; once answered, the
  // typed answer is what the card shows.
  const typedForm = parseForm(decision.formSchema);
  const [answered, setAnswered] = useState<string | null>(null);
  // AN AGENT'S PROPOSED SECRET (BOSSAPP-871): the person types the value; Approve is Store.
  const proposal = secretProposalOf(decision.recommendation);
  if (proposal && (decision.status === "pending" || decision.status === "delegated")) {
    if (answered) return <p className="tq-said" data-testid="secret-proposal-sent">{answered === "stored" ? "Stored — the agent can use it now." : "Declined — the agent was told."}</p>;
    return <SecretProposalCard decisionId={decision.id} proposal={proposal} actorId={actorId} onDone={setAnswered} />;
  }
  // THE INFRA AGENT'S DELETION (BOSSAPP-1156, S4): exact items, GB, the rule proposal, one tap.
  if (decision.kind === "storage_delete" && (decision.status === "pending" || decision.status === "delegated")) {
    return <StorageDeleteFromDecision decision={decision} projectId={projectId} actor={actorId} />;
  }
  if (typedForm) {
    if (answered) return <p className="tq-said" data-testid="typed-sent">{answered === "approved" ? "Answer sent — the agent carries on with it." : "Declined — the agent was told why."}</p>;
    return decision.status === "pending" || decision.status === "delegated"
      ? <TypedDecisionForm decisionId={decision.id} form={typedForm} projectId={projectId} actorId={actorId}
                           onAnswered={setAnswered} />
      : <TypedAnswer form={typedForm} answer={decision.answer} by={decision.answeredBy} />;
  }
  // OPTIONS WITH SIGNED EFFECTS (BOSSAPP-879): one Choose per option, its effects in words.
  if (optionsOf(decision.recommendation).length) {
    return <DecisionOptions decision={decision} projectId={projectId} actorId={actorId} offers={offers} />;
  }
  // "ALWAYS ALLOW THIS" (BOSSAPP-817): an Ask card about a tier 0-1 tool call may approve the
  // call AND keep allowing it for this agent; the engine said whether it may (always_allow_offer).
  const offer = (decision.status === "pending" || decision.status === "delegated")
    ? alwaysAllowOffer(decision.recommendation) : null;
  const directButton = (offer: DecisionActionOffer) => {
    // HELD BY CI (BOSSAPP-946): an approve CI still holds reads "Checks running…",
    // disabled, with the gate's words — never a press the gate refuses.
    const held = offer.action === "approve" ? decision.held ?? null : null;
    return (
      <SubmitButton permission="decision:approve"
        key={offer.action}
        className={`dq-btn ${offer.tone === "neutral" ? "neutral" : offer.tone}`}
        name="action"
        value={offer.action}
        disabled={held ? true : undefined}
        title={held ? held.reason : offer.effect || undefined}
        data-testid={`decision-${offer.action}`}
        data-held={held ? held.state : undefined}
      >
        {held ? held.label : offer.label}
      </SubmitButton>
    );
  };
  const settleButtons = settles.map((offer) => (
    <SubmitButton permission="decision:approve"
      key={offer.settle_at}
      className={`dq-btn ${offer.tone === "neutral" ? "neutral" : offer.tone}`}
      name="settle_at"
      value={offer.settle_at ?? ""}
      title={offer.effect || undefined}
      data-testid={`decision-settle-${offer.settle_at}`}
    >
      {offer.label}
    </SubmitButton>
  ));
  const reasonedBody = reasoned ? (
    <>
      {stageComment ? null : <input type="hidden" name="force" value="true" />}
      {Array.isArray(decision.recommendation.blocked_rows) && decision.recommendation.blocked_rows.length ? (
        // A blocked review packet: the open P0/P1 rows this merge goes past, one per line.
        <ul className="dq-blocked-rows" data-testid="decision-blocked-rows">
          {(decision.recommendation.blocked_rows as string[]).map((r) => <li key={r}>{r}</li>)}
        </ul>
      ) : null}
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
    </>
  ) : null;
  const heldNote = decision.held && direct.some((a) => a.action === "approve") ? (
    <span className="dq-held" data-testid="decision-held">{decision.held.reason}</span>
  ) : null;
  const whyNo = direct.some((a) => a.action === "reject") && !dismiss ? (
    <details className="dq-more dq-why" data-testid="decision-why">
      <summary title="Optional: tell BOSS why, before you reject">Why no?</summary>
      <div className="dq-more-body">
        <ReasonChips value={why} onChange={setWhy} />
        {/* THE REASON IN WORDS (BOSSAPP-1321): optional, kept on the decision and, for a
            plan, on its build, so the next plan reads why this one was refused. */}
        <textarea
          aria-label={`Why reject ${decision.id}`}
          className="textarea decision-note"
          name="reject_note"
          rows={2}
          maxLength={1000}
          placeholder="Why not? Optional — BOSS keeps it with the decision."
          data-testid="decision-reject-note"
        />
      </div>
    </details>
  ) : null;
  const reviseBody = revise ? (
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
  ) : null;
  const delegateBody = delegate ? (
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
  ) : null;
  const hidden = (
    <>
      <input type="hidden" name="decision_id" value={decision.id} />
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="actor" value={actorId} />
      {runId ? <input type="hidden" name="workflow_run_id" value={runId} /> : null}
      {startsWork ? <input type="hidden" name="starts_work" value="1" /> : null}
      {dismiss ? <input type="hidden" name="reject_label" value="Dismiss" /> : null}
      {contentHash ? <input type="hidden" name="content_hash" value={contentHash} /> : null}
      {settles.length > 0 && direct.length === 0 && !reasoned && !revise && !delegate ? (
        <input type="hidden" name="action" value="settle" />
      ) : null}
    </>
  );

  if (compact) {
    // PRIMARY + ONE SECONDARY. A reasoned merge ("Merge anyway…") is the secondary beside the
    // first plain answer ("Keep blocked"), and opens its reason box in place; the settles of a
    // legacy-exposure card are its answers and always show.
    const reviseShown = Boolean(reviseFirst && revise && !reasoned);
    const room = Math.max(0, 2 - settles.length - (reasoned || reviseShown ? 1 : 0));
    const shown = direct.slice(0, room);
    const folded = direct.slice(room);
    const foldedRevise = revise && !reviseShown;
    const overflow = folded.length > 0 || whyNo || foldedRevise || delegate;
    return (
      <>
      {offer ? <AlwaysAllowOption decisionId={decision.id} offer={offer} projectId={projectId} actorId={actorId} /> : null}
      <div className="im-contents" onClickCapture={onPress}>
      <DecisionResolveForm className="dq-actions dq-actions-compact">
        {hidden}
        {settleButtons}
        {shown.map(directButton)}
        {reasoned ? (
          <details className="dq-reasoned dq-reasoned-fold" data-testid="decision-reasoned-approve">
            <summary className="dq-btn reject" data-testid="decision-reasoned-toggle"
                     title={reasoned.effect || undefined}>{reasoned.label}…</summary>
            <div className="dq-more-body dq-pop">{reasonedBody}</div>
          </details>
        ) : null}
        {reviseShown ? (
          <details className="dq-reasoned dq-reasoned-fold" data-testid="decision-revise-fold">
            <summary className="dq-btn neutral" title={revise!.effect || undefined}>{revise!.label}…</summary>
            <div className="dq-more-body dq-pop">{reviseBody}</div>
          </details>
        ) : null}
        {/* A CI hold's words are on line 2 and in the held button's title: not a third time. */}
        {overflow ? (
          <details className="dq-more dq-overflow" data-testid="decision-more-actions">
            <summary aria-label="More answers" title="More answers">…</summary>
            <div className="dq-more-body dq-pop">
              {folded.map(directButton)}
              {whyNo}
              {foldedRevise || delegate ? (
                <details className="dq-more" data-testid="decision-revise-delegate">
                  <summary>{[foldedRevise ? "Request changes" : "", delegate ? "hand to someone else" : ""]
                    .filter(Boolean).join(", ").replace(/^h/, "H")}</summary>
                  <div className="dq-more-body">{foldedRevise ? reviseBody : null}{delegateBody}</div>
                </details>
              ) : null}
            </div>
          </details>
        ) : null}
      </DecisionResolveForm>
      </div>
      </>
    );
  }

  return (
    <>
    {offer ? <AlwaysAllowOption decisionId={decision.id} offer={offer} projectId={projectId} actorId={actorId} /> : null}
    {/* A layout-free wrapper: it only hears which button was pressed. */}
    <div className="im-contents" onClickCapture={onPress}>
    <DecisionResolveForm className="dq-actions">
      {hidden}
      {settleButtons}
      {reasoned ? (
        <div className="dq-reasoned" data-testid="decision-reasoned-approve">{reasonedBody}</div>
      ) : null}
      {direct.map(directButton)}
      {heldNote}
      {whyNo}
      {revise || delegate ? (
        <details className="dq-more">
          <summary>{[revise ? "Request changes" : "", delegate ? "hand to someone else" : ""]
            .filter(Boolean).join(", ").replace(/^h/, "H")}</summary>
          <div className="dq-more-body">{reviseBody}{delegateBody}</div>
        </details>
      ) : null}
      <span className="dq-waited">
        <ClientRelativeTime iso={decision.createdAt} empty="" as="span" />
      </span>
    </DecisionResolveForm>
    </div>
    </>
  );
}


