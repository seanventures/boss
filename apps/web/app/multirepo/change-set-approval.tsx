"use client";

/**
 * ONE APPROVAL FOR A CHANGE ACROSS REPOSITORIES (multi-repo screen 7), drawn inside its Approvals
 * row for `recommendation.action === "approve_change_set"`: every pull request of the set, the
 * review, the cross-repo check, the cost, what each component deploys as, and how all of it is
 * rolled back. One decision: Approve and ship all N · Send back (with a note) · Reject. There is
 * no per-PR approval.
 *
 * The answer goes through the inbox's own form (DecisionResolveForm → resolveDecisionAction) with
 * the preview's `content_hash`: the engine pins the approval to the head SHAs that were reviewed,
 * and refuses it when a PR moved since the question was asked — its sentence is shown as is.
 * The figures come from the change set (code_task:read) and its deploy plan (deployment:read);
 * when either cannot be read the card still shows what the decision itself carries.
 */
import { changeHref, linkTarget } from "../../lib/repo-links";
import React, { useEffect, useState } from "react";
import { ArrowCounterClockwise, Check, ShieldCheck, Clock, Warning } from "@phosphor-icons/react";

import { DecisionResolveForm } from "../client-forms";
import { SubmitButton } from "../submit-button";
import { ClientRelativeTime } from "../client-relative-time";
import { actionPreviewOf } from "../action-preview";
import { offeredActions, type DecisionFeedItem } from "../../lib/decisions";
import {
  CHANGE_SET_PLACES, isBlocking, isFixed, isOpenIssue, multirepo, shortName, type ChangeSetView, type DeployPlan,
} from "../../lib/multirepo";
import { areaHref } from "../factory/cockpit-util";
import { actorWords } from "../../lib/actor-words";

type PullRequest = { repository: string; pr_number?: number | null; pr_url?: string | null; head_sha?: string | null;
                     additions?: number | null; deletions?: number | null; components?: string[] };

const str = (v: unknown) => (typeof v === "string" ? v : "");

export function isChangeSetDecision(decision: DecisionFeedItem): boolean {
  return decision.recommendation.action === "approve_change_set";
}

function testsChip(state?: string | null) {
  const s = (state ?? "").toLowerCase();
  const ok = ["success", "passed", "pass", "green"].includes(s);
  const bad = ["failure", "failed", "fail", "red", "error"].includes(s);
  return (
    <span className="mr-ui-check" data-ok={ok ? "yes" : bad ? "no" : undefined} title={state ? `CI: ${state.replace(/_/g, " ")}` : "CI has not reported"}>
      {ok ? "✓ passed" : bad ? "✗ failed" : "… running"}
    </span>
  );
}

export function ChangeSetApproval({ decision, projectId, actorId, pending }: {
  decision: DecisionFeedItem; projectId: string; actorId: string; pending: boolean;
}) {
  const rec = decision.recommendation;
  const csId = str(rec.change_set_id);
  const app = str(rec.app_name) || str(rec.app_key) || "the app";
  const prs = (Array.isArray(rec.pull_requests) ? rec.pull_requests : []) as PullRequest[];
  const n = prs.length;
  const rawTitle = str(rec.title) || decision.question;
  const title = rawTitle.startsWith(`${app}: `) ? rawTitle.slice(app.length + 2) : rawTitle;
  const contentHash = actionPreviewOf(rec.action_preview)?.content_hash ?? "";
  const [cs, setCs] = useState<ChangeSetView | null>(null);
  const [plan, setPlan] = useState<DeployPlan | null>(null);
  const [sendingBack, setSendingBack] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!csId) return;
    let live = true;
    multirepo.changeSet(projectId, csId).then((v) => { if (live) setCs(v); }).catch(() => undefined);
    multirepo.deployPlan(projectId, csId).then((v) => { if (live) setPlan(v); }).catch(() => undefined);
    return () => { live = false; };
  }, [projectId, csId]);

  const lanes = cs ? cs.lanes.filter((l) => l.lane_state !== "unchanged") : [];
  const laneOf = (repo: string) => lanes.find((l) => l.full_name === repo);
  const findings = lanes.flatMap((l) => l.findings);
  const open = findings.filter(isOpenIssue).length;
  const fixed = findings.filter(isFixed).length;
  const check = cs?.cross_check ?? null;
  const blocked = cs?.state === "blocked" || lanes.some((l) => l.findings.some(isBlocking));
  const offers = offeredActions(decision).filter((a) => a.action !== "execute" && a.action !== "delegate");
  const approve = offers.find((a) => a.action === "approve");
  const revise = offers.find((a) => a.action === "revise");
  const reject = offers.find((a) => a.action === "reject");
  const waveOf = (repo: string) => plan?.waves.find((w) => w.repository === repo);
  const liveNow = (plan?.waves ?? []).filter((w) => w.from && w.from !== "—" && w.from !== "not deployed")
    .map((w) => `${w.components.join(" + ") || shortName(w.repository)} ${w.from}`);

  return (
    <div className="mr-appr" data-testid="mr-approval" data-change-set={csId} data-done={!pending || undefined}>
      <div className="mr-appr-h">
        <span className="mr-appr-icon" aria-hidden><ShieldCheck size={20} /></span>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="mr-ui-eyebrow mr-appr-eyebrow" data-testid="mr-approval-eyebrow">Ship a change · {app}</div>
          <h3 data-user-text>{title}</h3>
          <p className="mr-ui-sub" style={{ margin: 0 }} data-testid="mr-approval-lead">
            One decision for all {n} pull {n === 1 ? "request" : "requests"}. They merge together and deploy together, or not at all.
          </p>
        </div>
        <span className="mr-ui-pill" data-tone="warn" title="When BOSS asked">
          <Clock size={11} aria-hidden /> asked <ClientRelativeTime iso={decision.createdAt} empty="" as="span" />
        </span>
      </div>

      <div className="mr-appr-b">
        <div className="mr-stats" data-testid="mr-approval-stats">
          <div className="mr-stat">
            <small>Pull requests</small><b>{n} linked</b>
            <span data-user-text>{prs.map((p) => `${shortName(p.repository)} #${p.pr_number ?? "—"}`).join(" · ")}</span>
          </div>
          <div className="mr-stat" data-testid="mr-approval-review">
            <small>Review</small>
            <b data-tone={cs ? (open ? "bad" : "ok") : undefined}>{cs ? `${open} open` : "—"}</b>
            <span>{cs ? `${fixed} fixed · adversarial review on each PR` : "Review on each PR"}</span>
          </div>
          <div className="mr-stat" data-testid="mr-approval-xcheck">
            <small>Cross-repo</small>
            <b data-tone={check ? (check.passed === check.total ? "ok" : "bad") : undefined}>{check ? `${check.passed} / ${check.total}` : "—"}</b>
            <span>{check ? (check.passed === check.total ? "contract, pins, deploy order" : `${check.total - check.passed} failing`) : "Not run yet"}</span>
          </div>
          <div className="mr-stat" data-testid="mr-approval-cost">
            <small>Cost</small><b>{cs ? `$${cs.cost_usd.toFixed(2)}` : "—"}</b><span>every lane together</span>
          </div>
        </div>

        <div className="mr-ui-table-wrap">
          <table className="mr-ui-table" data-testid="mr-approval-table">
            <thead><tr><th>Repository</th><th>Pull request</th><th>Change</th><th>Tests</th><th className="mr-hide-narrow">Deploys as</th></tr></thead>
            <tbody>
              {prs.map((p) => {
                const lane = laneOf(p.repository);
                const wave = waveOf(p.repository);
                return (
                  <tr key={p.repository} data-testid={`mr-approval-row-${p.repository}`}>
                    <td className="mr-ui-mono" data-user-text>{p.repository}</td>
                    <td>{p.pr_url ? <a className="mr-ui-link mr-ui-mono" href={changeHref(p.pr_url)} {...linkTarget(p.pr_url)}>#{p.pr_number}</a>
                      : <span className="mr-ui-mono">#{p.pr_number ?? "—"}</span>}</td>
                    <td className="mr-ui-mono nw">+{p.additions ?? lane?.additions ?? 0} −{p.deletions ?? lane?.deletions ?? 0}</td>
                    <td>{testsChip(lane?.ci_state)}</td>
                    <td className="mr-ui-mono mr-hide-narrow" data-user-text>
                      {wave ? `${wave.to ?? "—"}${wave.kind === "host" ? ` → ${wave.host ?? "host"}` : " · with the merge"}` : (p.head_sha ?? "").slice(0, 7) || "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <div className="mr-ui-note" data-testid="mr-approval-rollback">
          <ArrowCounterClockwise size={15} aria-hidden />
          <div>
            <b>If anything fails</b> while merging or deploying, BOSS rolls every component back
            {liveNow.length ? <> to what is live now: <span data-user-text>{liveNow.join(", ")}</span></> : " to what is live now"}.{" "}
            {csId ? <a className="mr-ui-link" href={areaHref(CHANGE_SET_PLACES.deployPlan(csId))} data-testid="mr-approval-plan-link">See the deploy plan</a> : null}
          </div>
        </div>

        {pending && blocked ? (
          <div className="mr-ui-note" data-tone="bad" role="status" data-testid="mr-approval-blocked">
            <Warning size={15} weight="bold" aria-hidden />
            <div><b>Set blocked.</b> {cs?.blocked_reason || "A blocking finding is open."} BOSS asks again once it is fixed.</div>
          </div>
        ) : null}
      </div>

      {pending && !blocked ? (
        <DecisionResolveForm className="mr-appr-f">
          <input type="hidden" name="decision_id" value={decision.id} />
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="actor" value={actorId} />
          {contentHash ? <input type="hidden" name="content_hash" value={contentHash} /> : null}
          {approve ? (
            <SubmitButton permission="decision:approve" className="dq-btn approve" name="action" value="approve"
                          title={approve.effect || undefined} data-testid="mr-approval-approve">
              <Check size={13} weight="bold" aria-hidden /> {approve.label || `Approve and ship all ${n}`}
            </SubmitButton>
          ) : null}
          {revise ? (
            <button type="button" className="dq-btn neutral" aria-expanded={sendingBack} data-testid="mr-approval-send-back"
                    title={revise.effect || "Every PR gets a fix build from your note"} onClick={() => setSendingBack((v) => !v)}>
              {revise.label || "Send back"}
            </button>
          ) : null}
          {reject ? (
            <SubmitButton permission="decision:approve" className="dq-btn reject" name="action" value="reject"
                          title={reject.effect || undefined} data-testid="mr-approval-reject">
              {reject.label || "Reject"}
            </SubmitButton>
          ) : null}
          <span className="mr-ui-sub" data-testid="mr-approval-no-partial">No per-PR approval: approving part of a change is not possible.</span>
          {revise && sendingBack ? (
            <div className="mr-appr-note" data-testid="mr-approval-note-box">
              <textarea name="note" value={note} onChange={(e) => setNote(e.target.value)} rows={3}
                        aria-label="What should change before it is asked again" data-testid="mr-approval-note"
                        placeholder="What should change? Every PR gets a fix build from this note." />
              <div className="mr-ui-row">
                <SubmitButton permission="decision:approve" className="dq-btn neutral" name="action" value="revise"
                              disabled={!note.trim()} title={note.trim() ? revise.effect : "Write what should change first"}
                              data-testid="mr-approval-revise">Send back with this note</SubmitButton>
              </div>
            </div>
          ) : null}
        </DecisionResolveForm>
      ) : !pending ? (
        <div className="mr-appr-f" data-testid="mr-approval-verdict">
          <span className="mr-ui-pill" data-tone={decision.status === "approved" || decision.status === "executed" ? "ok"
            : decision.status === "rejected" ? "bad" : "muted"}>{decision.status.replace(/_/g, " ")}</span>
          {decision.approvedBy ? <span className="mr-ui-sub">by <span data-user-text>{actorWords(decision.approvedBy)}</span></span> : null}
          {csId ? <a className="mr-ui-link" href={areaHref(CHANGE_SET_PLACES.cockpit(csId))}>Open the change</a> : null}
        </div>
      ) : null}
    </div>
  );
}
