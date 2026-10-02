"use client";

/**
 * Create an agent: name, purpose, what it holds, who it reports to. One screen, one request.
 *
 * WHY NOT THE WIZARD. "Create agent" on the Agents page opened Agent Studio's five-step
 * commissioning wizard, which made a system prompt, a job prompt, at least one deterministic
 * rule, an artifact type, limits and A2A topics mandatory — none of which changes how the agent
 * works a workflow step — and then did not grant the skills it asked for, so the new agent held
 * nothing. Here the agent is made and given its skills and MCP servers in one transaction
 * (POST /v1/agents), so it can be put on a step the moment this closes. The full Studio profile
 * is still one press away for anyone who wants prompts and a model policy.
 */
import React, { useEffect, useMemo, useState } from "react";

import {
  createAgent, isAgentKey, loadGrantable, parseToolList,
  type Grantable, type McpGrant,
} from "../lib/agents";

export function AgentQuickCreate({ projectId, actor, onClose, onCreated, onOpenStudio }: {
  projectId: string;
  actor: string;
  onClose: () => void;
  onCreated: (roleKey: string) => void;
  /** The full Agent Studio wizard, for prompts, rules and a model policy. */
  onOpenStudio?: () => void;
}) {
  const [grantable, setGrantable] = useState<Grantable | null>(null);
  const [loadFailed, setLoadFailed] = useState("");
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("");
  const [skills, setSkills] = useState<string[]>([]);
  const [servers, setServers] = useState<Record<string, string>>({});
  const [reportsTo, setReportsTo] = useState("");
  const [skillFilter, setSkillFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");

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

  // THE KEY IT WILL GET, said before it is taken. An existing key is refused by the API with a
  // sentence; saying it here first means the operator is not surprised by that refusal.
  // Same rule as agents.key_from_name on the server, so the key shown is the key made.
  const slug = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  const derived = (slug && !/^[a-z]/.test(slug) ? `agent_${slug}` : slug).slice(0, 64);
  // A NAME WITH NO ENGLISH LETTERS (a Tamil name, or one letter) makes no usable key, and this
  // form had no key field, so the API's refusal named an empty key the operator never typed.
  // Now the key field appears exactly then; left empty, the server makes one (agent_…).
  const nameMakesNoKey = !!name.trim() && !isAgentKey(derived);
  const [ownKey, setOwnKey] = useState("");
  const key = nameMakesNoKey ? ownKey.trim().toLowerCase() : derived;
  const keyInvalid = nameMakesNoKey && !!key && !isAgentKey(key);
  const taken = !!key && (grantable?.agents ?? []).some((a) => a.role_key === key);

  const submit = async () => {
    if (!name.trim()) { setFailed("Give the agent a name."); return; }
    if (taken) {
      setFailed(`An agent "${key}" already exists. Open it from the roster to change it, or pick another name.`);
      return;
    }
    if (keyInvalid) {
      setFailed(`"${key}" cannot be a key: use English letters, digits and underscores, starting with a letter — or leave the key empty and BOSS will make one.`);
      return;
    }
    setBusy(true);
    setFailed("");
    const mcpServers: McpGrant[] = Object.entries(servers)
      .map(([server_id, tools]) => ({ server_id, tools: parseToolList(tools) }));
    const r = await createAgent(projectId, actor, {
      name: name.trim(), purpose: purpose.trim(), skills, mcpServers, reportsTo: reportsTo || null,
      ...(nameMakesNoKey && key ? { roleKey: key } : {}),
    });
    setBusy(false);
    if (!r.ok) { setFailed(r.error); return; }
    onCreated(String((r.data as { role_key?: string }).role_key ?? key));
  };

  return (
    <div className="aw-backdrop">
      <section className="aw-dialog aq-dialog" role="dialog" aria-modal="true"
               aria-labelledby="aq-title" data-testid="agent-quick-create">
        <header className="aw-header">
          <div>
            <span className="aw-kicker">Agents / new</span>
            <h2 id="aq-title">Create an agent</h2>
            <p>Name it, say what it is for, and give it what it may use. It can be put on a
              workflow step as soon as it exists.</p>
          </div>
          <button className="aw-close" onClick={onClose} disabled={busy}
                  aria-label="Close without creating">×</button>
        </header>

        <form className="aw-form aq-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
          <label className="aw-field">
            <span>Name</span>
            <input value={name} autoFocus data-testid="aq-name" placeholder="Release Auditor"
                   onChange={(e) => setName(e.target.value)} />
            {key && !nameMakesNoKey ? (
              <small className={taken ? "aq-bad" : "aw-field-note"}>
                {taken ? <>An agent <code>{key}</code> already exists — choose another name.</>
                  : <>Its key will be <code>{key}</code>.</>}
              </small>
            ) : null}
          </label>
          {nameMakesNoKey ? (
            <label className="aw-field">
              <span>Key</span>
              <input value={ownKey} data-testid="aq-key" placeholder="video_editor"
                     onChange={(e) => setOwnKey(e.target.value)} />
              <small className={keyInvalid || taken ? "aq-bad" : "aw-field-note"}>
                {taken ? <>An agent <code>{key}</code> already exists — choose another key.</>
                  : keyInvalid ? <>Use English letters, digits and underscores, starting with a letter.</>
                    : <>This name has no English letters to make a key from. Set one, or leave
                      it empty and BOSS will make one. The name stays exactly as you wrote it.</>}
              </small>
            </label>
          ) : null}
          <label className="aw-field">
            <span>Purpose</span>
            <textarea rows={3} value={purpose} data-testid="aq-purpose"
                      placeholder="What this agent is for. BOSS reads this when it picks an agent for a step nobody assigned."
                      onChange={(e) => setPurpose(e.target.value)} />
          </label>
          <label className="aw-field">
            <span>Reports to</span>
            <select value={reportsTo} data-testid="aq-reports-to"
                    onChange={(e) => setReportsTo(e.target.value)}>
              <option value="">Nobody — a root of the team</option>
              {(grantable?.agents ?? []).map((a) => (
                <option key={a.role_key} value={a.role_key}>{a.name}</option>
              ))}
            </select>
          </label>

          {loadFailed ? (
            <p className="aq-bad" data-testid="aq-load-failed">
              Could not read the skills and MCP servers this project has — {loadFailed}. You can
              still create the agent and give it skills from its page afterwards.
            </p>
          ) : null}

          <fieldset className="aq-group">
            <legend>Skills <span className="detail">· {skills.length} chosen</span></legend>
            {(grantable?.skills ?? []).length > 8 ? (
              <input type="search" className="aq-filter" placeholder="Find a skill"
                     value={skillFilter} onChange={(e) => setSkillFilter(e.target.value)} />
            ) : null}
            <div className="aw-skill-list aq-list" data-testid="aq-skills">
              {shownSkills.map((s) => {
                const on = skills.includes(s.skill_id);
                return (
                  <label key={s.skill_id} className={on ? "selected" : ""}>
                    <input type="checkbox" checked={on}
                           data-testid={`aq-skill-${s.skill_id}`}
                           onChange={() => setSkills(on ? skills.filter((x) => x !== s.skill_id)
                             : [...skills, s.skill_id])} />
                    <div>
                      <b>{s.skill_id}</b>
                      <small>{s.summary || s.label}{s.source === "authored" ? " · yours" : ""}
                        {s.risk_tier >= 2 ? ` · needs approval each time (tier ${s.risk_tier})` : ""}
                        {!s.enabled ? " · not enabled in this project" : ""}</small>
                    </div>
                  </label>
                );
              })}
              {grantable && !grantable.skills.length ? (
                <p className="aw-empty-line">This project has no skills yet — author one on the
                  Skills and MCP page.</p>
              ) : null}
            </div>
          </fieldset>

          <fieldset className="aq-group">
            <legend>MCP servers <span className="detail">· {Object.keys(servers).length} chosen</span></legend>
            <div className="aw-skill-list aq-list" data-testid="aq-mcp">
              {(grantable?.mcp_servers ?? []).map((m) => {
                const on = m.mcp_server_id in servers;
                return (
                  <label key={m.mcp_server_id} className={on ? "selected" : ""}>
                    <input type="checkbox" checked={on}
                           data-testid={`aq-mcp-${m.mcp_server_id}`}
                           onChange={() => {
                             const next = { ...servers };
                             if (on) delete next[m.mcp_server_id]; else next[m.mcp_server_id] = "";
                             setServers(next);
                           }} />
                    <div>
                      <b>{m.server}</b>
                      <small>{m.description || m.url}{!m.enabled ? " · disabled in this project — it will not be offered until enabled" : ""}</small>
                      {on ? (
                        <input className="aq-tools" placeholder="Only these tools (comma-separated) — empty for all"
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
                <p className="aw-empty-line">No MCP servers are registered — register one on the
                  Skills and MCP page to give an agent its tools.</p>
              ) : null}
            </div>
          </fieldset>

          {failed ? <p className="aq-bad" role="alert" data-testid="aq-failed">{failed}</p> : null}
        </form>

        <footer className="aw-footer">
          <span>
            {onOpenStudio ? (
              <button type="button" className="linklike" onClick={onOpenStudio} disabled={busy}
                      data-testid="aq-open-studio">
                Need prompts, rules or a model policy? Use the full Studio wizard
              </button>
            ) : null}
          </span>
          <div>
            <button className="as-button secondary" onClick={onClose} disabled={busy}>Cancel</button>
            <button className="as-button primary" onClick={() => void submit()}
                    disabled={busy || !name.trim() || taken || keyInvalid} data-testid="aq-create">
              {busy ? "Creating…" : "Create agent"}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
