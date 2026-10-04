"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";

import { onChanged } from "./changed";
import { describeActionError, resultFromResponse } from "../lib/action-result";
import {
  areaHref,
  attentionFromNeedsYou,
  commandCenterFrom,
  decisionsHref,
  missionFromRunning,
  outcomeTone,
  runHref,
  type CommandCenterData,
} from "../lib/command-center";
import { AttentionCard } from "./agents/attention-card";
import { MissionRow } from "./agents/mission-row";

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
          // ONE DESIGN WITH AGENTS › LIVE: the same card, at row density (agents-home §3.3).
          <div className="cc2-list ag-att-rows">
            {data.needs_you.map((item) => {
              const card = attentionFromNeedsYou(item);
              return (
                <AttentionCard key={`${item.kind}:${card.id}`} item={card} projectId={projectId}
                               actor={actor} density="row" orgId={orgId} onDone={() => void load()}
                               onRefresh={() => void load()} />
              );
            })}
          </div>
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
          <ul className="cc2-list ag-mission-list">
            {data.running.map((run) => (
              <MissionRow key={run.run_id} density="row" now={now} orgId={orgId}
                          mission={missionFromRunning(run, data.needs_you.find(
                            (n) => n.run_id === run.run_id && n.kind === "approval")?.decision_id)} />
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

