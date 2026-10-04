"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";

import { onChanged, notifyChanged } from "./changed";
import { showToast } from "./client-forms";
import { describeActionError, resultFromResponse } from "../lib/action-result";
import { sendRunAnswer } from "./delivery-workflows-ui";
import {
  agentPhrase,
  areaHref,
  commandCenterFrom,
  decisionHref,
  decisionsHref,
  elapsedLabel,
  needsYouLabel,
  outcomeTone,
  runHref,
  type CommandCenterData,
  type NeedsYouItem,
  type RunningRun,
} from "../lib/command-center";

/** How often the landing page re-reads itself while it is being looked at. */
const POLL_MS = 15_000;

/**
 * THE COMMAND CENTER, AROUND WORKFLOW RUNS.
 *
 * Three questions, in the order an operator asks them: what needs me, what is running, and how
 * did the recent work go. Every row links into the run (or the decision) it is about.
 *
 * It replaces a task-era page: KPI tiles over `tasks`, a "Live coordination" orb graph built
 * from `tasks.owner_role_id` (which nothing populates, so it animated idle agents whatever was
 * happening), a "Pending decisions" list whose rows linked nowhere, quick actions to areas the
 * rail no longer shows, and a leftover Metatron card whose "Open captain" button did nothing.
 * A run started by Build creates no task, so none of that ever showed it.
 */
export function CommandCenter({ projectId, orgId, actor, initial }: {
  projectId: string;
  orgId?: string;
  actor: string;
  initial: CommandCenterData;
}) {
  const [data, setData] = useState<CommandCenterData>(initial);
  const [loadError, setLoadError] = useState<string>(initial.apiReachable ? "" :
    initial.blockedReason ?? "");
  // The clock for elapsed times. Null until mounted so the server render and the first client
  // render agree (the server's elapsed_seconds is used until then).
  const [now, setNow] = useState<number | null>(null);
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const r = await fetch(
        `/v1/console/command-center?project_id=${encodeURIComponent(projectId)}`,
        { cache: "no-store" });
      if (!r.ok) {
        const failed = await resultFromResponse(r);
        setLoadError(failed.error ?? "");
        return;
      }
      setData(commandCenterFrom(await r.json()));
      setLoadError("");
    } catch (error) {
      setLoadError(describeActionError(error));
    } finally {
      inFlight.current = false;
    }
  }, [projectId]);

  useEffect(() => {
    setNow(Date.now());
    const tick = window.setInterval(() => setNow(Date.now()), 30_000);
    const poll = window.setInterval(() => {
      if (!document.hidden) void load();
    }, POLL_MS);
    const unsubscribe = onChanged(() => { void load(); }, "command-center");
    return () => { window.clearInterval(tick); window.clearInterval(poll); unsubscribe(); };
  }, [load]);

  const counts = data.counts;
  return (
    <div className="cc2" data-testid="command-center">
      <div className="cc2-kpis" role="list" aria-label="At a glance">
        <a role="listitem" className={`cc2-kpi${counts.needs_you ? " amber" : ""}`}
           href="#cc2-needs-you" data-testid="cc2-kpi-needs-you">
          <span className="cc2-kpi-n">{counts.needs_you}</span>
          <span className="cc2-kpi-l">need you</span>
        </a>
        <a role="listitem" className="cc2-kpi" href="#cc2-running" data-testid="cc2-kpi-running">
          <span className="cc2-kpi-n">{counts.running}</span>
          <span className="cc2-kpi-l">running</span>
        </a>
        <a role="listitem" className="cc2-kpi green" href="#cc2-recent">
          <span className="cc2-kpi-n">{counts.succeeded_7d}</span>
          <span className="cc2-kpi-l">finished · 7d</span>
        </a>
        <a role="listitem" className={`cc2-kpi${counts.failed_7d ? " red" : ""}`} href="#cc2-recent">
          <span className="cc2-kpi-n">{counts.failed_7d}</span>
          <span className="cc2-kpi-l">failed · 7d</span>
        </a>
      </div>

      {loadError ? (
        <p className="error-banner" role="alert" data-testid="cc2-load-error">
          Could not read the latest state: {loadError} What is shown may be out of date.
        </p>
      ) : null}

      <section className="cc2-section" id="cc2-needs-you" aria-labelledby="cc2-needs-you-h">
        <h2 id="cc2-needs-you-h" className="cc2-h">What needs you</h2>
        {data.needs_you.length === 0 ? (
          <p className="cc2-empty" data-testid="cc2-needs-you-empty">
            Nothing is waiting on you. Gates, failed steps and setup gaps appear here as soon as
            they happen.
          </p>
        ) : (
          <ul className="cc2-list">
            {data.needs_you.map((item) => (
              <NeedsYouRow key={`${item.kind}:${item.decision_id ?? item.run_id ?? item.title}:${
                item.step_id ?? ""}`} item={item} orgId={orgId} projectId={projectId}
                actor={actor} onDone={load} />
            ))}
          </ul>
        )}
        {/* Not in the list above: an expired hold decision applied nothing and can still be
            answered, but most are about work that has moved on (127 of 165 on the live
            project). Said once, with the way in, so it is neither hidden nor a wall of cards. */}
        {data.counts.expired_answerable > 0 ? (
          <p className="cc2-empty" data-testid="cc2-expired-answerable">
            {data.counts.expired_answerable === 1
              ? "1 decision expired without an answer and can still be answered."
              : `${data.counts.expired_answerable} decisions expired without an answer and can still be answered.`}{" "}
            Nothing was applied to them.{" "}
            <a href={decisionsHref("expired", orgId)}>Review expired decisions</a>
          </p>
        ) : null}
      </section>

      <section className="cc2-section" id="cc2-running" aria-labelledby="cc2-running-h">
        <h2 id="cc2-running-h" className="cc2-h">Running now</h2>
        {data.running.length === 0 ? (
          <p className="cc2-empty" data-testid="cc2-running-empty">
            No workflow is running. Describe what to build above, or start one in{" "}
            <a href={areaHref("delivery_workflows", orgId)}>Workflow</a>.
          </p>
        ) : (
          <ul className="cc2-list">
            {data.running.map((run) => (
              <RunningRow key={run.run_id} run={run} orgId={orgId} now={now}
                          observedAt={data.observed_at} />
            ))}
          </ul>
        )}
      </section>

      <section className="cc2-section" id="cc2-recent" aria-labelledby="cc2-recent-h">
        <h2 id="cc2-recent-h" className="cc2-h">Recent outcomes</h2>
        {data.recent.length === 0 ? (
          <p className="cc2-empty">No run has finished in the last seven days.</p>
        ) : (
          <ul className="cc2-list">
            {data.recent.map((r) => (
              <li key={r.run_id} className="cc2-row" data-testid="cc2-recent-row">
                <span className={`cc2-pill ${outcomeTone(r.outcome)}`}>{r.outcome}</span>
                <div className="cc2-main">
                  <a className="cc2-title" href={runHref(r.run_id, orgId)}>
                    {r.subject || r.workflow_name}
                  </a>
                  <span className="cc2-sub">
                    {r.subject ? `${r.workflow_name} · ` : ""}
                    {r.error ? r.error : r.finished_at ? `finished ${shortWhen(r.finished_at)}` : ""}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function shortWhen(iso: string): string {
  // Date only, in UTC: identical on the server and in the browser, so it cannot mismatch on
  // hydration the way a locale time would.
  return iso.slice(0, 10);
}

function RunningRow({ run, orgId, now, observedAt }: {
  run: RunningRun; orgId?: string; now: number | null; observedAt?: string;
}) {
  // Live elapsed once mounted; the server's figure before that, so hydration matches.
  let elapsed = run.elapsed_seconds;
  if (now !== null && run.since) {
    const since = Date.parse(run.since);
    if (Number.isFinite(since)) elapsed = Math.max(0, (now - since) / 1000);
  } else if (now !== null && observedAt && elapsed !== null) {
    elapsed += Math.max(0, (now - Date.parse(observedAt)) / 1000);
  }
  const waiting = run.status === "waiting" || run.status === "blocked";
  return (
    <li className="cc2-row" data-testid="cc2-running-row">
      <span className={`cc2-pill ${waiting ? "amber" : "blue"}`}>{run.status}</span>
      <div className="cc2-main">
        <a className="cc2-title" href={runHref(run.run_id, orgId)}>
          {run.subject || run.workflow_name}
        </a>
        <span className="cc2-sub">
          {run.subject ? `${run.workflow_name} · ` : ""}
          {run.step_label ? <>on <b>{run.step_label}</b></> : "starting"}
          {run.agent && agentPhrase(run.agent) ? <> · {agentPhrase(run.agent)}</> : null}
          {run.blocked_reason ? <> · {run.blocked_reason}</> : null}
        </span>
      </div>
      <div className="cc2-meta">
        <span className="cc2-elapsed" title="Time on the current step">{elapsedLabel(elapsed)}</span>
        <span className="cc2-progress">{run.steps_done}/{run.step_count} steps</span>
      </div>
    </li>
  );
}

function NeedsYouRow({ item, orgId, projectId, actor, onDone }: {
  item: NeedsYouItem; orgId?: string; projectId: string; actor: string;
  onDone: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const tone = item.kind === "failed_step" ? "red"
    : item.decision_kind === "refusal" ? "muted" : "amber";

  // THE RUN'S OWN ANSWER, never one this screen decides on (see sendRunAnswer).
  const retryAnswer = item.kind === "failed_step" && item.run_id
    ? (item.answers ?? []).find((a) => a.key === "retry_step" && a.enabled) : undefined;

  async function retry() {
    if (!retryAnswer || !item.run_id) return;
    setBusy(true);
    setError("");
    try {
      const r = await sendRunAnswer(projectId, actor, item.run_id, retryAnswer);
      if (r.ok) {
        showToast("success", "Retrying — the step is back in the queue.");
        notifyChanged("command-center");
        await onDone();
      } else {
        setError(r.error ?? "");
        showToast("error", r.error ?? "Retry failed, and the server gave no reason.");
      }
    } catch (e) {
      setError(describeActionError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="cc2-row" data-testid={`cc2-needs-${item.kind}`}>
      <span className={`cc2-pill ${tone}`}>{needsYouLabel(item)}</span>
      <div className="cc2-main">
        <span className="cc2-title">{item.title}</span>
        {item.detail ? <span className="cc2-sub">{item.detail}</span> : null}
        {item.what_to_do ? <span className="cc2-todo">{item.what_to_do}</span> : null}
        {error ? <span className="cc2-error" role="alert">{error}</span> : null}
      </div>
      <div className="cc2-actions">
        {retryAnswer ? (
          <button type="button" className="button primary" disabled={busy} onClick={retry}
                  data-testid="cc2-retry" title={retryAnswer.label}>
            {busy ? "Retrying…" : "Retry"}
          </button>
        ) : null}
        {item.kind === "approval" && item.decision_id ? (
          <a className={`button ${item.decision_kind === "refusal" ? "secondary" : "primary"}`}
             href={decisionHref(item.decision_id, orgId)} data-testid="cc2-open-decision">
            {item.decision_kind === "refusal" ? "Open notice" : "Review and decide"}
          </a>
        ) : null}
        {item.fix_area ? (
          <a className="button secondary" href={areaHref(item.fix_area, orgId, item.fix_focus)}
             data-testid="cc2-fix">
            {item.fix_label ?? "Fix it"}
          </a>
        ) : null}
        {item.run_id ? (
          <a className="button ghost" href={runHref(item.run_id, orgId)} data-testid="cc2-open-run">
            Open the run
          </a>
        ) : null}
      </div>
    </li>
  );
}
