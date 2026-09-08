"use client";

/**
 * One agent, in full.
 *
 * The roster card was an `<article>` — not a link, not addressable, with no detail view behind
 * it. The only interactive thing on it was "Ask", which opens the chat dock. So an agent was a
 * row of numbers on a card and there was nowhere to go from it.
 *
 * Everything here was already recorded and none of it was reachable from the agent it
 * describes:
 *
 *   * the APPS it worked on — the roster shows one `run_id`, the newest process step, for 7 of
 *     26 agents. This is the whole history, with per-app calls, spend and stages.
 *   * its SPEC — mission, boundaries, allowed and forbidden skills, output contracts, eval
 *     rubric. Twenty-two role YAMLs are installed into `roles.spec` and the console's
 *     `RoleFeedItem` drops the column, so the richest capability model BOSS has was invisible.
 *   * what it is ASSIGNED to, which is a different question from what it has touched. The gap
 *     between the two lists is the interesting part.
 *
 * Addressable: `?area=workforce&role=<key>`. A view you cannot link to is a view nobody can
 * send anybody, and the template page taught that lesson already.
 */

import { useCallback, useEffect, useState } from "react";

import { formatDate } from "../lib/datetime";

type App = {
  app: string; raw_title?: string; calls: number; tokens: number; spend_usd: number;
  ok_calls: number; last_at: string | null; stages: string[];
};
type Panel = { id: string; subject: string; status: string; phase: string; at: string | null };
type Spec = {
  mission?: string; primary_outcomes?: string[];
  boundaries?: { must?: string[]; must_not?: string[] };
  decision_rights?: string[]; allowed_skills?: string[]; forbidden_skills?: string[];
  output_contracts?: string[]; eval_rubric?: Record<string, unknown>; knowledge?: string;
};
type Detail = {
  role_key: string; name: string; registered: boolean;
  autonomy_level: number | null; trust_score: number | null; successful_runs: number | null;
  violations: number | null; hidden_failures: number | null; reports_to: string | null;
  spec: Spec; owns_stages: string[]; apps: App[]; panels: Panel[];
  assigned_to: Array<{ app_name: string; source: string }>;
  window_hours: number;
};

const money = (n: number) => (n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`);

export function AgentDetail({ projectId, roleKey, onClose }: {
  projectId: string; roleKey: string; onClose: () => void;
}) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [failed, setFailed] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await fetch(
        `/api/agents/${encodeURIComponent(roleKey)}?project_id=${encodeURIComponent(projectId)}`,
        { cache: "no-store" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { setFailed(String(body?.error ?? `HTTP ${r.status}`)); return; }
      setDetail(body as Detail);
      setFailed("");
    } catch (e) {
      setFailed(String((e as Error)?.message ?? e));
    }
  }, [projectId, roleKey]);

  useEffect(() => { void load(); }, [load]);

  // Escape closes it, and the page behind does not scroll. Same rules as the template page —
  // a full-screen view you cannot get out of is the bug that one shipped with.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [onClose]);

  const spec = detail?.spec ?? {};
  const worked = new Set((detail?.apps ?? []).map((a) => a.app));
  const spend = (detail?.apps ?? []).reduce((sum, a) => sum + (a.spend_usd || 0), 0);

  return (
    <div className="tpl-page" data-testid="agent-detail" role="dialog" aria-modal="true"
         aria-label={detail?.name ?? roleKey}>
      <header className="tpl-page-head">
        <button type="button" className="tpl-back" onClick={onClose}>← Back to agents</button>
        <span className="tpl-crumb">Agents · {detail?.name ?? roleKey}</span>
      </header>

      <div className="tpl-page-inner">
        {failed ? (
          <p className="detail" data-testid="agent-detail-failed">
            Could not read this agent — {failed}.
          </p>
        ) : null}
        {!detail && !failed ? <p className="detail">Reading…</p> : null}

        {detail ? (
          <>
            <div className="ad-head">
              <div>
                <h1 className="tpl-title">{detail.name}</h1>
                <p className="detail">
                  <code>{detail.role_key}</code>
                  {detail.reports_to ? <> · reports to <code>{detail.reports_to}</code></> : null}
                  {/* An agent doing work with no registered role has no trust score, no
                      autonomy level and nothing governing it. Said, not hidden. */}
                  {!detail.registered ? <> · <strong>no registered role — nothing governs
                    this agent</strong></> : null}
                </p>
              </div>
              <div className="ad-figs">
                <span className="ad-fig"><b>{detail.apps.length}</b><span>apps</span></span>
                <span className="ad-fig"><b>{money(spend)}</b><span>spent</span></span>
                {detail.panels.length
                  ? <span className="ad-fig"><b>{detail.panels.length}</b><span>panels</span></span>
                  : null}
                {detail.registered ? (
                  <span className="ad-fig" title="configured, not observed">
                    <b>{detail.trust_score}</b><span>trust set</span>
                  </span>
                ) : null}
                {detail.violations ? (
                  <span className="ad-fig" data-tone="bad">
                    <b>{detail.violations}</b><span>violations</span>
                  </span>
                ) : null}
              </div>
            </div>

            {/* WHAT IT HAS ACTUALLY WORKED ON. Attributed through the same stage→role map BOSS
                uses for the cost ledger, so this page and the spend figures cannot disagree
                about who did what. */}
            <section className="ct-block">
              <div className="ct-block-title">
                Apps it has worked on
                <span className="detail"> · last {Math.round(detail.window_hours / 24)} days</span>
              </div>
              {detail.apps.length ? (
                <table className="ad-table" data-testid="agent-apps">
                  <thead>
                    <tr><th>App</th><th>Stages</th><th>Calls</th><th>Spent</th><th>Last</th></tr>
                  </thead>
                  <tbody>
                    {detail.apps.map((a) => (
                      <tr key={a.app + (a.last_at ?? "")}>
                        <td>{a.app}</td>
                        <td className="detail">{a.stages.join(", ")}</td>
                        <td>{a.calls}{a.calls !== a.ok_calls
                          ? <span className="ad-bad"> · {a.calls - a.ok_calls} failed</span>
                          : null}</td>
                        <td>{money(a.spend_usd)}</td>
                        {/* The shared UTC helper, not toLocaleDateString: the server renders
                            in one timezone and locale and the browser in another, so the raw
                            call produces different text on each side and React tears the row
                            out on hydration. A test caught this before it shipped. */}
                        <td className="detail">{formatDate(a.last_at, "—")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="detail">
                  No model calls attributed to this agent&rsquo;s stages
                  {detail.owns_stages.length
                    ? <> ({detail.owns_stages.join(", ")})</>
                    : <>, and it owns no pipeline stage — its work is panels and process steps</>}.
                </p>
              )}
            </section>

            {detail.panels.length ? (
              <section className="ct-block">
                <div className="ct-block-title">Panels it sat on</div>
                <ul className="ad-list" data-testid="agent-panels">
                  {detail.panels.map((p) => (
                    <li key={p.id}>
                      {p.subject}
                      <span className="detail"> · {p.status}{p.phase ? ` · ${p.phase}` : ""}</span>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}

            {/* ASSIGNED IS NOT THE SAME AS TOUCHED, and the difference is the point: an agent
                assigned to an app it has never worked on is either idle or blocked, and one
                working on an app it is not assigned to will be refused at the next gate. */}
            <section className="ct-block">
              <div className="ct-block-title">Assigned to</div>
              {detail.assigned_to.length ? (
                <ul className="ad-list" data-testid="agent-assigned">
                  {detail.assigned_to.map((a) => (
                    <li key={a.app_name}>
                      {a.app_name}
                      <span className="detail"> · {a.source === "default"
                        ? "seeded by default" : "assigned by a person"}</span>
                      {!worked.has(a.app_name)
                        ? <span className="detail"> · no work recorded yet</span> : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail">
                  Not assigned to anything. It can still act on any app that has no assignment
                  at all, and will be refused at the gate on one that has.
                </p>
              )}
            </section>

            {/* THE CAPABILITY MODEL, on screen for the first time. */}
            <section className="ct-block">
              <div className="ct-block-title">What it is for</div>
              {spec.mission ? <p className="ad-mission">{spec.mission}</p> : (
                <p className="detail" data-testid="agent-no-spec">
                  This agent has no spec. A spec is what states its mission, its boundaries and
                  the skills it may use — without one, nothing constrains what it will attempt.
                </p>
              )}
              {spec.primary_outcomes?.length ? (
                <>
                  <h4 className="ad-h">Owns these outcomes</h4>
                  <ul className="ad-list">{spec.primary_outcomes.map((o) => <li key={o}>{o}</li>)}</ul>
                </>
              ) : null}
              {spec.boundaries?.must?.length || spec.boundaries?.must_not?.length ? (
                <>
                  <h4 className="ad-h">Boundaries</h4>
                  <ul className="ad-list">
                    {(spec.boundaries?.must ?? []).map((b) => (
                      <li key={`m${b}`}><span className="ad-must">must</span> {b}</li>
                    ))}
                    {(spec.boundaries?.must_not ?? []).map((b) => (
                      <li key={`n${b}`}><span className="ad-mustnot">must not</span> {b}</li>
                    ))}
                  </ul>
                </>
              ) : null}
              {spec.allowed_skills?.length || spec.forbidden_skills?.length ? (
                <>
                  <h4 className="ad-h">Skills</h4>
                  <div className="ad-chips">
                    {(spec.allowed_skills ?? []).map((s) => (
                      <span className="ad-chip" key={`a${s}`}>{s}</span>
                    ))}
                    {(spec.forbidden_skills ?? []).map((s) => (
                      <span className="ad-chip" data-forbidden="true" key={`f${s}`}>{s}</span>
                    ))}
                  </div>
                </>
              ) : null}
              {spec.output_contracts?.length ? (
                <>
                  <h4 className="ad-h">What it must produce</h4>
                  <ul className="ad-list">
                    {spec.output_contracts.map((c) => <li key={String(c)}>{String(c)}</li>)}
                  </ul>
                </>
              ) : null}
            </section>
          </>
        ) : null}
      </div>
    </div>
  );
}
