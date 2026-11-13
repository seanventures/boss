"use client";

/**
 * Gateways (BOSSAPP-851): hand a curated slice of this project's MCP servers to an MCP client
 * outside BOSS (Claude Code, Cursor). Pick servers and tools, mint a token — shown ONCE, BOSS
 * keeps only its hash — and revoke tokens or the whole gateway. Every call through a gateway is
 * governed and audited by BOSS exactly like an agent's.
 */
import React, { useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { mcpApi, type GatewayProfile, type McpGateway, type McpServer } from "../../lib/kit-api";
import { showToast } from "../client-forms";
import { shortError } from "./kit-copy";
import { ModalShell } from "./overlay";

type Pick = { on: boolean; tools: Record<string, boolean> };

export function GatewaysSheet({ projectId, actor, servers, onClose }: {
  projectId: string; actor: string; servers: McpServer[]; onClose: () => void;
}) {
  const [items, setItems] = useState<McpGateway[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [days, setDays] = useState(90);
  const [picks, setPicks] = useState<Record<string, Pick>>({});
  const [shown, setShown] = useState<{ gateway: string; token: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => void mcpApi.gateways(projectId).then((r) => setItems(r.ok ? r.data.items : []));
  useEffect(load, [projectId]);

  const toggleServer = (id: string) => setPicks((p) => ({ ...p, [id]: { on: !p[id]?.on, tools: p[id]?.tools ?? {} } }));
  const toggleTool = (id: string, tool: string) => setPicks((p) => {
    const cur = p[id] ?? { on: true, tools: {} };
    return { ...p, [id]: { on: true, tools: { ...cur.tools, [tool]: !(cur.tools[tool] ?? true) } } };
  });

  const profile = (): GatewayProfile => ({
    servers: servers.filter((s) => picks[s.id]?.on).map((s) => {
      const off = Object.entries(picks[s.id]?.tools ?? {}).filter(([, v]) => !v).map(([t]) => t);
      return { server_id: s.id, tools: off.length ? s.tools.map((t) => t.name).filter((t) => !off.includes(t)) : null, caps: {} };
    }),
  });

  const create = async () => {
    setBusy(true);
    const r = await mcpApi.createGateway(projectId, actor, name.trim(), profile(), days);
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    setShown({ gateway: r.data.gateway.id, token: r.data.token });
    setCreating(false); setName(""); setPicks({});
    load();
  };

  const mint = async (g: McpGateway) => {
    const r = await mcpApi.mintGatewayToken(projectId, actor, g.id, days);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    setShown({ gateway: g.id, token: r.data.token });
    load();
  };

  const revokeToken = async (g: McpGateway, tokenId: string) => {
    const r = await mcpApi.revokeGatewayToken(projectId, actor, g.id, tokenId);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", "Token revoked");
    load();
  };

  const revokeGateway = async (g: McpGateway) => {
    const r = await mcpApi.revokeGateway(projectId, actor, g.id);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `${g.name} revoked`);
    load();
  };

  const copy = (value: string) => { try { void navigator.clipboard.writeText(value); showToast("success", "Copied"); } catch { /* no clipboard */ } };
  const chosen = servers.some((s) => picks[s.id]?.on);

  return (
    <ModalShell label="Gateways" testId="kit-gateways" onClose={onClose} wide>
      <header className="kit-modal-head">
        <h3 className="kit-card-label">Gateways</h3>
        <span className="kit-spacer" />
        {!creating ? (
          <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-sm kit-btn-primary"
                       data-testid="kit-gw-new" onClick={() => setCreating(true)}>New gateway</GatedButton>
        ) : null}
        <button type="button" className="kit-icon-btn" aria-label="Close" onClick={onClose}>×</button>
      </header>

      {shown ? (
        <div className="kit-note" role="status" data-testid="kit-gw-token">
          <strong>Copy this token now — it is not shown again.</strong>
          <code className="kit-code" data-testid="kit-gw-token-value">{shown.token}</code>
          <button type="button" className="kit-btn kit-btn-sm" onClick={() => copy(shown.token)}>Copy</button>
          <p className="kit-faint">MCP URL: your BOSS API address + <code>/mcp/gw/{shown.gateway}</code>, header
            {" "}<code>Authorization: Bearer &lt;token&gt;</code>.</p>
          <button type="button" className="kit-btn kit-btn-sm" onClick={() => setShown(null)}>Done</button>
        </div>
      ) : null}

      {creating ? (
        <div className="kit-conn" data-testid="kit-gw-create">
          <label className="kit-field">
            <span className="kit-field-label">Name</span>
            <input type="text" value={name} data-testid="kit-gw-name" onChange={(e) => setName(e.target.value)} />
          </label>
          <div className="kit-field">
            <span className="kit-field-label">Servers and tools</span>
            {servers.length === 0 ? <span className="kit-faint">No MCP servers yet.</span> : null}
            {servers.map((s) => (
              <div key={s.id} className="kit-gw-server">
                <label><input type="checkbox" checked={!!picks[s.id]?.on} data-testid={`kit-gw-server-${s.name}`}
                              onChange={() => toggleServer(s.id)} /> {s.name}
                  {!s.trusted ? <span className="kit-faint" data-tip="Unreviewed servers serve no tools until reviewed"> · unreviewed</span> : null}</label>
                {picks[s.id]?.on ? (
                  <span className="kit-chips">
                    {s.tools.map((t) => (
                      <label key={t.name} className="kit-chip">
                        <input type="checkbox" checked={picks[s.id]?.tools[t.name] ?? true}
                               onChange={() => toggleTool(s.id, t.name)} /> {t.name}
                      </label>
                    ))}
                  </span>
                ) : null}
              </div>
            ))}
            <span className="kit-faint">Only tools that run without asking a person are served; Ask and Off tools never are.</span>
          </div>
          <label className="kit-field">
            <span className="kit-field-label">Token expires in (days)</span>
            <input type="number" min={1} max={365} value={days} onChange={(e) => setDays(Math.max(1, Math.min(365, Number(e.target.value) || 90)))} />
          </label>
          <div className="kit-actions">
            <button type="button" className="kit-btn" onClick={() => setCreating(false)}>Cancel</button>
            <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-primary" data-testid="kit-gw-create-save"
                         disabled={busy || !name.trim() || !chosen} onClick={() => void create()}>
              {busy ? "Creating…" : "Create and show token"}</GatedButton>
          </div>
        </div>
      ) : null}

      <div className="kit-catalogue" data-testid="kit-gw-list">
        {(items ?? []).map((g) => (
          <article key={g.id} className="kit-cat-card" data-testid={`kit-gw-${g.name}`}>
            <div className="kit-card-row">
              <h4 className="kit-card-label">{g.name}</h4>
              <span className="kit-chip">{g.status}</span>
              <span className="kit-chip">{g.profile.servers.length} server{g.profile.servers.length === 1 ? "" : "s"}</span>
              <span className="kit-spacer" />
              {g.status === "active" ? (
                <>
                  <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-sm"
                               data-testid={`kit-gw-mint-${g.name}`} onClick={() => void mint(g)}>New token</GatedButton>
                  <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-sm"
                               data-testid={`kit-gw-revoke-${g.name}`} onClick={() => void revokeGateway(g)}>Revoke</GatedButton>
                </>
              ) : null}
            </div>
            <ul className="kit-gw-tokens">
              {g.tokens.map((t) => (
                <li key={t.id} data-testid={`kit-gw-token-${t.last4}`}>
                  <code>…{t.last4}</code> <span className="kit-faint">{t.status}
                  {t.expires_at ? ` · expires ${t.expires_at.slice(0, 10)}` : ""}
                  {t.last_used_at ? ` · used ${t.last_used_at.slice(0, 10)}` : " · never used"}</span>
                  {t.status === "active" && g.status === "active" ? (
                    <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-sm"
                                 data-testid={`kit-gw-token-revoke-${t.last4}`} onClick={() => void revokeToken(g, t.id)}>Revoke</GatedButton>
                  ) : null}
                </li>
              ))}
            </ul>
          </article>
        ))}
        {items && items.length === 0 && !creating ? <p className="kit-faint">No gateways yet.</p> : null}
      </div>
    </ModalShell>
  );
}
