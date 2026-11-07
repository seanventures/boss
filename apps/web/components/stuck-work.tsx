"use client";

/**
 * WhyLine — what a build waits for, in one line, with whose move it is.
 * DiagnosisFlow — what the Shepherd found when work stopped moving, as a small diagram:
 *   stuck → checked → cause → tried → next
 * Few words on each node; the detail sits behind it (title + an expandable line), per the
 * visibility rule (a diagram, not prose).
 */
import React, { useState } from "react";

import { diagnosedBy, type Diagnosis, type WaitReason } from "../lib/stuck-work";
import "./stuck-work.css";

const WHO_WORD: Record<string, string> = { you: "Your move", boss: "BOSS is on it", queue: "In the queue", stuck: "Stuck" };
const STEP_WORD: Record<string, string> = { stuck: "Stuck", checked: "Checked", cause: "Cause", tried: "Tried", next: "Next" };

export function WhyLine({ why, testId = "why-line", compact = false }: { why: WaitReason | null | undefined; testId?: string; compact?: boolean }) {
  if (!why) return null;
  return (
    <span className="sw-why" data-who={why.who} data-code={why.code} data-testid={testId}
          title={[why.words, why.detail].filter(Boolean).join("\n")} data-compact={compact || undefined}>
      <span className="sw-why-dot" aria-hidden="true" />
      <span className="sw-why-words">{why.words}</span>
      {!compact && why.detail ? <span className="sw-why-detail">{why.detail}</span> : null}
      <span className="sw-sr">{WHO_WORD[why.who] ?? ""}</span>
    </span>
  );
}

export function DiagnosisFlow({ d, testId = "diagnosis", compact = false }: { d: Diagnosis | null | undefined; testId?: string; compact?: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!d || !d.steps.length) return null;
  const shown = d.steps.find((st) => st.k === open);
  const next = d.steps.find((st) => st.k === "next");
  return (
    <section className="sw-diag" aria-label="What the Shepherd found" data-testid={testId}
             data-needs-person={d.needsPerson || undefined}>
      {/* CONCISE (operator, 2026-11-07): one line of cause, one line of what to do, the diagram;
          everything else is behind Details, linked to the run's log. */}
      <p className="sw-diag-cause" data-testid={`${testId}-cause-line`}><span className="sw-diag-k">Cause</span>{d.cause || d.headline}</p>
      {d.next || next?.detail ? (
        <p className="sw-diag-next" data-who={d.who || next?.who} data-testid={`${testId}-next-line`}>
          <span className="sw-diag-k">{(d.who || next?.who) === "you" ? "You" : "Next"}</span>{d.next || next?.detail}</p>
      ) : null}
      <ol className="sw-flow">
        {d.steps.map((st, i) => (
          <li key={st.k} className="sw-node" data-k={st.k} data-who={st.who}
              data-testid={`${testId}-${st.k}`}>
            <button type="button" className="sw-node-btn" aria-expanded={open === st.k}
                    title={st.detail || st.label} onClick={() => setOpen(open === st.k ? null : st.k)}>
              <span className="sw-node-k">{STEP_WORD[st.k]}</span>
              <span className="sw-node-label">{st.label}</span>
            </button>
            {i < d.steps.length - 1 ? <span className="sw-arrow" aria-hidden="true">→</span> : null}
          </li>
        ))}
      </ol>
      {shown && shown.detail ? <p className="sw-diag-detail" data-testid={`${testId}-detail`}>{shown.detail}</p> : null}
      {compact ? null : (
        <details className="sw-diag-more" data-testid={`${testId}-more`}>
          <summary>Details</summary>
          {d.headline ? <p className="sw-diag-headline" data-testid={`${testId}-headline`}>{d.headline}</p> : null}
          <dl className="sw-diag-kv">
            {d.tried ? <><dt>Tried</dt><dd>{d.tried}</dd></> : null}
            {d.whyNotFixed ? <><dt>Not fixed because</dt><dd data-testid={`${testId}-why-not`}>{d.whyNotFixed}</dd></> : null}
            <dt>Fixes</dt><dd>{d.fixes} of {d.maxFixes}</dd>
            <dt>By</dt><dd>{diagnosedBy(d)}{d.at ? ` · ${d.at.slice(11, 16)} UTC` : ""}</dd>
          </dl>
          <span className="sw-diag-links">
            {d.links.log ? <a href={d.links.log} data-testid={`${testId}-log`}>Live log ›</a> : null}
            {d.links.run ? <a href={d.links.run} data-testid={`${testId}-run`}>The build ›</a> : null}
          </span>
        </details>
      )}
    </section>
  );
}
