"use client";

import Link from "next/link";
// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React from "react";

import type { V2Run, Workspace } from "../../lib/v2/workspace";

/**
 * One app: where it stands, every attempt it took to get there, and what it cost.
 *
 * THE THREE EMPTY STATES ARE NOT THE SAME
 * ---------------------------------------
 * "no such app", "this app has no runs" and "the control plane did not answer" all render as
 * an empty page, and they call for completely different reactions. Conflating them is a
 * recurring defect in this console — an outage read as an empty company, a deployed app read
 * as never deployed — so each one says which it is.
 *
 * WHY EVERY ATTEMPT IS LISTED
 * ---------------------------
 * The lineage is the app. Judging by the newest run made BOSS refuse to deploy something whose
 * completed build was sitting two rows down, and a card that shows only the latest attempt
 * understates the cost by however many retries it took.
 */

function money(n: number): string {
  return "$" + (n < 100 ? n.toFixed(2) : Math.round(n).toLocaleString());
}

export function WorkspaceScreen({
  workspace,
  orgId,
}: {
  workspace: Workspace;
  orgId?: string;
}) {
  const q = orgId ? `?org=${encodeURIComponent(orgId)}` : "";

  if (workspace.blockedReason) {
    return (
      <section className="v2-screen" data-testid="v2-workspace-unreachable">
        <BackLink q={q} />
        <p className="v2-empty">
          Could not reach the control plane ({workspace.blockedReason}). Nothing here is a
          statement about this app — it will appear once the API answers.
        </p>
      </section>
    );
  }

  if (!workspace.found) {
    return (
      <section className="v2-screen" data-testid="v2-workspace-missing">
        <BackLink q={q} />
        <p className="v2-empty">
          No app with id <code className="v2-mono">{workspace.app_id}</code> in this company.
          It may have been renamed, or the link may be from another org.
        </p>
      </section>
    );
  }

  const runs = workspace.runs ?? [];

  return (
    <section className="v2-screen" data-testid="v2-workspace">
      <BackLink q={q} />

      <header className="v2-screen-head">
        <h1 className="v2-screen-title" data-testid="v2-workspace-title">
          {workspace.title}
        </h1>
        <p className="v2-screen-sub">{workspace.description}</p>

        <div className="v2-ws-facts v2-mono">
          <span className="v2-status" data-status={workspace.status}>
            <span className="v2-dot" data-status={workspace.status} aria-hidden />
            {workspace.status}
          </span>
          <span>
            <span className="v2-row-meta-dim">repo</span> {workspace.repo}
          </span>
          <span>
            <b>{runs.length}</b> {runs.length === 1 ? "attempt" : "attempts"}
          </span>
          <span data-testid="v2-workspace-spend">
            <b>{money(workspace.spend_usd ?? 0)}</b> spent
          </span>
        </div>
      </header>

      {/* DISCOVERY -> BUILD -> REVIEW -> SHIPPED, with how far it actually got. */}
      <ol className="v2-phases" data-testid="v2-workspace-phases">
        {(workspace.phases ?? []).map((p) => (
          <li
            key={p.id}
            className="v2-phase"
            data-reached={p.reached}
            data-testid={`v2-phase-${p.id}`}
          >
            <span className="v2-phase-dot" aria-hidden />
            <span className="v2-phase-label v2-mono">{p.label}</span>
          </li>
        ))}
      </ol>

      {/* Whether it can ship, and if not, WHY. A disabled control with no reason is what
          sends people to the API to find out. */}
      <p
        className="v2-ws-ship"
        data-deployable={workspace.deployable}
        data-testid="v2-workspace-deployability"
      >
        {workspace.deployable
          ? "Reviewed and ready to ship."
          : `Cannot ship yet — ${workspace.deployable_reason}`}
      </p>

      <h2 className="v2-ws-h">Attempts</h2>
      {runs.length === 0 ? (
        <p className="v2-empty" data-testid="v2-workspace-no-runs">
          This app exists but nothing has run yet.
        </p>
      ) : (
        <ul className="v2-rows" data-testid="v2-workspace-runs">
          {runs.map((r) => (
            <RunRow key={r.id} run={r} />
          ))}
        </ul>
      )}
    </section>
  );
}

function BackLink({ q }: { q: string }) {
  return (
    <Link href={`/v2${q}`} className="v2-back v2-mono" data-testid="v2-workspace-back">
      ← Projects
    </Link>
  );
}

function RunRow({ run }: { run: V2Run }) {
  return (
    <li className="v2-row v2-ws-run" data-testid={`v2-run-${run.id}`}>
      <span className="v2-status v2-row-status" data-status={run.status.toUpperCase()}>
        <span className="v2-dot" data-status={run.status.toUpperCase()} aria-hidden />
        {run.status}
      </span>

      <span className="v2-row-main">
        <span className="v2-row-title v2-mono">
          attempt {run.attempt}
          <span className="v2-row-meta-dim"> · {run.id}</span>
        </span>

        {/* A failure with no text is a dead end: the only move left is to go and read logs. */}
        {run.error ? (
          <span className="v2-run-error" data-testid={`v2-run-error-${run.id}`}>
            {run.error}
          </span>
        ) : null}

        <span className="v2-row-meta v2-mono">
          <b>{run.turns}</b> turns
          {"  "}
          <b>{money(run.spend_usd)}</b>
          {run.pull_request_number ? (
            <>
              {"  "}
              {/* Linked from the URL the run itself recorded. A card once linked to a PR
                  number that had been guessed and did not exist. */}
              {run.pull_request_url ? (
                <a
                  className="v2-meta-link"
                  href={run.pull_request_url}
                  target="_blank"
                  rel="noreferrer"
                  data-testid={`v2-run-pr-${run.id}`}
                >
                  PR #{run.pull_request_number} ↗
                </a>
              ) : (
                <span className="v2-row-meta-dim">PR #{run.pull_request_number}</span>
              )}
            </>
          ) : null}
        </span>
      </span>
    </li>
  );
}
