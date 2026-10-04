"use client";

/**
 * The skill drawer (spec §5.5): new, edit, or a built-in's facts. Progressive: Name, What it does
 * and How it runs first; then only the fields that way of running needs. Every hint that used to
 * be a paragraph is a tooltip on its field. The save payload, the Edit loader (draftFromSkill)
 * and the delete consequences (deleteConsequence) are the ones the old form used.
 */
import React, { useMemo, useState } from "react";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { DotsThree, X } from "@phosphor-icons/react";

import { apiWrite } from "../../lib/api-write";
import type { Ability } from "../../lib/kit-types";
import type { CrewMember, McpServer } from "../../lib/kit-api";
import { AgentAvatar } from "../agent-avatar";
import { showToast } from "../client-forms";
import { Section } from "./ability-drawer";
import { shortError, TIP, agentName } from "./kit-copy";
import { BLANK, type BuiltIn, deleteConsequence, type Draft, draftFromSkill, type Skill, skillStatus } from "./library-data";
import { StatusPill } from "./ability-card";
import { RiskDot } from "./tools-section";

const HOW = [
  { id: "sandboxed_command", label: "Command", tip: "Runs a command in a sandbox, e.g. ffmpeg or pandoc" },
  { id: "mcp_tool", label: "MCP tool", tip: "Calls one tool on an MCP server" },
  { id: "builtin", label: "Built-in", tip: "Wraps a skill BOSS ships, with your knowledge" },
  { id: "agent_program", label: "Program", tip: "A program an agent wrote on an earlier run" },
];
const RISKS = [
  { tier: "0", label: "Reads", tip: TIP.reads },
  { tier: "1", label: "Writes", tip: TIP.writes },
  { tier: "2", label: "Changes the world", tip: "Always asks a person" },
  { tier: "3", label: "Public", tip: TIP.public },
];

async function skillsWrite(body: Record<string, unknown>) {
  const r = await apiWrite("/api/skills", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, data, error: String(data?.error ?? `HTTP ${r.status}`) };
}

function keyOf(label: string): string {
  return label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
}

export function SkillDrawer({ projectId, actor, skill, builtIn, creating, crew, holders, servers, builtIns, ability, onClose, onSaved }: {
  projectId: string; actor: string; skill: Skill | null; builtIn: BuiltIn | null; creating: boolean;
  crew: CrewMember[]; holders: string[]; servers: McpServer[]; builtIns: BuiltIn[]; ability: Ability | undefined;
  onClose: () => void; onSaved: (key: string | null) => void;
}) {
  useIdentityVersion(); // names repaint when identities load
  const [draft, setDraft] = useState<Draft>(() => (skill ? draftFromSkill(skill) : { ...BLANK }));
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [held, setHeld] = useState<string[]>(holders);
  const editable = creating || skill !== null;
  const id = skill?.skill_key ?? builtIn?.id ?? "";
  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));
  const server = servers.find((s) => s.id === draft.mcp_server_id);
  const consequence = useMemo(() => (skill ? deleteConsequence(skill) : null), [skill]);

  const save = async () => {
    setBusy(true);
    const names = (raw: string) => raw.split(/[\n,]/).map((s) => s.trim()).filter(Boolean).map((name) => ({ name }));
    const config: Record<string, unknown> =
      draft.behaviour_kind === "sandboxed_command"
        ? { image: draft.image.trim(), command: draft.command.trim(), timeout_seconds: Number(draft.timeout_seconds) || 300,
            network: draft.network, outputs: names(draft.produces) }
        : draft.behaviour_kind === "mcp_tool"
          ? { mcp_server_id: draft.mcp_server_id.trim(), tool_name: draft.tool_name.trim() }
          : draft.behaviour_kind === "agent_program"
            ? { ...(skill?.config ?? {}), language: draft.language, source: draft.source,
                timeout_seconds: Number(draft.program_timeout) || 60 }
            : { executor_id: draft.executor_id.trim() };
    const key = skill ? skill.skill_key : (draft.skill_key.trim() || keyOf(draft.label));
    const r = await skillsWrite({
      project_id: projectId, actor, skill_key: key, label: draft.label.trim(), summary: draft.summary.trim(),
      behaviour_kind: draft.behaviour_kind, knowledge: draft.knowledge, inputs: names(draft.inputs),
      produces: names(draft.produces), config, risk_tier: Number(draft.risk_tier), trusted: draft.trusted,
      enabled: draft.enabled, mode: skill ? "update" : "create",
    });
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", "Saved");
    onSaved(key);
  };

  const remove = async () => {
    if (!skill) return;
    setConfirm(false); setBusy(true);
    const r = await skillsWrite({ action: "delete", project_id: projectId, actor, skill_key: skill.skill_key,
                                  release_grants: (skill.references?.agents ?? []).length > 0 });
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", "Saved");
    onSaved(null); onClose();
  };

  const toggleOn = async () => {
    if (!skill) return;
    setMenu(false);
    const r = await skillsWrite({ action: skill.enabled ? "disable" : "enable", project_id: projectId, actor, skill_key: skill.skill_key });
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", skill.enabled ? `${skill.label} off` : "Saved");
    onSaved(skill.skill_key);
  };

  const toggleAgent = async (role: string) => {
    const on = held.includes(role);
    const r = await skillsWrite({ action: on ? "unassign" : "assign", project_id: projectId, actor, role_key: role, skill_id: id });
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    const label = skill?.label ?? id;
    showToast("success", on ? `Took ${label} from ${agentName(role)}` : `Gave ${label} to ${agentName(role)}`);
    setHeld((h) => (on ? h.filter((x) => x !== role) : [...h, role]));
  };

  const title = creating ? "New skill" : skill?.label ?? id;
  const tier = skill ? (skill.catalogue?.risk_tier ?? skill.risk_tier) : builtIn?.risk_tier ?? 0;
  const status = skill ? skillStatus(skill.enabled, skill.catalogue?.usable !== false, tier >= 1 && !skill.trusted, held.length)
    : skillStatus(true, true, false, held.length);

  return (
    <aside className="kit-drawer" role="dialog" aria-label={title} data-testid="kit-skill-drawer">
      <header className="kit-drawer-head">
        <h3 className="kit-drawer-title" data-tip={skill?.summary || builtIn?.description || undefined} data-user-text>{title}</h3>
        {!creating ? <StatusPill status={status} tip={skill?.catalogue?.reason ?? undefined} /> : null}
        <span className="kit-chip" data-tip={builtIn ? TIP.builtIn : TIP.custom}>{builtIn ? "Built in" : "Yours"}</span>
        <span className="kit-spacer" />
        {skill ? (
          <span className="kit-confirm-wrap">
            <button type="button" className="kit-icon-btn" aria-label="More" data-testid="kit-skill-more" onClick={() => setMenu((m) => !m)}>
              <DotsThree size={18} />
            </button>
            {menu ? (
              <span className="kit-pop kit-menu" role="menu">
                <button type="button" role="menuitem" onClick={() => void toggleOn()}>{skill.enabled ? "Turn off" : "Turn on"}</button>
                <button type="button" role="menuitem" data-testid="kit-skill-delete" onClick={() => { setMenu(false); setConfirm(true); }}>Delete</button>
              </span>
            ) : null}
            {confirm && consequence ? (
              <span className="kit-pop" role="dialog" aria-label="Confirm delete" data-testid="kit-skill-delete-confirm">
                {consequence.blocked ? (
                  <>
                    <strong data-tip={consequence.blocked}>In use · can&apos;t delete</strong>
                    <span className="kit-pop-actions"><button type="button" className="kit-btn kit-btn-sm" onClick={() => setConfirm(false)}>Close</button></span>
                  </>
                ) : (
                  <>
                    <strong data-tip={consequence.confirm}>Remove {skill.label}?</strong>
                    <span className="kit-pop-actions">
                      <button type="button" className="kit-btn kit-btn-danger kit-btn-sm" data-testid="kit-skill-delete-yes" onClick={() => void remove()}>Delete</button>
                      <button type="button" className="kit-btn kit-btn-sm" onClick={() => setConfirm(false)}>Cancel</button>
                    </span>
                  </>
                )}
              </span>
            ) : null}
          </span>
        ) : null}
        <button type="button" className="kit-icon-btn" aria-label="Close" onClick={onClose}><X size={16} /></button>
      </header>
      <div className="kit-drawer-body">
        {editable ? (
          <Section id="skill" label="Skill" tip="What it is and how it runs">
            <label className="kit-field">
              <span className="kit-field-label">Name</span>
              <input type="text" value={draft.label} data-testid="skill-label" placeholder="Trim a clip"
                     onChange={(e) => set({ label: e.target.value })} />
            </label>
            <label className="kit-field">
              <span className="kit-field-label">What it does</span>
              <input type="text" value={draft.summary} data-testid="skill-summary" placeholder="Cuts a clip from a video"
                     onChange={(e) => set({ summary: e.target.value })} />
            </label>
            <div className="kit-field">
              <span className="kit-field-label">How it runs</span>
              <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label="How it runs" data-testid="skill-behaviour">
                {HOW.map((h) => (
                  <button key={h.id} type="button" role="radio" aria-checked={draft.behaviour_kind === h.id} data-tip={h.tip}
                          data-testid={`skill-how-${h.id}`} onClick={() => set({ behaviour_kind: h.id })}>{h.label}</button>
                ))}
              </span>
            </div>
            {draft.behaviour_kind === "sandboxed_command" ? (
              <>
                <label className="kit-field">
                  <span className="kit-field-label" data-tip="Pinned by digest: name@sha256:…">Image</span>
                  <input type="text" value={draft.image} spellCheck={false} data-testid="skill-image"
                         placeholder="ghcr.io/acme/ffmpeg@sha256:…" onChange={(e) => set({ image: e.target.value })} />
                </label>
                <label className="kit-field">
                  <span className="kit-field-label" data-tip="Each {name} is an input, shell-quoted; {workdir} is always there">Command</span>
                  <textarea rows={3} value={draft.command} spellCheck={false} data-testid="skill-command"
                            onChange={(e) => set({ command: e.target.value })} />
                </label>
              </>
            ) : draft.behaviour_kind === "mcp_tool" ? (
              <>
                <label className="kit-field">
                  <span className="kit-field-label">MCP</span>
                  <select value={draft.mcp_server_id} data-testid="skill-mcp-server"
                          onChange={(e) => set({ mcp_server_id: e.target.value, tool_name: "" })}>
                    <option value="">Choose a server…</option>
                    {servers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </select>
                </label>
                <label className="kit-field">
                  <span className="kit-field-label">Tool</span>
                  {(server?.tools ?? []).length ? (
                    <select value={draft.tool_name} data-testid="skill-mcp-tool" onChange={(e) => set({ tool_name: e.target.value })}>
                      <option value="">Choose a tool…</option>
                      {(server?.tools ?? []).map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
                    </select>
                  ) : (
                    <input type="text" value={draft.tool_name} spellCheck={false} data-testid="skill-mcp-tool"
                           onChange={(e) => set({ tool_name: e.target.value })} />
                  )}
                </label>
              </>
            ) : draft.behaviour_kind === "builtin" ? (
              <label className="kit-field">
                <span className="kit-field-label">Built-in</span>
                <select value={draft.executor_id} data-testid="skill-executor" onChange={(e) => set({ executor_id: e.target.value })}>
                  <option value="">Choose one…</option>
                  {builtIns.map((b) => <option key={b.id} value={b.id}>{b.id}</option>)}
                </select>
              </label>
            ) : (
              <label className="kit-field">
                <span className="kit-field-label" data-tip="Written by an agent; read it before relying on it">Program</span>
                <textarea rows={8} value={draft.source} spellCheck={false} readOnly={!skill} data-testid="skill-program-source"
                          onChange={(e) => set({ source: e.target.value })} />
              </label>
            )}
            <label className="kit-field">
              <span className="kit-field-label" data-tip="Names, comma-separated">Inputs</span>
              <input type="text" value={draft.inputs.replace(/\n/g, ", ")} data-testid="skill-inputs" placeholder="video, start, end"
                     onChange={(e) => set({ inputs: e.target.value })} />
            </label>
            <label className="kit-field">
              <span className="kit-field-label" data-tip="What a later step can read from it">Makes</span>
              <input type="text" value={draft.produces.replace(/\n/g, ", ")} data-testid="skill-produces" placeholder="clip"
                     onChange={(e) => set({ produces: e.target.value })} />
            </label>
            <label className="kit-field">
              <span className="kit-field-label" data-tip="Markdown: the house rules this work is held to">Knowledge</span>
              <textarea rows={8} value={draft.knowledge} data-testid="skill-knowledge" onChange={(e) => set({ knowledge: e.target.value })} />
            </label>
            <div className="kit-field">
              <span className="kit-field-label" data-tip="Never safer than what it runs">Risk</span>
              <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label="Risk" data-testid="skill-risk">
                {RISKS.map((r) => (
                  <button key={r.tier} type="button" role="radio" aria-checked={draft.risk_tier === r.tier} data-tip={r.tip}
                          data-testid={`skill-risk-${r.tier}`} onClick={() => set({ risk_tier: r.tier })}>{r.label}</button>
                ))}
              </span>
            </div>
            <div className="kit-field">
              <span className="kit-field-label" data-tip="A person has read this and vouches for it">Reviewed</span>
              <button type="button" role="switch" aria-checked={draft.trusted} className="kit-switch" data-testid="skill-trusted"
                      aria-label="Reviewed" onClick={() => set({ trusted: !draft.trusted })} />
            </div>
            <div className="kit-actions">
              <button type="button" className="kit-btn kit-btn-primary" data-testid="skill-save"
                      disabled={busy || !draft.label.trim()} onClick={() => void save()}>{busy ? "Saving…" : "Save"}</button>
            </div>
          </Section>
        ) : (
          <Section id="skill" label="Skill" tip={builtIn?.description ?? ""}>
            <div className="kit-row"><RiskDot tier={tier} />{ability ? <span className="kit-chip" data-tone="accent" data-tip={ability.tip}>{ability.label}</span> : null}</div>
          </Section>
        )}
        {!creating ? (
          <>
            <Section id="agents" label="Agents" tip="Who holds this skill">
              <ul className="kit-agent-grid">
                {crew.map((c) => {
                  const on = held.includes(c.role_key);
                  return (
                    <li key={c.role_key} className="kit-agent" data-on={on} data-testid={`kit-skill-agent-${c.role_key}`}>
                      <AgentAvatar agent={c.role_key} size={24} aiBadge={false} />
                      <span className="kit-agent-name" data-tip={c.display_name ?? c.role_key}>{agentName(c.role_key)}</span>
                      <button type="button" role="switch" aria-checked={on} className="kit-switch"
                              aria-label={`${agentName(c.role_key)} holds it`} onClick={() => void toggleAgent(c.role_key)} />
                    </li>
                  );
                })}
              </ul>
            </Section>
            <Section id="used" label="Used in" tip={TIP.usedIn}>
              {skill && ((skill.references?.custom_steps ?? []).length + (skill.references?.workflows ?? []).length) > 0 ? (
                <ul className="kit-used">
                  {(skill.references?.custom_steps ?? []).map((st) => <li key={st.step_key} data-user-text>{st.label || st.step_key}</li>)}
                  {(skill.references?.workflows ?? []).map((w) => (
                    <li key={w.workflow_key}><a href={`?area=delivery_workflows&wf=${encodeURIComponent(w.workflow_key)}`} data-user-text>{w.name || w.workflow_key}</a></li>
                  ))}
                </ul>
              ) : ability && ability.used_in.length ? (
                <ul className="kit-used">
                  {ability.used_in.map((u) => (
                    <li key={`${u.workflow_key}:${u.step_id}`}>
                      <a href={`?area=delivery_workflows&wf=${encodeURIComponent(u.workflow_key)}&step=${encodeURIComponent(u.step_id)}`}>
                        <span data-user-text>{u.workflow_name}</span> › <span data-user-text>{u.step_title}</span>
                      </a>
                    </li>
                  ))}
                </ul>
              ) : <div className="kit-faint">Not in any workflow</div>}
            </Section>
          </>
        ) : null}
      </div>
    </aside>
  );
}
