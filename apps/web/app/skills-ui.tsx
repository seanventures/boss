"use client";

/**
 * Skills, as objects you author rather than ids you enable.
 *
 * The thing most worth customising is the thing BOSS knew least about: the operator's own
 * tools, accounts and house rules. Behaviour used to live in a Python executor inside the
 * product, so every capability a customer wanted was a pull request. A skill authored here
 * attaches at both ends that already existed — an agent's requested skills, and a workflow's
 * custom step — so nothing downstream had to be invented to make one reusable.
 *
 * The form is deliberately plain and the knowledge box is deliberately large. Knowledge is the
 * part an operator will spend real time on: the format their team uses, the clauses that
 * matter, the thing that makes the skill behave the same on its hundredth run.
 */

import React, { useCallback, useEffect, useState } from "react";
import { MemoryHome } from "./memory-ui";

type Skill = {
  skill_key: string; label: string; summary: string; behaviour_kind: string;
  knowledge: string; inputs: Array<{ name: string; required?: boolean }>;
  produces: Array<{ name: string }>; config: Record<string, unknown>;
  risk_tier: number; trusted: boolean; enabled: boolean; requires_approval: boolean;
};

const BEHAVIOURS = [
  { id: "sandboxed_command", label: "Runs a command in a sandbox",
    hint: "ffmpeg, pandoc, ImageMagick — anything with a command line. Needs a digest-pinned "
        + "image and real isolation to execute." },
  { id: "mcp_tool", label: "Calls an MCP tool",
    hint: "The answer for a third-party account: the MCP server holds the integration, the "
        + "skill names one of its tools." },
  { id: "builtin", label: "Names a built-in executor",
    hint: "One BOSS already ships. Use this to give an existing skill knowledge and a risk "
        + "tier without changing its code." },
];

const BLANK = {
  skill_key: "", label: "", summary: "", behaviour_kind: "sandboxed_command",
  knowledge: "", inputs: "", produces: "",
  image: "", command: "", timeout_seconds: "300", network: false,
  mcp_server_id: "", tool_name: "", executor_id: "",
  risk_tier: "1", trusted: false,
};

/** A registered MCP server. Where a connector's tools live — the realistic path to a
 *  third-party account, since the server holds the integration and a skill names one of its
 *  tools. Read-only here: adding one is a governed registration, not a form field. */
type McpServer = {
  id: string; name: string; url: string; transport?: string; risk_tier?: number;
  data_boundary?: string; enabled?: boolean; description?: string;
};

/** One of the skills BOSS ships. A different shape from an authored one: it has an executor
 *  behind it and a project enables it rather than writing it. */
type BuiltIn = { id: string; kind?: string; source?: string; risk_tier?: number;
                 enabled?: boolean; description?: string;
                 permissions?: { allowed_callers?: string[] } };

/** WHO THE SPEC SAYS MAY CALL THIS, LIMITED TO AGENTS THAT ACTUALLY EXIST HERE.
 *
 *  Skills declare `allowed_callers`, and several of them name only internal subsystems —
 *  `worker`, `software_factory`, `router`, `control_plane`, `resource_plane_kernel`. Those are
 *  machinery, not members of the workforce, and handing such a skill to an agent buys nothing:
 *  the thing that calls it is not an agent and already has permission.
 *
 *  Seven of the seventeen shipped skills are in that position. Offering them for assignment
 *  invites a pairing that is pointless at best, so the page shows them and does not offer them.
 */
function declaredAgentCallers(s: BuiltIn, roles: string[]): string[] {
  const callers = s.permissions?.allowed_callers ?? [];
  if (callers.includes("*")) return roles;
  return callers.filter((c) => roles.includes(c));
}

export function SkillsHome(
  { projectId, actor, children }:
  { projectId: string; actor: string; children?: React.ReactNode },
) {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [builtIn, setBuiltIn] = useState<BuiltIn[] | null>(null);
  const [mcp, setMcp] = useState<McpServer[] | null>(null);
  const [draft, setDraft] = useState({ ...BLANK });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  // WHO HOLDS WHAT. One read for the whole page: the alternative is a request per row, and
  // this page lists twenty skills.
  const [roles, setRoles] = useState<string[]>([]);
  const [assignments, setAssignments] = useState<Array<{ role_key: string; skill_id: string }>>([]);

  const reload = useCallback(async () => {
    try {
      const r = await fetch(`/api/skills?project_id=${encodeURIComponent(projectId)}`,
                            { cache: "no-store" });
      if (r.ok) setSkills(((await r.json()).skills ?? []) as Skill[]);
      else setSkills([]);
    } catch { setSkills([]); }
  }, [projectId]);

  const reloadAssignments = useCallback(async () => {
    try {
      const r = await fetch(
        `/api/skills?view=assignments&project_id=${encodeURIComponent(projectId)}`,
        { cache: "no-store" });
      if (r.ok) setAssignments(((await r.json()).assignments ?? []));
    } catch { /* leave what we had; a failed refresh must not blank the chips */ }
  }, [projectId]);

  useEffect(() => { void reload(); void reloadAssignments(); }, [reload, reloadAssignments]);

  // The agents a skill can be given to. Same list the workflow page addresses captains from.
  useEffect(() => {
    (async () => {
      try {
        const r = await fetch(`/api/agent?view=roles&project_id=${encodeURIComponent(projectId)}`,
                              { cache: "no-store" });
        if (!r.ok) return;
        const body = await r.json();
        const items = (body.roles ?? body.items ?? []) as Array<Record<string, unknown>>;
        setRoles(items
          .map((x) => String(x.key ?? x.role_key ?? x.id ?? ""))
          .filter(Boolean)
          .sort());
      } catch { /* the rest of the page still works without the picker */ }
    })();
  }, [projectId]);

  const holdersOf = useCallback(
    (skillId: string) => assignments.filter((a) => a.skill_id === skillId).map((a) => a.role_key),
    [assignments]);

  const setAssigned = useCallback(async (
    skillId: string, roleKey: string, assign: boolean,
  ) => {
    if (busy) return;
    setBusy(true); setNote(null);
    try {
      const r = await fetch("/api/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: assign ? "assign" : "unassign",
          project_id: projectId, actor, role_key: roleKey, skill_id: skillId,
        }),
      });
      const data = await r.json().catch(() => ({}));
      setBusy(false);
      if (!r.ok) { setNote({ ok: false, text: String(data?.error ?? `HTTP ${r.status}`) }); return; }
      setNote({ ok: true, text: assign
        ? `${roleLabel(roleKey)} now holds ${skillId}.`
        : `${roleLabel(roleKey)} no longer holds ${skillId}.` });
      void reloadAssignments();
    } catch (e) {
      setBusy(false);
      setNote({ ok: false, text: String((e as Error)?.message ?? e) });
    }
  }, [busy, projectId, actor, reloadAssignments]);

  useEffect(() => {
    (async () => {
      try {
        const r = await fetch(
          `/api/skills?view=mcp&project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        setMcp(r.ok ? (((await r.json()).items ?? []) as McpServer[]) : []);
      } catch { setMcp([]); }
    })();
  }, [projectId]);

  // WHAT THIS INSTALL ALREADY HAS. Shown beside what the operator wrote, because "you have no
  // skills" is false on a fresh project — BOSS ships seventeen, and several of them are the
  // ones somebody would otherwise try to author from scratch.
  useEffect(() => {
    (async () => {
      try {
        const r = await fetch(
          `/api/skills?view=builtin&project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (r.ok) {
          const body = await r.json();
          setBuiltIn(((body.items ?? body.skills ?? []) as BuiltIn[]));
        } else setBuiltIn([]);
      } catch { setBuiltIn([]); }
    })();
  }, [projectId]);

  const save = useCallback(async () => {
    if (busy) return;
    setBusy(true); setNote(null);
    // Names, one per line or comma-separated — whichever an operator happens to type.
    const names = (raw: string) => raw.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
      .map((name) => ({ name }));
    const config: Record<string, unknown> =
      draft.behaviour_kind === "sandboxed_command"
        ? { image: draft.image.trim(), command: draft.command.trim(),
            timeout_seconds: Number(draft.timeout_seconds) || 300,
            network: draft.network, outputs: names(draft.produces) }
        : draft.behaviour_kind === "mcp_tool"
          ? { mcp_server_id: draft.mcp_server_id.trim(), tool_name: draft.tool_name.trim() }
          : { executor_id: draft.executor_id.trim() };
    try {
      const r = await fetch("/api/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: projectId, actor,
          skill_key: draft.skill_key.trim(), label: draft.label.trim(),
          summary: draft.summary.trim(), behaviour_kind: draft.behaviour_kind,
          knowledge: draft.knowledge, inputs: names(draft.inputs),
          produces: names(draft.produces), config,
          risk_tier: Number(draft.risk_tier) || 1, trusted: draft.trusted,
        }),
      });
      const data = await r.json().catch(() => ({}));
      setBusy(false);
      if (!r.ok) {
        // The API's messages are already written for the operator, so they are shown as-is.
        setNote({ ok: false, text: String(data?.error ?? `HTTP ${r.status}`) });
        return;
      }
      setNote({ ok: true, text: `Saved ${draft.skill_key.trim()}.` });
      setDraft({ ...BLANK });
      void reload();
    } catch (e) {
      setBusy(false);
      setNote({ ok: false, text: String((e as Error)?.message ?? e) });
    }
  }, [busy, draft, projectId, actor, reload]);

  const remove = useCallback(async (key: string) => {
    setBusy(true); setNote(null);
    try {
      const r = await fetch("/api/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "delete", skill_key: key, project_id: projectId, actor }),
      });
      const data = await r.json().catch(() => ({}));
      setBusy(false);
      if (!r.ok) { setNote({ ok: false, text: String(data?.error ?? `HTTP ${r.status}`) }); return; }
      void reload();
    } catch (e) {
      setBusy(false);
      setNote({ ok: false, text: String((e as Error)?.message ?? e) });
    }
  }, [projectId, actor, reload]);

  const chosen = BEHAVIOURS.find((b) => b.id === draft.behaviour_kind);

  return (
    <section className="panel-stack" aria-label="Skills">

      {/* DESCRIBE IT; DO NOT SPECIFY IT.
          The form below asks an operator to already know what a behaviour kind is, which
          fields a sandboxed command needs, and that its image has to be digest-pinned. That is
          the shape of the DATA, not the shape of anything a person wants to say. What they
          want to say is "trim a video between two timecodes and stamp our watermark on it" —
          so that is the way in, and BOSS turns the answers into the skill and offers it back
          as one press. The form stays for correcting a field afterwards; it is no longer the
          thing you meet first. */}
      <article className="stack-card" data-testid="skill-agent">
        <div className="section-head">
          <div>
            <span className="eyebrow">Author a skill</span>
            <h2>Tell BOSS what it should be able to do.</h2>
          </div>
        </div>
        <p className="detail">
          Describe the job in your own words. BOSS asks for whatever it still needs — what it
          takes in, what it hands back, how it runs — and then offers the finished skill to
          create in one press.
        </p>
        <button type="button" className="cap-btn primary" data-testid="skill-agent-open"
                onClick={() => askBossAboutSkills(projectId)}>
          Design a skill with BOSS
        </button>
      </article>

      {/* THE SLOT. Channels sits here — after the way IN to authoring, before the
          catalogues — because connecting an account is setup you do once and the thing you
          came to do is above it. */}
      {children}

      {/* WHAT AGENTS KNOW, beside what they can DO.
          A rule recorded in an earlier session changes every later answer, so it belongs on
          the page an operator already opens to see what their agents are capable of — not on
          a settings tab nobody visits. It opens itself when something is waiting for review,
          because a review queue behind a closed fold is one that never gets emptied. */}
      <MemoryHome projectId={projectId} actor={actor} />

      <details className="stack-card" data-testid="skills-custom">
        <summary className="skill-form-summary">
          {/* A COLLAPSED SECTION MUST STILL SAY WHAT IS IN IT. "Custom skills" tells a reader
              nothing about whether opening it is worth the click; the count does. */}
          Custom skills{skills === null ? "" : ` (${skills.length})`}
        </summary>
        <div className="section-head">
          <div>
            <h2>The ones you wrote for this project.</h2>
            <p className="detail">
              A skill is one reusable unit of work. Attach it to an agent, or place it in a
              workflow as a step — the same object either way.
            </p>
          </div>
        </div>
        {skills === null ? <p className="detail">Loading…</p>
         : skills.length === 0 ? (
           <p className="detail" data-testid="skills-empty">
             Nothing authored yet. Describe one to BOSS below and it will draft it for you.
           </p>
         ) : (
          <ul className="skill-list" data-testid="skill-list">
            {skills.map((s) => (
              <li key={s.skill_key} className="skill-row" data-enabled={s.enabled}>
                <span className="skill-row-main">
                  <b>{s.label}</b>
                  <code className="detail">{s.skill_key}</code>
                  <span className="cap-badge tone-muted">{s.behaviour_kind.replace(/_/g, " ")}</span>
                  {/* THE CONSEQUENCE, ON THE ROW. Tier alone means nothing to a reader; "needs
                      a person" is the fact they act on. */}
                  {s.requires_approval ? (
                    <span className="cap-badge tone-amber">needs a person</span>
                  ) : null}
                  {!s.trusted && s.requires_approval ? (
                    <span className="cap-badge tone-pink" title="An untrusted skill with this
                      much reach cannot be placed in a workflow until somebody reviews it.">
                      unreviewed
                    </span>
                  ) : null}
                </span>
                {s.summary ? <span className="detail">{s.summary}</span> : null}
                <AssignRow
                  skillId={s.skill_key}
                  roles={roles}
                  declared={[]}
                  holders={holdersOf(s.skill_key)}
                  busy={busy}
                  onAssign={(r) => void setAssigned(s.skill_key, r, true)}
                  onUnassign={(r) => void setAssigned(s.skill_key, r, false)}
                  // Authored skills are what a custom workflow step runs, so both ends are
                  // open to them. A built-in has an executor and a standard capability
                  // already covers it in a workflow, so it offers the agent end only.
                  canPlaceInWorkflow
                />
                <button type="button" className="linklike" disabled={busy}
                        data-testid={`skill-delete-${s.skill_key}`}
                        onClick={() => void remove(s.skill_key)}>Delete</button>
              </li>
            ))}
          </ul>
        )}
      </details>

      <details className="stack-card" data-testid="skills-builtin">
        <summary className="skill-form-summary">
          Already in BOSS{builtIn === null ? "" : ` (${builtIn.length})`}
        </summary>
        <div className="section-head">
          <div>
            <h2>{builtIn === null ? "Built-in skills"
                 : `${builtIn.length} skills ship with BOSS`}</h2>
            <p className="detail">
              These have code behind them. Give one to an agent and it can use it in its own
              work. Author a new skill above only when nothing here covers the job.
            </p>
          </div>
        </div>
        {builtIn === null ? <p className="detail">Loading…</p>
         : builtIn.length === 0 ? (
           <p className="detail" data-testid="builtin-empty">
             None registered on this project yet.
           </p>
         ) : (
          <ul className="skill-list" data-testid="builtin-list">
            {builtIn.map((s) => (
              <li key={s.id} className="skill-row" data-enabled={s.enabled !== false}>
                <span className="skill-row-main">
                  <code><b>{s.id}</b></code>
                  {s.kind ? <span className="cap-badge tone-muted">{s.kind}</span> : null}
                  {(s.risk_tier ?? 0) >= 2 ? (
                    <span className="cap-badge tone-amber">needs a person</span>
                  ) : null}
                  {s.enabled === false ? (
                    <span className="cap-badge tone-muted">off</span>
                  ) : null}
                </span>
                {s.description ? <span className="detail">{s.description}</span> : null}
                {roles.length > 0 && declaredAgentCallers(s, roles).length === 0
                 && holdersOf(s.id).length === 0 ? (
                  <span className="detail skill-internal" data-testid={`skill-internal-${s.id}`}>
                    Used inside BOSS by{" "}
                    <code>{(s.permissions?.allowed_callers ?? []).join(", ") || "the platform"}</code>
                    {" "}— machinery rather than an agent, so there is nobody to give it to.
                  </span>
                ) : (
                  <AssignRow
                    skillId={s.id}
                    roles={roles}
                    // The agents the spec already names go first: they are the ones that can
                    // call it without the assignment having to widen anything.
                    declared={declaredAgentCallers(s, roles)}
                    holders={holdersOf(s.id)}
                    busy={busy}
                    onAssign={(r) => void setAssigned(s.id, r, true)}
                    onUnassign={(r) => void setAssigned(s.id, r, false)}
                    canPlaceInWorkflow={false}
                  />
                )}
              </li>
            ))}
          </ul>
        )}
      </details>

      <details className="stack-card" data-testid="mcp-servers">
        <summary className="skill-form-summary">
          MCP servers{mcp === null ? "" : ` (${mcp.length})`}
        </summary>
        <div className="section-head">
          <div>
            <h2>Tools BOSS reaches over a protocol.</h2>
            <p className="detail">
              An MCP server holds an integration — a third-party account, a data source — and a
              skill names one of its tools. This is the realistic path to connecting something
              BOSS does not ship, because the server maintains the API and the auth, not BOSS.
            </p>
          </div>
        </div>
        {mcp === null ? <p className="detail">Loading…</p>
         : mcp.length === 0 ? (
           <p className="detail" data-testid="mcp-empty">
             None registered. A server is registered through the API — deliberately not a form
             here, because pointing BOSS at a tool endpoint is a governed act with a risk tier
             and a data boundary, not a text field.
           </p>
         ) : (
          <ul className="skill-list" data-testid="mcp-list">
            {mcp.map((m) => (
              <li key={m.id} className="skill-row" data-enabled={m.enabled !== false}>
                <span className="skill-row-main">
                  <b>{m.name}</b>
                  {m.transport ? (
                    <span className="cap-badge tone-muted">{m.transport}</span>
                  ) : null}
                  {(m.risk_tier ?? 0) >= 2 ? (
                    <span className="cap-badge tone-amber">needs a person</span>
                  ) : null}
                  {m.data_boundary ? (
                    <span className="cap-badge tone-muted">{m.data_boundary}</span>
                  ) : null}
                  {m.enabled === false ? (
                    <span className="cap-badge tone-muted">off</span>
                  ) : null}
                </span>
                {m.description ? <span className="detail">{m.description}</span> : null}
                {/* The URL, quietly. It is how an operator tells two servers apart when the
                    names are similar, and it is not a secret. */}
                <code className="detail">{m.url}</code>
              </li>
            ))}
          </ul>
        )}
      </details>

      <details className="stack-card" data-testid="skill-form">
        <summary className="skill-form-summary">
          {draft.skill_key
            ? `Edit ${draft.skill_key} by hand`
            : "Write one by hand instead"}
        </summary>
        <div className="setup-form">
          <label className="setup-field">
            <span>Name</span>
            <input type="text" value={draft.label} data-testid="skill-label"
                   placeholder="Trim a clip"
                   onChange={(e) => setDraft((d) => ({ ...d, label: e.target.value }))} />
          </label>
          <label className="setup-field">
            <span>Key</span>
            <input type="text" value={draft.skill_key} data-testid="skill-key"
                   placeholder="trim_clip" spellCheck={false}
                   onChange={(e) => setDraft((d) => ({ ...d, skill_key: e.target.value }))} />
          </label>
          <label className="setup-field">
            <span>What it does</span>
            <input type="text" value={draft.summary} data-testid="skill-summary"
                   placeholder="Cuts a clip out of a longer video."
                   onChange={(e) => setDraft((d) => ({ ...d, summary: e.target.value }))} />
          </label>
          <label className="setup-field">
            <span>How it runs</span>
            <select value={draft.behaviour_kind} data-testid="skill-behaviour"
                    onChange={(e) => setDraft((d) => ({ ...d, behaviour_kind: e.target.value }))}>
              {BEHAVIOURS.map((b) => <option key={b.id} value={b.id}>{b.label}</option>)}
            </select>
          </label>
          {chosen ? <p className="detail">{chosen.hint}</p> : null}

          {draft.behaviour_kind === "sandboxed_command" ? (
            <>
              <label className="setup-field">
                <span>Image (pinned by digest)</span>
                <input type="text" value={draft.image} data-testid="skill-image"
                       spellCheck={false} placeholder="ghcr.io/acme/ffmpeg@sha256:…"
                       onChange={(e) => setDraft((d) => ({ ...d, image: e.target.value }))} />
              </label>
              <label className="setup-field">
                <span>Command</span>
                <textarea rows={3} value={draft.command} data-testid="skill-command"
                          spellCheck={false}
                          placeholder="ffmpeg -i {video} -ss {start} -to {end} -c copy {workdir}/clip.mp4"
                          onChange={(e) => setDraft((d) => ({ ...d, command: e.target.value }))} />
              </label>
              {/* SAID BEFORE THEY WRITE IT, not after the save fails. Both of these are
                  structural and neither is obvious from a command box. */}
              <p className="detail">
                Every <code>{"{name}"}</code> must be one of the inputs below, and each one is
                shell-quoted — an input cannot become a second command. <code>{"{workdir}"}</code>
                {" "}is always available.
              </p>
              <label className="setup-field">
                <span>Network</span>
                <select value={draft.network ? "yes" : "no"} data-testid="skill-network"
                        onChange={(e) => setDraft((d) => ({ ...d, network: e.target.value === "yes" }))}>
                  <option value="no">Off — the command cannot reach anything</option>
                  <option value="yes">On — it needs to fetch or send</option>
                </select>
              </label>
            </>
          ) : draft.behaviour_kind === "mcp_tool" ? (
            <>
              <label className="setup-field">
                <span>MCP server id</span>
                <input type="text" value={draft.mcp_server_id} data-testid="skill-mcp-server"
                       spellCheck={false}
                       onChange={(e) => setDraft((d) => ({ ...d, mcp_server_id: e.target.value }))} />
              </label>
              <label className="setup-field">
                <span>Tool name</span>
                <input type="text" value={draft.tool_name} data-testid="skill-mcp-tool"
                       spellCheck={false}
                       onChange={(e) => setDraft((d) => ({ ...d, tool_name: e.target.value }))} />
              </label>
            </>
          ) : (
            <label className="setup-field">
              <span>Executor id</span>
              <input type="text" value={draft.executor_id} data-testid="skill-executor"
                     spellCheck={false} placeholder="deployment.health.check"
                     onChange={(e) => setDraft((d) => ({ ...d, executor_id: e.target.value }))} />
            </label>
          )}

          <label className="setup-field">
            <span>Inputs — one per line</span>
            <textarea rows={3} value={draft.inputs} data-testid="skill-inputs"
                      placeholder={"video\nstart\nend"}
                      onChange={(e) => setDraft((d) => ({ ...d, inputs: e.target.value }))} />
          </label>
          <label className="setup-field">
            <span>Produces — one per line</span>
            <textarea rows={2} value={draft.produces} data-testid="skill-produces"
                      placeholder="clip"
                      onChange={(e) => setDraft((d) => ({ ...d, produces: e.target.value }))} />
          </label>

          {/* THE PART WORTH THE MOST TIME. A binding says which tool runs; it says nothing
              about the standard the work is held to, and that knowledge used to live in the
              head of whoever defined the step. */}
          <label className="setup-field">
            <span>What this skill knows</span>
            <textarea rows={8} value={draft.knowledge} data-testid="skill-knowledge"
                      placeholder={"Markdown. The house rules this work is held to —\n"
                        + "the format your team uses, what must never happen,\n"
                        + "the internal doc that explains the system it touches.\n\n"
                        + "Carried into every execution, so the skill behaves the same on its\n"
                        + "hundredth run as on the one somebody watched."}
                      onChange={(e) => setDraft((d) => ({ ...d, knowledge: e.target.value }))} />
          </label>

          <label className="setup-field">
            <span>Risk</span>
            <select value={draft.risk_tier} data-testid="skill-risk"
                    onChange={(e) => setDraft((d) => ({ ...d, risk_tier: e.target.value }))}>
              <option value="0">0 — reads only</option>
              <option value="1">1 — writes something recoverable</option>
              <option value="2">2 — changes the world (always needs a person)</option>
              <option value="3">3 — irreversible and public</option>
            </select>
          </label>
          {Number(draft.risk_tier) >= 2 ? (
            <p className="detail" data-testid="skill-risk-note">
              At this tier a step using it cannot be set to auto — BOSS forces a human
              approval — and it must be marked reviewed before it can be placed at all.
            </p>
          ) : null}
          <label className="setup-field">
            <span>Reviewed</span>
            <select value={draft.trusted ? "yes" : "no"} data-testid="skill-trusted"
                    onChange={(e) => setDraft((d) => ({ ...d, trusted: e.target.value === "yes" }))}>
              <option value="no">Not yet reviewed</option>
              <option value="yes">A person has read this and vouches for it</option>
            </select>
          </label>

          <button type="button" className="cap-btn primary" data-testid="skill-save"
                  disabled={busy || !draft.skill_key.trim() || !draft.label.trim()}
                  onClick={() => void save()}>
            {busy ? "Saving…" : "Save skill"}
          </button>
        </div>
        {note ? (
          <p className={note.ok ? "setup-ok" : "setup-bad"} data-testid="skill-note">
            {note.text}
          </p>
        ) : null}
      </details>
    </section>
  );
}

/** Open the dock addressed to the Program Manager, whose remit is process, with an opener that
 *  says what it can do here. Same door the Workflow page uses to design a workflow — a skill is
 *  the unit a custom workflow step runs, so it is the same conversation one level down. */
function askBossAboutSkills(projectId: string): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("agent", "general");
    url.searchParams.set("dock_role", "Program Manager");
    url.searchParams.set(
      "dock_ask",
      "I want to create a skill for this project. A skill is the reusable unit of doing that a"
      + " workflow step runs — trimming a video, posting to a channel, calling one tool on an"
      + " MCP server. Ask me what you need to know, then draft it and I will create it."
      + ` Start by telling me which skills ${projectId} already has, so I do not ask for one`
      + " that exists.",
    );
    window.history.replaceState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

/** WHERE A SKILL CAN GO, ON THE ROW THAT OWNS IT.
 *
 *  A skill attaches at two independent ends: an AGENT holds it, and a WORKFLOW STEP runs it.
 *  Both, either or neither is a legitimate answer, so this offers them separately rather than
 *  making the operator pick one. The agent end is assigned right here because it is a single
 *  row in one table; the workflow end opens the workflow agent, because placing a step means
 *  deciding where in a path it sits and what feeds it — a conversation, not a dropdown.
 */
function AssignRow({
  skillId, roles, declared, holders, onAssign, onUnassign, busy, canPlaceInWorkflow,
}: {
  skillId: string;
  roles: string[];
  /** Agents the skill's own spec already names as callers. Offered first — they are the ones
   *  that can invoke it without the assignment having to widen anything. */
  declared: string[];
  holders: string[];
  onAssign: (roleKey: string) => void;
  onUnassign: (roleKey: string) => void;
  busy: boolean;
  canPlaceInWorkflow: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [pick, setPick] = useState("");
  const free = roles.filter((r) => !holders.includes(r));
  const suggested = free.filter((r) => declared.includes(r));
  const rest = free.filter((r) => !declared.includes(r));
  return (
    <div className="skill-assign" data-testid={`skill-assign-${skillId}`}>
      {holders.length > 0 ? (
        <span className="skill-holders">
          {holders.map((r) => (
            <span key={r}
                  className={`cap-badge ${declared.includes(r) ? "tone-muted" : "tone-amber"}`}
                  title={declared.includes(r)
                    ? `${skillId} already names ${roleLabel(r)} as a caller.`
                    : `${roleLabel(r)} is not in this skill's declared callers — the assignment `
                      + "is what permits it. Approvals and budget still apply."}
                  data-testid={`skill-holder-${r}`}>
              {roleLabel(r)}
              <button type="button" className="linklike skill-holder-drop" disabled={busy}
                      title={`Take ${skillId} off ${roleLabel(r)}`}
                      onClick={() => onUnassign(r)}>×</button>
            </span>
          ))}
        </span>
      ) : (
        <span className="detail">Held by nobody yet.</span>
      )}
      {open ? (
        <span className="skill-assign-pick">
          <select value={pick} data-testid={`skill-assign-select-${skillId}`}
                  onChange={(e) => setPick(e.target.value)}>
            <option value="">Choose an agent…</option>
            {/* GROUPED, because the two groups mean different things. An agent the spec
                already names can call this today; any other works because the assignment
                itself grants it, which is a decision worth making on purpose. */}
            {suggested.length > 0 ? (
              <optgroup label="Already named by this skill">
                {suggested.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
              </optgroup>
            ) : null}
            {rest.length > 0 ? (
              <optgroup label={suggested.length > 0 ? "Any other agent" : "Agents"}>
                {rest.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
              </optgroup>
            ) : null}
          </select>
          <button type="button" className="cap-btn" disabled={busy || !pick}
                  data-testid={`skill-assign-confirm-${skillId}`}
                  onClick={() => { onAssign(pick); setPick(""); setOpen(false); }}>
            Assign
          </button>
          <button type="button" className="linklike" onClick={() => setOpen(false)}>Cancel</button>
        </span>
      ) : (
        <span className="skill-assign-actions">
          {free.length > 0 ? (
            <button type="button" className="linklike" data-testid={`skill-assign-open-${skillId}`}
                    onClick={() => setOpen(true)}>
              Assign to an agent
            </button>
          ) : null}
          {canPlaceInWorkflow ? (
            <button type="button" className="linklike"
                    data-testid={`skill-to-workflow-${skillId}`}
                    onClick={() => askBossToPlaceSkill(skillId)}>
              Use in a workflow
            </button>
          ) : null}
        </span>
      )}
    </div>
  );
}

function roleLabel(roleKey: string): string {
  return roleKey.replace(/_/g, " ").replace(/\b[a-z]/g, (c) => c.toUpperCase());
}

/** Placing a step means deciding where in a path it sits and what feeds it. That is the
 *  workflow agent's job, so this hands the skill to it by name rather than inventing a second
 *  half-built step editor here. */
function askBossToPlaceSkill(skillId: string): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("agent", "general");
    url.searchParams.set("dock_role", "Program Manager");
    url.searchParams.set(
      "dock_ask",
      `I want to use the skill \`${skillId}\` as a step in a workflow. Define the custom step`
      + " that runs it, then show me which workflows could place it and where.",
    );
    window.history.pushState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}
