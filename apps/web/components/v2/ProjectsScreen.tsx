"use client";

import Link from "next/link";
// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React, { useMemo, useState } from "react";

import type { CompanyOverview, V2Project } from "../../lib/v2/overview";

/**
 * Projects — every project across the company.
 *
 * One row per app, where an app is a LINEAGE of runs rather than a single run. That
 * distinction is load-bearing: judging an app by its newest run once made BOSS refuse to
 * deploy something whose completed build was sitting in the same lineage.
 */

const FILTERS = ["ALL", "RUNNING", "GATED", "DONE"] as const;
type Filter = (typeof FILTERS)[number];

function money(n: number): string {
  return "$" + (n < 100 ? n.toFixed(2) : Math.round(n).toLocaleString());
}

export function ProjectsScreen({
  overview,
  orgId,
}: {
  overview: CompanyOverview;
  orgId?: string;
}) {
  const [filter, setFilter] = useState<Filter>("ALL");
  const q = orgId ? `?org=${encodeURIComponent(orgId)}` : "";

  const shown = useMemo(
    () =>
      filter === "ALL"
        ? overview.projects
        : overview.projects.filter((p) => p.status === filter),
    [overview.projects, filter],
  );

  return (
    <section className="v2-screen" data-testid="v2-projects">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Projects</h1>
        <p className="v2-screen-sub">every project across {overview.company.name}</p>

        <div className="v2-tabs" role="tablist" aria-label="Filter projects">
          {FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              role="tab"
              aria-selected={filter === f}
              className="v2-tab v2-mono"
              data-active={filter === f}
              onClick={() => setFilter(f)}
              data-testid={`v2-filter-${f}`}
            >
              {f}
            </button>
          ))}
        </div>
      </header>

      {/* Distinguish "nothing matches this filter" from "the control plane is unreachable"
          and from "this company has no projects". They look identical as an empty list and
          call for completely different reactions. */}
      {!overview.apiReachable ? (
        <p className="v2-empty" data-testid="v2-projects-unreachable">
          Could not reach the control plane{overview.blockedReason ? ` (${overview.blockedReason})` : ""}.
          Nothing here is a statement about your projects — this page will show them once the
          API answers.
        </p>
      ) : overview.projects.length === 0 ? (
        <p className="v2-empty" data-testid="v2-projects-none">
          No projects yet. Describe an outcome and BOSS will start one.
        </p>
      ) : shown.length === 0 ? (
        <p className="v2-empty" data-testid="v2-projects-filtered-out">
          No projects are {filter.toLowerCase()} right now.
        </p>
      ) : (
        <ul className="v2-rows">
          {shown.map((p) => (
            <ProjectRow key={p.id} p={p} q={q} />
          ))}
        </ul>
      )}
    </section>
  );
}

function ProjectRow({ p, q }: { p: V2Project; q: string }) {
  return (
    <li className="v2-row" data-testid={`v2-project-${p.id}`}>
      <Link href={`/v2/missions/${encodeURIComponent(p.id)}${q}`} className="v2-row-link">
        {/* Status is a dot AND a word — never colour alone, so it survives greyscale and
            colourblind readers. */}
        <span className="v2-status v2-row-status" data-status={p.status}>
          <span className="v2-dot" data-status={p.status} aria-hidden />
          {p.status}
        </span>

        <span className="v2-row-main">
          <span className="v2-row-title">{p.title}</span>
          {p.description ? <span className="v2-row-desc">{p.description}</span> : null}
          <span className="v2-row-meta v2-mono">
            <span className="v2-row-meta-dim">repo</span> {p.repo}
            {"  "}
            <b>{p.runs}</b> {p.runs === 1 ? "run" : "runs"}
            {"  "}
            <b>{p.steps}</b> steps
            {"  "}
            <b>{money(p.spend_usd)}</b> spent
            {p.pull_request_number ? (
              <>
                {"  "}
                <span className="v2-row-meta-dim">PR</span> #{p.pull_request_number}
              </>
            ) : null}
          </span>
        </span>

        <span className="v2-row-progress">
          <span className="v2-row-progress-head v2-mono">
            <span>{p.stage}</span>
            <span>{p.pct}%</span>
          </span>
          <span className="v2-bar" data-status={p.status}>
            <span style={{ width: `${p.pct}%` }} />
          </span>
        </span>
      </Link>
    </li>
  );
}
