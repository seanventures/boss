"use client";

/**
 * LINKED PULL REQUESTS (multi-repo screen 6): a change's PRs side by side, each with its CI,
 * gates and the reviewers' findings, then the cross-repo check ("do the changes fit
 * together?") and the set's status. One blocking finding in any PR, or a failed cross-repo
 * check, holds the whole set: nothing asks for approval until it is fixed.
 *
 * "Run the check again" (code_task:steer) re-runs the cross-repo check now; the API refuses it
 * (409 not_ready) until every repository has its PR open, and that sentence is shown as is.
 */
import React, { useState } from "react";
import { ArrowRight, ArrowsClockwise, Check, GitPullRequest, LinkSimple, Warning, X } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import {
  diffWords, isBlocking, isFixed, multirepo, refusalMessage, shortName, type ChangeSetView, type Finding, type Lane,
} from "../../lib/multirepo";
import { TipQ } from "./parts";

const SEVERITY_TONE: Record<string, "bad" | "warn" | "muted" | "ok"> = {
  critical: "bad", blocker: "bad", high: "bad", medium: "warn", low: "muted", note: "muted", info: "muted", pass: "ok",
};
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** CI or gates in one chip: ✓ passed, ✗ failed, … running. */
function StateChip({ label, state, tip }: { label: string; state?: string | null; tip: string }) {
  const s = (state ?? "").toLowerCase();
  const ok = ["success", "passed", "pass", "green", "approved", "clean", "ok"].includes(s);
  const bad = ["failure", "failed", "fail", "red", "changes_requested", "blocked", "error"].includes(s);
  return (
    <span className="mr-ui-check" data-ok={ok ? "yes" : bad ? "no" : undefined} title={`${tip}${state ? `, ${state.replace(/_/g, " ")}` : ", not yet"}`}>
      {ok ? "✓" : bad ? "✗" : "…"} {label}
    </span>
  );
}

function FindingCard({ f, i, lane }: { f: Finding; i: number; lane: Lane }) {
  const sev = (f.severity ?? "note").toLowerCase();
  const blocking = isBlocking(f);
  const fixed = isFixed(f);
  const who = f.author || "Reviewer";
  return (
    <div className="mr-cmt" data-blocking={blocking || undefined} data-fixed={fixed || undefined} data-testid={`mr-finding-${lane.order}-${i}`}>
      <div className="mr-cmt-who">
        <span className="mr-ui-gh sm" aria-hidden style={{ fontWeight: 700, fontSize: "0.66rem" }}>{who.charAt(0).toUpperCase()}</span>
        <b data-user-text>{who}</b>
        {f.path ? <span className="mr-ui-mono mr-ui-dim" data-user-text>{f.path}{f.line ? `:${f.line}` : ""}</span> : null}
        <span className="mr-ui-spacer" />
        <span className="mr-ui-pill" data-tone={SEVERITY_TONE[sev] ?? "muted"} title={blocking ? "Blocks the whole set until fixed" : undefined}>{cap(sev)}</span>
        {fixed ? <span className="mr-ui-pill" data-tone="muted">fixed</span> : null}
      </div>
      <p data-user-text>{f.body}</p>
    </div>
  );
}

function PrCard({ lane, others, title }: { lane: Lane; others: number; title: string }) {
  const blocking = lane.findings.filter(isBlocking).length;
  const plan = lane.plan?.[0];
  const prTitle = plan?.change || title;
  const pill = lane.lane_state === "failed" ? { tone: "bad", word: "Failed" }
    : blocking ? { tone: "bad", word: `${blocking} blocking` }
    : lane.lane_state === "merged" ? { tone: "ok", word: "Merged" }
    : lane.lane_state === "reverted" ? { tone: "warn", word: "Reverted" }
    : lane.pr_number && lane.lane_state === "ready" ? { tone: "ok", word: "Ready" }
    : { tone: "muted", word: lane.pr_number ? "Building" : "No PR yet" };
  return (
    <article className="mr-pr" data-testid={`mr-pr-${lane.order}`} data-blocked={blocking > 0 || lane.lane_state === "failed" || undefined}>
      <div className="mr-pr-h">
        <div className="mr-ui-row">
          <span className="mr-ui-mono" data-user-text style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{lane.full_name}</span>
          <span className="mr-ui-spacer" />
          <span className="mr-ui-pill" data-tone={pill.tone} data-testid={`mr-pr-pill-${lane.order}`}>{pill.word}</span>
        </div>
        <div className="mr-pr-t">
          <GitPullRequest size={15} aria-hidden style={{ flex: "none", marginTop: 2 }} />
          <span data-user-text>{lane.pr_number ? `#${lane.pr_number}, ` : ""}{prTitle}</span>
        </div>
        <div className="mr-ui-row wrap" style={{ gap: 6 }}>
          <span className="mr-ui-sub">{diffWords(lane) || "No diff yet"}</span>
          <span className="mr-ui-spacer" />
          <StateChip label="CI" state={lane.ci_state} tip="The repository's own CI on this PR" />
          <StateChip label="gates" state={lane.review_state} tip="BOSS's gates and review on this PR" />
        </div>
        <span className="mr-ui-sub" data-testid={`mr-pr-links-${lane.order}`}>
          <LinkSimple size={11} aria-hidden /> Links the other {others}
          {lane.base_branch || lane.branch ? <>, base <span data-user-text>{lane.base_branch ?? "main"}</span> ← <span data-user-text>{lane.branch ?? "—"}</span></> : null}
        </span>
      </div>
      <div className="mr-pr-b">
        {lane.lane_state === "failed" && lane.error ? (
          <div className="mr-ui-note" data-tone="bad"><Warning size={14} weight="bold" aria-hidden /><div>{lane.error}</div></div>
        ) : null}
        {lane.findings.length ? lane.findings.map((f, i) => <FindingCard key={i} f={f} i={i} lane={lane} />)
          : <p className="mr-ui-sub" style={{ margin: 0 }}>{lane.pr_number ? "No review findings." : "Reviewed once its PR is open."}</p>}
        {lane.pr_url ? (
          <a className="mr-ui-link" href={lane.pr_url} target="_blank" rel="noreferrer" data-testid={`mr-pr-open-${lane.order}`}
             style={{ marginTop: "auto" }}>Open on GitHub <ArrowRight size={11} aria-hidden /></a>
        ) : null}
      </div>
    </article>
  );
}

export function LinkedPrs({ cs, projectId, onChanged }: {
  cs: ChangeSetView; projectId: string; onChanged: (next: ChangeSetView) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const touched = cs.lanes.filter((l) => l.lane_state !== "unchanged");
  const unchanged = cs.lanes.filter((l) => l.lane_state === "unchanged");
  const check = cs.cross_check;
  const blockingFindings = touched.reduce((n, l) => n + l.findings.filter(isBlocking).length, 0);
  const failedChecks = check ? check.results.filter((r) => !r.pass).length : 0;
  const issues = blockingFindings + failedChecks + touched.filter((l) => l.lane_state === "failed").length;
  const blocked = cs.state === "blocked";
  const title = cs.title.startsWith(`${cs.app_name}: `) ? cs.title.slice(cs.app_name.length + 2) : cs.title;

  const recheck = async () => {
    setBusy(true);
    setError(null);
    try {
      onChanged(await guarded("Run the cross-repo check again.", () => multirepo.crossCheck(projectId, cs.id)));
    } catch (e) {
      if ((e as { code?: string }).code !== "step_up_cancelled") setError(refusalMessage(e));
    } finally { setBusy(false); }
  };

  let status: React.ReactNode;
  if (blocked) {
    status = (
      <span className="mr-ui-pill" data-tone="bad" data-testid="mr-set-status" title={cs.blocked_reason || undefined}>
        Set blocked{issues ? `, ${issues} ${issues === 1 ? "issue" : "issues"}` : ""}
      </span>
    );
  } else if (cs.state === "in_review") {
    status = <span className="mr-ui-pill" data-tone="info" data-testid="mr-set-status" title="One decision for every PR, in Approvals">Waiting for your approval</span>;
  } else if (["approved", "merging", "merged", "deploying", "live"].includes(cs.state)) {
    status = <span className="mr-ui-pill" data-tone="ok" data-testid="mr-set-status">Approved{cs.approved_by ? ` by ${cs.approved_by}` : ""}</span>;
  } else if (check?.verdict === "pass" && touched.every((l) => l.lane_state === "ready")) {
    status = <span className="mr-ui-pill" data-tone="ok" data-testid="mr-set-status">Set ready</span>;
  } else {
    status = <span className="mr-ui-pill" data-tone="muted" data-testid="mr-set-status">Building</span>;
  }

  return (
    <div className="mr-csc" data-testid="mr-linked-prs">
      <div className="mr-ui-row wrap">
        <span className="mr-ui-sub">{touched.length} linked pull {touched.length === 1 ? "request" : "requests"}, reviewed as one set</span>
        <span className="mr-ui-spacer" />
        {status}
      </div>
      {blocked && cs.blocked_reason ? (
        <p className="mr-ui-error" style={{ margin: 0 }} data-testid="mr-set-reason">{cs.blocked_reason}</p>
      ) : null}

      <div className="mr-prs">
        {touched.map((l) => <PrCard key={l.id} lane={l} others={Math.max(0, touched.length - 1)} title={title} />)}
      </div>
      {unchanged.length ? (
        <p className="mr-ui-sub" data-testid="mr-prs-unchanged">
          Unchanged, so no PR: <span data-user-text>{unchanged.map((l) => shortName(l.full_name)).join(", ")}</span>
        </p>
      ) : null}

      <div className="mr-ui-card mr-xcheck" data-testid="mr-xcheck">
        <div className="mr-ui-card-h">
          <h3>Cross-repo check</h3>
          <span className="mr-ui-sub">Do the changes fit together? Run after every push to any PR in the set
            <TipQ tip="Contract vs implementation vs callers, version pins, deploy order, and that every PR links the others." /></span>
          <span className="mr-ui-spacer" />
          {check ? (
            <span className="mr-ui-pill" data-tone={check.passed === check.total ? "ok" : "warn"} data-testid="mr-xcheck-count">
              {check.passed} of {check.total} pass
            </span>
          ) : null}
          <GatedButton permission="code_task:steer" className="ag-btn" data-testid="mr-xcheck-rerun" disabled={busy}
                       title="Runs the check now on every PR's latest commit" onClick={() => void recheck()}>
            <ArrowsClockwise size={13} aria-hidden /> {busy ? "Checking…" : "Run the check again"}
          </GatedButton>
        </div>
        {error ? <p className="mr-ui-error" role="alert" data-testid="mr-xcheck-error">{error}</p> : null}
        {check && check.results.length ? check.results.map((r, i) => (
          <div key={`${r.name}-${i}`} className="mr-ui-xrow" data-testid={`mr-xcheck-row-${i}`} data-pass={r.pass ? "yes" : "no"}>
            {r.pass ? <Check size={15} weight="bold" color="var(--green)" aria-hidden /> : <X size={15} weight="bold" color="var(--red)" aria-hidden />}
            <div style={{ minWidth: 0 }}><span data-user-text>{r.name}</span>{r.detail ? <small data-user-text>{r.detail}</small> : null}</div>
            <span className="mr-ui-pill" data-tone={r.pass ? "ok" : "bad"}>{r.pass ? "Pass" : "Fail"}</span>
          </div>
        )) : (
          <p className="mr-ui-empty" data-testid="mr-xcheck-none">Not run yet: it runs once every PR in the set is open.</p>
        )}
      </div>

      {blocked ? (
        <div className="mr-ui-note" data-tone="bad" role="status" data-testid="mr-set-waits">
          <Warning size={15} weight="bold" aria-hidden />
          <div>
            <b>The whole set waits.</b> One blocking finding in any PR, or a failed cross-repo check, holds
            all {touched.length}. The set is reviewed again on the next push, then BOSS asks for approval.
          </div>
        </div>
      ) : null}
    </div>
  );
}
