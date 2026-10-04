"use client";

/**
 * The MCP drawer (spec §5.5): the ability drawer's sections for one server — Connection (URL and
 * sign-in, Save & test), Tools (Allow · Ask · Off per tool, replacing the "tool = tier" box),
 * Agents, Used in — plus `More` (the data boundary), collapsed. A token is typed once and never
 * shown again; a URL change voids the review (the API does it, the drawer says it).
 */
import React, { useState } from "react";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { DotsThree, Star, X } from "@phosphor-icons/react";

import type { Ability, AbilityStatus, KitTool, ToolPolicy } from "../../lib/kit-types";
import { mcpApi, type CrewMember, type McpAuthInput, type McpServer } from "../../lib/kit-api";
import { AgentAvatar } from "../agent-avatar";
import { showToast } from "../client-forms";
import { StatusPill } from "./ability-card";
import { DrawerShell } from "./overlay";
import { Section } from "./ability-drawer";
import { plural, POLICY_LABEL, PROVENANCE, shortError, TIP, agentName } from "./kit-copy";
import { serverProvenance } from "./library";
import { PolicySeg, ReviewRow, RiskDot } from "./tools-section";
import { TrySection } from "./try-section";

/** The Kit abilities a server backs (Library docs for context7; Web research and Web search for
 *  web-intel). Their recommendations star the drawer's agents and their Try it runs here too. */
export function abilitiesOfServer(abilities: Ability[], name: string): Ability[] {
  return abilities.filter((a) => a.mcp.some((m) => m.name === name) || (a.mcp_names ?? []).includes(name)
    || a.tools.some((t) => t.ref.startsWith(`mcp:${name}.`)));
}

/** The server with one agent's grant added or taken, as the list will say once re-read. */
export function withAgent(server: McpServer, role: string, on: boolean): McpServer {
  const refs = server.references ?? { custom_steps: [], skills: [], agents: [] };
  const agents = refs.agents.filter((a) => a.role_key !== role);
  return { ...server, references: { ...refs, agents: on ? [...agents, { role_key: role, tools: [] }] : agents } };
}

export function serverStatus(s: McpServer, holders: number): AbilityStatus {
  if (s.reachable === false) return "failing";
  if (s.enabled && !s.trusted) return "review";
  if (!s.enabled) return "off";
  if (holders === 0) return "unheld";
  return "ready";
}

function toolTier(s: McpServer, name: string): number {
  const own = s.tool_risk_overrides?.[name];
  return typeof own === "number" ? own : s.risk_tier;
}

function effective(tier: number, chosen: string | undefined): ToolPolicy {
  const p = (chosen === "allow" || chosen === "ask" || chosen === "off") ? chosen : tier <= 1 ? "allow" : tier === 2 ? "ask" : "off";
  return p === "allow" && tier >= 2 ? "ask" : p;
}

export function McpDrawer({ projectId, actor, server, crew, abilities = [], onClose, onChanged, onRemoved }: {
  projectId: string; actor: string; server: McpServer; crew: CrewMember[]; abilities?: Ability[];
  onClose: () => void;
  /** `next`: the server as this change left it, shown at once while the list is re-read. */
  onChanged: (next?: McpServer) => void;
  onRemoved: () => void;
}) {
  useIdentityVersion(); // names repaint when identities load
  const [url, setUrl] = useState(server.url);
  const [authType, setAuthType] = useState<McpAuthInput["type"]>((server.auth?.type as McpAuthInput["type"]) ?? "none");
  const [header, setHeader] = useState(server.auth?.header_name ?? "");
  const [secret, setSecret] = useState("");
  const [replacing, setReplacing] = useState(false);
  const [error, setError] = useState<{ field: string | null; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const holders = (server.references?.agents ?? []).map((a) => a.role_key);
  const status = serverStatus(server, holders.length);
  const prov = PROVENANCE[serverProvenance(server)];
  const backs = abilitiesOfServer(abilities, server.name);
  const recommended = new Set(backs.flatMap((a) => a.recommended));
  const tries = backs.filter((a) => a.try);
  const ordered = [...crew].sort((a, b) =>
    Number(recommended.has(b.role_key)) - Number(recommended.has(a.role_key))
    || agentName(a.role_key).localeCompare(agentName(b.role_key)));

  const tools: KitTool[] = server.tools.map((t) => {
    const tier = toolTier(server, t.name) as 0 | 1 | 2 | 3;
    return { ref: `mcp:${server.name}.${t.name}`, label: t.name, tier,
             policy: effective(tier, server.tool_policy?.[t.name]), floor: tier >= 2 ? "ask" : null, kind: "mcp" };
  });

  const saveTest = async () => {
    setBusy(true); setError(null);
    const auth: McpAuthInput = { type: authType, ...(authType === "header" ? { header_name: header } : {}),
                                 ...(secret ? { secret } : {}) };
    if (secret || url !== server.url || authType === "none") {
      // TEST WHAT WAS TYPED FIRST. With no new token typed, the stored one is used by the API
      // only after save, so a URL-only change is tested by saving and probing.
      if (secret || authType === "none") {
        const t = await mcpApi.test(projectId, actor, url, auth);
        if (!t.ok || !t.data.ok) {
          setBusy(false);
          const message = t.ok ? String(t.data.error ?? "Can't reach server") : t.error;
          setError({ field: secret ? "secret" : "url", message });
          showToast("error", `Test failed · ${shortError(message)}`);
          return;
        }
      }
    }
    const r = await mcpApi.update(projectId, actor, server.id, { url, auth });
    if (!r.ok) { setBusy(false); setError({ field: r.field ?? null, message: r.error }); showToast("error", shortError(r.error)); return; }
    const probe = await mcpApi.list(projectId, "now");
    setBusy(false);
    const fresh = probe.ok ? probe.data.items.find((x) => x.id === server.id) : null;
    if (fresh && fresh.reachable === false) {
      setError({ field: "url", message: fresh.last_error ?? "Can't reach server" });
      showToast("error", `Test failed · ${shortError(fresh.last_error)}`);
    } else {
      showToast("success", `Connected · ${plural(fresh?.tools.length ?? server.tools.length, "tool")}`);
    }
    setSecret(""); setReplacing(false);
    onChanged();
  };

  const pick = async (tool: KitTool, policy: ToolPolicy) => {
    const r = await mcpApi.policy(projectId, actor, server.id, tool.label, policy);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `${tool.label} · ${POLICY_LABEL[policy]}`);
    onChanged();
  };

  const toggleAgent = async (role: string) => {
    const on = holders.includes(role);
    const r = on ? await mcpApi.revoke(projectId, actor, server.id, role) : await mcpApi.grant(projectId, actor, server.id, role);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", on ? `Took ${server.name} from ${agentName(role)}` : `Gave ${server.name} to ${agentName(role)}`);
    // SHOWN NOW (K12): the row and this drawer say who holds it the moment the grant lands,
    // then the list is re-read.
    onChanged(withAgent(server, role, !on));
  };

  const toggleOn = async () => {
    setMenu(false);
    const r = await mcpApi.update(projectId, actor, server.id, { enabled: !server.enabled });
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", server.enabled ? `${server.name} off` : `${server.name} on`);
    onChanged();
  };

  const remove = async () => {
    setConfirm(false);
    const r = await mcpApi.remove(projectId, actor, server.id, holders.length > 0);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `Removed ${server.name}`);
    onRemoved();
  };

  const boundary = async (value: string) => {
    const r = await mcpApi.update(projectId, actor, server.id, { data_boundary: value });
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", "Saved");
    onChanged();
  };

  return (
    <DrawerShell label={server.name} testId="kit-mcp-drawer" onClose={onClose}>
      <header className="kit-drawer-head">
        <h3 className="kit-drawer-title" data-tip={server.description ?? server.url} data-user-text>{server.name}</h3>
        <StatusPill status={status} tip={server.last_error ?? (status === "review" ? TIP.review : undefined)} />
        <span className="kit-chip" data-tip={prov.tip}>{prov.label}</span>
        <span className="kit-spacer" />
        <span className="kit-confirm-wrap">
          <button type="button" className="kit-icon-btn" aria-label="More" onClick={() => setMenu((m) => !m)}><DotsThree size={18} /></button>
          {menu ? (
            <span className="kit-pop kit-menu" role="menu">
              <button type="button" role="menuitem" onClick={() => void toggleOn()}>{server.enabled ? "Turn off" : "Turn on"}</button>
              <button type="button" role="menuitem" data-testid="kit-mcp-remove" onClick={() => { setMenu(false); setConfirm(true); }}>Remove</button>
            </span>
          ) : null}
          {confirm ? (
            <span className="kit-pop" role="dialog" aria-label="Confirm remove" data-testid="kit-mcp-remove-confirm">
              <strong>Remove {server.name}?</strong>
              {holders.length ? <span>Agents lose {server.name}</span> : null}
              <span className="kit-pop-actions">
                <button type="button" className="kit-btn kit-btn-danger kit-btn-sm" data-testid="kit-mcp-remove-yes" onClick={() => void remove()}>Remove</button>
                <button type="button" className="kit-btn kit-btn-sm" onClick={() => setConfirm(false)}>Cancel</button>
              </span>
            </span>
          ) : null}
        </span>
        <button type="button" className="kit-icon-btn" aria-label="Close" onClick={onClose}><X size={16} /></button>
      </header>
      <div className="kit-drawer-body">
        <Section id="connection" label="Connection" tip="Where the server is and how BOSS signs in">
          <label className="kit-field">
            <span className="kit-field-label">URL</span>
            <input type="text" value={url} spellCheck={false} data-testid="kit-mcp-url" onChange={(e) => setUrl(e.target.value)} />
            {error?.field === "url" ? <span className="kit-field-err" role="alert" data-tip={error.message}>{shortError(error.message)}</span> : null}
          </label>
          <div className="kit-field">
            <span className="kit-field-label">Auth</span>
            <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label="Auth">
              {(["none", "bearer", "header"] as const).map((t) => (
                <button key={t} type="button" role="radio" aria-checked={authType === t} data-testid={`kit-mcp-auth-${t}`}
                        onClick={() => setAuthType(t)}>{t === "none" ? "None" : t === "bearer" ? "Bearer token" : "Header"}</button>
              ))}
            </span>
          </div>
          {authType === "header" ? (
            <label className="kit-field">
              <span className="kit-field-label">Header</span>
              <input type="text" value={header} spellCheck={false} placeholder="X-API-Key" onChange={(e) => setHeader(e.target.value)} />
            </label>
          ) : null}
          {authType !== "none" ? (
            server.auth?.set && !replacing ? (
              <div className="kit-field" data-testid="kit-mcp-secret-set">
                <span className="kit-field-label">Token</span>
                <span className="kit-secret-set">Set · …{server.auth.last4 ?? ""}</span>
                <button type="button" className="kit-link" onClick={() => setReplacing(true)}>Replace</button>
              </div>
            ) : (
              <label className="kit-field">
                <span className="kit-field-label">Token</span>
                <input type="password" value={secret} autoComplete="new-password" data-testid="kit-mcp-secret"
                       onChange={(e) => setSecret(e.target.value)} />
                {error?.field === "secret" ? <span className="kit-field-err" role="alert" data-tip={error.message}>{shortError(error.message)}</span> : null}
              </label>
            )
          ) : null}
          {error && error.field !== "url" && error.field !== "secret" ? <div className="kit-err" role="alert" data-tip={error.message}>{shortError(error.message)}</div> : null}
          <div className="kit-actions">
            <button type="button" className="kit-btn kit-btn-primary" disabled={busy || !url.trim()} data-testid="kit-mcp-save"
                    onClick={() => void saveTest()}>{busy ? "Testing…" : "Save & test"}</button>
          </div>
        </Section>
        <Section id="tools" label="Tools" tip="What agents may call, and when they ask">
          {!server.trusted && server.enabled ? (
            <ReviewRow server={{ server_id: server.id, name: server.name, url: server.url }} projectId={projectId} actor={actor}
                       tools={tools} onReviewed={() => onChanged()} />
          ) : null}
          {tools.length === 0 ? <div className="kit-faint">No tools yet</div> : null}
          {tools.map((t) => (
            <div key={t.ref} className="kit-tool" data-testid={`kit-tool-${t.ref}`}>
              <span className="kit-tool-label" data-tip={server.tools.find((x) => x.name === t.label)?.description || t.label}>{t.label}</span>
              <RiskDot tier={t.tier} />
              <PolicySeg tool={t} onPick={(p) => void pick(t, p)} />
            </div>
          ))}
        </Section>
        <Section id="agents" label="Agents" tip="Who may use this server's tools">
          <ul className="kit-agent-grid">
            {ordered.map((c) => {
              const on = holders.includes(c.role_key);
              return (
                <li key={c.role_key} className="kit-agent" data-on={on} data-testid={`kit-mcp-agent-${c.role_key}`}>
                  <AgentAvatar agent={c.role_key} size={24} aiBadge={false} />
                  <span className="kit-agent-name" data-tip={c.display_name ?? c.role_key}>{agentName(c.role_key)}</span>
                  {recommended.has(c.role_key) ? (
                    <span className="kit-star" data-tip={`In ${c.display_name ?? agentName(c.role_key)}'s default loadout`}
                          data-testid={`kit-mcp-agent-rec-${c.role_key}`}>
                      <Star size={12} weight="fill" aria-label="Recommended" />
                    </span>
                  ) : null}
                  <button type="button" role="switch" aria-checked={on} className="kit-switch"
                          aria-label={`${agentName(c.role_key)} holds ${server.name}`}
                          data-testid={`kit-mcp-agent-toggle-${c.role_key}`} onClick={() => void toggleAgent(c.role_key)} />
                </li>
              );
            })}
          </ul>
        </Section>
        <Section id="used" label="Used in" tip={TIP.usedIn}>
          {(server.references?.custom_steps.length ?? 0) + (server.references?.skills.length ?? 0) === 0
            ? <div className="kit-faint">Not in any workflow</div>
            : (
              <ul className="kit-used">
                {server.references?.custom_steps.map((s) => <li key={s.step_key} data-user-text>{s.label || s.step_key}</li>)}
                {server.references?.skills.map((s) => <li key={s.skill_key} data-user-text>{s.label || s.skill_key}</li>)}
              </ul>
            )}
        </Section>
        {tries.map((a) => (
          <Section key={a.key} id={`try-${a.key}`} label={tries.length > 1 ? `Try it · ${a.label}` : "Try it"} tip={TIP.tryIt}>
            <TrySection ability={a} projectId={projectId} actor={actor} />
          </Section>
        ))}
        <details className="kit-more">
          <summary>More</summary>
          <label className="kit-field">
            <span className="kit-field-label" data-tip={TIP.dataBoundary}>Data boundary</span>
            <select value={server.data_boundary} onChange={(e) => void boundary(e.target.value)}>
              {["public", "internal", "confidential", "restricted", "local-only"].map((b) => <option key={b} value={b}>{b}</option>)}
            </select>
          </label>
        </details>
      </div>
    </DrawerShell>
  );
}
