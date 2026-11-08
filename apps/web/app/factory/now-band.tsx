"use client";

/**
 * The Now band (build page, top of the body): what the build is doing, in one look — the stage
 * against its time limit, the attempt, where it was seeded from, your retry note, files so far,
 * the last test result with the failing tests, the gates and the cost. Short values; the
 * details are in tooltips and in the one list under Tests and Gates.
 */
import React from "react";

import type { Cockpit } from "../../lib/cockpit";
import { nowFacts, type NowFact } from "../../lib/build-detail";
import { WhyLine } from "../../components/stuck-work";
import { isWaitingForGitHub } from "../../lib/github-wait";

export function nowInput(c: Cockpit) {
  return {
    summary: c.summary, state: c.state, stations: c.stations, live: c.live,
    startedAt: c.startedAt, updatedAt: c.run.updatedAt, lineage: c.lineage,
    costUsd: c.costUsd, metered: c.metered, costWord: c.outputs.cost.word,
  };
}

/** The names behind a fact (failing tests, failed gates): one wrapping line under the facts. */
function Detail({ fact }: { fact: NowFact }) {
  if (!fact.detail?.length) return null;
  const shown = fact.detail.slice(0, 8);
  const rest = fact.detail.length - shown.length;
  return (
    <div className="fx-now-detail" data-tone={fact.tone} data-testid={`fx-now-detail-${fact.key}`}>
      <span className="fx-now-detail-label">{fact.key === "tests" ? "Failing" : "Failed gates"}</span>
      <ul>
        {shown.map((d) => <li key={d} className={fact.key === "tests" ? "fx-mono" : undefined} title={d} data-user-text>{d}</li>)}
        {rest > 0 ? <li className="fx-muted" title={fact.detail.slice(8).join("\n")}>+{rest} more</li> : null}
      </ul>
    </div>
  );
}

export function NowBand({ c, now }: { c: Cockpit; now: number | null }) {
  if (!c.summary) return null;
  const facts = nowFacts(nowInput(c), now);
  const [head, ...rest] = facts;
  const note = rest.find((f) => f.key === "note");
  const grid = rest.filter((f) => f.key !== "note");
  return (
    <section className="fx-nowband" aria-label="Now" data-testid="fx-nowband" data-state={c.state.state}>
      <div className="fx-nowband-head">
        <span className="fx-sec-title">NOW</span>
        <span className="fx-nowband-stage" data-tone={head.tone} title={head.tip} data-testid="fx-now-stage">{head.value}</span>
        {/* What it waits for, from the server (stuck_work.wait_reason): "Queued · 1h 19m" alone
            said nothing about what the build was waiting on. */}
        {c.why && !isWaitingForGitHub(c.why.code) ? <WhyLine why={c.why} testId="fx-now-why" compact /> : null}
        {note ? <span className="fx-nowband-note ag-trunc" title={`${note.tip}: ${note.value}`} data-user-text data-testid="fx-now-note">{note.value}</span> : null}
      </div>
      <dl className="fx-nowband-grid">
        {grid.map((f) => (
          <div key={f.key} className="fx-nowfact" data-key={f.key} data-tone={f.tone} data-testid={`fx-now-${f.key}`}>
            <dt>{f.label}</dt>
            <dd className="fx-nowfact-value ag-trunc" title={f.tip}>{f.value}</dd>
          </div>
        ))}
      </dl>
      {grid.map((f) => <Detail key={f.key} fact={f} />)}
    </section>
  );
}
