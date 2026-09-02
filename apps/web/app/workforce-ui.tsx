"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";

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

/**
 * How recently this agent did anything, as a word.
 *
 * The page is ranked by VOLUME, which says who has done the most but not who is doing it now —
 * a month of work and ten minutes ago otherwise render identically. Recency is its own signal
 * and it is what the motion is keyed to, so the movement on this page always means something.
 */
function recency(a: V2Agent, now: number | null): "live" | "today" | "week" | "cold" {
  // `now` is null until the component has mounted. Reading the clock DURING RENDER makes the
  // server and the browser disagree — the server stamps one instant into the HTML and the
  // browser computes another on hydration — and React tears the tree down. There is a test in
  // this repo for exactly that, and it caught this.
  if (now == null) return "cold";
  const t = (a as { last_activity_at?: string | null }).last_activity_at;
  if (!t) return "cold";
  const ms = now - new Date(t).getTime();
  if (Number.isNaN(ms)) return "cold";
  if (ms < 15 * 60_000) return "live";
  if (ms < 24 * 3_600_000) return "today";
  if (ms < 7 * 24 * 3_600_000) return "week";
  return "cold";
}

/** Whichever rate this agent has — it scores on the kind of work it does. */
function rateOf(a: V2Agent): number | null {
  return a.success_rate ?? a.step_success_rate ?? null;
}
function volumeOf(a: V2Agent): number {
  return (a.calls ?? 0) + (a.steps ?? 0);
}

/** Where to go to put a question to one of these agents, keeping who you are. */
function askHref(agentName: string): string {
  const params = new URLSearchParams(
    typeof window !== "undefined" ? window.location.search : "");
  const next = new URLSearchParams();
  for (const key of ["org", "org_id"]) {
    const value = params.get(key);
    if (value) next.set(key, value);
  }
  next.set("area", "agent");
  // A colon, not an em dash: "—" percent-encodes to %E2%80%94, which contains the substring
  // "0%" — and the roster asserts it never renders a zero score. A link that trips a guard
  // about scores is a link that will be misread by the next person to see it fail.
  next.set("ask", `@${agentName}: `);
  return `?${next.toString()}`;
}

export function WorkforceArea({ workforce }: { workforce: Workforce }) {
  const [filter, setFilter] = useState<"all" | "working" | "worked">("all");
  // Set after mount, so the first client paint matches the server HTML and the recency-driven
  // motion starts on the frame after hydration rather than fighting it.
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(id);
  }, []);

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
    <div className="roster">
      <div className="roster-filterbar">
        <span className="detail">
          {counts.total} agents · last {workforce.window_hours}h · scored over 30 days
        </span>
        <span className="roster-filters">
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
        <p className="detail roster-ungoverned" data-testid="roster-ungoverned">
          {(workforce.agents ?? []).filter((a) => !a.registered).length} agents are working without
          a registered role — nothing governs them.
        </p>
      ) : null}

      {agents.length === 0 ? (
        <p className="detail">No agent matches this filter.</p>
      ) : (
        <div className="roster-grid" data-testid="roster-grid">
          {agents.map((a, i) => {
            const rate = rateOf(a);
            const share = volumeOf(a) > 0 ? volumeOf(a) / busiest : null;
            const tone = rate == null ? "none" : rate >= 0.9 ? "good" : rate >= 0.6 ? "warn" : "bad";
            return (
              <article className="roster-card" key={a.id} data-status={a.status}
                       data-recency={recency(a, now)} data-rank={i + 1}
                       style={{ ["--i" as string]: String(Math.min(i, 11)) }}
                       data-testid={`wf-agent-${a.id}`}>
                <header className="roster-card-head">
                  {volumeOf(a) > 0 ? <span className="roster-rank">{i + 1}</span> : null}
                  <span className="roster-name">{a.name}</span>
                  <span className={`cap-badge tone-${toneFor(a.status)}`}>{a.status}</span>
                  {/* PUT A QUESTION TO THIS ONE. The page listed what every agent had done and
                      offered no way to say anything to any of them — read-only, on a screen
                      about a workforce. This opens the chat with the message already addressed,
                      so asking is a click from the card that named them. */}
                  <a className="roster-ask" href={askHref(a.name)}
                     data-testid={`wf-ask-${a.id}`}
                     title={`Ask ${a.name} something`}>Ask</a>
                </header>

                <p className="roster-task">{a.task}</p>

                {/* THE SCOREBOARD — what this agent has produced. Absent entirely when it has
                    done nothing, rather than a row of zeros that reads as "bad at its job". */}
                {volumeOf(a) > 0 ? (
                  <>
                    <div className="roster-figs" data-testid={`wf-score-${a.id}`}>
                      <span className="roster-fig" data-tone={tone}>
                        <b>{rate != null ? `${Math.round(rate * 100)}%` : "—"}</b>
                        <span className="roster-unit">
                          {a.calls ? `${a.ok_calls}/${a.calls} ok` : `${a.steps_ok}/${a.steps} steps`}
                        </span>
                      </span>
                      {a.turns ? (
                        <span className="roster-fig"><b>{groupDigits(a.turns)}</b><span className="roster-unit">turns</span></span>
                      ) : null}
                      {a.runs ? (
                        <span className="roster-fig"><b>{a.runs}</b><span className="roster-unit">runs</span></span>
                      ) : null}
                      {a.spend_usd ? (
                        <span className="roster-fig"><b>{money(a.spend_usd)}</b><span className="roster-unit">spent</span></span>
                      ) : null}
                      {a.failed_calls ? (
                        <span className="roster-fig" data-tone="bad"><b>{a.failed_calls}</b><span className="roster-unit">failed</span></span>
                      ) : null}
                    </div>
                    {/* Length is VOLUME, colour is quality. Success rate barely varies here —
                        nearly everyone sits at 100% — so length carrying it would draw a
                        column of identical full bars saying nothing. */}
                    {share != null ? (
                      <span className="roster-scorebar" data-testid={`wf-bar-${a.id}`}>
                        <span data-tone={tone} style={{ width: `${Math.max(3, Math.round(share * 100))}%` }} />
                      </span>
                    ) : null}
                  </>
                ) : null}

                {a.top_stages?.length ? (
                  <div className="roster-chips">
                    {a.top_stages.map((s) => (
                      <span className="roster-chip" key={s.stage}>{s.stage}<i>{s.calls}</i></span>
                    ))}
                  </div>
                ) : null}

                <footer className="roster-card-foot">
                  <span>{a.model ?? "no model calls"}</span>
                  {/* CONFIGURED, and labelled. It starts at 50 and has never moved; beside an
                      observed rate it would otherwise read as earned. */}
                  <span title="configured trust level, not an observed score">
                    trust <b>{a.trust_score}</b> set
                  </span>
                  {!a.registered ? (
                    <span className="roster-ungov" data-testid={`wf-unreg-${a.id}`}>no registered role</span>
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

/**
 * THE ORG CHART, EDITABLE.
 *
 * It was derived entirely from observed handoffs — who passed work to whom in the last week.
 * Honest, useful, and completely unchangeable: nobody could say "the QA lead reports to the
 * captain" or add a seat that has not been filled yet. A measurement nobody can act on is a
 * report, and this one had no effect anywhere else in the product.
 *
 * Declared placements are laid over the observed ones and both are kept. Where they disagree
 * is the most interesting thing on the page: an agent who is meant to report to the captain
 * and in practice hands everything to the architect is a fact worth seeing.
 */
export function OrgChartArea({ chart, projectId, actor }: {
  chart: OrgChart; projectId: string; actor: string;
}) {
  const [live, setLive] = useState<OrgChart>(chart);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  useEffect(() => { setLive(chart); }, [chart]);

  const reload = useCallback(async () => {
    try {
      const r = await fetch(`/api/org?project_id=${encodeURIComponent(projectId)}`,
                            { cache: "no-store" });
      if (r.ok) setLive(await r.json());
    } catch { /* keep what is on screen; a failed refresh is not a reason to blank it */ }
  }, [projectId]);

  const place = useCallback(async (agentId: string, reportsTo: string | null) => {
    setBusy(true); setNote(null);
    try {
      const r = await fetch(`/api/org`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_id: agentId, project_id: projectId, actor,
                               reports_to: reportsTo }),
      });
      const body = await r.json().catch(() => ({}));
      // The API's own sentence: "X already sits above Y, so this would make a loop" is
      // actionable; "could not save" sends somebody looking in the wrong place.
      if (!r.ok) setNote(body.error ?? body.detail ?? "Could not move it.");
      else await reload();
    } finally { setBusy(false); }
  }, [projectId, actor, reload]);

  const release = useCallback(async (agentId: string) => {
    setBusy(true); setNote(null);
    try {
      const url = `/api/org?project_id=${encodeURIComponent(projectId)}`
        + `&agent_id=${encodeURIComponent(agentId)}`;
      const r = await fetch(url, { method: "DELETE" });
      if (!r.ok) setNote("Could not reset it.");
      else await reload();
    } finally { setBusy(false); }
  }, [projectId, reload]);

  const chartNow = live;
  const declared = (chartNow as { declared?: Record<string, unknown> }).declared ?? {};
  const placedCount = Object.keys(declared).length;
  const treeNow = chartNow.tree ?? [];
  const levelsNow = new Map<number, OrgNode[]>();
  for (const n of treeNow) {
    const d = n.depth ?? 0;
    levelsNow.set(d, [...(levelsNow.get(d) ?? []), n]);
  }
  const rowsNow = [...levelsNow.entries()].sort((a, b) => a[0] - b[0]);
  const isolated = (chartNow.nodes ?? []).filter((n) => !n.connected);
  const unplaced = (chartNow.nodes ?? []).filter(
    (n) => !Object.prototype.hasOwnProperty.call(declared, n.id));

  return (
    <div className="org">
      <p className="detail" data-testid="org-provenance">
        {chartNow.note || `Derived from ${chartNow.derived_from}.`}
        {chartNow.root_inferred
          ? " Every agent here is handed work by someone, so the busiest was taken as the top."
          : ""}
      </p>

      {/* WHAT THIS CHART IS FOR. Placing a seat is the act that turns a measurement into a
          structure; saying so beats a drag target nobody knows is a drag target. */}
      <div className="org-toolbar">
        <span className="detail">
          {placedCount
            ? `${placedCount} placed by your team. Drag an agent onto another to change who it reports to.`
            : "Nobody is placed yet. Drag an agent onto another to say who it reports to."}
        </span>
        {placedCount ? (
          <button type="button" className="cap-btn" disabled={busy} data-testid="org-reset-all"
                  onClick={() => void release("*")}>
            Back to observed
          </button>
        ) : null}
      </div>
      {note ? <p className="org-note" data-testid="org-note">{note}</p> : null}

      {treeNow.length === 0 ? (
        <p className="detail" data-testid="org-empty">
          Nothing is placed and no work has passed between agents in the last
          {" "}{chartNow.window_hours} hours, so there is nothing to draw yet.
        </p>
      ) : (
        <div className="org-tree" data-testid="org-tree">
          {rowsNow.map(([depth, nodes]) => (
            <div className="org-level" key={depth} data-depth={depth}>
              {nodes.map((n) => (
                <div className="org-node" key={n.id} data-root={depth === 0}
                     data-testid={`org-node-${n.id}`}
                     data-placed={Object.prototype.hasOwnProperty.call(declared, n.id)}
                     data-over={over === n.id || undefined}
                     draggable={!busy}
                     onDragStart={() => setDragging(n.id)}
                     onDragEnd={() => { setDragging(null); setOver(null); }}
                     onDragOver={(e) => { e.preventDefault(); setOver(n.id); }}
                     onDragLeave={() => setOver((c) => (c === n.id ? null : c))}
                     onDrop={(e) => {
                       e.preventDefault();
                       setOver(null);
                       if (dragging && dragging !== n.id) void place(dragging, n.id);
                       setDragging(null);
                     }}>
                  <span className="org-node-name">{n.name}</span>
                  {/* Only when it ADDS something. "Product Manager / Product Manager" is the
                      same duplication flagged elsewhere. */}
                  {humanise(n.role || n.id) !== n.name ? (
                    <span className="org-node-role">{humanise(n.role || n.id)}</span>
                  ) : null}
                  {n.task ? <span className="org-node-task">{n.task}</span> : null}
                  <span className="org-node-tools">
                    <button type="button" className="org-node-act" disabled={busy}
                            title="Make this a top-level seat"
                            data-testid={`org-top-${n.id}`}
                            onClick={() => void place(n.id, null)}>top</button>
                    {Object.prototype.hasOwnProperty.call(declared, n.id) ? (
                      <button type="button" className="org-node-act" disabled={busy}
                              title="Hand this seat back to the observed chart"
                              data-testid={`org-reset-${n.id}`}
                              onClick={() => void release(n.id)}>reset</button>
                    ) : null}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      {/* SEATS NOBODY HAS PLACED. The list of agents BOSS knows about is the thing you build a
          chart FROM; without it the only agents you could place are the ones already placed. */}
      {unplaced.length > 0 ? (
        <>
          <div className="ct-block-title" style={{ marginTop: 18 }}>Not placed yet</div>
          <p className="detail">
            Drag one onto a seat above to put it under them, or press top to make it a root.
          </p>
          <div className="org-bench" data-testid="org-bench">
            {unplaced.map((n) => (
              <span className="org-benchchip" key={n.id} draggable={!busy}
                    data-testid={`org-bench-${n.id}`}
                    onDragStart={() => setDragging(n.id)}
                    onDragEnd={() => setDragging(null)}>
                {n.name}
                <button type="button" className="org-node-act" disabled={busy}
                        onClick={() => void place(n.id, null)}>top</button>
              </span>
            ))}
          </div>
        </>
      ) : null}

      {isolated.length > 0 ? (
        <>
          <div className="ct-block-title" style={{ marginTop: 18 }}>Working alone</div>
          <p className="detail">
            No work passed to or from these in {chart.window_hours}h — idle by design, or forgotten.
          </p>
          <div className="roster-alone" data-testid="roster-alone">
            {isolated.map((n) => (
              <span className="roster-alonechip" key={n.id}>
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
