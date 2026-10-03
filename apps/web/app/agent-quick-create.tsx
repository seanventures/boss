"use client";

/**
 * New agent: the one door.
 *
 * There were four: the roster's quick create, Agent Studio's five-step wizard, Team
 * structure's "Add agent from template", and Studio's "Install starter catalog". Each made a
 * different thing — a role with grants, a Studio definition with none, a definition with no
 * role — so what an operator had "created" depended on where they had clicked. Now:
 *
 *   - Blank makes the agent and gives it its skills and MCP servers in one request
 *     (POST /v1/agents), so it can be put on a workflow step the moment this closes;
 *   - Advanced, on the same tab, adds the runtime profile the wizard collected (prompts, rules,
 *     model selection, limits) — written after the agent, with what it uses granted;
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
import { createStudioAgent, seedStudioCatalog } from "./agent-studio-actions";
import { HelpTip } from "./help-tip";

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

export function AgentQuickCreate({ projectId, actor, onClose, onCreated, orphans = [], initialName = "" }: {
  projectId: string;
  /** Registering an agent that already does work: its name, so the key it gets is its own. */
  initialName?: string;
  actor: string;
  onClose: () => void;
  onCreated: (roleKey: string) => void;
  /** Studio definitions no agent answers to yet, offered under From template. */
  orphans?: StudioAgent[];
}) {
  const [tab, setTab] = useState<"blank" | "template">("blank");
  const [grantable, setGrantable] = useState<Grantable | null>(null);
  const [loadFailed, setLoadFailed] = useState("");
  const [name, setName] = useState(initialName);
  const [purpose, setPurpose] = useState("");
  const [skills, setSkills] = useState<string[]>([]);
  const [servers, setServers] = useState<Record<string, string>>({});
  const [reportsTo, setReportsTo] = useState("");
  const [skillFilter, setSkillFilter] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advanced, setAdvanced] = useState<Advanced>(initialAdvanced);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");
  /** The agent a profile attempt already made. POST /v1/agents refuses an existing key, so a
   *  retry after the profile failed must target this agent instead of making it again. */
  const [made, setMade] = useState("");

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

  const submit = async () => {
    if (!name.trim()) { setFailed("Name it first"); return; }
    if (taken) { setFailed(`“${key}” exists — pick another name`); return; }
    if (keyInvalid) { setFailed("Key: letters, digits, _ — start with a letter"); return; }
    const mcpServers: McpGrant[] = Object.entries(servers)
      .map(([server_id, tools]) => ({ server_id, tools: parseToolList(tools) }));
    setBusy(true);
    setFailed("");
    if (profile) {
      // WITH A PROFILE: the wizard's submission — the agent and its grants first, then the
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
      const result = await callAction(createStudioAgent)(buildAgentWizardSubmission(projectId, draft, actor), true);
      if (!result.ok) {
        // THE AGENT EXISTS, ITS PROFILE DOES NOT: the next press finishes the profile.
        const createdKey = result.item?.agent_created;
        if (typeof createdKey === "string") setMade(createdKey);
        setBusy(false); setFailed(result.message); return;
      }
      if (reportsTo) await updateAgent(projectId, actor, key, { reports_to: reportsTo });
      setBusy(false);
      onCreated(key);
      return;
    }
    const r = await createAgent(projectId, actor, {
      name: name.trim(), purpose: purpose.trim(), skills, mcpServers, reportsTo: reportsTo || null,
      ...(nameMakesNoKey && key ? { roleKey: key } : {}),
    });
    setBusy(false);
    if (!r.ok) { setFailed(r.error); return; }
    onCreated(String((r.data as { role_key?: string }).role_key ?? key));
  };

  const setA = <K extends keyof Advanced>(k: K, v: Advanced[K]) => setAdvanced((a) => ({ ...a, [k]: v }));

  return (
    <div className="aw-backdrop">
      <section className="aw-dialog aq-dialog" role="dialog" aria-modal="true"
               aria-labelledby="aq-title" data-testid="agent-quick-create">
        <header className="aw-header">
          <h2 id="aq-title">New agent</h2>
          <div className="wf-seg" role="tablist" aria-label="Start from">
            <button type="button" role="tab" aria-selected={tab === "blank"} data-active={tab === "blank"}
                    data-testid="aq-tab-blank" onClick={() => setTab("blank")}>Blank</button>
            <button type="button" role="tab" aria-selected={tab === "template"} data-active={tab === "template"}
                    data-testid="aq-tab-template" onClick={() => setTab("template")}>From template</button>
          </div>
          <button className="aw-close" onClick={onClose} disabled={busy} aria-label="Close">×</button>
        </header>

        {tab === "template" ? (
          <TemplatePicker projectId={projectId} actor={actor} orphans={orphans}
                          existing={existing} onCreated={onCreated} />
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

            {loadFailed ? (
              <p className="aq-bad" data-testid="aq-load-failed" title={loadFailed}>
                Could not load skills — add them later
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
                                   s.risk_tier >= 2 ? `Risk ${s.risk_tier} — needs approval each time` : "",
                                   !s.enabled ? "Not enabled in this project" : ""].filter(Boolean).join(" · ")}>
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
                             .filter(Boolean).join(" · ")}>
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
                <label className="aw-field"><span>Model</span>
                  <select value={advanced.selectionMode} data-testid="aq-selection"
                          onChange={(e) => setA("selectionMode", e.target.value as Advanced["selectionMode"])}>
                    <option value="router">Routed</option>
                    <option value="pinned">Pinned</option>
                    <option value="local">Local</option>
                  </select>
                </label>
                {advanced.selectionMode === "pinned" ? (
                  <label className="aw-field"><span>Model id</span>
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
                <button type="submit" className="wf-btn wf-btn-primary"
                        disabled={busy || !name.trim() || taken || keyInvalid} data-testid="aq-create">
                  {busy ? "Creating…" : "Create"}
                </button>
              </div>
            </footer>
          </form>
        )}
      </section>
    </div>
  );
}

/** Shipped templates, and Studio definitions no agent answers to yet. */
function TemplatePicker({ projectId, actor, orphans, existing, onCreated }: {
  projectId: string; actor: string; orphans: StudioAgent[]; existing: Set<string>;
  onCreated: (roleKey: string) => void;
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

  /** The agent a template or definition stands for: made when no agent has its key yet. */
  const ensureAgent = async (roleKey: string, name: string, purpose: string): Promise<boolean> => {
    if (existing.has(roleKey)) return true;
    const r = await createAgent(projectId, actor, { name, purpose, skills: [], mcpServers: [], roleKey });
    if (!r.ok) { setFailed(r.error); return false; }
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
      if (await ensureAgent(t.role_key, t.name, t.description)) onCreated(t.role_key);
    } catch (e) {
      setFailed(String((e as Error).message ?? e));
    } finally { setWorking(""); }
  };

  const [seeded, setSeeded] = useState("");
  const seed = async () => {
    setWorking("seed"); setFailed(""); setSeeded("");
    const r = await callAction(seedStudioCatalog)(projectId, actor);
    setWorking("");
    if (!r.ok) { setFailed(r.message); return; }
    setSeeded(r.message);
  };

  const adopt = async (a: StudioAgent) => {
    const roleKey = a.active_version?.role_key || a.agent_key;
    setWorking(a.id); setFailed("");
    if (await ensureAgent(roleKey, a.name, a.description ?? "")) onCreated(roleKey);
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
        <button type="button" className="wf-btn" disabled={!!working} data-testid="aq-seed"
                title="Adds the reviewed starter agents and panels. Starts nothing, spends nothing."
                onClick={() => void seed()}>
          {working === "seed" ? "Installing…" : "Install starter set"}
        </button>
      </div>
      {seeded ? <p className="detail" role="status" data-testid="aq-seeded">{seeded}</p> : null}
      {failed ? <p className="aq-bad" role="alert">{failed}</p> : null}
      {templates === null ? <p className="detail">Loading…</p> : null}
      <ul className="aq-template-list">
        {shownOrphans.map((a) => (
          <li key={a.id} data-testid={`aq-orphan-${a.agent_key}`}>
            <span title={a.description || undefined}><b>{a.name}</b> <span className="wf-tag">Profile</span></span>
            <button type="button" className="wf-btn" disabled={!!working}
                    title="A profile with no agent yet" onClick={() => void adopt(a)}>
              {working === a.id ? "Adding…" : "Add"}
            </button>
          </li>
        ))}
        {shownTemplates.map((t) => {
          const have = existing.has(t.role_key);
          return (
            <li key={t.id} data-testid={`aq-template-${t.id}`}>
              <span title={t.description || undefined}><b>{t.name}</b> <span className="detail">{t.category}</span></span>
              <button type="button" className="wf-btn" disabled={!!working || (have && t.installed)}
                      onClick={() => void install(t)}>
                {have && t.installed ? "Added" : working === t.id ? "Adding…" : "Add"}
              </button>
            </li>
          );
        })}
      </ul>
      {templates && !shownTemplates.length && !shownOrphans.length ? <p className="detail">No match</p> : null}
    </div>
  );
}
