"use client";

import { changeHref, linkTarget } from "../../lib/repo-links";
import Link from "next/link";
// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope, see app/previews-ui.tsx.
import React from "react";

import type { V2Deployment, V2Evidence, V2Gate, V2Provenance, V2Run, V2Step, Workspace }
  from "../../lib/v2/workspace";
import { Markdown } from "../../app/markdown";
import { DeployControls } from "./DeployControls";

/**
 * One app: where it stands, every attempt it took to get there, and what it cost.
 *
 * THE THREE EMPTY STATES ARE NOT THE SAME
 * ---------------------------------------
 * "no such app", "this app has no runs" and "the control plane did not answer" all render as
 * an empty page, and they call for completely different reactions. Conflating them is a
 * recurring defect in this console, an outage read as an empty company, a deployed app read
 * as never deployed, so each one says which it is.
 *
 * WHY EVERY ATTEMPT IS LISTED
 * ---------------------------
 * The lineage is the app. Judging by the newest run made BOSS refuse to deploy something whose
 * completed build was sitting two rows down, and a card that shows only the latest attempt
 * understates the cost by however many retries it took.
 */

function money(n: number): string {
  return "$" + (n < 100 ? n.toFixed(2) : Math.round(n).toLocaleString());
}

export function WorkspaceScreen({
  workspace,
  orgId,
  actor = "executive_console",
}: {
  workspace: Workspace;
  orgId?: string;
  /** Who is acting. NEVER invented: defaulting to "operator" got the API's refusal
   *  "Authenticated principal 'ceo' is not allowed to act as 'operator'", which was right , 
   *  claiming a principal you are not is impersonation whether or not it was meant. */
  actor?: string;
}) {
  const q = orgId ? `?org=${encodeURIComponent(orgId)}` : "";

  if (workspace.blockedReason) {
    return (
      <section className="v2-screen" data-testid="v2-workspace-unreachable">
        <BackLink q={q} />
        <p className="v2-empty">
          Could not reach the control plane ({workspace.blockedReason}). Nothing here is a
          statement about this app, it will appear once the API answers.
        </p>
      </section>
    );
  }

  if (!workspace.found) {
    return (
      <section className="v2-screen" data-testid="v2-workspace-missing">
        <BackLink q={q} />
        <p className="v2-empty">
          No app with id <code className="v2-mono">{workspace.app_id}</code> in this company.
          It may have been renamed, or the link may be from another org.
        </p>
      </section>
    );
  }

  const runs = workspace.runs ?? [];

  return (
    <section className="v2-screen" data-testid="v2-workspace">
      <BackLink q={q} />

      <header className="v2-screen-head">
        <h1 className="v2-screen-title" data-testid="v2-workspace-title">
          {workspace.title}
        </h1>
        <p className="v2-screen-sub">{workspace.description}</p>

        {/* The contract this work was accepted against. Collapsed, because it is reference
            material you consult rather than read, but present, because without it the
            quality checks below are marks with no exam paper. */}
        <Contract criteria={workspace.criteria} verification={workspace.verification} />

        <div className="v2-ws-facts v2-mono">
          <span className="v2-status" data-status={workspace.status}>
            <span className="v2-dot" data-status={workspace.status} aria-hidden />
            {workspace.status}
          </span>
          <span>
            <span className="v2-row-meta-dim">repo</span> {workspace.repo}
          </span>
          <span>
            <b>{runs.length}</b> {runs.length === 1 ? "attempt" : "attempts"}
          </span>
          <span data-testid="v2-workspace-spend">
            <b>{money(workspace.spend_usd ?? 0)}</b> spent
          </span>
        </div>
      </header>

      {/* DISCOVERY -> BUILD -> REVIEW -> SHIPPED, with how far it actually got. */}
      <ol className="v2-phases" data-testid="v2-workspace-phases">
        {(workspace.phases ?? []).map((p) => (
          <li
            key={p.id}
            className="v2-phase"
            data-reached={p.reached}
            data-testid={`v2-phase-${p.id}`}
          >
            <span className="v2-phase-dot" aria-hidden />
            <span className="v2-phase-label v2-mono">{p.label}</span>
          </li>
        ))}
      </ol>

      {/* Whether it can ship, and if not, WHY. A disabled control with no reason is what
          sends people to the API to find out. */}
      <p
        className="v2-ws-ship"
        data-deployable={workspace.deployable}
        data-testid="v2-workspace-deployability"
      >
        {workspace.deployable
          ? "Reviewed and ready to ship."
          : `Cannot ship yet, ${workspace.deployable_reason}`}
      </p>

      {/* The four controls, on the app rather than buried in a list of runs. They issue the
          same requests as the v1 card, deliberately: those handlers are already off the
          Server-Action path, and reusing them is what lets the parity check report MOVED
          instead of MISSING. */}
      {/* WHERE IT IS RUNNING. The operator asked why the console could not load this when the
          app was plainly deployed, the state was never missing, v2 just never read
          deployment_targets.domains or the deploy runs beside them. */}
      {workspace.deployment ? <Deployment d={workspace.deployment} /> : null}

      {workspace.latest_run_id ? (
        <DeployControls
          runId={workspace.latest_run_id}
          appName={workspace.title ?? ""}
          projectId={workspace.project_id}
          actor={actor}
          deployable={Boolean(workspace.deployable)}
          deployableReason={workspace.deployable_reason ?? ""}
        />
      ) : null}

      <h2 className="v2-ws-h">Attempts</h2>
      {runs.length === 0 ? (
        <p className="v2-empty" data-testid="v2-workspace-no-runs">
          This app exists but nothing has run yet.
        </p>
      ) : (
        <ul className="v2-rows" data-testid="v2-workspace-runs">
          {runs.map((r) => (
            <RunRow key={r.id} run={r} />
          ))}
        </ul>
      )}
    </section>
  );
}

function BackLink({ q }: { q: string }) {
  return (
    <Link href={`/v2${q}`} className="v2-back v2-mono" data-testid="v2-workspace-back">
      ← Projects
    </Link>
  );
}

/** Where the app is running, and how the last deploy went. */
function Deployment({ d }: { d: V2Deployment }) {
  const last = d.last_deploy;
  return (
    <div className="v2-deployment" data-testid="v2-deployment">
      <span className="v2-deploy-env v2-mono" data-status={d.status}>
        {d.environment || "deployed"}
        {d.status && d.status !== "active" ? `, ${d.status}` : ""}
      </span>

      {/* The link the operator was looking for. Only when there is an address, a bound target
          with no domain is not a link, and a broken link is worse than none. */}
      {d.url ? (
        <a className="v2-deploy-url v2-mono" href={d.url} target="_blank" rel="noreferrer"
           data-testid="v2-deploy-url">
          {d.domains[0]} ↗
        </a>
      ) : (
        <span className="v2-row-meta-dim v2-mono" data-testid="v2-deploy-no-url">
          no address recorded for this target
        </span>
      )}

      {last ? (
        <span className="v2-deploy-last v2-mono" data-status={last.status}
              data-testid="v2-deploy-last">
          last deploy {last.status}
          {last.commit ? `, ${last.commit}` : ""}
          {/* Why it failed, on the screen. Otherwise "failed" is another dead end. */}
          {last.failure_reason ? `, ${last.failure_reason}` : ""}
        </span>
      ) : (
        <span className="v2-row-meta-dim v2-mono">never deployed</span>
      )}

      <span className="v2-deploy-ref v2-mono v2-row-meta-dim">{d.target_ref}</span>
    </div>
  );
}

function Contract({ criteria, verification }: {
  criteria?: string[]; verification?: string[];
}) {
  const items = (criteria ?? []).filter(Boolean);
  const checks = (verification ?? []).filter(Boolean);
  if (!items.length && !checks.length) return null;
  return (
    <details className="v2-contract" data-testid="v2-contract">
      <summary className="v2-contract-head">Acceptance criteria &amp; verification</summary>
      {items.length ? (
        <ul className="v2-contract-list" data-testid="v2-contract-criteria">
          {items.map((c, i) => <li key={i}>{c}</li>)}
        </ul>
      ) : null}
      {checks.length ? (
        <>
          <h4 className="v2-ws-h">Verification</h4>
          <ul className="v2-contract-list" data-testid="v2-contract-verification">
            {checks.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </>
      ) : null}
    </details>
  );
}

function RunRow({ run }: { run: V2Run }) {
  return (
    <li className="v2-row v2-ws-run" data-testid={`v2-run-${run.id}`}>
      <span className="v2-status v2-row-status" data-status={run.status.toUpperCase()}>
        <span className="v2-dot" data-status={run.status.toUpperCase()} aria-hidden />
        {run.status}
      </span>

      <span className="v2-row-main">
        <span className="v2-row-title v2-mono">
          attempt {run.attempt}
          <span className="v2-row-meta-dim">, {run.id}</span>
        </span>

        {/* WHAT THIS ATTEMPT WAS DOING, in sentences.
            "blocked" plus a list of stage names tells someone who already knows the pipeline
            what happened and tells everyone else nothing, and it reads as a failure even when
            the pipeline is simply holding for an approval. */}
        {run.narrative ? (
          <span className="v2-narrative" data-waiting={run.narrative.waiting}
                data-testid={`v2-narrative-${run.id}`}>
            <span className="v2-narrative-outcome">{run.narrative.outcome}</span>
            {run.narrative.why ? (
              <span className="v2-narrative-why" data-testid={`v2-narrative-why-${run.id}`}>
                {run.narrative.why}
              </span>
            ) : null}
          </span>
        ) : null}

        {/* A failure with no text is a dead end: the only move left is to go and read logs. */}
        {run.error ? (
          <span className="v2-run-error" data-testid={`v2-run-error-${run.id}`}>
            {run.error}
          </span>
        ) : null}

        {/* THE STEPS THIS ATTEMPT RAN, which stage, which model, how long, what it cost.
            "attempt 3, failed" and nothing else leaves reading logs as the only next move. */}
        {run.steps.length > 0 ? (
          <ol className="v2-steps" data-testid={`v2-run-steps-${run.id}`}>
            {run.steps.map((s, i) => (
              <RunStep key={`${s.stage}-${i}`} s={s} runId={run.id} index={i} />
            ))}
          </ol>
        ) : null}

        <span className="v2-row-meta v2-mono">
          <b>{run.turns}</b> turns
          {"  "}
          <b>{money(run.spend_usd)}</b>
          {run.latency_ms > 0 ? (
            <>
              {"  "}
              <span data-testid={`v2-run-latency-${run.id}`}>
                <b>{(run.latency_ms / 1000).toFixed(1)}s</b> model time
              </span>
            </>
          ) : null}
          {run.pull_request_number ? (
            <>
              {"  "}
              {/* Linked from the URL the run itself recorded. A card once linked to a PR
                  number that had been guessed and did not exist. */}
              {run.pull_request_url ? (
                <a
                  className="v2-meta-link"
                  href={changeHref(run.pull_request_url)}
                  {...linkTarget(run.pull_request_url)}
                  data-testid={`v2-run-pr-${run.id}`}
                >
                  PR #{run.pull_request_number} ↗
                </a>
              ) : (
                <span className="v2-row-meta-dim">PR #{run.pull_request_number}</span>
              )}
            </>
          ) : null}
        </span>

        <RunEvidence run={run} />
      </span>
    </li>
  );
}

/**
 * How long something took, in the units a person would say it in.
 *
 * Tenths under a minute, because the difference between 9.4s and 9s is the difference between
 * two steps; whole minutes above it, because nobody reads "1803.4s" as half an hour.
 */
function fmtDuration(ms: number): string {
  const sec = Math.max(0, ms) / 1000;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const whole = Math.round(sec);
  const m = Math.floor(whole / 60);
  const rs = whole % 60;
  return rs ? `${m}m ${rs}s` : `${m}m`;
}

/**
 * What the governed pipeline checked, who checked it, and what they wrote.
 *
 * v1 showed all of this and v2 showed none of it, the columns were being SELECTed and thrown
 * away, so an attempt rendered as "attempt 1 - completed" and the only move left was to go and
 * read logs. The operator asked for the v1 content back, and it is the content that makes a
 * green tick mean something.
 */
function RunEvidence({ run }: { run: V2Run }) {
  const gates = run.gates ?? [];
  const timings = run.phase_timings ?? [];
  const evidence = run.evidence ?? [];
  const files = [...(run.test_files ?? []), ...(run.impl_files ?? [])];
  // provenance counts too: an attempt whose only record is "the same model built and reviewed
  // this" still has something worth saying, and the earlier guard dropped it silently.
  if (!gates.length && !timings.length && !evidence.length && !files.length
      && !run.provenance) return null;

  const failed = gates.filter((g) => g.ok === false).length;
  const total = fmtDuration(timings.reduce((a, t) => a + t.ms, 0));

  return (
    <details className="v2-run-evidence" data-testid={`v2-run-evidence-${run.id}`}>
      <summary className="v2-run-evidence-head">
        Steps &amp; quality checks
        {gates.length ? (
          <span
            className="v2-gate-verdict"
            data-tone={failed ? (run.advisory ? "warn" : "bad") : "good"}
            data-testid={`v2-gate-verdict-${run.id}`}
          >
            {failed ? `${failed} failing` : "passed"}
          </span>
        ) : null}
      </summary>

      {/* A green header above a red row is unreadable unless you say which one won. */}
      {run.advisory ? (
        <p className="v2-gate-advisory" data-testid={`v2-gate-advisory-${run.id}`}>
          First-pass build: the checks below are advisory, a ✗ did not block the PR. Shipped so
          you can preview now; retry to clear it.
        </p>
      ) : null}

      {gates.length ? (
        <ul className="v2-gates" data-testid={`v2-gates-${run.id}`}>
          {gates.map((g) => <GateRow key={g.label} g={g} runId={run.id} />)}
        </ul>
      ) : null}

      {run.provenance ? <Provenance p={run.provenance} runId={run.id} /> : null}

      {/* Step timing: where the wall-clock actually went, which is rarely where you expect. */}
      {timings.length ? (
        <div className="v2-timing" data-testid={`v2-run-timing-${run.id}`}>
          <h4 className="v2-ws-h">Step timing</h4>
          <ul className="v2-timing-rows">
            {timings.map((t, i) => (
              <li key={`${t.phase}-${i}`}>
                <span>{t.phase}</span>
                <span className="v2-mono">{fmtDuration(t.ms)}</span>
              </li>
            ))}
            <li className="v2-timing-total">
              <span>Total</span>
              <span className="v2-mono">{total}</span>
            </li>
          </ul>
        </div>
      ) : null}

      {evidence.map((e) => <Evidence key={e.key} e={e} runId={run.id} />)}

      {files.length ? (
        <p className="v2-run-files v2-mono" data-testid={`v2-run-files-${run.id}`}>
          {run.test_files?.length ? `Test files: ${run.test_files.join(", ")}. ` : ""}
          {run.impl_files?.length ? `Impl files: ${run.impl_files.join(", ")}.` : ""}
        </p>
      ) : null}
    </details>
  );
}

/** One check. Three states, never two, see the engine's _gate_rows for why the third matters. */
function GateRow({ g, runId }: { g: V2Gate; runId: string }) {
  const mark = g.skipped ? "–" : g.ok ? "✓" : "✗";
  const tone = g.skipped ? "skip" : g.ok ? "good" : "bad";
  return (
    <li
      className="v2-gate"
      data-tone={tone}
      data-testid={`v2-gate-${runId}-${g.label.replace(/[^a-z]+/gi, "-").toLowerCase()}`}
    >
      <span className="v2-gate-mark" aria-hidden>{mark}</span>
      <span className="v2-gate-label">{g.label}</span>
      {/* "not run" is stated, never left blank, a blank reads as a pass. */}
      <span className="v2-gate-note">{g.note ?? (g.skipped ? "not run" : "")}</span>
    </li>
  );
}

function Provenance({ p, runId }: { p: V2Provenance; runId: string }) {
  return (
    <div className="v2-provenance" data-testid={`v2-provenance-${runId}`}>
      <p className="v2-provenance-line">
        Independent reviewer: <b>{p.reviewer_independent ? "yes" : "no"}</b>
        {", "}Independent test author: <b>{p.test_author_independent ? "yes" : "no"}</b>
        {p.profile ? <>, profile: <b>{p.profile}</b></> : null}
        {p.risk_tier != null ? <>, risk tier <b>{p.risk_tier}</b></> : null}
      </p>
      {p.build_engine || p.review_engine ? (
        <p className="v2-provenance-line">
          Builder: <b>{p.build_engine ?? "—"}</b>
          {p.build_model ? ` (${p.build_model})` : ""}
          {", "}Adversary: <b>{p.review_engine ?? "—"}</b>
          {p.review_model ? ` (${p.review_model})` : ""}
          {/* Stated either way. A model grading its own work is the single fact that most
              changes how much a green review is worth. */}
          <span className="v2-provenance-note" data-testid={`v2-cross-model-${runId}`}>
            {p.cross_model
              ? ", cross-model review (no self-grading)"
              : ", same model builds + reviews"}
          </span>
        </p>
      ) : null}
    </div>
  );
}

/** A reviewer's own words. Markdown, because that is how the agents write. */
function Evidence({ e, runId }: { e: V2Evidence; runId: string }) {
  return (
    <details className="v2-evidence" data-testid={`v2-evidence-${runId}-${e.key}`}>
      <summary className="v2-evidence-head">{e.title}</summary>
      <div className="v2-evidence-text">
        <Markdown text={e.text} />
        {e.truncated ? (
          <p className="v2-turn-clipped v2-mono">summary continues, clipped for this view</p>
        ) : null}
      </div>
    </details>
  );
}


/**
 * One step of an attempt.
 *
 * Latency is shown because it is what separates a slow model from a slow pipeline, a run that
 * took ten minutes with nine seconds of model time was not waiting on the model.
 */
function RunStep({ s, runId, index }: { s: V2Step; runId: string; index: number }) {
  return (
    <li
      className="v2-step"
      data-ok={s.ok}
      data-testid={`v2-step-${runId}-${index}`}
    >
      <span className="v2-step-name">{s.stage}</span>
      {/* WHICH AGENT owns this stage, so the work maps onto the org chart. From the same map
          BOSS uses at write time to attribute spend, not a second one invented here. */}
      {s.agent ? (
        <span className="v2-step-agent" data-registered={s.agent.registered}
              data-testid={`v2-step-agent-${runId}-${index}`}>
          {s.agent.role}
          {/* BOSS attributes work to roles its own registry does not contain. Saying so beats
              linking to an agent that is not there. */}
          {!s.agent.registered ? (
            <span className="v2-step-agent-gap" title="no such role in the agent registry">
              {" "}· unregistered
            </span>
          ) : null}
          {/* Observed reliability, with its denominator. Not a confidence score: BOSS records
              none, and three calls and three hundred are different kinds of evidence. */}
          {s.agent.success_rate != null ? (
            <span className="v2-step-agent-rate" data-testid={`v2-step-rate-${runId}-${index}`}>
              {" "}{Math.round(s.agent.success_rate * 100)}% of {s.agent.calls}
            </span>
          ) : null}
        </span>
      ) : null}
      {s.model ? <span className="v2-step-role">{s.model}</span> : null}
      <span className="v2-step-timing">
        {s.turns > 0 ? `${s.turns} turn${s.turns === 1 ? "" : "s"}, ` : ""}
        {fmtDuration(s.latency_ms)}
        {s.spend_usd > 0 ? `, $${s.spend_usd.toFixed(3)}` : ""}
        {s.retries > 0 ? `, ${s.retries} retr${s.retries === 1 ? "y" : "ies"}` : ""}
      </span>
      {/* A run that failed at one step and succeeded at nine others is a different story from
          one that failed everywhere. The error text is the whole reason to open a failed
          attempt, so it is shown here rather than left in the logs. */}
      {!s.ok ? (
        <span className="v2-step-failed" data-testid={`v2-step-failed-${runId}-${index}`}>
          ✗ {s.error || "failed"}
        </span>
      ) : null}
    </li>
  );
}
