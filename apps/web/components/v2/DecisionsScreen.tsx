"use client";

import Link from "next/link";
// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React from "react";

import { useState } from "react";

import type {
  DecisionQueue, PanelFeed, V2Decision, V2Panel, V2Turn,
} from "../../lib/v2/decisions";

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

export function DecisionsScreen({ queue, panels }: { queue: DecisionQueue; panels?: PanelFeed }) {
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

      {panels ? <Panels panels={panels} /> : null}
    </section>
  );
}

/**
 * The panels: which one you are looking at on the left, what was said on the right.
 *
 * NOT A LIVE FEED, AND THE PAGE SAYS SO
 * -------------------------------------
 * The reference streams a scripted debate with a filling vote tally. BOSS's panels are real and
 * produce real minutes, but they are not watched live — so this shows where each one got to and
 * what each agent actually said, rather than animating turns that are not arriving. A fake live
 * feed would be the most convincing lie in this console: it would look exactly like the feature
 * working.
 *
 * NO STANCE, NO TALLY
 * -------------------
 * The reference labels every speaker FOR or AGAINST and totals them. BOSS records the words,
 * not a position, so the badges and the tally are absent rather than inferred from the text.
 * Guessing would put a vote in an agent's name on a page whose whole purpose is the record.
 */
function Panels({ panels }: { panels: PanelFeed }) {
  const [selected, setSelected] = useState(0);

  if (!panels.apiReachable) {
    return (
      <>
        <h2 className="v2-ws-h">Panels</h2>
        <p className="v2-empty" data-testid="v2-panels-unreachable">
          Could not reach the control plane
          {panels.blockedReason ? ` (${panels.blockedReason})` : ""}.
        </p>
      </>
    );
  }

  if (panels.sessions.length === 0) {
    return (
      <>
        <h2 className="v2-ws-h">Panels</h2>
        <p className="v2-org-isolated-note" data-testid="v2-panels-not-streamed">
          {panels.stream_note}
        </p>
        <p className="v2-empty" data-testid="v2-panels-none">
          No panels have convened for this company yet.
        </p>
      </>
    );
  }

  const current = panels.sessions[Math.min(selected, panels.sessions.length - 1)];

  return (
    <>
      <h2 className="v2-ws-h">Panels</h2>
      <p className="v2-org-isolated-note" data-testid="v2-panels-not-streamed">
        {panels.stream_note}
      </p>

      <div className="v2-wf-split" data-testid="v2-panels-list">
        <ul className="v2-wf-list">
          {panels.sessions.slice(0, 20).map((p, i) => (
            <li key={p.id}>
              <button
                type="button"
                className="v2-wf-card v2-panel-card"
                data-selected={i === selected}
                data-state={p.blocked ? "blocked" : p.deliberating ? "debating" : "concluded"}
                onClick={() => setSelected(i)}
                data-testid={`v2-panel-${p.id}`}
              >
                <span className="v2-panel-state v2-mono" data-state={p.blocked ? "blocked" : p.deliberating ? "debating" : "concluded"}>
                  {p.blocked ? "BLOCKED" : p.deliberating ? "DEBATING" : "CONCLUDED"}
                  {p.completed_at ? (
                    <span className="v2-row-meta-dim"> {p.completed_at.slice(11, 16)}</span>
                  ) : null}
                </span>
                <span className="v2-card-name">{p.title}</span>
                {p.purpose ? <span className="v2-wf-card-desc">{p.purpose}</span> : null}
              </button>
            </li>
          ))}
        </ul>

        <PanelDetail p={current} />
      </div>
    </>
  );
}

function PanelDetail({ p }: { p: V2Panel }) {
  return (
    <div className="v2-wf-detail" data-testid={`v2-panel-detail-${p.id}`}>
      <header className="v2-wf-detail-head">
        <h3 className="v2-wf-detail-title">{p.title}</h3>
        <span className="v2-panel-state v2-mono" data-state={p.blocked ? "blocked" : p.deliberating ? "debating" : "concluded"}>
          {p.blocked ? "BLOCKED" : p.deliberating ? "DEBATING" : "CONCLUDED"}
        </span>
      </header>

      {p.purpose ? <p className="v2-wf-detail-desc">{p.purpose}</p> : null}

      {/* Who was on it. */}
      {p.panelists.length > 0 ? (
        <p className="v2-panelists v2-mono" data-testid={`v2-panel-panelists-${p.id}`}>
          {p.panelists.map((name) => (
            <span className="v2-panelist" key={name}>
              <span className="v2-wf-owner-dot" aria-hidden />
              {name}
            </span>
          ))}
        </p>
      ) : null}

      {/* A blocked panel needs someone. Showing it as running or concluded loses that. */}
      {p.blocked && p.blocked_reason ? (
        <p className="v2-run-error" data-testid={`v2-panel-blocked-${p.id}`}>
          {p.blocked_reason}
        </p>
      ) : null}

      {/* WHAT WAS SAID. */}
      {p.turns.length === 0 ? (
        <p className="v2-empty" data-testid={`v2-panel-noturns-${p.id}`}>
          No deliberation was recorded for this panel, so what was argued is not available —
          only the outcome below.
        </p>
      ) : (
        <ol className="v2-turns" data-testid={`v2-panel-turns-${p.id}`}>
          {p.turns.map((t, i) => (
            <Turn key={i} t={t} panel={p.id} index={i} />
          ))}
        </ol>
      )}

      {/* The outcome, and any recorded objection. Dissent is never folded into the decision: a
          panel that agreed and a panel that overruled an objection produce the same decision
          and are not the same event. */}
      {p.synthesis || p.decisions.length > 0 || p.dissent.length > 0 ? (
        <div className="v2-verdict" data-testid={`v2-panel-verdict-${p.id}`}>
          <span className="v2-verdict-label v2-mono">PANEL VERDICT</span>
          {/* What the panel agreed, in its own words. The `decisions` column is empty on every
              real meeting; the synthesis is where the outcome actually is. */}
          {p.synthesis ? (
            <p className="v2-verdict-text" data-testid={`v2-panel-synthesis-${p.id}`}>
              {p.synthesis}
            </p>
          ) : null}
          {p.decisions.length > 0 ? (
            <p className="v2-verdict-text" data-testid={`v2-panel-decisions-${p.id}`}>
              {p.decisions.join(" · ")}
            </p>
          ) : null}
          {p.dissent.length > 0 ? (
            <p className="v2-panel-dissent" data-testid={`v2-panel-dissent-${p.id}`}>
              dissent: {p.dissent.join(" · ")}
            </p>
          ) : null}
        </div>
      ) : null}

      <p className="v2-row-meta v2-mono">
        {p.has_minutes ? (
          <span data-testid={`v2-panel-minutes-${p.id}`}>minutes recorded</span>
        ) : (
          <span className="v2-row-meta-dim" data-testid={`v2-panel-nominutes-${p.id}`}>
            no minutes yet
          </span>
        )}
      </p>
    </div>
  );
}

function Turn({ t, panel, index }: { t: V2Turn; panel: string; index: number }) {
  return (
    <li className="v2-turn" data-stance={t.stance ?? "none"} data-testid={`v2-turn-${panel}-${index}`}>
      <span className="v2-turn-head">
        <span className="v2-turn-speaker">{t.speaker}</span>
        {t.phase ? <span className="v2-turn-phase v2-mono">{t.phase}</span> : null}
        {/* Only when the transcript recorded one. BOSS does not, so inferring a position from
            the words would put a vote in an agent's name. */}
        {t.stance ? (
          <span className="v2-turn-stance v2-mono" data-stance={t.stance} data-testid={`v2-turn-stance-${panel}-${index}`}>
            {t.stance}
          </span>
        ) : null}
      </span>
      <p className="v2-turn-text">{t.text}</p>
    </li>
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
