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
import {
  deleteAgent, grantMcp, grantSkill, loadGrantable, parseToolList, revokeMcp, revokeSkill,
  updateAgent, type AgentSummary,
} from "../lib/agents";

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
/** One MCP server held (with its tool allowlist), or available to be given. */
type McpHeld = {
  mcp_server_id: string; server: string; tools: string[]; enabled: boolean;
  url?: string | null; risk_tier?: number | null; description?: string | null;
};
type McpAvailable = {
  mcp_server_id: string; server: string; url: string; enabled: boolean;
  risk_tier: number; description?: string | null;
};
type McpPanel = { role_key: string; holds: McpHeld[]; available: McpAvailable[] };
type NamedOn = {
  workflow_key: string; workflow: string; version: number; step_id: string; step: string;
  how: string; because: string;
};
type Detail = {
  role_key: string; name: string; registered: boolean;
  autonomy_level: number | null; trust_score: number | null; successful_runs: number | null;
  violations: number | null; hidden_failures: number | null; reports_to: string | null;
  spec: Spec; owns_stages: string[]; apps: App[]; panels: Panel[];
  assigned_to: Array<{ app_name: string; source: string }>;
  skills?: SkillPanel;
  mcp?: McpPanel;
  named_on_steps?: NamedOn[];
  studio_profiles?: Array<{ id: string; agent_key: string; name: string; status: string }>;
  window_hours: number;
};

const money = (n: number) => (n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`);

export function AgentDetail({ projectId, roleKey, actor, onClose, onDeleted }: {
  projectId: string; roleKey: string;
  /** The signed-in actor every change on this page is made as — never a literal. */
  actor: string;
  onClose: () => void;
  /** Told when the agent is deleted, so the roster can drop it before its next refresh. */
  onDeleted?: (roleKey: string) => void;
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
            <AgentSkills projectId={projectId} roleKey={detail.role_key} actor={actor}
                         panel={detail.skills} spec={spec} onChanged={load} />

            {/* AND THE MCP SERVERS IT HOLDS — the grant that did not exist. A workflow step this
                agent works is offered exactly these servers' tools. */}
            {detail.registered ? (
              <AgentMcp projectId={projectId} roleKey={detail.role_key} actor={actor}
                        panel={detail.mcp} onChanged={load} />
            ) : null}

            {detail.registered ? (
              <AgentEdit projectId={projectId} actor={actor} detail={detail} onChanged={load} />
            ) : null}

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

            {detail.registered ? (
              <AgentDelete projectId={projectId} actor={actor} detail={detail}
                           onDeleted={() => { onDeleted?.(detail.role_key); onClose(); }} />
            ) : null}
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
export function AgentSkills({ projectId, roleKey, actor, panel, spec, onChanged }: {
  projectId: string; roleKey: string; actor: string; panel?: SkillPanel; spec: Spec;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState("");
  const [failed, setFailed] = useState("");
  const [pick, setPick] = useState("");
  const [open, setOpen] = useState(false);

  const holds = panel?.holds ?? [];
  const available = panel?.available ?? [];

  // AS THE SIGNED-IN ACTOR. This posted the literal 'executive_console', so the audit event
  // for every grant named the console rather than the person who pressed the button.
  const write = useCallback(async (action: "assign" | "unassign", skillId: string) => {
    setBusy(skillId);
    setFailed("");
    const r = action === "assign"
      ? await grantSkill(projectId, actor, roleKey, skillId)
      : await revokeSkill(projectId, actor, roleKey, skillId);
    setBusy("");
    if (!r.ok) { setFailed(r.error); return; }
    onChanged();
  }, [projectId, roleKey, actor, onChanged]);

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
          This agent holds no skills. Unless it holds an MCP server, every step that names it
          will be refused at the gate.
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


/**
 * The MCP servers one agent holds, and the ones it could be given.
 *
 * THE GRANT THAT DID NOT EXIST. There was no way to give an agent an MCP server: the Studio
 * wizard's "integrations" was free text nothing read, and a bounded agent step was offered no MCP
 * tool at all. A server given here (all its tools, or only those named) is what a workflow step
 * this agent works is offered; the server's own risk tier still decides when a call waits for a
 * person.
 */
export function AgentMcp({ projectId, roleKey, actor, panel, onChanged }: {
  projectId: string; roleKey: string; actor: string; panel?: McpPanel; onChanged: () => void;
}) {
  const [busy, setBusy] = useState("");
  const [failed, setFailed] = useState("");
  const [pick, setPick] = useState("");
  const [tools, setTools] = useState("");
  const [open, setOpen] = useState(false);
  const holds = panel?.holds ?? [];
  const available = panel?.available ?? [];

  const give = useCallback(async () => {
    if (!pick) return;
    setBusy(pick);
    setFailed("");
    const r = await grantMcp(projectId, actor, roleKey, pick, parseToolList(tools));
    setBusy("");
    if (!r.ok) { setFailed(r.error); return; }
    setPick(""); setTools(""); setOpen(false);
    onChanged();
  }, [pick, tools, projectId, actor, roleKey, onChanged]);

  const take = useCallback(async (serverId: string) => {
    setBusy(serverId);
    setFailed("");
    const r = await revokeMcp(projectId, actor, roleKey, serverId);
    setBusy("");
    if (!r.ok) { setFailed(r.error); return; }
    onChanged();
  }, [projectId, actor, roleKey, onChanged]);

  return (
    <section className="ct-block" data-testid="agent-mcp">
      <div className="ct-block-title">
        MCP servers it can use
        <span className="detail"> · {holds.length
          ? `${holds.length} server${holds.length === 1 ? "" : "s"} held` : "none yet"}</span>
      </div>
      {holds.length ? (
        <ul className="ad-list" data-testid="agent-mcp-held">
          {holds.map((m) => (
            <li key={m.mcp_server_id} data-server={m.mcp_server_id}>
              <code>{m.server}</code>
              <span className="detail"> · {m.tools.length ? `only ${m.tools.join(", ")}` : "every tool it offers"}</span>
              {!m.enabled ? (
                <span className="ad-bad"> · disabled in this project, so it is not offered until
                  enabled on the Skills and MCP page</span>
              ) : null}
              {m.risk_tier != null && m.risk_tier >= 2 ? (
                <span className="detail"> · its calls wait for approval (tier {m.risk_tier})</span>
              ) : null}
              <button type="button" className="linklike" disabled={busy === m.mcp_server_id}
                      data-testid={`agent-mcp-drop-${m.mcp_server_id}`}
                      onClick={() => void take(m.mcp_server_id)}>
                {busy === m.mcp_server_id ? "…" : "remove"}
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="detail" data-testid="agent-mcp-empty">
          It holds no MCP server, so a workflow step it works is offered no MCP tool.
        </p>
      )}
      {failed ? <p className="ad-bad" data-testid="agent-mcp-failed">{failed}</p> : null}
      {open ? (
        <div className="skill-assign-pick">
          <select value={pick} data-testid="agent-mcp-select"
                  onChange={(e) => setPick(e.target.value)}>
            <option value="">Choose an MCP server…</option>
            {available.map((m) => (
              <option key={m.mcp_server_id} value={m.mcp_server_id}>
                {m.server}{m.enabled ? "" : " (disabled)"}
              </option>
            ))}
          </select>
          <input placeholder="Only these tools (comma-separated) — empty for all" value={tools}
                 data-testid="agent-mcp-tools" onChange={(e) => setTools(e.target.value)} />
          <button type="button" className="cap-btn" disabled={!pick || busy !== ""}
                  data-testid="agent-mcp-confirm" onClick={() => void give()}>
            Give it this server
          </button>
          <button type="button" className="linklike"
                  onClick={() => { setOpen(false); setPick(""); setTools(""); }}>Cancel</button>
        </div>
      ) : (
        <div className="skill-assign-actions">
          {available.length ? (
            <button type="button" className="linklike" data-testid="agent-mcp-open"
                    onClick={() => setOpen(true)}>Give this agent an MCP server</button>
          ) : holds.length ? (
            <span className="detail">It already holds every MCP server in this project.</span>
          ) : (
            <span className="detail">No MCP servers are registered — register one on the Skills
              and MCP page first.</span>
          )}
        </div>
      )}
    </section>
  );
}


/**
 * Edit what the agent is: its name, mission, boundaries and who it reports to.
 *
 * There was no edit path for an agent in the console beyond its skills — mission, display name
 * and boundaries could only be changed through the API, and POST /v1/roles used as an update
 * reset its autonomy and reporting line. Only the fields changed here are sent, and the API
 * changes only those. An edited bundled agent stays edited across upgrades.
 */
export function AgentEdit({ projectId, actor, detail, onChanged }: {
  projectId: string; actor: string; detail: Detail; onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(detail.name);
  const [mission, setMission] = useState(detail.spec.mission ?? "");
  const [must, setMust] = useState((detail.spec.boundaries?.must ?? []).join("\n"));
  const [mustNot, setMustNot] = useState((detail.spec.boundaries?.must_not ?? []).join("\n"));
  const [reportsTo, setReportsTo] = useState(detail.reports_to ?? "");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");
  const [saved, setSaved] = useState("");
  // WHO IT CAN REPORT TO, as a list to pick from. This was a free-text box expecting an agent
  // key, while quick create offered a picker — so the same field took a key on one screen and
  // a name on the other, and a typo was only caught by the API's refusal.
  const [agents, setAgents] = useState<AgentSummary[] | null>(null);
  const [agentsFailed, setAgentsFailed] = useState("");
  useEffect(() => {
    if (!open || agents) return;
    let alive = true;
    void loadGrantable(projectId).then((r) => {
      if (!alive) return;
      if (r.ok) setAgents(r.data.agents); else setAgentsFailed(r.error);
    });
    return () => { alive = false; };
  }, [open, agents, projectId]);
  const managers = (agents ?? []).filter((a) => a.role_key !== detail.role_key);
  const current = reportsTo.trim();

  const lines = (v: string) => v.split("\n").map((x) => x.trim()).filter(Boolean);
  const save = async () => {
    const changes: Parameters<typeof updateAgent>[3] = {};
    if (name.trim() !== detail.name) changes.display_name = name.trim();
    if (mission.trim() !== (detail.spec.mission ?? "")) changes.mission = mission.trim();
    const b = { must: lines(must), must_not: lines(mustNot) };
    if (JSON.stringify(b) !== JSON.stringify({
      must: detail.spec.boundaries?.must ?? [], must_not: detail.spec.boundaries?.must_not ?? [],
    })) changes.boundaries = b;
    if ((reportsTo.trim() || null) !== (detail.reports_to || null)) {
      changes.reports_to = reportsTo.trim() || null;
    }
    if (!Object.keys(changes).length) { setSaved("Nothing changed."); return; }
    setBusy(true); setFailed(""); setSaved("");
    const r = await updateAgent(projectId, actor, detail.role_key, changes);
    setBusy(false);
    if (!r.ok) { setFailed(r.error); return; }
    setSaved("Saved.");
    setOpen(false);
    onChanged();
  };

  return (
    <section className="ct-block" data-testid="agent-edit">
      <div className="ct-block-title">
        Edit this agent
        {!open ? (
          <button type="button" className="linklike" data-testid="agent-edit-open"
                  onClick={() => setOpen(true)}> change name, mission, boundaries or manager</button>
        ) : null}
      </div>
      {saved ? <p className="detail" data-testid="agent-edit-saved">{saved}</p> : null}
      {open ? (
        <form className="ad-edit" onSubmit={(e) => { e.preventDefault(); void save(); }}>
          <label>Name
            <input value={name} data-testid="agent-edit-name" onChange={(e) => setName(e.target.value)} />
          </label>
          <label>Mission
            <textarea rows={3} value={mission} data-testid="agent-edit-mission"
                      onChange={(e) => setMission(e.target.value)} />
          </label>
          <label>It must… (one per line)
            <textarea rows={3} value={must} data-testid="agent-edit-must"
                      onChange={(e) => setMust(e.target.value)} />
          </label>
          <label>It must never… (one per line)
            <textarea rows={3} value={mustNot} data-testid="agent-edit-must-not"
                      onChange={(e) => setMustNot(e.target.value)} />
          </label>
          <label>Reports to
            <select value={current} data-testid="agent-edit-reports-to"
                    onChange={(e) => setReportsTo(e.target.value)}>
              <option value="">Nobody — a root of the team</option>
              {/* The current manager stays selectable while the list loads, or if it failed. */}
              {current && !managers.some((a) => a.role_key === current) ? (
                <option value={current}>{current}</option>
              ) : null}
              {managers.map((a) => (
                <option key={a.role_key} value={a.role_key}>{a.name} ({a.role_key})</option>
              ))}
            </select>
            {agentsFailed ? (
              <small className="ad-bad">Could not list this project's agents — {agentsFailed}.
                The current manager is kept unless you choose Nobody.</small>
            ) : null}
          </label>
          {failed ? <p className="ad-bad" data-testid="agent-edit-failed">{failed}</p> : null}
          <div className="ad-edit-actions">
            <button type="submit" className="cap-btn" disabled={busy} data-testid="agent-edit-save">
              {busy ? "Saving…" : "Save"}
            </button>
            <button type="button" className="linklike" disabled={busy}
                    onClick={() => { setOpen(false); setFailed(""); }}>Cancel</button>
          </div>
        </form>
      ) : null}
    </section>
  );
}


/**
 * Delete the agent — or be told, before pressing, why it cannot be deleted yet.
 *
 * Refused by the API while a workflow step names it, because deleting the agent under a step
 * turns a working workflow into one that fails at that step minutes into a run. The steps are
 * listed here first so the refusal is never a surprise.
 */
export function AgentDelete({ projectId, actor, detail, onDeleted }: {
  projectId: string; actor: string; detail: Detail; onDeleted: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");
  const named = detail.named_on_steps ?? [];
  const profiles = (detail.studio_profiles ?? []).filter((p) => p.status !== "retired");

  const remove = async () => {
    setBusy(true); setFailed("");
    const r = await deleteAgent(projectId, actor, detail.role_key);
    setBusy(false);
    if (!r.ok) { setFailed(r.error); setConfirming(false); return; }
    onDeleted();
  };

  return (
    <section className="ct-block ad-danger" data-testid="agent-delete">
      <div className="ct-block-title">Delete this agent</div>
      {named.length ? (
        <>
          <p className="detail" data-testid="agent-delete-blocked">
            It cannot be deleted while {named.length} workflow step{named.length === 1 ? " names" : "s name"} it.
            Give {named.length === 1 ? "that step" : "those steps"} to another agent (or leave
            them for BOSS to pick), save the workflow, then come back.
          </p>
          <ul className="ad-list" data-testid="agent-named-on">
            {named.map((n) => (
              <li key={`${n.workflow_key}:${n.version}:${n.step_id}`}>
                {n.workflow} · <code>{n.step}</code>
                <span className="detail"> · {n.how}{n.because === "live run" ? " · a run in flight" : ` · v${n.version}`}</span>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="detail">
          Its skills and MCP servers are released{profiles.length
            ? `, its ${profiles.length} Studio profile${profiles.length === 1 ? " is" : "s are"} retired (their runs are kept)`
            : ""}, and anyone reporting to it moves up to {detail.reports_to
            ? <code>{detail.reports_to}</code> : "the top of the team"}. An upgrade will not bring
          it back.
        </p>
      )}
      {failed ? <p className="ad-bad" role="alert" data-testid="agent-delete-failed">{failed}</p> : null}
      <div className="ad-danger-actions">
        {confirming ? (
          <>
            <button type="button" className="cap-btn" data-tone="danger" disabled={busy}
                    data-testid="agent-delete-confirm" onClick={() => void remove()}>
              {busy ? "Deleting…" : `Yes, delete ${detail.name}`}
            </button>
            <button type="button" className="linklike" disabled={busy}
                    onClick={() => setConfirming(false)}>Keep it</button>
          </>
        ) : (
          <button type="button" className="linklike" disabled={named.length > 0}
                  data-testid="agent-delete-open" onClick={() => setConfirming(true)}>
            Delete {detail.name}
          </button>
        )}
      </div>
    </section>
  );
}
