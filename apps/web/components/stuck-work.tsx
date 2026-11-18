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
import { isWaitingForGitHub } from "../lib/github-wait";
import { GitHubWait } from "./github-wait";
import { AgentAvatar } from "../app/agent-avatar";
import "./stuck-work.css";

/** The engine's wait code for a run the Infra Agent holds (stuck_work: `infra_hold`). */
export function isInfraHold(code: string | null | undefined): boolean {
  return !!code && /^infra_hold/.test(code);
}

const WHO_WORD: Record<string, string> = { you: "Your move", boss: "BOSS is on it", queue: "In the queue", stuck: "Stuck" };
const FIX_WORD: Record<string, string> = { needs_model: "Add a model" };
const STEP_WORD: Record<string, string> = { stuck: "Stuck", checked: "Checked", cause: "Cause", tried: "Tried", next: "Next" };

export function WhyLine({ why, testId = "why-line", compact = false }: { why: WaitReason | null | undefined; testId?: string; compact?: boolean }) {
  if (!why) return null;
  // WAITING FOR GITHUB (BOSSAPP-922): the one drawing, on every surface WhyLine is on.
  if (isWaitingForGitHub(why.code)) {
    return <span className="sw-why" data-who={why.who} data-code={why.code} data-testid={testId}><GitHubWait nextAt={why.nextAt} compact={compact} /></span>;
  }
  // PAUSED BY THE INFRA AGENT (BOSSAPP-1156): the one chip, with the agent's face, on every
  // surface a run shows why it waits. It stopped at a checkpoint; nothing is lost or repeated.
  if (isInfraHold(why.code)) {
    return (
      <span className="sw-why sw-why-infra" data-who={why.who} data-code={why.code} data-testid={testId}
            title={[why.words, why.detail || "Stopped at its last checkpoint; it continues by itself when the disk is back above target. Nothing is lost or repeated."].filter(Boolean).join("\n")}>
        <AgentAvatar agent="infra_steward" size={16} aiBadge={false} />
        <span className="sw-why-words">{why.words || "Paused by Infra Agent"}</span>
        {!compact && why.detail ? <span className="sw-why-detail">{why.detail}</span> : null}
        <span className="sw-sr">{WHO_WORD[why.who] ?? ""}</span>
      </span>
    );
  }
  return (
    <span className="sw-why" data-who={why.who} data-code={why.code} data-testid={testId}
          title={[why.words, why.detail].filter(Boolean).join("\n")} data-compact={compact || undefined}>
      <span className="sw-why-dot" aria-hidden="true" />
      <span className="sw-why-words">{why.words}</span>
      {!compact && why.detail ? <span className="sw-why-detail">{why.detail}</span> : null}
      {/* WHERE TO FIX IT (a run waiting for a model links to Setup › Models). */}
      {why.href ? <a className="sw-why-fix" href={why.href} data-testid={`${testId}-fix`}>{FIX_WORD[why.code] ?? "Fix it"}</a> : null}
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
