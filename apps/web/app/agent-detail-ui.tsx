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

import type { AgentStudioFeed } from "../lib/agent-studio";
import { openPlan } from "../lib/agent-converse";
import { formatDate } from "../lib/datetime";
import { AgentProfile } from "./agent-profile-ui";
import { AgentQuickCreate } from "./agent-quick-create";
import {
  deleteAgent, loadGrantable, repointMapping, updateAgent, type AgentSummary,
} from "../lib/agents";
import type { Passport, Persona } from "../lib/agent-world-types";
import { askAgent, getPassport, openAgent } from "../lib/agent-world";
import { useLivePresence } from "../lib/agents-live";
import {
  PassportHeader, PassportNav, goToSection, readTab, type SectionKey,
} from "./agents/passport-header";
import { PassportLineage } from "./agents/passport-lineage";
import { PassportPersona } from "./agents/passport-persona";
import { PassportTest } from "./agents/passport-test";
import { PassportWork } from "./agents/passport-work";
import { ShareCardDialog } from "./agents/share-card-dialog";
import { PassportLoadout } from "./agents/passport-loadout";
import { SectionLabel } from "./section-label";

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
/** A mapping or parent workflow step that starts the version naming the agent. */
type Pointer = {
  via: "mapped" | "subworkflow";
  mapping_id?: string; selector_kind?: string; selector_value?: string;
  workflow?: string; version?: number; step?: string;
};
type NamedOn = {
  workflow_key: string; workflow: string; version: number; step_id: string; step: string;
  how: string; because: string;
  /** Only an OLDER version nothing starts by default: deleting may retire it (agents.py). */
  retirable?: boolean;
  /** Where it is and what to do about it, written ONCE by the API (agents.REACH_SAID) — the
   *  same words its 409 uses, so the page and the refusal cannot disagree. */
  where?: string; remedy?: string;
  pointed_by?: Pointer[];
  /** Older parent versions a delete retires along with this one. */
  also_retires?: Array<{ workflow: string; version: number }>;
};

/** Which version names the agent, the way the API's refusal says it. */
export function whereNamed(n: NamedOn): string {
  if (n.where) return n.where;
  // An API from before the round-3 review sends no `where`.
  if (n.because === "live run") return "a run in flight";
  if (n.because === "active") return `v${n.version}, the version it runs`;
  if (n.because === "older") return `v${n.version}, an older version still startable by id`;
  return `v${n.version}, ${n.because}`;
}

/** What to do so the steps stop blocking the delete: each step's own remedy, once each.
 *
 *  Review of milestone/agents, round 3: the page said "save the workflow" for every reach, and
 *  saving moves neither a mapping (it pins one exact version) nor a parent's subworkflow
 *  binding, so doing what it said left the delete blocked for good. */
export function remediesFor(blocking: NamedOn[]): string[] {
  const out: string[] = [];
  for (const n of blocking) {
    const said = n.remedy
      ?? "give the step to another agent (or leave it for BOSS to pick) and save the workflow";
    if (!out.includes(said)) out.push(said);
  }
  return out;
}
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

/** The steps that name this agent, once each, newest version first: where "Used in" links. */
export function usedIn(named: NamedOn[] | undefined): Array<{ workflow_key: string; workflow: string;
                                                          step_id: string; step: string; href: string }> {
  const seen = new Set<string>();
  const out = [];
  const order = (n: NamedOn) => (n.because === "active" ? 0 : n.because === "live run" ? 1 : 2);
  for (const n of [...(named ?? [])].sort((a, b) => order(a) - order(b) || b.version - a.version)) {
    const id = `${n.workflow_key}\u0000${n.step_id}`;
    if (seen.has(id)) continue;
    seen.add(id);
    const q = new URLSearchParams({ area: "delivery_workflows", wf: n.workflow_key, step: n.step_id });
    out.push({ workflow_key: n.workflow_key, workflow: n.workflow, step_id: n.step_id, step: n.step,
               href: `?${q}` });
  }
  return out;
}

export function AgentDetail({ projectId, roleKey, actor, onClose, onDeleted, studio }: {
  projectId: string; roleKey: string;
  /** The Studio catalog, for this agent's Profile. */
  studio?: AgentStudioFeed;
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

  // THE PASSPORT: identity, birth, version and vitals (Agent World). An API without it (before
  // the identity stream) answers 404 and the page is the agent page it always was.
  const [passport, setPassport] = useState<Passport | null>(null);
  const loadPassport = useCallback(async () => {
    const r = await getPassport(projectId, roleKey);
    setPassport(r.ok && r.data.roleKey ? r.data : null);
  }, [projectId, roleKey]);
  useEffect(() => { void loadPassport(); }, [loadPassport]);
  const live = useLivePresence(projectId).get(roleKey);
  const [tab, setTab] = useState<SectionKey>("loadout");
  const [sharing, setSharing] = useState(false);
  const [registering, setRegistering] = useState(false);
  const [draftPersona, setDraftPersona] = useState<Persona | null>(null);
  useEffect(() => {
    const t = readTab(window.location.search);
    setTab(t);
    if (t !== "loadout" && detail) setTimeout(() => goToSection(t), 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [!!detail]);
  const pick = (k: SectionKey) => { setTab(k); goToSection(k); };

  // Escape closes it, and the page behind does not scroll. Same rules as the template page —
  // a full-screen view you cannot get out of is the bug that one shipped with.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !e.defaultPrevented) onClose(); };
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
        {/* PLAN SOMETHING WITH IT (agents/converse-host.tsx). Talking to it is the Passport's
            own Ask, below: one door for one action. */}
        {detail?.registered ? (
          <span className="ag-head-acts" style={{ marginLeft: "auto" }}>
            <button type="button" className="ag-btn" data-testid="agent-detail-plan"
                    title="Discuss a piece of work, then start it"
                    onClick={() => openPlan(roleKey, { from: "passport" })}>Plan it</button>
          </span>
        ) : null}
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
            <PassportHeader projectId={projectId} actor={actor} roleKey={detail.role_key} name={detail.name}
                            passport={passport} presenceState={live ? (live.inPanel ? "speaking" : live.state) : undefined}
                            onAsk={() => askAgent(detail.role_key, detail.name, { from: "passport" })}
                            onShare={() => setSharing(true)}
                            onDelete={() => pick("settings")}
                            onChanged={(next) => setPassport(next)}
                            facts={{
                              violations: detail.violations ?? 0, reportsTo: detail.reports_to ?? null,
                              apps: detail.apps.length, panels: detail.panels.length,
                              spend: detail.apps.reduce((sum, a) => sum + (a.spend_usd || 0), 0),
                              trust: detail.registered ? detail.trust_score : null,
                            }} />
            {registering ? (
              <AgentQuickCreate projectId={projectId} actor={actor} initialName={detail.name}
                                onClose={() => setRegistering(false)}
                                onCreated={(key) => { setRegistering(false); void load(); openAgent(key); }} />
            ) : null}
            <PassportNav active={tab} onPick={pick} />
            {sharing && passport ? (
              <ShareCardDialog projectId={projectId} roleKey={detail.role_key} version={passport.version}
                               name={passport.persona.callsign || passport.displayName || detail.name}
                               onClose={() => setSharing(false)} />
            ) : null}

            {/* LOADOUT FIRST (agent-loadout spec §3): what it can do and how it is told to work,
                above everything it has done. An unregistered agent shows Register here. */}
            <PassportLoadout projectId={projectId} roleKey={detail.role_key} actor={actor}
                             agentName={passport?.persona.callsign || detail.name}
                             registered={detail.registered} skills={detail.skills} mcp={detail.mcp}
                             allowed={spec.allowed_skills ?? []} forbidden={spec.forbidden_skills ?? []}
                             onRegister={() => setRegistering(true)}
                             onChanged={() => { void load(); void loadPassport(); }}
                             onVersion={(version) => { setPassport((p) => (p ? { ...p, version } : p)); void loadPassport(); }} />

            <section className="ct-block" id="ag-sec-work">
              <SectionLabel level={1} as="h2">Work</SectionLabel>
              <PassportWork projectId={projectId} actor={actor} roleKey={detail.role_key} name={detail.name}
                            presence={live} />

              {/* WHAT IT HAS ACTUALLY WORKED ON. Attributed through the same stage→role map BOSS
                  uses for the cost ledger, so this page and the spend figures cannot disagree
                  about who did what. */}
              <SectionLabel tip={`Last ${Math.round(detail.window_hours / 24)} days`}>Apps it worked on</SectionLabel>
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

              {detail.panels.length ? (
                <>
                  <SectionLabel>Panels it sat on</SectionLabel>
                  <ul className="ad-list" data-testid="agent-panels">
                    {detail.panels.map((p) => (
                      <li key={p.id}>
                        {p.subject}
                        <span className="detail"> · {p.status}{p.phase ? ` · ${p.phase}` : ""}</span>
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}

              {/* ASSIGNED IS NOT THE SAME AS TOUCHED, and the difference is the point: an agent
                  assigned to an app it has never worked on is either idle or blocked, and one
                  working on an app it is not assigned to will be refused at the next gate. */}
              <SectionLabel testid="agent-assigned-label">Assigned to</SectionLabel>
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

            <section className="ct-block" id="ag-sec-profile">
              <SectionLabel level={1} as="h2" tip="How it speaks and looks. Never what it may do.">Profile</SectionLabel>
              {/* ONE Profile: the voice and face, the one Test (a real model call in that voice),
                  then the Studio definition's rows. */}
              <PassportPersona projectId={projectId} actor={actor} roleKey={detail.role_key} passport={passport}
                               onDraft={setDraftPersona}
                               onSaved={(version, fingerprint, persona) => setPassport((p) => (p
                                 ? { ...p, version, fingerprint, persona } : p))} />
              <PassportTest projectId={projectId} actor={actor} roleKey={detail.role_key} persona={draftPersona} />
              {studio ? <AgentProfile feed={studio} roleKey={detail.role_key} actor={actor} bare /> : null}

              {/* ITS REMIT: the capability model, on screen. What it may run is the Loadout's. */}
              <SectionLabel testid="agent-remit-label">Remit</SectionLabel>
              {spec.mission ? <p className="ad-mission">{spec.mission}</p> : (
                <p className="detail" data-testid="agent-no-spec">
                  This agent has no spec. A spec is what states its mission, its boundaries and
                  the skills it may use — without one, nothing constrains what it will attempt.
                </p>
              )}
              {spec.primary_outcomes?.length ? (
                <>
                  <SectionLabel as="h4">Owns these outcomes</SectionLabel>
                  <ul className="ad-list">{spec.primary_outcomes.map((o) => <li key={o}>{o}</li>)}</ul>
                </>
              ) : null}
              {spec.boundaries?.must?.length || spec.boundaries?.must_not?.length ? (
                <>
                  <SectionLabel as="h4">Boundaries</SectionLabel>
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
              {spec.output_contracts?.length ? (
                <>
                  <SectionLabel as="h4">What it must produce</SectionLabel>
                  <ul className="ad-list">
                    {spec.output_contracts.map((c) => <li key={String(c)}>{String(c)}</li>)}
                  </ul>
                </>
              ) : null}
            </section>

            {/* WHERE IT IS PUT TO WORK: each workflow step naming it, opening on that step. */}
            <section className="ct-block" data-testid="agent-used-in" id="ag-sec-used">
              <SectionLabel level={1} as="h2" tip="Workflow steps this agent runs">Used in</SectionLabel>
              {usedIn(detail.named_on_steps).length ? (
                <ul className="ad-list">
                  {usedIn(detail.named_on_steps).map((u) => (
                    <li key={`${u.workflow_key}:${u.step_id}`}>
                      <a href={u.href} data-testid={`agent-used-in-${u.workflow_key}-${u.step_id}`}>
                        {u.workflow} · {u.step}
                      </a>
                    </li>
                  ))}
                </ul>
              ) : <p className="detail">No workflow step names it</p>}
            </section>

            <section className="ct-block" id="ag-sec-lineage">
              <SectionLabel level={1} as="h2">Lineage</SectionLabel>
              <PassportLineage projectId={projectId} actor={actor} roleKey={detail.role_key} />
            </section>

            {detail.registered ? (
              <div className="ld-settings" id="ag-sec-settings">
                <SectionLabel level={1} as="h2">Settings</SectionLabel>
                <AgentEdit projectId={projectId} actor={actor} detail={detail} onChanged={load} />
                <AgentDelete projectId={projectId} actor={actor} detail={detail}
                             onChanged={() => void load()}
                             onDeleted={() => { onDeleted?.(detail.role_key); onClose(); }} />
              </div>
            ) : <div id="ag-sec-settings" />}
          </>
        ) : null}
      </div>
    </div>
  );
  return typeof document !== "undefined" ? createPortal(page, document.body) : null;
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
 *
 * A step in an OLDER version (published, startable by id, but not what anything starts by
 * default) does not block: the page says those versions are retired with the agent, and the
 * delete asks for exactly that. Review of milestone/agents, round 2 — the guard used to read
 * only the newest version, so the version that actually ran was not protected; reading every
 * startable version without this way through would make an agent once named in v1 of a
 * workflow now at v5 impossible to delete.
 *
 * WHAT TO DO IS SAID PER STEP (review of milestone/agents, round 3). Every blocking step got
 * "save the workflow", which moves neither a mapping nor a parent's subworkflow binding; each
 * step now carries the API's own `remedy`, and a mapping that still sends work to the old
 * version gets the button that moves it.
 */
export function AgentDelete({ projectId, actor, detail, onDeleted, onChanged }: {
  projectId: string; actor: string; detail: Detail; onDeleted: () => void;
  /** Told when something on this panel changed what names the agent, so it is read again. */
  onChanged?: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");
  const named = detail.named_on_steps ?? [];
  const blocking = named.filter((n) => !n.retirable);
  const older = named.filter((n) => n.retirable);
  const alsoRetired = older.flatMap((n) => (n.also_retires ?? [])
    .map((p) => `${p.workflow} v${p.version}`));
  const mappings: Array<{ id: string; label: string }> = [];
  for (const n of blocking) {
    for (const p of n.pointed_by ?? []) {
      if (p.via === "mapped" && p.mapping_id && !mappings.some((m) => m.id === p.mapping_id)) {
        mappings.push({ id: p.mapping_id, label: `${n.workflow}: ${p.selector_value ?? "work"}` });
      }
    }
  }
  const profiles = (detail.studio_profiles ?? []).filter((p) => p.status !== "retired");

  const remove = async () => {
    setBusy(true); setFailed("");
    const r = await deleteAgent(projectId, actor, detail.role_key,
                                { retireOlderVersions: older.length > 0 });
    setBusy(false);
    if (!r.ok) { setFailed(r.error); setConfirming(false); return; }
    onDeleted();
  };

  const repoint = async (mappingId: string) => {
    setBusy(true); setFailed("");
    const r = await repointMapping(projectId, actor, mappingId);
    setBusy(false);
    if (!r.ok) { setFailed(r.error); return; }
    onChanged?.();
  };

  return (
    <section className="ct-block ad-danger" data-testid="agent-delete">
      <div className="ct-block-title">Delete this agent</div>
      {blocking.length ? (
        <>
          <p className="detail" data-testid="agent-delete-blocked">
            It cannot be deleted while {blocking.length} workflow step{blocking.length === 1 ? " names" : "s name"} it.
            To delete it: {remediesFor(blocking).join("; ")} — then come back.
          </p>
          <ul className="ad-list" data-testid="agent-named-on">
            {blocking.map((n) => (
              <li key={`${n.workflow_key}:${n.version}:${n.step_id}`}>
                {n.workflow} · <code>{n.step}</code>
                <span className="detail"> · {n.how} · {whereNamed(n)}</span>
              </li>
            ))}
          </ul>
          {mappings.length ? (
            <div className="ad-danger-actions" data-testid="agent-delete-repoint">
              {mappings.map((m) => (
                <button key={m.id} type="button" className="cap-btn" disabled={busy}
                        data-testid={`agent-delete-repoint-${m.id}`}
                        onClick={() => void repoint(m.id)}>
                  Point at the current version ({m.label})
                </button>
              ))}
            </div>
          ) : null}
        </>
      ) : (
        <p className="detail">
          Its skills and MCP servers are released{profiles.length
            ? `, its ${profiles.length} Studio profile${profiles.length === 1 ? " is" : "s are"} retired (their runs are kept)`
            : ""}, and anyone reporting to it moves up to {detail.reports_to
            ? <code>{detail.reports_to}</code> : "the top of the team"}. An upgrade will not bring
          it back.
          {older.length ? (
            <span data-testid="agent-delete-retires">
              {" "}Only older workflow versions still name it — nothing starts them by default —
              so deleting also retires {older.length === 1 ? "that version" : "those versions"}{" "}
              ({Array.from(new Set(older.map((n) => `${n.workflow} v${n.version}`))).join(", ")}):
              their history is kept, but no run can start from them.
              {alsoRetired.length ? (
                <>
                  {" "}Older versions that start {older.length === 1 ? "it" : "them"} as a
                  subworkflow are retired with {older.length === 1 ? "it" : "them"} too
                  ({Array.from(new Set(alsoRetired)).join(", ")}).
                </>
              ) : null}
            </span>
          ) : null}
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
          <button type="button" className="linklike" disabled={blocking.length > 0}
                  data-testid="agent-delete-open" onClick={() => setConfirming(true)}>
            Delete {detail.name}
          </button>
        )}
      </div>
    </section>
  );
}
