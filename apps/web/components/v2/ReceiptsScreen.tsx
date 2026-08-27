"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React from "react";

import type { ReceiptFeed, V2Receipt } from "../../lib/v2/receipts";

/**
 * Receipts — what BOSS actually did, and what can be checked.
 *
 * EVERY NUMBER SAYS WHAT IT COUNTS
 * --------------------------------
 * A tile on the old console read "Verified today: 100". That was the page size: it fetched the
 * hundred most recent receipts and counted them, so it would have read 100 for a project with
 * a million and for a project with exactly a hundred. Here the window count and the number of
 * rows on screen are shown as two different things, labelled.
 *
 * UNFINISHED IS ITS OWN STATE
 * ---------------------------
 * A receipt sits at in_progress until its effect resolves, and if the API restarts mid-effect
 * nothing ever moves it. That is precisely what "the API restarted while this deployment was
 * running" leaves behind. Rolling those into "done" reports a clean day for a day that lost
 * work, so they get their own count and their own marker.
 */

/** Colour per family, so the eye can sort the ledger before reading a word of it. */
const FAMILY_TONE: Record<string, string> = {
  CODE: "mint", DEPLOY: "green", SPEND: "amber", GATE: "amber",
  PANEL: "purple", PROCESS: "blue", RESEARCH: "purple", SYSTEM: "dim", OTHER: "dim",
};

export function ReceiptsScreen({ feed }: { feed: ReceiptFeed }) {
  if (!feed.apiReachable) {
    return (
      <section className="v2-screen" data-testid="v2-receipts-unreachable">
        <h1 className="v2-screen-title">Receipts</h1>
        <p className="v2-empty">
          Could not reach the control plane
          {feed.blockedReason ? ` (${feed.blockedReason})` : ""}. No counts are shown, because a
          zero here would be indistinguishable from a quiet day.
        </p>
      </section>
    );
  }

  const work = feed.items.filter((r) => !r.routine);
  const routine = feed.items.filter((r) => r.routine);

  return (
    <section className="v2-screen" data-testid="v2-receipts">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Receipts</h1>
        <p className="v2-screen-sub">
          every effect BOSS applied in the last {feed.window_hours}h, with its provider
          reference
        </p>

        <div className="v2-ws-facts v2-mono">
          {/* Labelled with the window, so it can never be read as "all time" or as the page. */}
          <span data-testid="v2-receipts-window-total">
            <b>{feed.window_total.toLocaleString()}</b> in {feed.window_hours}h
          </span>
          {feed.window_unfinished > 0 ? (
            <span className="v2-rec-open" data-testid="v2-receipts-unfinished">
              <b>{feed.window_unfinished}</b> never finished
            </span>
          ) : null}
          {feed.window_failed > 0 ? (
            <span className="v2-rec-failed" data-testid="v2-receipts-failed">
              <b>{feed.window_failed}</b> failed
            </span>
          ) : null}
          {/* Said explicitly whenever the page is smaller than the window, so the list is not
              mistaken for the whole of it. */}
          {feed.shown < feed.window_total ? (
            <span className="v2-row-meta-dim" data-testid="v2-receipts-shown">
              showing the {feed.shown} most recent
            </span>
          ) : null}
        </div>
      </header>

      {feed.items.length === 0 ? (
        <p className="v2-empty" data-testid="v2-receipts-none">
          No effects applied in the last {feed.window_hours} hours.
        </p>
      ) : (
        <>
          {/* WORK FIRST, HEARTBEAT SECOND.
              1,901 receipts in a day and four of them were work. Sorted newest-first with no
              distinction, the work sat under forty-six ticks — which is exactly why the
              operator could not correlate anything here. */}
          {work.length > 0 ? (
            <ul className="v2-rows" data-testid="v2-receipts-list">
              {work.map((r) => <ReceiptRow key={r.id} r={r} />)}
            </ul>
          ) : (
            <p className="v2-empty" data-testid="v2-receipts-no-work">
              Nothing but routine upkeep in this window. BOSS was running, and nobody asked it
              to do anything.
            </p>
          )}

          {routine.length > 0 ? (
            <details className="v2-routine" data-testid="v2-receipts-routine">
              <summary className="v2-routine-head">
                {routine.length} routine {routine.length === 1 ? "effect" : "effects"} — BOSS
                keeping itself alive (ticks, watches, spend settlement)
              </summary>
              <ul className="v2-rows">
                {routine.map((r) => <ReceiptRow key={r.id} r={r} />)}
              </ul>
            </details>
          ) : null}
        </>
      )}
    </section>
  );
}

function ReceiptRow({ r }: { r: V2Receipt }) {
  return (
    <li
      className="v2-row v2-rec"
      data-testid={`v2-receipt-${r.id}`}
      data-unfinished={r.unfinished}
    >
      <span className="v2-status v2-row-status" data-status={r.status.toUpperCase()}>
        <span className="v2-dot" data-status={r.status.toUpperCase()} aria-hidden />
        {/* The word, not just a colour — and "never finished" rather than the raw status,
            because "in_progress" from three days ago is not in progress. */}
        {r.unfinished ? "never finished" : r.status}
      </span>

      <span className="v2-row-main">
        <span className="v2-row-title">
          {/* The family, as a coloured tag. Both halves of the identity are kept: the sentence
              says what happened, the dotted name says it precisely. */}
          <span className="v2-fam v2-mono" data-tone={FAMILY_TONE[r.family] ?? "dim"}
                data-testid={`v2-receipt-family-${r.id}`}>
            {r.family}
          </span>
          <span className="v2-rec-summary">{r.summary || r.effect}</span>
        </span>

        <span className="v2-row-meta v2-mono">
          <span className="v2-row-meta-dim">{r.effect}</span>
          {r.scope ? <span>{r.scope}</span> : null}
          {/* The link back to the work. This is the correlation that was missing entirely. */}
          {r.run_id ? (
            <a className="v2-meta-link" href={`/v2?run=${encodeURIComponent(r.run_id)}`}
               data-testid={`v2-receipt-run-${r.id}`}>
              {r.run_id} ↗
            </a>
          ) : null}
        </span>

        {r.error ? (
          <span className="v2-run-error" data-testid={`v2-receipt-error-${r.id}`}>
            {r.error}
          </span>
        ) : null}

        <span className="v2-row-meta v2-mono">
          {/* The provider's own reference is what makes this evidence rather than a claim:
              it can be checked against the system that did the work. */}
          {r.provider_ref ? (
            <span data-testid={`v2-receipt-ref-${r.id}`}>
              <span className="v2-row-meta-dim">ref</span> {r.provider_ref}
            </span>
          ) : (
            <span className="v2-row-meta-dim" data-testid={`v2-receipt-noref-${r.id}`}>
              no provider reference — this one cannot be independently checked
            </span>
          )}
          {r.created_at ? (
            <>
              {"  "}
              <span className="v2-row-meta-dim">
                {r.created_at.slice(0, 16).replace("T", " ")}
              </span>
            </>
          ) : null}
        </span>
      </span>
    </li>
  );
}
