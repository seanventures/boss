"use client";

import Link from "next/link";
// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React from "react";

import type { DecisionQueue, V2Decision } from "../../lib/v2/decisions";

/**
 * Decisions — every gate waiting on a human.
 *
 * THE CONSEQUENCE IS THE HEADLINE, NOT THE QUESTION
 * -------------------------------------------------
 * A gate carries a deadline and a default action, so an unanswered decision does not wait: it
 * resolves itself, and the work either proceeds unreviewed or is dropped. A queue that shows
 * only the question reads as a list of things you could get to later, when it is actually a
 * list of things about to be decided for you.
 *
 * So each row leads with what happens if it is ignored, and the count of self-resolving gates
 * sits at the top — that is the number worth interrupting someone for.
 */

export function DecisionsScreen({ queue }: { queue: DecisionQueue }) {
  if (!queue.apiReachable) {
    return (
      <section className="v2-screen" data-testid="v2-decisions-unreachable">
        <h1 className="v2-screen-title">Decisions</h1>
        <p className="v2-empty">
          Could not reach the control plane
          {queue.blockedReason ? ` (${queue.blockedReason})` : ""}. This is NOT &ldquo;no gates
          are waiting&rdquo; — an unanswered gate can resolve itself, so an empty list here
          would be the most dangerous thing this page could say.
        </p>
      </section>
    );
  }

  return (
    <section className="v2-screen" data-testid="v2-decisions">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Decisions</h1>
        <p className="v2-screen-sub">
          {queue.total === 0
            ? "nothing is waiting on you"
            : `${queue.total} waiting on you`}
          {queue.auto_resolving > 0 ? (
            <>
              {" · "}
              <b className="v2-dec-auto" data-testid="v2-decisions-auto">
                {queue.auto_resolving} will be decided without you
              </b>
            </>
          ) : null}
        </p>
      </header>

      {queue.items.length === 0 ? (
        <p className="v2-empty" data-testid="v2-decisions-none">
          No open gates. Work is not waiting on an approval right now.
        </p>
      ) : (
        <ul className="v2-rows" data-testid="v2-decisions-list">
          {queue.items.map((d) => (
            <DecisionRow key={d.id} d={d} />
          ))}
        </ul>
      )}
    </section>
  );
}

function DecisionRow({ d }: { d: V2Decision }) {
  // A gate with a deadline AND a default will fire on its own. That is the one distinction
  // this row exists to make visible.
  const willFire = Boolean(d.deadline_at && d.default_action);
  return (
    <li className="v2-row v2-dec" data-testid={`v2-decision-${d.id}`} data-will-fire={willFire}>
      <span className="v2-row-main">
        <span className="v2-row-title">{d.question}</span>

        {d.recommendation ? (
          <span className="v2-row-desc" data-testid={`v2-decision-rec-${d.id}`}>
            {d.recommendation}
          </span>
        ) : null}

        {/* What happens if nobody answers. Leading, not buried in metadata. */}
        <span
          className="v2-dec-ignored v2-mono"
          data-will-fire={willFire}
          data-testid={`v2-decision-if-ignored-${d.id}`}
        >
          {willFire ? "⏳ " : ""}
          {d.if_ignored}
          {d.deadline_at ? (
            <span className="v2-row-meta-dim"> · by {d.deadline_at.slice(0, 16).replace("T", " ")}</span>
          ) : null}
        </span>

        <span className="v2-row-meta v2-mono">
          <span className="v2-row-meta-dim">risk</span> tier {d.risk_tier}
          {d.status !== "pending" ? (
            <>
              {"  "}
              <span className="v2-row-meta-dim">status</span> {d.status}
              {d.delegated_to ? ` → ${d.delegated_to}` : ""}
            </>
          ) : null}
          {/* The options, so the row says how it can be answered rather than only that it
              must be. A question with no visible answers is a dead end. */}
          {d.options.length > 0 ? (
            <>
              {"  "}
              <span className="v2-row-meta-dim">options</span> {d.options.join(" · ")}
            </>
          ) : null}
        </span>
      </span>

      {/* Answering still happens in v1's Approvals area: duplicating an approval control
          before the parity check has run would be the fastest way to ship two paths to one
          irreversible act. */}
      <Link
        className="v2-dec-open v2-mono"
        href={`/?area=decisions#${encodeURIComponent(d.id)}`}
        data-testid={`v2-decision-open-${d.id}`}
      >
        Review →
      </Link>
    </li>
  );
}
