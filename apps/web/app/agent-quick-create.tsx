"use client";

/**
 * New agent: the one door.
 *
 * There were four: the roster's quick create, Agent Studio's five-step wizard, Team
 * structure's "Add agent from template", and Studio's "Install starter catalog". Each made a
 * different thing, a role with grants, a Studio definition with none, a definition with no
 * role, so what an operator had "created" depended on where they had clicked. Now:
 *
 *   - Blank makes the agent and gives it its skills and MCP servers in one request
 *     (POST /v1/agents), so it can be put on a workflow step the moment this closes;
 *   - Advanced, on the same tab, adds the runtime profile the wizard collected (prompts, rules,
 *     model selection, limits), written after the agent, with what it uses granted;
 *   - From template installs a shipped template, or adopts a Studio definition no agent
 *     answers to yet, and makes the agent for it.
 */
import React, { useEffect, useMemo, useState } from "react";

import {
  createAgent, isAgentKey, loadGrantable, parseToolList, updateAgent,
  type Grantable, type McpGrant,
} from "../lib/agents";
import {
  NEW_ROLE, adoptCreatedAgent, buildAgentWizardSubmission, createInitialAgentWizardDraft,
  parseWizardLines, validateAgentWizardDraft, type AgentWizardDraft,
} from "../lib/agent-wizard";
import type { StudioAgent } from "../lib/agent-studio";
import { apiWrite } from "../lib/api-write";
import { callAction } from "../lib/call-action";
import { GatedButton } from "../components/gated";
import { useCan } from "../components/identity/identity-provider";
import { createStudioAgent, seedStudioCatalog } from "./agent-studio-actions";
import { HelpTip } from "./help-tip";
import type { AgentIdentity, AvatarSpec, Persona } from "../lib/agent-world-types";
import {
  ARCHETYPE_KEYS, CAST_KEYS, DISCLOSURE, askAgent, draftPersonaField, identityOf, newSeed, resolveAvatar,
  shortId, toast,
} from "../lib/agent-world";
import { seededSpec } from "../lib/agent-avatar";
import { resolveRoleKey } from "../lib/agent-identity";
import { FacePicker } from "./agents/face-picker";
import type { BornFrom } from "../lib/agents";
import { AgFace } from "./agents/agent-face";
import { AgentBirth } from "./agents/agent-birth";
import { useIdentityVersion } from "../lib/use-agent-identities";
import { TEAM_LABELS, TEAM_ORDER } from "../lib/agents-live-board";

type Template = {
  id: string; name: string; description: string; role_key: string;
  category: string; installed: boolean; active: boolean;
};

type Advanced = Pick<AgentWizardDraft, "systemPrompt" | "jobPrompt" | "rulesText" | "selectionMode"
  | "pinnedModel" | "dataBoundary" | "maxSteps" | "maxCalls" | "timeoutSeconds" | "maxRetries">;

/** The wizard's runtime defaults, so an untouched Advanced section means "no profile". */
function initialAdvanced(): Advanced {
  const d = createInitialAgentWizardDraft();
  return {
    systemPrompt: "", jobPrompt: "", rulesText: "", selectionMode: d.selectionMode,
    pinnedModel: "", dataBoundary: d.dataBoundary, maxSteps: d.maxSteps, maxCalls: d.maxCalls,
    timeoutSeconds: d.timeoutSeconds, maxRetries: d.maxRetries,
  };
}

/** An Advanced section somebody filled in: a prompt is what makes it a profile. */
export function wantsProfile(a: Advanced): boolean {
  return Boolean(a.systemPrompt.trim() || a.jobPrompt.trim() || parseWizardLines(a.rulesText).length);
}

/** The wizard draft for a NEW agent with a profile: the same submission the wizard sent. */
export function profileDraft(input: {
  name: string; key: string; purpose: string; actor: string; skills: string[];
  servers: Array<{ serverId: string; name: string; tools: string }>; advanced: Advanced;
}): AgentWizardDraft {
  return {
    ...createInitialAgentWizardDraft(NEW_ROLE, input.actor),
    ...input.advanced,
    name: input.name,
    agentKey: input.key,
    newRoleName: input.name,
    description: input.purpose,
    requestedSkills: input.skills,
    mcpServers: input.servers,
  };
}

/**
 * The voice a new agent is born with: the face the operator chose (if any), and a callsign and
 * tagline if they typed them. Never a cast member's callsign, tagline or hand-drawn parts, a new
 * agent born from `qa` is not a second Vera (principle 2.1, one identity everywhere).
 */
export function birthPersona(o: { face?: AvatarSpec; callsign?: string; tagline?: string; about?: string;
                                  archetype?: string | null }): Persona {
  const p: Persona = { version: 1, archetype: o.archetype ?? null };
  if (o.face) p.avatar = { ...o.face, kit: "boss-v1" };
  if (o.callsign?.trim()) p.callsign = o.callsign.trim();
  if (o.tagline?.trim()) p.tagline = o.tagline.trim();
  if (o.about?.trim()) p.about_me = o.about.trim();
  return p;
}

/** Register (from the roster) with nothing written: ask for the shipped definition. */
export function asShipped(o: { registering: boolean; purpose: string; skills: string[]; mcpServers: unknown[];
                               reportsTo: string; callsign: string; tagline: string }): boolean {
  return o.registering && !o.purpose.trim() && o.skills.length === 0 && o.mcpServers.length === 0
    && !o.reportsTo && !o.callsign.trim() && !o.tagline.trim();
}

/** Where a cast face came from: one of the four "thinks like" archetypes, or a role template. */
export function bornFromFor(key: string): BornFrom {
  return (ARCHETYPE_KEYS as readonly string[]).includes(key)
    ? { kind: "archetype", archetype: key } : { kind: "template", template_key: key };
}

type Birth = { identity: AgentIdentity; agentId: string; roleKey: string };

export function AgentQuickCreate({ projectId, actor, onClose, onCreated, orphans = [], initialName = "",
                                   initialTeam = "yours" }: {
  projectId: string;
  /** Registering an agent that already does work: its name, so the key it gets is its own. */
  initialName?: string;
  /** Organization's `Make agent` for a missing role: the team whose empty seat it fills. */
  initialTeam?: string;
  actor: string;
  onClose: () => void;
  onCreated: (roleKey: string) => void;
  /** Studio definitions no agent answers to yet, offered under From template. */
  orphans?: StudioAgent[];
}) {
  useIdentityVersion(); // names and faces below are resolved at render: repaint when identities load
  const [tab, setTab] = useState<"blank" | "template" | "archetype">("blank");
  // FACE & VOICE: the face a new agent is born with (Reroll keeps picked parts, re-seeds the rest).
  const [face, setFace] = useState<AvatarSpec>(() => seededSpec(newSeed()));
  const [callsign, setCallsign] = useState("");
  const [tagline, setTagline] = useState("");
  const [birth, setBirth] = useState<Birth | null>(null);
  const [grantable, setGrantable] = useState<Grantable | null>(null);
  const [loadFailed, setLoadFailed] = useState("");
  const [name, setName] = useState(initialName);
  const [purpose, setPurpose] = useState("");
  const [skills, setSkills] = useState<string[]>([]);
  const [servers, setServers] = useState<Record<string, string>>({});
  const [reportsTo, setReportsTo] = useState("");
  // WHICH TEAM it sits in on Live, Organization and Directory (agents-home spec §4.6).
  const [team, setTeam] = useState(initialTeam);
  const [skillFilter, setSkillFilter] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advanced, setAdvanced] = useState<Advanced>(initialAdvanced);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");
  /** The agent a profile attempt already made. POST /v1/agents refuses an existing key, so a
   *  retry after the profile failed must target this agent instead of making it again. */
  const [made, setMade] = useState("");

  /** Play the birth, then hand over: the Passport opens and the agent introduces itself. */
  const born = (roleKey: string, agentId: string, displayName: string, persona?: Persona) => {
    const base = identityOf(roleKey, displayName);
    setBirth({
      roleKey, agentId,
      identity: { ...base, name: displayName, agentId: agentId || null, callsign: persona?.callsign,
                  tagline: persona?.tagline, avatar: resolveAvatar(roleKey, agentId, persona?.avatar) },
    });
  };
  const afterBirth = () => {
    if (!birth) return;
    const shown = birth.identity.callsign || birth.identity.name;
    toast("success", birth.agentId ? `${shown} is born, ${shortId(birth.agentId)}` : `${shown} is born`);
    onCreated(birth.roleKey);
    askAgent(birth.roleKey, birth.identity.name, { from: "passport" });
  };

  useEffect(() => {
    let alive = true;
    void loadGrantable(projectId).then((r) => {
      if (!alive) return;
      if (r.ok) setGrantable(r.data); else setLoadFailed(r.error);
    });
    return () => { alive = false; };
  }, [projectId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const shownSkills = useMemo(() => {
    const q = skillFilter.trim().toLowerCase();
    const all = grantable?.skills ?? [];
    return q ? all.filter((s) => `${s.skill_id} ${s.label} ${s.summary}`.toLowerCase().includes(q))
      : all;
  }, [grantable, skillFilter]);

  // THE KEY IT WILL GET, said before it is taken (agents.key_from_name on the server). A name
  // with no English letters makes no key, so the key field appears exactly then.
  const slug = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  const derived = (slug && !/^[a-z]/.test(slug) ? `agent_${slug}` : slug).slice(0, 64);
  const nameMakesNoKey = !!name.trim() && !isAgentKey(derived);
  const [ownKey, setOwnKey] = useState("");
  const key = nameMakesNoKey ? ownKey.trim().toLowerCase() : derived;
  const keyInvalid = nameMakesNoKey && !!key && !isAgentKey(key);
  const existing = new Set((grantable?.agents ?? []).map((a) => a.role_key));
  const taken = !!key && existing.has(key) && key !== made;
  const profile = wantsProfile(advanced);

  // The profile step after create is an UPDATE (agent:update): an Operator may create but not
  // update, so the step is skipped rather than sent to be refused.
  const mayUpdate = useCan("agent:update").allowed;
  const submit = async () => {
    if (!name.trim()) { setFailed("Name it first"); return; }
    if (taken) { setFailed(`“${key}” exists, pick another name`); return; }
    if (keyInvalid) { setFailed("Key: letters, digits, _, start with a letter"); return; }
    if (callsignTaken(callsign)) { setFailed(`Callsign “${callsign.trim()}” is taken`); return; }
    const mcpServers: McpGrant[] = Object.entries(servers)
      .map(([server_id, tools]) => ({ server_id, tools: parseToolList(tools) }));
    setBusy(true);
    setFailed("");
    if (profile) {
      // WITH A PROFILE: the wizard's submission, the agent and its grants first, then the
      // Studio definition, validated and published.
      if (nameMakesNoKey) { setBusy(false); setFailed("A profile needs a name in English letters"); return; }
      const fresh = profileDraft({
        name: name.trim(), key, purpose: purpose.trim(), actor, skills,
        servers: Object.entries(servers).map(([serverId, tools]) => ({
          serverId, tools,
          name: grantable?.mcp_servers.find((m) => m.mcp_server_id === serverId)?.server ?? serverId,
        })),
        advanced,
      });
      const draft = made === key ? adoptCreatedAgent(fresh, made) : fresh;
      const issue = validateAgentWizardDraft(draft)[0];
      if (issue) { setBusy(false); setFailed(issue.message); return; }
      const result = await callAction(createStudioAgent, undefined, "agent:create")(buildAgentWizardSubmission(projectId, draft, actor), true);
      if (!result.ok) {
        // THE AGENT EXISTS, ITS PROFILE DOES NOT: the next press finishes the profile.
        const createdKey = result.item?.agent_created;
        if (typeof createdKey === "string") setMade(createdKey);
        setBusy(false); setFailed(result.message); return;
      }
      const persona = birthPersona({ face, callsign, tagline });
      if (mayUpdate) await updateAgent(projectId, actor, key, {
        ...(reportsTo ? { reports_to: reportsTo } : {}), ...(team !== "yours" ? { team } : {}), persona });
      setBusy(false);
      born(key, "", name.trim(), persona);
      return;
    }
    const persona = birthPersona({ face, callsign, tagline });
    // REGISTER ON A SHIPPED AGENT REGISTERS THE SHIPPED AGENT: from the roster, with nothing
    // written here, the API installs BOSS's own definition for the key (Splice for
    // video_editor), and a blank agent only when BOSS ships none.
    const shipped = team === "yours"
      && asShipped({ registering: !!initialName, purpose, skills, mcpServers, reportsTo, callsign, tagline });
    const r = await createAgent(projectId, actor, {
      name: name.trim(), purpose: purpose.trim(), skills, mcpServers, reportsTo: reportsTo || null,
      ...(nameMakesNoKey && key ? { roleKey: key } : {}),
      ...(shipped ? { bornFrom: { kind: "shipped" } } : { bornFrom: { kind: "blank" }, persona, team }),
    });
    setBusy(false);
    if (!r.ok) { setFailed(r.error); return; }
    born(String(r.data.role_key ?? key), String(r.data.agent_id ?? ""), name.trim(), persona);
  };

  const setA = <K extends keyof Advanced>(k: K, v: Advanced[K]) => setAdvanced((a) => ({ ...a, [k]: v }));

  if (birth) {
    return (
      <div className="aw-backdrop">
        <AgentBirth identity={birth.identity} agentId={birth.agentId} onDone={afterBirth} />
      </div>
    );
  }

  return (
    <div className="aw-backdrop">
      <section className="aw-dialog aq-dialog" role="dialog" aria-modal="true"
               aria-labelledby="aq-title" data-testid="agent-quick-create">
        <header className="aw-header">
          <h2 id="aq-title" title={initialName ? "Leave the rest empty to register BOSS's own definition, if it ships one" : undefined}>
            {initialName ? "Register agent" : "New agent"}</h2>
          <div className="wf-seg" role="tablist" aria-label="Start from">
            <button type="button" role="tab" aria-selected={tab === "blank"} data-active={tab === "blank"}
                    data-testid="aq-tab-blank" onClick={() => setTab("blank")}>Blank</button>
            <button type="button" role="tab" aria-selected={tab === "template"} data-active={tab === "template"}
                    data-testid="aq-tab-template" onClick={() => setTab("template")}>From template</button>
            <button type="button" role="tab" aria-selected={tab === "archetype"} data-active={tab === "archetype"}
                    data-testid="aq-tab-archetype" onClick={() => setTab("archetype")}>From archetype</button>
          </div>
          <button className="aw-close" onClick={onClose} disabled={busy} aria-label="Close">×</button>
        </header>

        {tab === "template" ? (
          <TemplatePicker projectId={projectId} actor={actor} orphans={orphans}
                          existing={existing} onCreated={onCreated} onBorn={born} />
        ) : tab === "archetype" ? (
          <ArchetypePicker projectId={projectId} actor={actor} existing={existing} onBorn={born} />
        ) : (
          <form className="aw-form aq-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
            <label className="aw-field">
              <span>Name</span>
              <input value={name} autoFocus data-testid="aq-name" placeholder="Release Auditor"
                     onChange={(e) => setName(e.target.value)} />
              {key && !nameMakesNoKey ? (
                <small className={taken ? "aq-bad" : "aw-field-note"} title="Used in workflow JSON">
                  {taken ? <>Taken: <code>{key}</code></> : <>Key <code>{key}</code></>}
                </small>
              ) : null}
            </label>
            {nameMakesNoKey ? (
              <label className="aw-field">
                <span>Key <HelpTip label="Key" text="English letters, digits and _. Empty: BOSS makes one." /></span>
                <input value={ownKey} data-testid="aq-key" placeholder="video_editor"
                       onChange={(e) => setOwnKey(e.target.value)} />
                {taken || keyInvalid ? <small className="aq-bad">{taken ? "Taken" : "Not a valid key"}</small> : null}
              </label>
            ) : null}
            <label className="aw-field">
              <span>Purpose <HelpTip label="Purpose" text="BOSS reads this when it picks an agent for a step." /></span>
              <textarea rows={2} value={purpose} data-testid="aq-purpose"
                        placeholder="What is it for?"
                        onChange={(e) => setPurpose(e.target.value)} />
            </label>
            <label className="aw-field">
              <span>Reports to</span>
              <select value={reportsTo} data-testid="aq-reports-to"
                      onChange={(e) => setReportsTo(e.target.value)}>
                <option value="">Nobody</option>
                {(grantable?.agents ?? []).map((a) => (
                  <option key={a.role_key} value={a.role_key}>{a.name}</option>
                ))}
              </select>
            </label>

            <label className="aw-field">
              <span>Team</span>
              <select value={team} data-testid="aq-team" onChange={(e) => setTeam(e.target.value)}>
                {TEAM_ORDER.map((k) => (
                  <option key={k} value={k} title={TEAM_LABELS[k]?.tip}>{TEAM_LABELS[k]?.label ?? k}</option>
                ))}
              </select>
            </label>

            <FaceAndVoice name={name} face={face} onFace={setFace}
                          callsign={callsign} onCallsign={setCallsign} tagline={tagline} onTagline={setTagline} />

            {loadFailed ? (
              <p className="aq-bad" data-testid="aq-load-failed" title={loadFailed}>
                Could not load skills, add them later
              </p>
            ) : null}

            <fieldset className="aq-group">
              <legend>Skills <span className="detail">· {skills.length}</span>{" "}
                <HelpTip label="Skills" text="Granted now. A step can then use them." /></legend>
              {(grantable?.skills ?? []).length > 8 ? (
                <input type="search" className="aq-filter" placeholder="Find a skill"
                       value={skillFilter} onChange={(e) => setSkillFilter(e.target.value)} />
              ) : null}
              <div className="aw-skill-list aq-list" data-testid="aq-skills">
                {shownSkills.map((s) => {
                  const on = skills.includes(s.skill_id);
                  return (
                    <label key={s.skill_id} className={on ? "selected" : ""}
                           title={[s.summary || s.label,
                                   s.risk_tier >= 2 ? `Risk ${s.risk_tier}, needs approval each time` : "",
                                   !s.enabled ? "Not enabled in this project" : ""].filter(Boolean).join(", ")}>
                      <input type="checkbox" checked={on}
                             data-testid={`aq-skill-${s.skill_id}`}
                             onChange={() => setSkills(on ? skills.filter((x) => x !== s.skill_id)
                               : [...skills, s.skill_id])} />
                      <div><b>{s.skill_id}</b></div>
                    </label>
                  );
                })}
                {grantable && !grantable.skills.length ? (
                  <p className="aw-empty-line">No skills yet</p>
                ) : null}
              </div>
            </fieldset>

            <fieldset className="aq-group">
              <legend>MCP <span className="detail">· {Object.keys(servers).length}</span></legend>
              <div className="aw-skill-list aq-list" data-testid="aq-mcp">
                {(grantable?.mcp_servers ?? []).map((m) => {
                  const on = m.mcp_server_id in servers;
                  return (
                    <label key={m.mcp_server_id} className={on ? "selected" : ""}
                           title={[m.description || m.url, !m.enabled ? "Disabled in this project" : ""]
                             .filter(Boolean).join(", ")}>
                      <input type="checkbox" checked={on}
                             data-testid={`aq-mcp-${m.mcp_server_id}`}
                             onChange={() => {
                               const next = { ...servers };
                               if (on) delete next[m.mcp_server_id]; else next[m.mcp_server_id] = "";
                               setServers(next);
                             }} />
                      <div>
                        <b>{m.server}</b>
                        {on ? (
                          <input className="aq-tools" placeholder="Only these tools (empty: all)"
                                 value={servers[m.mcp_server_id]}
                                 data-testid={`aq-mcp-tools-${m.mcp_server_id}`}
                                 onClick={(e) => e.preventDefault()}
                                 onChange={(e) => setServers({ ...servers, [m.mcp_server_id]: e.target.value })} />
                        ) : null}
                      </div>
                    </label>
                  );
                })}
                {grantable && !grantable.mcp_servers.length ? (
                  <p className="aw-empty-line">No MCP servers yet</p>
                ) : null}
              </div>
            </fieldset>

            <details className="aq-advanced" open={advancedOpen} data-testid="aq-advanced"
                     onToggle={(e) => setAdvancedOpen(e.currentTarget.open)}>
              <summary>Advanced <HelpTip label="Advanced" text="Prompts, rules and model settings. Optional." /></summary>
              <label className="aw-field"><span>System prompt</span>
                <textarea rows={3} value={advanced.systemPrompt} data-testid="aq-system-prompt"
                          onChange={(e) => setA("systemPrompt", e.target.value)} />
              </label>
              <label className="aw-field"><span>Job prompt</span>
                <textarea rows={2} value={advanced.jobPrompt} data-testid="aq-job-prompt"
                          onChange={(e) => setA("jobPrompt", e.target.value)} />
              </label>
              <label className="aw-field">
                <span>Rules <HelpTip label="Rules" text="One per line. Become its must-not boundaries." /></span>
                <textarea rows={2} value={advanced.rulesText} data-testid="aq-rules"
                          onChange={(e) => setA("rulesText", e.target.value)} />
              </label>
              <div className="aq-row">
                <label className="aw-field"><span>Model <HelpTip label="Model" text="Routed: the model router picks per task. Pinned: always this model. Local: local models only." /></span>
                  <select value={advanced.selectionMode} data-testid="aq-selection"
                          onChange={(e) => setA("selectionMode", e.target.value as Advanced["selectionMode"])}>
                    <option value="router">Routed</option>
                    <option value="pinned">Pinned</option>
                    <option value="local">Local</option>
                  </select>
                </label>
                {advanced.selectionMode === "pinned" ? (
                  <label className="aw-field"><span>Model id <HelpTip label="Model id" text="A model, or source:model, e.g. claude_code:opus, ollama:llama3.1." /></span>
                    <input value={advanced.pinnedModel} data-testid="aq-pinned-model"
                           onChange={(e) => setA("pinnedModel", e.target.value)} />
                  </label>
                ) : null}
                <label className="aw-field"><span>Data</span>
                  <select value={advanced.dataBoundary}
                          onChange={(e) => setA("dataBoundary", e.target.value as Advanced["dataBoundary"])}>
                    <option value="public">Public</option>
                    <option value="internal">Internal</option>
                    <option value="confidential">Confidential</option>
                    <option value="restricted">Restricted</option>
                  </select>
                </label>
              </div>
              <div className="aq-row">
                {([["maxSteps", "Steps"], ["maxCalls", "Calls"], ["timeoutSeconds", "Timeout (s)"],
                   ["maxRetries", "Retries"]] as const).map(([k, label]) => (
                  <label className="aw-field" key={k}><span>{label}</span>
                    <input type="number" value={advanced[k]}
                           onChange={(e) => setA(k, Number(e.target.value))} />
                  </label>
                ))}
              </div>
            </details>

            {failed ? <p className="aq-bad" role="alert" data-testid="aq-failed">{failed}</p> : null}
            <footer className="aw-footer">
              <span />
              <div>
                <button type="button" className="wf-btn" onClick={onClose} disabled={busy}>Cancel</button>
                <GatedButton permission="agent:create" type="submit" className="wf-btn wf-btn-primary"
                        disabled={busy || !name.trim() || taken || keyInvalid || callsignTaken(callsign)} data-testid="aq-create">
                  {busy ? "Creating…" : initialName ? "Register" : "Create"}
                </GatedButton>
              </div>
            </footer>
          </form>
        )}
      </section>
    </div>
  );
}

/** Shipped templates, and Studio definitions no agent answers to yet. */
function TemplatePicker({ projectId, actor, orphans, existing, onCreated, onBorn }: {
  projectId: string; actor: string; orphans: StudioAgent[]; existing: Set<string>;
  onCreated: (roleKey: string) => void;
  /** A new agent was made: play its birth. An agent that already existed just opens. */
  onBorn: (roleKey: string, agentId: string, name: string, persona?: Persona) => void;
}) {
  const [templates, setTemplates] = useState<Template[] | null>(null);
  const [failed, setFailed] = useState("");
  const [search, setSearch] = useState("");
  const [working, setWorking] = useState("");

  useEffect(() => {
    let alive = true;
    void fetch(`/v1/agent-studio/templates?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail ?? `Templates returned ${r.status}`);
        if (alive) setTemplates(body.items ?? []);
      })
      .catch((e) => { if (alive) { setTemplates([]); setFailed(String((e as Error).message ?? e)); } });
    return () => { alive = false; };
  }, [projectId]);

  /** The agent a template or definition stands for: made when no agent has its key yet, and
   *  born, so a template agent has a passport like any other. */
  const ensureAgent = async (roleKey: string, name: string, purpose: string, templateKey: string): Promise<boolean> => {
    if (existing.has(roleKey)) { onCreated(roleKey); return true; }
    // A template keeps its role's face as shipped (docs/cast): no new seed, no persona face. A
    // role the cast does not know is seeded from its agent_id by the resolver, so it never drifts.
    const r = await createAgent(projectId, actor, { name, purpose, skills: [], mcpServers: [], roleKey,
                                                     bornFrom: { kind: "template", template_key: templateKey } });
    if (!r.ok) { setFailed(r.error); return false; }
    onBorn(roleKey, String(r.data.agent_id ?? ""), name);
    return true;
  };

  const install = async (t: Template) => {
    setWorking(t.id); setFailed("");
    try {
      if (!t.installed) {
        const r = await apiWrite(`/v1/agent-studio/templates/${encodeURIComponent(t.id)}/install`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId, actor }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail ?? `Install returned ${r.status}`);
      }
      await ensureAgent(t.role_key, t.name, t.description, t.id);
    } catch (e) {
      setFailed(String((e as Error).message ?? e));
    } finally { setWorking(""); }
  };

  const [seeded, setSeeded] = useState("");
  const seed = async () => {
    setWorking("seed"); setFailed(""); setSeeded("");
    const r = await callAction(seedStudioCatalog, undefined, "agent:create")(projectId, actor);
    setWorking("");
    if (!r.ok) { setFailed(r.message); return; }
    setSeeded(r.message);
  };

  const adopt = async (a: StudioAgent) => {
    const roleKey = a.active_version?.role_key || a.agent_key;
    setWorking(a.id); setFailed("");
    await ensureAgent(roleKey, a.name, a.description ?? "", a.agent_key);
    setWorking("");
  };

  const q = search.trim().toLowerCase();
  const shownTemplates = (templates ?? []).filter((t) =>
    `${t.name} ${t.description} ${t.category} ${t.role_key}`.toLowerCase().includes(q));
  const shownOrphans = orphans.filter((a) => `${a.name} ${a.agent_key} ${a.description ?? ""}`.toLowerCase().includes(q));

  return (
    <div className="aw-form aq-templates" data-testid="aq-templates">
      <div className="aq-row">
        <input type="search" className="aq-filter" placeholder="Find a template" aria-label="Find a template"
               value={search} onChange={(e) => setSearch(e.target.value)} />
        <GatedButton permission="agent:create" type="button" className="wf-btn" disabled={!!working} data-testid="aq-seed"
                title="Adds the reviewed starter agents and panels. Starts nothing, spends nothing."
                onClick={() => void seed()}>
          {working === "seed" ? "Installing…" : "Install starter set"}
        </GatedButton>
      </div>
      {seeded ? <p className="detail" role="status" data-testid="aq-seeded">{seeded}</p> : null}
      {failed ? <p className="aq-bad" role="alert">{failed}</p> : null}
      {templates === null ? <p className="detail">Loading…</p> : null}
      <ul className="aq-template-list">
        {shownOrphans.map((a) => (
          <li key={a.id} data-testid={`aq-orphan-${a.agent_key}`}>
            <span title={a.description || undefined}><b>{a.name}</b> <span className="wf-tag">Profile</span></span>
            <GatedButton permission="agent:create" type="button" className="wf-btn" disabled={!!working}
                    title="A profile with no agent yet" onClick={() => void adopt(a)}>
              {working === a.id ? "Adding…" : "Add"}
            </GatedButton>
          </li>
        ))}
        {shownTemplates.map((t) => {
          const have = existing.has(t.role_key);
          return (
            <li key={t.id} data-testid={`aq-template-${t.id}`}>
              <span title={t.description || undefined}><b>{t.name}</b> <span className="detail">{t.category}</span></span>
              <GatedButton permission="agent:publish" type="button" className="wf-btn" disabled={!!working || (have && t.installed)}
                      onClick={() => void install(t)}>
                {have && t.installed ? "Added" : working === t.id ? "Adding…" : "Add"}
              </GatedButton>
            </li>
          );
        })}
      </ul>
      {templates && !shownTemplates.length && !shownOrphans.length ? <p className="detail">No match</p> : null}
    </div>
  );
}


/** The Face & voice row: the kit's face picker, a callsign and tagline, and the line that never changes. */
function FaceAndVoice({ name, face, onFace, callsign, onCallsign, tagline, onTagline }: {
  name: string; face: AvatarSpec; onFace: (next: AvatarSpec) => void;
  callsign: string; onCallsign: (v: string) => void; tagline: string; onTagline: (v: string) => void;
}) {
  useIdentityVersion(); // names and faces below are resolved at render: repaint when identities load
  const taken = callsignTaken(callsign);
  return (
    <fieldset className="aq-group ag-fv" data-testid="aq-face-voice">
      <legend>Face &amp; voice <HelpTip label="Face & voice" text="How it looks and speaks. Never what it may do." /></legend>
      <FacePicker value={face} onChange={onFace} name={callsign.trim() || name.trim() || "New agent"} />
      <div className="ag-fv-row">
        <label className="aw-field">
          <span>Callsign</span>
          <input value={callsign} maxLength={16} placeholder="Optional" data-testid="aq-callsign"
                 onChange={(e) => onCallsign(e.target.value)} />
          {taken ? <small className="aq-bad">Taken</small> : null}
        </label>
        <label className="aw-field ag-grow">
          <span>Tagline</span>
          <input value={tagline} maxLength={60} placeholder="Optional" data-testid="aq-tagline"
                 onChange={(e) => onTagline(e.target.value)} />
        </label>
      </div>
      <span className="ag-disclosure">{DISCLOSURE}</span>
    </fieldset>
  );
}

/** A callsign another agent (or a cast member) already answers to: names resolve by callsign,
 *  so a second "Vera" would make `@Vera` ambiguous. */
export function callsignTaken(callsign: string): boolean {
  const c = callsign.trim();
  return !!c && resolveRoleKey(c) !== null;
}

/** From archetype: the hand-drawn cast, the operator's crew first and the "thinks like"
 *  archetypes last. A "thinks like" archetype is a way of thinking, never a person; a role starts
 *  from that role's voice. The new agent gets its OWN face and name, never the cast member's. */
function ArchetypePicker({ projectId, actor, existing, onBorn }: {
  projectId: string; actor: string; existing: Set<string>;
  onBorn: (roleKey: string, agentId: string, name: string, persona?: Persona) => void;
}) {
  useIdentityVersion(); // names and faces below are resolved at render: repaint when identities load
  const [pick, setPick] = useState("");
  const [name, setName] = useState("");
  const [about, setAbout] = useState("");
  const [face, setFace] = useState<AvatarSpec>(() => seededSpec(newSeed()));
  const [callsign, setCallsign] = useState("");
  const [busy, setBusy] = useState("");
  const [failed, setFailed] = useState("");
  const base = pick ? identityOf(pick) : null;
  const slug = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  const key = (slug && !/^[a-z]/.test(slug) ? `agent_${slug}` : slug).slice(0, 64);
  const taken = !!key && existing.has(key);
  const callsignClash = callsignTaken(callsign);

  const choose = (k: string) => {
    setPick(k);
    const who = identityOf(k);
    setName(`My ${who.name.replace(/^The /, "")}`);
    setAbout("");
    setFailed("");
  };
  const persona = () => birthPersona({
    face, callsign, about,
    archetype: (ARCHETYPE_KEYS as readonly string[]).includes(pick) ? pick : null,
  });

  const draft = async () => {
    setBusy("draft"); setFailed("");
    const r = await draftPersonaField(projectId, actor, pick, "about_me", persona());
    setBusy("");
    if (!r.ok) { setFailed(r.status === 409 ? "Needs a model. Connect one in Repo and models." : r.error); return; }
    setAbout(r.data.slice(0, 280));
  };
  const create = async () => {
    if (!pick || !name.trim() || taken || callsignClash || !isAgentKey(key)) return;
    setBusy("create"); setFailed("");
    const p = persona();
    const r = await createAgent(projectId, actor, {
      name: name.trim(), purpose: base?.tagline ?? "", skills: [], mcpServers: [], bornFrom: bornFromFor(pick),
      persona: p,
    });
    setBusy("");
    if (!r.ok) { setFailed(r.error); return; }
    onBorn(String(r.data.role_key ?? key), String(r.data.agent_id ?? ""), name.trim(), p);
  };

  return (
    <div className="aw-form ag-archetypes" data-testid="aq-archetypes">
      <div className="ag-arch-grid" role="radiogroup" aria-label="Archetype">
        {CAST_KEYS.map((k) => {
          const who = identityOf(k);
          const thinks = (ARCHETYPE_KEYS as readonly string[]).includes(k);
          return (
            <button key={k} type="button" role="radio" aria-checked={pick === k} className="ag-arch"
                    data-testid={`aq-arch-${k}`}
                    title={[thinks ? "Inspired by a way of thinking" : "Starts from this role's voice", who.tagline]
                      .filter(Boolean).join(", ")}
                    onClick={() => choose(k)}>
              <AgFace roleKey={k} identity={who} size={48} decorative />
              <span className="ag-arch-name">{who.callsign || who.name}</span>
              {who.callsign ? <span className="ag-muted">{who.name}</span> : null}
            </button>
          );
        })}
      </div>
      {pick ? (
        <div className="ag-arch-form">
          <FacePicker value={face} onChange={setFace} name={callsign.trim() || name.trim() || "New agent"} />
          <div className="ag-fv-row">
            <label className="aw-field ag-grow">
              <span>Name</span>
              <input value={name} data-testid="aq-arch-name" onChange={(e) => setName(e.target.value)} />
              {taken ? <small className="aq-bad">Taken</small> : null}
            </label>
            <label className="aw-field">
              <span>Callsign</span>
              <input value={callsign} maxLength={16} placeholder="Optional" data-testid="aq-arch-callsign"
                     onChange={(e) => setCallsign(e.target.value)} />
              {callsignClash ? <small className="aq-bad">Taken</small> : null}
            </label>
          </div>
          <label className="aw-field">
            <span>About me
              <GatedButton permission="agent:create" type="button" className="ag-link" disabled={!!busy} data-testid="aq-draft-about"
                      title="One model call in its voice. Not saved until Create." onClick={() => void draft()}>
                {busy === "draft" ? "Drafting…" : "Draft about me"}
              </GatedButton>
            </span>
            <textarea rows={2} value={about} maxLength={280} placeholder="In first person"
                      onChange={(e) => setAbout(e.target.value)} />
          </label>
          <span className="ag-disclosure">{DISCLOSURE}</span>
          {failed ? <p className="aq-bad" role="alert">{failed}</p> : null}
          <footer className="aw-footer">
            <span />
            <GatedButton permission="agent:create" type="button" className="wf-btn wf-btn-primary" data-testid="aq-arch-create"
                    disabled={!!busy || !name.trim() || taken || callsignClash || !isAgentKey(key)} onClick={() => void create()}>
              {busy === "create" ? "Creating…" : "Create agent"}
            </GatedButton>
          </footer>
        </div>
      ) : null}
    </div>
  );
}
