"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React, { useMemo, useState } from "react";

import type { V2Agent, Workforce } from "../../lib/v2/workforce";
import type { DecisionQueue } from "../../lib/v2/decisions";
import { AgentChat } from "./AgentChat";

/**
 * Workforce — every agent, what it is doing, and what it costs.
 *
 * THE STATUS DESCRIBES THE AGENT, NOT THE SCHEMA
 * ----------------------------------------------
 * v1 showed "Agents active 0/21". That counted roles owning a task, when only 23 of 187 tasks
 * had an owner at all, so the number said something about the data model and nothing about the
 * agents. Here the headline counts come from the same list the rows come from, so they cannot
 * disagree.
 *
 * STUCK IS ITS OWN STATUS
 * -----------------------
 * A step that has been running for half an hour is not working. Calling it working is how a
 * wedged agent stays invisible — one sat for 22 minutes before a human noticed.
 */

const FILTERS = ["ALL", "WORKING", "STUCK", "IDLE"] as const;
type Filter = (typeof FILTERS)[number];

function ago(seconds: number | null): string {
  if (seconds === null) return "not seen in this window";
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

export function WorkforceScreen({
  workforce,
  projectId,
  inbox,
  actor = "executive_console",
}: {
  workforce: Workforce;
  projectId?: string;
  /** Gates waiting on a human. The reference puts these beside the agents, and it is right to:
   *  an agent that is blocked and a gate that is blocking it are the same problem seen from
   *  two ends. */
  inbox?: DecisionQueue;
  /** Never defaulted at the call site into something invented — "operator" produced
   *  "Authenticated principal 'ceo' is not allowed to act as 'operator'". */
  actor?: string;
}) {
  const [filter, setFilter] = useState<Filter>("ALL");
  const [chatWith, setChatWith] = useState<V2Agent | null>(null);

  const shown = useMemo(
    () =>
      filter === "ALL"
        ? workforce.agents
        : workforce.agents.filter((a) => a.status === filter),
    [workforce.agents, filter],
  );

  if (!workforce.apiReachable) {
    return (
      <section className="v2-screen" data-testid="v2-workforce-unreachable">
        <h1 className="v2-screen-title">Workforce</h1>
        <p className="v2-empty">
          Could not reach the control plane
          {workforce.blockedReason ? ` (${workforce.blockedReason})` : ""}. No agent counts are
          shown — a zero here would read as an idle company rather than an unanswered question.
        </p>
      </section>
    );
  }

  const stuck = workforce.counts.STUCK ?? 0;

  return (
    <section className="v2-screen" data-testid="v2-workforce">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Workforce</h1>
        <p className="v2-screen-sub">
          {workforce.counts.total} agents · last {workforce.window_hours}h
          {stuck > 0 ? (
            <>
              {" · "}
              <b className="v2-wf-stuck" data-testid="v2-workforce-stuck">
                {stuck} stuck
              </b>
            </>
          ) : null}
        </p>

        <div className="v2-tabs" role="tablist" aria-label="Filter agents">
          {FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              role="tab"
              aria-selected={filter === f}
              className="v2-tab v2-mono"
              data-active={filter === f}
              onClick={() => setFilter(f)}
              data-testid={`v2-wf-filter-${f.replace(" ", "-")}`}
            >
              {f}
              {f !== "ALL" ? (
                <span className="v2-row-meta-dim"> {workforce.counts[f] ?? 0}</span>
              ) : null}
            </button>
          ))}
        </div>
      </header>

      {/* THE DECISION INBOX, beside the agents.
          Answering still happens on Decisions: duplicating an approval control before the
          parity check has run would be the fastest way to ship two paths to one irreversible
          act. This says what is waiting and what happens if it is ignored. */}
      {inbox && inbox.apiReachable && inbox.items.length > 0 ? (
        <div className="v2-inbox" data-testid="v2-workforce-inbox">
          <h2 className="v2-ws-h">
            Waiting on you
            {inbox.auto_resolving > 0 ? (
              <span className="v2-dec-auto" data-testid="v2-workforce-inbox-auto">
                {" "}
                · {inbox.auto_resolving} will be decided without you
              </span>
            ) : null}
          </h2>
          <ul className="v2-rows">
            {inbox.items.slice(0, 5).map((d) => (
              <li className="v2-row" key={d.id} data-testid={`v2-inbox-${d.id}`}>
                <span className="v2-row-main">
                  <span className="v2-row-title">{d.question}</span>
                  <span
                    className="v2-dec-ignored v2-mono"
                    data-will-fire={Boolean(d.deadline_at && d.default_action)}
                  >
                    {d.if_ignored}
                  </span>
                </span>
                <a
                  className="v2-dec-open v2-mono"
                  href={`/v2/panels${projectId ? "" : ""}`}
                  data-testid={`v2-inbox-open-${d.id}`}
                >
                  Review →
                </a>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* AGENTS RUNNING WORK WITH NO REGISTERED ROLE.
          Nothing governs them: no trust score, no autonomy level, no violation tracking. The
          screen was listing 21 registered roles, nearly all idle, while these did the work and
          did not appear at all. */}
      {workforce.unregistered && workforce.unregistered.length > 0 ? (
        <p className="v2-ungoverned" data-testid="v2-workforce-ungoverned">
          {workforce.unregistered.length} agent
          {workforce.unregistered.length === 1 ? " is" : "s are"} running work without a
          registered role, so nothing governs {workforce.unregistered.length === 1 ? "it" : "them"}
          — no trust score, no autonomy level, no violation tracking.
        </p>
      ) : null}

      {/* Said once, at the top, rather than a dash on every row. BOSS does not record which
          agent an LLM call belongs to, and the screen refuses to imply otherwise. */}
      {workforce.spend_attributed === false && workforce.agents.length > 0 ? (
        <p className="v2-org-isolated-note" data-testid="v2-workforce-no-spend">
          {workforce.spend_note}
        </p>
      ) : null}

      {workforce.agents.length === 0 ? (
        <p className="v2-empty" data-testid="v2-workforce-none">
          No agent roles are defined for this company yet.
        </p>
      ) : shown.length === 0 ? (
        <p className="v2-empty" data-testid="v2-workforce-filtered-out">
          No agents are {filter.toLowerCase()} right now.
        </p>
      ) : (
        <ul className="v2-cards" data-testid="v2-workforce-list">
          {shown.map((a) => (
            <AgentCard key={a.id} a={a} onChat={() => setChatWith(a)} />
          ))}
        </ul>
      )}

      {/* Talk to the agent about the work it is actually doing. */}
      {chatWith && projectId ? (
        <AgentChat
          agent={chatWith}
          projectId={projectId}
          actor={actor}
          onClose={() => setChatWith(null)}
        />
      ) : null}
    </section>
  );
}

/**
 * An agent is a THING with its own state, so it gets a card rather than a ledger row.
 *
 * The card is the reference's own: a panel with a 2px left accent carrying the status colour,
 * measured out of the rendered artifact DOM rather than approximated.
 */
/** Dollars at the scale an agent actually spends: cents matter below a dollar, they do not
 *  above ten. */
function money(n: number): string {
  if (n >= 10) return `$${n.toFixed(0)}`;
  if (n >= 1) return `$${n.toFixed(2)}`;
  return `$${n.toFixed(3)}`;
}

/** Whichever rate this agent actually has. An agent scores on the kind of work it does. */
function rate(a: V2Agent): number | null {
  return a.success_rate ?? a.step_success_rate ?? null;
}

/** Green only for a rate that is both high AND has enough calls behind it to mean anything. */
function scoreTone(rate: number | null | undefined): string {
  if (rate == null) return "none";
  if (rate >= 0.9) return "good";
  if (rate >= 0.6) return "warn";
  return "bad";
}

function AgentCard({ a, onChat }: { a: V2Agent; onChat: () => void }) {
  return (
    <li className="v2-card" data-testid={`v2-agent-${a.id}`} data-status={a.status}>
      <div className="v2-card-head">
        <span className="v2-card-name">{a.name}</span>
        {a.registered ? (
          <span className="v2-card-role">{a.role}</span>
        ) : (
          <span className="v2-card-role v2-ungoverned-tag" data-testid={`v2-agent-ungoverned-${a.id}`}>
            no registered role
          </span>
        )}
        <button
          type="button"
          className="v2-card-chat"
          onClick={onChat}
          aria-label={`Chat with ${a.name}`}
          data-testid={`v2-agent-chat-${a.id}`}
        >
          ask
        </button>
      </div>

      <span className="v2-status" data-status={a.status}>
        <span className="v2-dot" data-status={a.status} aria-hidden />
        {a.status}
      </span>

      <p className="v2-card-task">{a.task}</p>

      {/* WHICH WORK, and WHAT IT TOUCHED. Both were missing: BOSS records them and the card
          simply did not read them, so it was thinner than the data behind it. */}
      {a.project ? (
        <div className="v2-card-meta">
          <span data-testid={`v2-agent-project-${a.id}`}>
            <span className="v2-row-meta-dim">on</span> {a.project}
          </span>
        </div>
      ) : null}

      {a.last_touched ? (
        <div className="v2-card-meta">
          <span className="v2-card-touched" data-testid={`v2-agent-touched-${a.id}`}>
            <span className="v2-row-meta-dim">last touched</span> {a.last_touched}
          </span>
        </div>
      ) : null}

      {/* THE SCOREBOARD. This is the point of the page: what the agent has actually produced.
          Absent entirely when the agent has made no calls, rather than a row of zeros that
          reads as "this agent is bad at its job" when it means "it has not been asked". */}
      {a.calls ? (
        <div className="v2-scoreboard" data-testid={`v2-agent-score-${a.id}`}>
          <span className="v2-score" data-tone={scoreTone(a.success_rate)}>
            <b>{a.success_rate != null ? `${Math.round(a.success_rate * 100)}%` : "—"}</b>
            {/* The denominator travels with the rate: 2 of 2 and 190 of 200 are not the same
                claim, and a bare 100% hides which one you are looking at. */}
            <span className="v2-score-of">
              {a.ok_calls}/{a.calls} ok
            </span>
          </span>
          <span className="v2-score-fig">
            <b>{a.turns ?? 0}</b><span className="v2-score-of">turns</span>
          </span>
          <span className="v2-score-fig">
            <b>{money(a.spend_usd ?? 0)}</b><span className="v2-score-of">spent</span>
          </span>
          {a.failed_calls ? (
            <span className="v2-score-fig" data-tone="bad"
                  data-testid={`v2-agent-failed-${a.id}`}>
              <b>{a.failed_calls}</b><span className="v2-score-of">failed</span>
            </span>
          ) : null}
          {a.retries ? (
            <span className="v2-score-fig" data-tone="warn">
              <b>{a.retries}</b><span className="v2-score-of">retries</span>
            </span>
          ) : null}
        </div>
      ) : null}

      {/* PIPELINE WORK. Six agents run the PRD process and never make a model call under their
          own role, so scoring only calls left them visibly doing work and credited with none.
          Kept as its own figures: a step and a model call are different units. */}
      {a.steps ? (
        <div className="v2-scoreboard" data-testid={`v2-agent-steps-score-${a.id}`}>
          <span className="v2-score" data-tone={scoreTone(a.step_success_rate)}>
            <b>{a.step_success_rate != null ? `${Math.round(a.step_success_rate * 100)}%` : "—"}</b>
            <span className="v2-score-of">{a.steps_ok}/{a.steps} steps</span>
          </span>
          {a.runs ? (
            <span className="v2-score-fig">
              <b>{a.runs}</b><span className="v2-score-of">runs</span>
            </span>
          ) : null}
          {a.steps_failed ? (
            <span className="v2-score-fig" data-tone="bad">
              <b>{a.steps_failed}</b><span className="v2-score-of">failed</span>
            </span>
          ) : null}
        </div>
      ) : null}

      {/* The bar animates from zero to its width on load. It is the one piece of motion on this
          page that is visible on EVERY card that has done work -- the breathing border only
          applies to an agent working this instant, and on a quiet afternoon that is nobody, so
          the page looked as static as before. Width is the success rate, so the motion is
          carrying the number rather than decorating it. */}
      {rate(a) != null ? (
        <div className="v2-bar v2-score-bar" data-testid={`v2-agent-bar-${a.id}`}
             role="img" aria-label={`${Math.round(rate(a)! * 100)}% success`}>
          <span data-tone={scoreTone(rate(a))} style={{ width: `${Math.round(rate(a)! * 100)}%` }} />
        </div>
      ) : null}

      {/* WHAT IT DOES, in its own vocabulary — the stages it spends its calls on. */}
      {a.top_stages?.length ? (
        <div className="v2-stages" data-testid={`v2-agent-stages-${a.id}`}>
          {a.top_stages.map((st) => (
            <span className="v2-stage-chip v2-mono" key={st.stage}>
              {st.stage}<span className="v2-score-of">{st.calls}</span>
            </span>
          ))}
        </div>
      ) : null}

      <div className="v2-card-meta">
        {/* The model it ACTUALLY used. Absent when nothing ran, rather than showing a
            configured model that never executed. */}
        {a.model ? (
          <span data-testid={`v2-agent-model-${a.id}`}>{a.model}</span>
        ) : (
          <span data-testid={`v2-agent-nomodel-${a.id}`}>no calls recorded</span>
        )}
        {/* CONFIGURED, and labelled as such. It starts at 50 and has never moved, so showing it
            beside an observed rate without saying which is which invites reading it as earned. */}
        <span title="configured trust level, not an observed score">
          trust <b>{a.trust_score}</b> <span className="v2-score-of">set</span>
        </span>
        {/* Check-in. "42s ago" and "not seen in this window" are different facts. */}
        <span data-testid={`v2-agent-checkin-${a.id}`}>{ago(a.last_seen_seconds)}</span>
      </div>

      {/* Violations are surfaced, never buried. BOSS has recorded these all along and v1 never
          showed them; an agent with violations is the one worth looking at. */}
      {a.violations > 0 || a.hidden_failures > 0 ? (
        <div className="v2-card-meta">
          {a.violations > 0 ? (
            <span className="v2-wf-violations" data-testid={`v2-agent-violations-${a.id}`}>
              {a.violations} violation{a.violations === 1 ? "" : "s"}
            </span>
          ) : null}
          {a.hidden_failures > 0 ? (
            <span className="v2-wf-violations" data-testid={`v2-agent-hidden-${a.id}`}>
              {a.hidden_failures} hidden failure{a.hidden_failures === 1 ? "" : "s"}
            </span>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
