"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React, { useMemo, useState } from "react";

import type { V2Agent, Workforce } from "../../lib/v2/workforce";
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
  actor = "executive_console",
}: {
  workforce: Workforce;
  projectId?: string;
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
function AgentCard({ a, onChat }: { a: V2Agent; onChat: () => void }) {
  return (
    <li className="v2-card" data-testid={`v2-agent-${a.id}`} data-status={a.status}>
      <div className="v2-card-head">
        <span className="v2-card-name">{a.name}</span>
        <span className="v2-card-role">{a.role}</span>
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

      <div className="v2-card-meta">
        {/* The model it ACTUALLY used. Absent when nothing ran, rather than showing a
            configured model that never executed. */}
        {a.model ? (
          <span data-testid={`v2-agent-model-${a.id}`}>{a.model}</span>
        ) : (
          <span data-testid={`v2-agent-nomodel-${a.id}`}>no calls recorded</span>
        )}
        <span>
          trust <b>{a.trust_score}</b>
        </span>
        <span>{ago(a.last_seen_seconds)}</span>
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
