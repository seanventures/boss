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
import { McpServersPanel, type McpServerRow } from "./mcp-servers-ui";
import { MemoryHome } from "./memory-ui";
import { apiWrite } from "../lib/api-write";
import { openDockAbout } from "../lib/dock-subject";

type Skill = {
  skill_key: string; label: string; summary: string; behaviour_kind: string;
  knowledge: string; inputs: Array<{ name: string; required?: boolean }>;
  produces: Array<{ name: string }>; config: Record<string, unknown>;
  risk_tier: number; trusted: boolean; enabled: boolean; requires_approval: boolean;
  /** What points at it — read before Delete is pressed, not discovered after. */
  references?: { custom_steps: Array<{ step_key: string; label?: string }>; agents: string[] };
  /** What the ONE skill catalogue says: the tier the floor will use (never below what it wraps
   *  or calls), and whether it can run right now. */
  catalogue?: { risk_tier: number; usable: boolean; reason?: string | null; runs_through?: string };
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
  // SHOWN, because agents write these: a program an agent worked out on one run and the
  // workflow kept, so later runs reuse it with no model call. Leaving the kind out of this
  // list made such a skill open in the editor as a "sandboxed command" with empty fields.
  { id: "agent_program", label: "Reruns a program an agent wrote",
    hint: "Usually written by an agent on an earlier run and kept so later runs need no model "
        + "call. Read the program below before you rely on it." },
];

const BLANK = {
  skill_key: "", label: "", summary: "", behaviour_kind: "sandboxed_command",
  knowledge: "", inputs: "", produces: "",
  image: "", command: "", timeout_seconds: "300", network: false,
  mcp_server_id: "", tool_name: "", executor_id: "",
  language: "python", source: "", program_timeout: "60",
  risk_tier: "1", trusted: false, enabled: true,
};

type Draft = typeof BLANK;

/** An existing skill, as the form's fields — what Edit loads. Before this there was no Edit:
 *  the header said "Edit <key> by hand" as soon as a key was typed, nothing loaded, and saving
 *  replaced whatever skill already had that key. */
export function draftFromSkill(s: Skill): Draft {
  const c = (s.config ?? {}) as Record<string, unknown>;
  const lines = (xs: Array<{ name: string }> | undefined) => (xs ?? []).map((x) => x.name).join("\n");
  return {
    ...BLANK,
    skill_key: s.skill_key, label: s.label, summary: s.summary ?? "",
    behaviour_kind: s.behaviour_kind, knowledge: s.knowledge ?? "",
    inputs: lines(s.inputs), produces: lines(s.produces),
    image: String(c.image ?? ""), command: String(c.command ?? ""),
    timeout_seconds: String(c.timeout_seconds ?? "300"), network: Boolean(c.network),
    mcp_server_id: String(c.mcp_server_id ?? ""), tool_name: String(c.tool_name ?? ""),
    executor_id: String(c.executor_id ?? ""),
    language: String(c.language ?? "python"), source: String(c.source ?? ""),
    program_timeout: String(c.timeout_seconds ?? "60"),
    risk_tier: String(s.risk_tier), trusted: Boolean(s.trusted), enabled: s.enabled !== false,
  };
}

/** What deleting would do, said before the press. A step that runs the skill blocks it; agents
 *  that hold it lose it, which the operator confirms. */
export function deleteConsequence(s: Skill): { blocked: string | null; confirm: string } {
  const steps = s.references?.custom_steps ?? [];
  const agents = s.references?.agents ?? [];
  if (steps.length > 0) {
    return {
      blocked: `${s.skill_key} is run by ${steps.map((x) => x.label || x.step_key).join(", ")}. `
        + "Delete or re-point those workflow steps first — or turn the skill off instead.",
      confirm: "",
    };
  }
  return {
    blocked: null,
    confirm: agents.length > 0
      ? `Delete ${s.skill_key}? ${agents.map(roleLabel).join(", ")} will no longer hold it. `
        + "This cannot be undone."
      : `Delete ${s.skill_key}? This cannot be undone.`,
  };
}

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
  const [mcp, setMcp] = useState<McpServerRow[]>([]);
  const [draft, setDraft] = useState<Draft>({ ...BLANK });
  // WHICH SKILL THE FORM IS EDITING, or null for a new one. Creating and editing are different
  // intentions and the save says which (mode create | update), so a new skill whose key
  // happens to match an old one is refused instead of silently replacing it.
  const [editing, setEditing] = useState<string | null>(null);
  // The loaded row's own config, kept so an edit does not drop what the form has no field for
  // — an agent-written program's provenance (written_by, written_for, written_in_run).
  const [loadedConfig, setLoadedConfig] = useState<Record<string, unknown>>({});
  const [formOpen, setFormOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
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
      const r = await apiWrite("/api/skills", {
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
          // The runtime catalogue carries this project's AUTHORED skills too (that is what
          // lets them run). They are listed above as "Custom skills"; listing them here as
          // well would show each one twice, once as something BOSS ships.
          setBuiltIn(((body.items ?? body.skills ?? []) as BuiltIn[])
            .filter((s) => s.source !== "authored"));
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
          : draft.behaviour_kind === "agent_program"
            ? { ...(editing ? loadedConfig : {}), language: draft.language, source: draft.source,
                timeout_seconds: Number(draft.program_timeout) || 60 }
            : { executor_id: draft.executor_id.trim() };
    try {
      const r = await apiWrite("/api/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: projectId, actor,
          skill_key: draft.skill_key.trim(), label: draft.label.trim(),
          summary: draft.summary.trim(), behaviour_kind: draft.behaviour_kind,
          knowledge: draft.knowledge, inputs: names(draft.inputs),
          produces: names(draft.produces), config,
          // Number(), not `Number(x) || 1`: tier 0 is falsy, and `|| 1` made "0 — reads only"
          // an option that silently saved tier 1.
          risk_tier: Number(draft.risk_tier), trusted: draft.trusted, enabled: draft.enabled,
          mode: editing ? "update" : "create",
        }),
      });
      const data = await r.json().catch(() => ({}));
      setBusy(false);
      if (!r.ok) {
        // The API's messages are already written for the operator, so they are shown as-is.
        setNote({ ok: false, text: String(data?.error ?? `HTTP ${r.status}`) });
        return;
      }
      setNote({ ok: true, text: editing
        ? `Saved your changes to ${draft.skill_key.trim()}.`
        : `Created ${draft.skill_key.trim()}.` });
      setDraft({ ...BLANK });
      setEditing(null);
      void reload();
    } catch (e) {
      setBusy(false);
      setNote({ ok: false, text: String((e as Error)?.message ?? e) });
    }
  }, [busy, draft, editing, loadedConfig, projectId, actor, reload]);

  /** One authored-skill write that is not a save: delete, or switch on/off. Every outcome says
   *  what happened — Delete used to say nothing at all when it worked. */
  const act = useCallback(async (payload: Record<string, unknown>, done: string) => {
    if (busy) return;
    setBusy(true); setNote(null);
    try {
      const r = await apiWrite("/api/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, actor, ...payload }),
      });
      const data = await r.json().catch(() => ({}));
      setBusy(false);
      if (!r.ok) { setNote({ ok: false, text: String(data?.error ?? `HTTP ${r.status}`) }); return; }
      setNote({ ok: true, text: done });
      void reload(); void reloadAssignments();
    } catch (e) {
      setBusy(false);
      setNote({ ok: false, text: String((e as Error)?.message ?? e) });
    }
  }, [busy, projectId, actor, reload, reloadAssignments]);

  const remove = useCallback((s: Skill) => {
    setConfirmDelete(null);
    void act({ action: "delete", skill_key: s.skill_key,
               // Sent only after the confirmation named the agents that will lose it.
               release_grants: (s.references?.agents ?? []).length > 0 },
             `Deleted ${s.skill_key}.`);
  }, [act]);

  const startEdit = useCallback((s: Skill) => {
    setDraft(draftFromSkill(s));
    setLoadedConfig({ ...(s.config ?? {}) });
    setEditing(s.skill_key);
    setFormOpen(true);
    setNote(null);
  }, []);

  const startNew = useCallback(() => {
    setDraft({ ...BLANK });
    setLoadedConfig({});
    setEditing(null);
  }, []);

  const server = mcp.find((m) => m.id === draft.mcp_server_id);
  const serverTools = (server?.tools ?? []).map((t) => t.name);

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
             Nothing authored yet. Describe one to BOSS above and it will draft it for you.
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
                  {!s.enabled ? (
                    <span className="cap-badge tone-muted" data-testid={`skill-off-${s.skill_key}`}>
                      off
                    </span>
                  ) : null}
                </span>
                {s.summary ? <span className="detail">{s.summary}</span> : null}
                {/* WHAT THE RUNTIME WILL ACTUALLY DO with it, from the one catalogue. A skill can
                    be switched on and still unable to run — its MCP server removed or turned
                    off — and the row is where that has to be said. */}
                {s.enabled && s.catalogue && !s.catalogue.usable && s.catalogue.reason ? (
                  <span className="setup-bad" data-testid={`skill-unusable-${s.skill_key}`}>
                    Cannot run: {s.catalogue.reason}
                  </span>
                ) : null}
                {s.catalogue && s.catalogue.risk_tier > s.risk_tier ? (
                  <span className="detail" data-testid={`skill-effective-tier-${s.skill_key}`}>
                    Runs at tier {s.catalogue.risk_tier}, not {s.risk_tier}: what it calls
                    {s.catalogue.runs_through ? ` (${s.catalogue.runs_through})` : ""} is tier
                    {" "}{s.catalogue.risk_tier}, and a skill is never safer than what it runs.
                  </span>
                ) : null}
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
                <span className="skill-assign-actions">
                  <button type="button" className="linklike" disabled={busy}
                          data-testid={`skill-edit-${s.skill_key}`}
                          onClick={() => startEdit(s)}>Edit</button>
                  <button type="button" className="linklike" disabled={busy}
                          data-testid={`skill-toggle-${s.skill_key}`}
                          onClick={() => void act(
                            { action: s.enabled ? "disable" : "enable", skill_key: s.skill_key },
                            s.enabled
                              ? `${s.skill_key} is off — no agent or step will run it until it `
                                + "is turned back on."
                              : `${s.skill_key} is on.`)}>
                    {s.enabled ? "Turn off" : "Turn on"}
                  </button>
                  {confirmDelete === s.skill_key ? (
                    <span className="skill-assign-pick"
                          data-testid={`skill-delete-confirm-${s.skill_key}`}>
                      <span className="detail">{deleteConsequence(s).confirm}</span>
                      <button type="button" className="cap-btn danger" disabled={busy}
                              data-testid={`skill-delete-yes-${s.skill_key}`}
                              onClick={() => remove(s)}>Delete</button>
                      <button type="button" className="linklike"
                              onClick={() => setConfirmDelete(null)}>Keep it</button>
                    </span>
                  ) : (
                    <button type="button" className="linklike" disabled={busy}
                            data-testid={`skill-delete-${s.skill_key}`}
                            onClick={() => {
                              const { blocked } = deleteConsequence(s);
                              // Refused HERE, with the steps named, rather than as a 409 after.
                              if (blocked) { setNote({ ok: false, text: blocked }); return; }
                              setConfirmDelete(s.skill_key);
                            }}>Delete</button>
                  )}
                </span>
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
          MCP servers{mcp.length === 0 ? "" : ` (${mcp.length})`}
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
        {/* THE SAME PANEL every MCP screen mounts (Capabilities manages with it too, System
            Configuration summarises with it), so they cannot tell three stories again. It
            reports the servers back so the skill form below can offer them by name. */}
        <McpServersPanel projectId={projectId} actor={actor} onLoaded={setMcp} />
      </details>

      <details className="stack-card" data-testid="skill-form" open={formOpen}
               onToggle={(e) => setFormOpen((e.currentTarget as HTMLDetailsElement).open)}>
        <summary className="skill-form-summary">
          {/* THE HEADER SAYS WHAT A SAVE WILL DO. It used to switch to "Edit <key>" as soon as
              a key was typed — while saving silently replaced any skill with that key. Now it
              says Edit only when Edit loaded one, and a new skill's key that is taken is
              refused by the save. */}
          {editing
            ? `Edit ${editing} by hand`
            : "Write one by hand instead"}
        </summary>
        <div className="setup-form">
          {editing ? (
            <p className="detail" data-testid="skill-editing">
              Editing <code>{editing}</code>. Saving changes this skill for every agent that holds
              it and every step that runs it.{" "}
              <button type="button" className="linklike" data-testid="skill-new"
                      onClick={startNew}>Write a new one instead</button>
            </p>
          ) : null}
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
                   // The key is what steps and agents point at, so an edit cannot change it.
                   readOnly={editing !== null}
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
              {/* PICKED, NOT TYPED. This asked for a free-text "MCP server id" that no screen
                  showed, and a typo saved fine and failed on the first run. */}
              <label className="setup-field">
                <span>MCP server</span>
                <select value={draft.mcp_server_id} data-testid="skill-mcp-server"
                        onChange={(e) => setDraft((d) => ({ ...d, mcp_server_id: e.target.value,
                                                            tool_name: "" }))}>
                  <option value="">Choose a server…</option>
                  {mcp.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name}{m.enabled ? "" : " (off)"}{m.reachable === false ? " (unreachable)" : ""}
                    </option>
                  ))}
                </select>
              </label>
              {mcp.length === 0 ? (
                <p className="detail">No MCP servers yet — register one in the MCP servers
                  section above first.</p>
              ) : null}
              <label className="setup-field">
                <span>Tool</span>
                {serverTools.length > 0 ? (
                  <select value={draft.tool_name} data-testid="skill-mcp-tool"
                          onChange={(e) => setDraft((d) => ({ ...d, tool_name: e.target.value }))}>
                    <option value="">Choose a tool…</option>
                    {serverTools.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                ) : (
                  // A server never reached says nothing about its tools; the name is typed, and
                  // checked against its real list once it answers.
                  <input type="text" value={draft.tool_name} data-testid="skill-mcp-tool"
                         spellCheck={false} placeholder="create_issue"
                         onChange={(e) => setDraft((d) => ({ ...d, tool_name: e.target.value }))} />
                )}
              </label>
              {server && server.status_note ? (
                <p className="setup-bad">{server.status_note}</p>
              ) : null}
            </>
          ) : draft.behaviour_kind === "agent_program" ? (
            <>
              {draft.source && editing ? (
                <p className="detail">Written by an agent — read it before relying on it. It runs
                  in the sandbox with its declared inputs as files.</p>
              ) : null}
              <label className="setup-field">
                <span>Language</span>
                <select value={draft.language} data-testid="skill-program-language"
                        onChange={(e) => setDraft((d) => ({ ...d, language: e.target.value }))}>
                  <option value="python">python</option>
                  <option value="bash">bash</option>
                  <option value="sh">sh</option>
                </select>
              </label>
              <label className="setup-field">
                <span>Program</span>
                <textarea rows={10} value={draft.source} data-testid="skill-program-source"
                          spellCheck={false}
                          onChange={(e) => setDraft((d) => ({ ...d, source: e.target.value }))} />
              </label>
            </>
          ) : (
            <label className="setup-field">
              <span>Built-in skill it runs</span>
              {(builtIn ?? []).length > 0 ? (
                <select value={draft.executor_id} data-testid="skill-executor"
                        onChange={(e) => setDraft((d) => ({ ...d, executor_id: e.target.value }))}>
                  <option value="">Choose one…</option>
                  {(builtIn ?? []).map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.id}{(b.risk_tier ?? 0) >= 2 ? " — needs a person" : ""}
                    </option>
                  ))}
                </select>
              ) : (
                <input type="text" value={draft.executor_id} data-testid="skill-executor"
                       spellCheck={false} placeholder="deployment.health.check"
                       onChange={(e) => setDraft((d) => ({ ...d, executor_id: e.target.value }))} />
              )}
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
          <p className="detail">
            A skill is never safer than what it runs: wrapping a tier-3 publish, or calling an MCP
            tool its server marks tier 2, runs at that tier whatever you choose here.
          </p>
          <label className="setup-field">
            <span>Reviewed</span>
            <select value={draft.trusted ? "yes" : "no"} data-testid="skill-trusted"
                    onChange={(e) => setDraft((d) => ({ ...d, trusted: e.target.value === "yes" }))}>
              <option value="no">Not yet reviewed</option>
              <option value="yes">A person has read this and vouches for it</option>
            </select>
          </label>
          <label className="setup-field">
            <span>On</span>
            <select value={draft.enabled ? "yes" : "no"} data-testid="skill-enabled"
                    onChange={(e) => setDraft((d) => ({ ...d, enabled: e.target.value === "yes" }))}>
              <option value="yes">On — agents and steps can run it</option>
              <option value="no">Off — kept, but nothing runs it</option>
            </select>
          </label>

          <button type="button" className="cap-btn primary" data-testid="skill-save"
                  disabled={busy || !draft.skill_key.trim() || !draft.label.trim()}
                  onClick={() => void save()}>
            {busy ? "Saving…" : editing ? "Save changes" : "Create skill"}
          </button>
        </div>
      </details>
      {/* OUTSIDE the form, because the row actions (Edit, Turn off, Delete, Assign) report
          here too, and a result inside a closed disclosure is a result nobody sees. */}
      {note ? (
        <p className={note.ok ? "setup-ok" : "setup-bad"} data-testid="skill-note" role="status">
          {note.text}
        </p>
      ) : null}
    </section>
  );
}

/** Open the dock addressed to the Program Manager, whose remit is process, with an opener that
 *  says what it can do here. Same door the Workflow page uses to design a workflow — a skill is
 *  the unit a custom workflow step runs, so it is the same conversation one level down. */
function askBossAboutSkills(projectId: string): void {
  openDockAbout({
    subject: null,
    role: "Program Manager",
    question:
      "I want to create a skill for this project. A skill is the reusable unit of doing that a"
      + " workflow step runs — trimming a video, posting to a channel, calling one tool on an"
      + " MCP server. Ask me what you need to know, then draft it and I will create it."
      + ` Start by telling me which skills ${projectId} already has, so I do not ask for one`
      + " that exists.",
  });
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
  openDockAbout({
    subject: null,
    role: "Program Manager",
    question:
      `I want to use the skill \`${skillId}\` as a step in a workflow. Define the custom step`
      + " that runs it, then show me which workflows could place it and where.",
    history: "push",
  });
}
