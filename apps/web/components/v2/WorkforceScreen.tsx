"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React, { useMemo, useState } from "react";

import type { V2Agent, Workforce } from "../../lib/v2/workforce";

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

function money(n: number): string {
  return "$" + (n < 10 ? n.toFixed(2) : Math.round(n).toLocaleString());
}

function ago(seconds: number | null): string {
  if (seconds === null) return "not seen in this window";
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

export function WorkforceScreen({ workforce }: { workforce: Workforce }) {
  const [filter, setFilter] = useState<Filter>("ALL");

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

      {workforce.agents.length === 0 ? (
        <p className="v2-empty" data-testid="v2-workforce-none">
          No agent roles are defined for this company yet.
        </p>
      ) : shown.length === 0 ? (
        <p className="v2-empty" data-testid="v2-workforce-filtered-out">
          No agents are {filter.toLowerCase()} right now.
        </p>
      ) : (
        <ul className="v2-rows" data-testid="v2-workforce-list">
          {shown.map((a) => (
            <AgentRow key={a.id} a={a} />
          ))}
        </ul>
      )}
    </section>
  );
}

function AgentRow({ a }: { a: V2Agent }) {
  return (
    <li className="v2-row v2-agent" data-testid={`v2-agent-${a.id}`} data-status={a.status}>
      <span className="v2-status v2-row-status" data-status={a.status}>
        <span className="v2-dot" data-status={a.status} aria-hidden />
        {a.status}
      </span>

      <span className="v2-row-main">
        <span className="v2-row-title">
          {a.name}
          <span className="v2-row-meta-dim"> · {a.role}</span>
        </span>
        <span className="v2-row-desc">{a.task}</span>

        <span className="v2-row-meta v2-mono">
          {/* The model it ACTUALLY used. Absent when nothing ran, rather than showing a
              configured model that never executed. */}
          {a.model ? (
            <span data-testid={`v2-agent-model-${a.id}`}>{a.model}</span>
          ) : (
            <span className="v2-row-meta-dim" data-testid={`v2-agent-nomodel-${a.id}`}>
              no calls recorded
            </span>
          )}
          {"  "}
          <b>{money(a.spend_usd)}</b>
          {"  "}
          <span className="v2-row-meta-dim">trust</span> {a.trust_score}
          {"  "}
          <span className="v2-row-meta-dim">{ago(a.last_seen_seconds)}</span>
          {/* Violations are surfaced, never buried. BOSS has recorded these all along and v1
              never showed them; an agent with violations is the one worth looking at. */}
          {a.violations > 0 ? (
            <>
              {"  "}
              <span className="v2-wf-violations" data-testid={`v2-agent-violations-${a.id}`}>
                {a.violations} violation{a.violations === 1 ? "" : "s"}
              </span>
            </>
          ) : null}
          {a.hidden_failures > 0 ? (
            <>
              {"  "}
              <span className="v2-wf-violations" data-testid={`v2-agent-hidden-${a.id}`}>
                {a.hidden_failures} hidden failure{a.hidden_failures === 1 ? "" : "s"}
              </span>
            </>
          ) : null}
        </span>
      </span>
    </li>
  );
}
