"use client";

import React, { useMemo, useState } from "react";

import type { OrgChart, OrgNode, V2Agent, Workforce } from "../lib/v2/workforce";

/**
 * Workforce and Org Chart, in v1's own styling.
 *
 * The data is the same the v2 screens read — one fetch each, already bounded server-side. What
 * differs is the chrome: v2's classes live under `.v2-shell` and render unstyled here, so these
 * are built from v1's own card, badge and block vocabulary rather than by importing components
 * that would arrive naked.
 */

/**
 * Digit grouping WITHOUT toLocaleString.
 *
 * The server and the browser can disagree about locale, so toLocaleString() renders "16,428"
 * on one and "16 428" on the other and React tears the tree down on hydration. There is a test
 * that forbids it here, and it caught this.
 */
function groupDigits(n: number): string {
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function money(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 10) return `$${n.toFixed(0)}`;
  if (n >= 1) return `$${n.toFixed(2)}`;
  return `$${n.toFixed(3)}`;
}

/** Acronyms stay acronyms: "Qa" and "Cto" read as typos. */
const ACRONYMS: Record<string, string> = {
  qa: "QA", cto: "CTO", coo: "COO", cao: "CAO", ux: "UX", vp: "VP", pm: "PM", sre: "SRE",
};
function humanise(key: string): string {
  return key.split("_")
    .map((w) => ACRONYMS[w.toLowerCase()] ?? (w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

function toneFor(status: string): string {
  if (status === "WORKING") return "green";
  if (status === "STUCK") return "red";
  if (status === "AT GATE") return "amber";
  return "muted";
}

/** Whichever rate this agent has — it scores on the kind of work it does. */
function rateOf(a: V2Agent): number | null {
  return a.success_rate ?? a.step_success_rate ?? null;
}
function volumeOf(a: V2Agent): number {
  return (a.calls ?? 0) + (a.steps ?? 0);
}

export function WorkforceArea({ workforce }: { workforce: Workforce }) {
  const [filter, setFilter] = useState<"all" | "working" | "worked">("all");

  const agents = useMemo(() => {
    const list = workforce.agents ?? [];
    if (filter === "working") return list.filter((a) => a.status === "WORKING" || a.status === "STUCK");
    if (filter === "worked") return list.filter((a) => volumeOf(a) > 0);
    return list;
  }, [workforce.agents, filter]);

  // The busiest agent sets the scale, so every bar is comparable.
  const busiest = Math.max(1, ...(workforce.agents ?? []).map(volumeOf));
  const counts = workforce.counts ?? { WORKING: 0, STUCK: 0, IDLE: 0, total: 0 };

  if (!workforce.apiReachable) {
    return (
      <p className="detail">
        Could not reach the control plane{workforce.blockedReason ? ` — ${workforce.blockedReason}` : ""}.
        An empty roster would read as a company with no agents.
      </p>
    );
  }

  return (
    <div className="wf">
      <div className="wf-bar">
        <span className="detail">
          {counts.total} agents · last {workforce.window_hours}h · scored over 30 days
        </span>
        <span className="wf-filters">
          {(["all", "working", "worked"] as const).map((f) => (
            <button
              key={f}
              type="button"
              className="cap-btn"
              data-active={filter === f}
              onClick={() => setFilter(f)}
              data-testid={`wf-filter-${f}`}
            >
              {f === "all" ? `All ${counts.total}` : f === "working" ? `Active ${counts.WORKING}` : "Has done work"}
            </button>
          ))}
        </span>
      </div>

      {/* Governance gap, said once and at the top: an agent running work without a registered
          role has no trust score, no autonomy level and no violation tracking. */}
      {(workforce.agents ?? []).some((a) => !a.registered) ? (
        <p className="detail wf-ungoverned" data-testid="wf-ungoverned">
          {(workforce.agents ?? []).filter((a) => !a.registered).length} agents are working without
          a registered role — nothing governs them.
        </p>
      ) : null}

      {agents.length === 0 ? (
        <p className="detail">No agent matches this filter.</p>
      ) : (
        <div className="wf-grid" data-testid="wf-grid">
          {agents.map((a, i) => {
            const rate = rateOf(a);
            const share = volumeOf(a) > 0 ? volumeOf(a) / busiest : null;
            const tone = rate == null ? "none" : rate >= 0.9 ? "good" : rate >= 0.6 ? "warn" : "bad";
            return (
              <article className="wf-card" key={a.id} data-status={a.status}
                       data-testid={`wf-agent-${a.id}`}>
                <header className="wf-card-head">
                  {volumeOf(a) > 0 ? <span className="wf-rank">{i + 1}</span> : null}
                  <span className="wf-name">{a.name}</span>
                  <span className={`cap-badge tone-${toneFor(a.status)}`}>{a.status}</span>
                </header>

                <p className="wf-task">{a.task}</p>

                {/* THE SCOREBOARD — what this agent has produced. Absent entirely when it has
                    done nothing, rather than a row of zeros that reads as "bad at its job". */}
                {volumeOf(a) > 0 ? (
                  <>
                    <div className="wf-figs" data-testid={`wf-score-${a.id}`}>
                      <span className="wf-fig" data-tone={tone}>
                        <b>{rate != null ? `${Math.round(rate * 100)}%` : "—"}</b>
                        <span className="wf-unit">
                          {a.calls ? `${a.ok_calls}/${a.calls} ok` : `${a.steps_ok}/${a.steps} steps`}
                        </span>
                      </span>
                      {a.turns ? (
                        <span className="wf-fig"><b>{groupDigits(a.turns)}</b><span className="wf-unit">turns</span></span>
                      ) : null}
                      {a.runs ? (
                        <span className="wf-fig"><b>{a.runs}</b><span className="wf-unit">runs</span></span>
                      ) : null}
                      {a.spend_usd ? (
                        <span className="wf-fig"><b>{money(a.spend_usd)}</b><span className="wf-unit">spent</span></span>
                      ) : null}
                      {a.failed_calls ? (
                        <span className="wf-fig" data-tone="bad"><b>{a.failed_calls}</b><span className="wf-unit">failed</span></span>
                      ) : null}
                    </div>
                    {/* Length is VOLUME, colour is quality. Success rate barely varies here —
                        nearly everyone sits at 100% — so length carrying it would draw a
                        column of identical full bars saying nothing. */}
                    {share != null ? (
                      <span className="wf-bar" data-testid={`wf-bar-${a.id}`}>
                        <span data-tone={tone} style={{ width: `${Math.max(3, Math.round(share * 100))}%` }} />
                      </span>
                    ) : null}
                  </>
                ) : null}

                {a.top_stages?.length ? (
                  <div className="wf-chips">
                    {a.top_stages.map((s) => (
                      <span className="wf-chip" key={s.stage}>{s.stage}<i>{s.calls}</i></span>
                    ))}
                  </div>
                ) : null}

                <footer className="wf-card-foot">
                  <span>{a.model ?? "no model calls"}</span>
                  {/* CONFIGURED, and labelled. It starts at 50 and has never moved; beside an
                      observed rate it would otherwise read as earned. */}
                  <span title="configured trust level, not an observed score">
                    trust <b>{a.trust_score}</b> set
                  </span>
                  {!a.registered ? (
                    <span className="wf-ungov" data-testid={`wf-unreg-${a.id}`}>no registered role</span>
                  ) : null}
                </footer>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function OrgChartArea({ chart }: { chart: OrgChart }) {
  if (!chart.apiReachable) {
    return <p className="detail">Could not reach the control plane. An empty chart would read as a company with nobody in it.</p>;
  }
  const tree = chart.tree ?? [];
  const levels = new Map<number, OrgNode[]>();
  for (const n of tree) {
    const d = n.depth ?? 0;
    levels.set(d, [...(levels.get(d) ?? []), n]);
  }
  const rows = [...levels.entries()].sort((a, b) => a[0] - b[0]);
  const isolated = (chart.nodes ?? []).filter((n) => !n.connected);

  return (
    <div className="org">
      <p className="detail" data-testid="org-provenance">
        {chart.note || `Derived from ${chart.derived_from}.`}
        {chart.root_inferred
          ? " Every agent here is handed work by someone, so the busiest was taken as the top."
          : ""}
      </p>

      {tree.length === 0 ? (
        <p className="detail" data-testid="org-empty">
          No work has passed between agents in the last {chart.window_hours} hours, so there is no
          observed structure to draw yet.
        </p>
      ) : (
        <div className="org-tree" data-testid="org-tree">
          {rows.map(([depth, nodes]) => (
            <div className="org-level" key={depth} data-depth={depth}>
              {nodes.map((n) => (
                <div className="org-node" key={n.id} data-root={depth === 0}
                     data-testid={`org-node-${n.id}`}>
                  <span className="org-node-name">{n.name}</span>
                  {/* Only when it ADDS something. "Product Manager / Product Manager" and
                      "CTO / CTO" are the same duplication the operator flagged elsewhere. */}
                  {humanise(n.role || n.id) !== n.name ? (
                    <span className="org-node-role">{humanise(n.role || n.id)}</span>
                  ) : null}
                  {n.task ? <span className="org-node-task">{n.task}</span> : null}
                  {!n.registered ? (
                    <span className="wf-ungov" data-testid={`org-unreg-${n.id}`}>no registered role</span>
                  ) : null}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      {isolated.length > 0 ? (
        <>
          <div className="ct-block-title" style={{ marginTop: 18 }}>Working alone</div>
          <p className="detail">
            No work passed to or from these in {chart.window_hours}h — idle by design, or forgotten.
          </p>
          <div className="org-isolated" data-testid="org-isolated">
            {isolated.map((n) => (
              <span className="org-chip" key={n.id}>
                {n.name}
                {humanise(n.role || n.id) !== n.name ? <i>{humanise(n.role || n.id)}</i> : null}
              </span>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}
