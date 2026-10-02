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

// React by name as well as its hooks: the suite renders these components with
// renderToStaticMarkup, which compiles JSX to React.createElement and needs React in
// scope. Same import shape as delivery-workflows-ui for the same reason.
import React, { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { formatDate } from "../lib/datetime";
import { apiWrite } from "../lib/api-write";

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
/** One row of "what this agent can do" — held, or available to be given. */
type Skill = {
  skill_id: string; label: string; summary: string; source: string;
  behaviour_kind: string; risk_tier: number; enabled: boolean;
  missing?: boolean; assigned_by?: string; assigned_at?: string | null;
};
type SkillPanel = { role_key: string; holds: Skill[]; available: Skill[] };
type Detail = {
  role_key: string; name: string; registered: boolean;
  autonomy_level: number | null; trust_score: number | null; successful_runs: number | null;
  violations: number | null; hidden_failures: number | null; reports_to: string | null;
  spec: Spec; owns_stages: string[]; apps: App[]; panels: Panel[];
  assigned_to: Array<{ app_name: string; source: string }>;
  skills?: SkillPanel;
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

  // PORTALLED, AND I MADE THIS MISTAKE TWICE.
  //
  // `.tpl-page` is `position: fixed; inset: 0`, and reusing the class without the portal put
  // this view back inside `.bx-shell`, which carries `isolation: isolate`. The console renders
  // more than one shell, so a fixed element inside one cannot escape it — the template page
  // was covered by the project strip and this one rendered clipped to 260px with all 38 rows
  // present and invisible. A DOM query said "rendered: true"; the screenshot said otherwise.
  // The class is not the fix; leaving the shell is.
  const page = (
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

            {/* WHAT IT CAN DO — the section this page was missing, and the reason an
                operator could look at a fully specced agent and conclude BOSS had given it
                nothing. Above the history deliberately: the first question about an agent is
                what it is able to do, not what it has already done. */}
            <AgentSkills projectId={projectId} roleKey={detail.role_key}
                         panel={detail.skills} spec={spec} onChanged={load} />

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
  return typeof document !== "undefined" ? createPortal(page, document.body) : null;
}


/**
 * The skills one agent holds, and the ones it could be given.
 *
 * THE DIRECTION THIS FIXES. `role_skills` has always been many-to-many — an agent holds many
 * skills, a skill is held by many agents — but the only place to write it was the Skills page,
 * one skill at a time, choosing an agent from a dropdown. That is backwards from how anyone
 * builds a team: you open the editor and say what it can do. Same table, same endpoint, read
 * and written from the agent's side.
 *
 * AND IT IS NOT `spec.allowed_skills`. That list is prose in a spec pack — what an agent MAY be
 * given. This is the grant, and it is the one `skill_runtime` checks before the agent is allowed
 * to call anything. A page that showed only the first is why an agent with eleven allowed skills
 * and an empty `role_skills` row looked capable and could do nothing.
 */
export function AgentSkills({ projectId, roleKey, panel, spec, onChanged }: {
  projectId: string; roleKey: string; panel?: SkillPanel; spec: Spec; onChanged: () => void;
}) {
  const [busy, setBusy] = useState("");
  const [failed, setFailed] = useState("");
  const [pick, setPick] = useState("");
  const [open, setOpen] = useState(false);

  const holds = panel?.holds ?? [];
  const available = panel?.available ?? [];

  const write = useCallback(async (action: "assign" | "unassign", skillId: string) => {
    setBusy(skillId);
    setFailed("");
    try {
      const r = await apiWrite("/api/skills", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, project_id: projectId, role_key: roleKey,
                               skill_id: skillId, actor: "executive_console" }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { setFailed(String(body?.error ?? `HTTP ${r.status}`)); return; }
      onChanged();
    } catch (e) {
      setFailed(String((e as Error)?.message ?? e));
    } finally {
      setBusy("");
    }
  }, [projectId, roleKey, onChanged]);

  // GROUPED THE WAY THE DECISION IS MADE. A skill the agent's own spec already names is the
  // uncontroversial one; anything else works too — the grant itself is the permission — but
  // it is a wider decision and worth seeing as one.
  const named = new Set(spec.allowed_skills ?? []);
  const forbidden = new Set(spec.forbidden_skills ?? []);
  const suggested = available.filter((s) => named.has(s.skill_id));
  const rest = available.filter((s) => !named.has(s.skill_id) && !forbidden.has(s.skill_id));

  return (
    <section className="ct-block" data-testid="agent-skills">
      <div className="ct-block-title">
        What it can do
        <span className="detail"> · {holds.length
          ? `${holds.length} skill${holds.length === 1 ? "" : "s"} held`
          : "nothing yet"}</span>
      </div>

      {holds.length ? (
        <ul className="ad-list" data-testid="agent-skills-held">
          {holds.map((s) => (
            <li key={s.skill_id} data-skill={s.skill_id}>
              <code>{s.skill_id}</code>
              {s.summary ? <span className="detail"> — {s.summary}</span> : null}
              {/* A tier at or above the approval floor means a run will stop and wait for a
                  person. Said here, because it is a property of giving the skill out. */}
              {s.risk_tier >= 2 ? (
                <span className="detail"> · needs approval each time (tier {s.risk_tier})</span>
              ) : null}
              {!s.enabled && !s.missing ? (
                <span className="detail"> · not enabled in this project</span>
              ) : null}
              {s.missing ? (
                <span className="ad-bad"> · no such skill here any more — this grant does
                  nothing</span>
              ) : null}
              <button type="button" className="linklike" disabled={busy === s.skill_id}
                      data-testid={`agent-skill-drop-${s.skill_id}`}
                      title={`Take ${s.skill_id} off this agent`}
                      onClick={() => void write("unassign", s.skill_id)}>
                {busy === s.skill_id ? "…" : "remove"}
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="detail" data-testid="agent-skills-empty">
          This agent holds no skills, so every step that names it will be refused at the gate.
          {named.size
            ? " Its spec says it may use " + [...named].slice(0, 6).join(", ")
              + " — give it those below."
            : ""}
        </p>
      )}

      {failed ? <p className="ad-bad" data-testid="agent-skills-failed">{failed}</p> : null}

      {open ? (
        <div className="skill-assign-pick">
          <select value={pick} data-testid="agent-skill-select"
                  onChange={(e) => setPick(e.target.value)}>
            <option value="">Choose a skill…</option>
            {suggested.length ? (
              <optgroup label="Its spec already allows these">
                {suggested.map((s) => (
                  <option key={s.skill_id} value={s.skill_id}>{s.skill_id}</option>
                ))}
              </optgroup>
            ) : null}
            {rest.length ? (
              <optgroup label={suggested.length ? "Anything else in this project" : "Skills"}>
                {rest.map((s) => (
                  <option key={s.skill_id} value={s.skill_id}>{s.skill_id}</option>
                ))}
              </optgroup>
            ) : null}
          </select>
          <button type="button" className="cap-btn" disabled={!pick || busy !== ""}
                  data-testid="agent-skill-confirm"
                  onClick={() => { void write("assign", pick); setPick(""); setOpen(false); }}>
            Give it this skill
          </button>
          <button type="button" className="linklike"
                  onClick={() => { setOpen(false); setPick(""); }}>Cancel</button>
        </div>
      ) : (
        <div className="skill-assign-actions">
          {available.length ? (
            <button type="button" className="linklike" data-testid="agent-skill-open"
                    onClick={() => setOpen(true)}>
              Give this agent a skill
            </button>
          ) : (
            <span className="detail">It already holds every skill in this project.</span>
          )}
        </div>
      )}
      {forbidden.size ? (
        <p className="detail">
          Its spec forbids {[...forbidden].join(", ")}. Those are left out of the list above.
        </p>
      ) : null}
    </section>
  );
}
