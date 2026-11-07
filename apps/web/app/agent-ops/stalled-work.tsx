"use client";

/**
 * STALLED WORK (Agents at work › Stalled work): what the Shepherd — ONE always-on agent scanning
 * every project's builds every minute — did about work that stopped moving:
 *   Fixed          it took a safe step and the work moved on;
 *   Trying         it took a step (at most a few per build) and is watching;
 *   Needs a person only what truly needs one: one line of what to do, why BOSS can't, the
 *                  diagram, Details behind a fold with the log.
 */
import React from "react";

import { DiagnosisFlow } from "../../components/stuck-work";
import { useConsoleLive } from "../../lib/console-live";
import { usePolledRead } from "../../lib/log-stream";
import { polled } from "../../lib/ambient";
import { parseStalledBoard, stalledPath, type StalledEntry } from "../../lib/stuck-work";

const SECTIONS: { key: "needsPerson" | "trying" | "fixed"; title: string; tip: string }[] = [
  { key: "needsPerson", title: "Needs a person", tip: "BOSS cannot fix these itself; each says why and what to do" },
  { key: "trying", title: "Trying", tip: "The Shepherd took a step and is watching it" },
  { key: "fixed", title: "Fixed", tip: "The Shepherd's step got it moving again" },
];

/** One line each: the build, its cause, what happens next; the diagram behind Details. */
function Row({ e }: { e: StalledEntry }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div className="sw-stalled-row" data-testid={`sw-stalled-${e.runId}`}>
      <span className="sw-stalled-line">
        <b data-user-text>{e.title || e.runId}</b>
        <span className="sw-stalled-cause" title={e.cause}>{e.cause}</span>
      </span>
      <span className="sw-stalled-next" data-who={e.who} title={e.next}>
        {e.who === "you" ? "You: " : "Next: "}{e.next}
      </span>
      <span className="sw-stalled-acts">
        <button type="button" className="ag-link" aria-expanded={open} data-testid={`sw-stalled-more-${e.runId}`}
                onClick={() => setOpen((v) => !v)}>{open ? "Hide details" : "Details ›"}</button>
        {e.links.log ? <a className="ag-link" href={e.links.log} data-testid={`sw-stalled-log-${e.runId}`}>Live log ›</a> : null}
      </span>
      {open ? (
        <DiagnosisFlow testId={`sw-stalled-diag-${e.runId}`} d={{
          at: e.at, headline: e.cause, cause: e.cause, chosen: "", acted: e.fixes > 0, tried: e.tried, next: e.next,
          who: e.who, needsPerson: !!e.whyNotFixed, cardId: e.cardId, by: { agent: "Shepherd" }, steps: e.steps,
          whyNotFixed: e.whyNotFixed, links: e.links, fixes: e.fixes, maxFixes: e.maxFixes,
        }} />
      ) : null}
    </div>
  );
}

export function StalledWork({ projectId }: { projectId: string }) {
  const live = useConsoleLive();
  const r = usePolledRead({ key: stalledPath(projectId), read: polled(stalledPath(projectId), parseStalledBoard),
                            pollMs: 30_000, paused: !live, enabled: !!projectId });
  const b = r.data;
  if (!b) return <p className="sw-stalled-empty" data-testid="sw-stalled-loading">Loading…</p>;
  return (
    <section className="sw-stalled" aria-label="Stalled work" data-testid="sw-stalled">
      <p className="sw-stalled-head" data-testid="sw-stalled-owner">
        {b.owner} scans every project&apos;s builds each minute · at most {b.maxFixes} fixes per build, then it stops and says why
      </p>
      {SECTIONS.map((sec) => (
        <div key={sec.key} className="sw-stalled-sec" data-testid={`sw-stalled-sec-${sec.key}`}>
          <h3 title={sec.tip}>{sec.title} <small>{b[sec.key].length}</small></h3>
          {b[sec.key].length ? b[sec.key].map((e) => <Row key={e.runId} e={e} />)
            : <p className="sw-stalled-empty">Nothing here.</p>}
        </div>
      ))}
    </section>
  );
}
