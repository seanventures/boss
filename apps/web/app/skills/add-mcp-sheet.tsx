"use client";

/**
 * + Add MCP (spec §5.5): the curated catalogue (one press; a token when the server needs one),
 * or any server by URL. Both test first and store only what passed; a server someone adds starts
 * unreviewed until a person marks it reviewed in its drawer.
 */
import { GatedButton } from "../../components/gated";
import React, { useEffect, useState } from "react";

import type { McpCatalogueEntry } from "../../lib/kit-types";
import { mcpApi, toolOverrides, type Connector, type McpAuthInput } from "../../lib/kit-api";
import { showToast } from "../client-forms";
import { ConnectorsTab } from "./connectors-tab";
import { PROVENANCE, plural, shortError } from "./kit-copy";
import { ModalShell } from "./overlay";

const RISKS = [{ tier: 0, label: "Reads" }, { tier: 2, label: "Writes" }, { tier: 3, label: "Public" }];

export function AddMcpSheet({ projectId, actor, onClose, onAdded }: {
  projectId: string; actor: string; onClose: () => void; onAdded: (serverId: string) => void;
}) {
  const [tab, setTab] = useState<"catalogue" | "connectors" | "custom">("catalogue");
  const [picked, setPicked] = useState<Connector | null>(null);
  const [items, setItems] = useState<McpCatalogueEntry[] | null>(null);
  const [tokenFor, setTokenFor] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ field: string | null; message: string } | null>(null);
  const [form, setForm] = useState({ name: "", url: "", auth: "none" as McpAuthInput["type"], header: "", secret: "", risk: 0,
                                     transport: "http" as "http" | "stdio" });

  // A connector picked from the catalog fills the form (BOSSAPP-849); the operator still tests
  // and saves it here, and its tool tiers/policies are applied to the tools the test found.
  const pick = (c: Connector) => {
    const p = c.prefill;
    setPicked(c);
    setError(null);
    setForm({ name: p.name, url: p.url, auth: p.auth.type === "oauth" ? "none" : p.auth.type,
              header: p.auth.header_name ?? "", secret: "", risk: p.risk_tier, transport: p.transport });
    setTab("custom");
  };
  const oauthOnly = picked?.prefill.auth.type === "oauth";

  useEffect(() => {
    void mcpApi.catalogue(projectId).then((r) => setItems(r.ok ? r.data.items : []));
  }, [projectId]);

  const add = async (entry: McpCatalogueEntry) => {
    if (entry.auth !== "none" && tokenFor !== entry.key) { setTokenFor(entry.key); setToken(""); setError(null); return; }
    setBusy(true); setError(null);
    const r = await mcpApi.addFromCatalogue(projectId, actor, entry.key, entry.auth === "none" ? null : token);
    setBusy(false);
    if (!r.ok) { setError({ field: r.field ?? "token", message: r.error }); showToast("error", `Test failed, ${shortError(r.error)}`); return; }
    const count = r.data.test?.tools?.length ?? r.data.server?.tools?.length ?? 0;
    showToast("success", `Added ${entry.label}, ${plural(count, "tool")}`);
    onAdded(r.data.server.id);
  };

  const custom = async () => {
    setBusy(true); setError(null);
    const auth: McpAuthInput = { type: form.auth, ...(form.auth === "header" ? { header_name: form.header } : {}),
                                 ...(form.secret ? { secret: form.secret } : {}) };
    const t = await mcpApi.test(projectId, actor, form.url, auth);
    if (!t.ok || !t.data.ok) {
      setBusy(false);
      const message = t.ok ? String(t.data.error ?? "Can't reach server") : t.error;
      setError({ field: form.secret ? "secret" : "url", message });
      showToast("error", `Test failed, ${shortError(message)}`);
      return;
    }
    const hints = picked ? toolOverrides(picked.prefill, t.data.tools.map((x) => x.name)) : { tiers: {}, policies: {} };
    const r = await mcpApi.register(projectId, actor, { name: form.name.trim(), url: form.url.trim(), risk_tier: form.risk,
                                                        transport: form.transport, auth,
                                                        ...(picked ? { description: picked.prefill.description,
                                                                       data_boundary: picked.prefill.data_boundary,
                                                                       tool_risk_overrides: hints.tiers } : {}),
                                                        provenance: picked ? picked.prefill.provenance : { source: "custom" } });
    if (r.ok) {
      // The connector's default policies, best effort: a tool the floor holds at Ask stays Ask.
      for (const [tool, policy] of Object.entries(hints.policies)) {
        await mcpApi.policy(projectId, actor, r.data.id, tool, String(policy));
      }
    }
    setBusy(false);
    if (!r.ok) { setError({ field: r.field ?? "name", message: r.error }); showToast("error", shortError(r.error)); return; }
    showToast("success", `Added ${form.name.trim()}, ${plural(t.data.tools.length, "tool")}`);
    onAdded(r.data.id);
  };

  return (
    <ModalShell label="Add MCP" testId="kit-add-mcp" onClose={onClose} wide>
        <header className="kit-modal-head">
          <span className="kit-seg" role="radiogroup" aria-label="Add MCP">
            <button type="button" role="radio" aria-checked={tab === "catalogue"} data-testid="kit-add-catalogue" onClick={() => setTab("catalogue")}>Catalogue</button>
            <button type="button" role="radio" aria-checked={tab === "connectors"} data-testid="kit-add-connectors" onClick={() => setTab("connectors")}>Connectors</button>
            <button type="button" role="radio" aria-checked={tab === "custom"} data-testid="kit-add-custom" onClick={() => setTab("custom")}>Custom</button>
          </span>
          <button type="button" className="kit-icon-btn" aria-label="Close" onClick={onClose}>×</button>
        </header>
        {tab === "connectors" ? (
          <ConnectorsTab projectId={projectId} actor={actor} onPick={pick} />
        ) : tab === "catalogue" ? (
          <div className="kit-catalogue" data-testid="kit-catalogue">
            {(items ?? []).map((e) => (
              <article key={e.key} className="kit-cat-card" data-testid={`kit-cat-${e.key}`}>
                <div className="kit-card-row">
                  <h4 className="kit-card-label" data-tip={e.description}>{e.label}</h4>
                  <span className="kit-chip" data-tip={PROVENANCE[e.provenance]?.tip}>{PROVENANCE[e.provenance]?.label ?? "Official"}</span>
                </div>
                <div className="kit-card-row">
                  <span className="kit-chip" data-tip="Reached over HTTP; nothing to install">Remote</span>
                  <span className="kit-chip">{e.auth === "none" ? "No key" : "Token"}</span>
                  <span className="kit-spacer" />
                  {e.added ? <span className="kit-faint">Added</span> : (
                    <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-sm kit-btn-primary" disabled={busy}
                            data-testid={`kit-cat-add-${e.key}`} onClick={() => void add(e)}>Add</GatedButton>
                  )}
                </div>
                {tokenFor === e.key ? (
                  <label className="kit-field">
                    <span className="kit-field-label">Token</span>
                    <input type="password" value={token} autoComplete="new-password" data-testid={`kit-cat-token-${e.key}`}
                           onChange={(ev) => setToken(ev.target.value)} />
                    {e.token_url ? <a className="kit-link" href={e.token_url} target="_blank" rel="noreferrer noopener"
                                      data-tip={e.scopes_tip ? `Needs scopes: ${e.scopes_tip}` : undefined}>Get a token ↗</a> : null}
                    {error ? <span className="kit-field-err" role="alert" data-tip={error.message}>{shortError(error.message)}</span> : null}
                  </label>
                ) : null}
              </article>
            ))}
          </div>
        ) : (
          <div className="kit-conn" data-testid="kit-custom-mcp">
            {picked ? (
              <div className="kit-note" data-testid="kit-custom-picked">
                <strong>{picked.name}</strong> <span className="kit-faint">{picked.setup}</span>
                <span className="kit-faint" data-tip={picked.risk_notes}> · Risk: {shortError(picked.risk_notes)}</span>
                {picked.token_url ? <> · <a className="kit-link" href={picked.token_url} target="_blank" rel="noreferrer noopener"
                                            data-tip={picked.scopes_tip ? `Needs: ${picked.scopes_tip}` : undefined}>Get a token ↗</a></> : null}
                {oauthOnly ? <span className="kit-field-err" role="note"> · Needs OAuth sign-in, which is not available here yet.</span> : null}
              </div>
            ) : null}
            <label className="kit-field">
              <span className="kit-field-label">Name</span>
              <input type="text" value={form.name} data-testid="kit-custom-name" onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} />
              {error?.field === "name" ? <span className="kit-field-err" role="alert" data-tip={error.message}>{shortError(error.message)}</span> : null}
            </label>
            <label className="kit-field">
              <span className="kit-field-label">URL</span>
              <input type="text" value={form.url} spellCheck={false} placeholder="https://mcp.example.com/mcp" data-testid="kit-custom-url"
                     onChange={(e) => setForm((f) => ({ ...f, url: e.target.value }))} />
              {error?.field === "url" ? <span className="kit-field-err" role="alert" data-tip={error.message}>{shortError(error.message)}</span> : null}
            </label>
            <div className="kit-field">
              <span className="kit-field-label">Auth</span>
              <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label="Auth">
                {(["none", "bearer", "header"] as const).map((t) => (
                  <button key={t} type="button" role="radio" aria-checked={form.auth === t} data-testid={`kit-custom-auth-${t}`}
                          onClick={() => setForm((f) => ({ ...f, auth: t }))}>{t === "none" ? "None" : t === "bearer" ? "Bearer token" : "Header"}</button>
                ))}
              </span>
            </div>
            {form.auth === "header" ? (
              <label className="kit-field">
                <span className="kit-field-label">Header</span>
                <input type="text" value={form.header} placeholder="X-API-Key" onChange={(e) => setForm((f) => ({ ...f, header: e.target.value }))} />
              </label>
            ) : null}
            {form.auth !== "none" ? (
              <label className="kit-field">
                <span className="kit-field-label">Token</span>
                <input type="password" value={form.secret} autoComplete="new-password" data-testid="kit-custom-secret"
                       onChange={(e) => setForm((f) => ({ ...f, secret: e.target.value }))} />
                {error?.field === "secret" ? <span className="kit-field-err" role="alert" data-tip={error.message}>{shortError(error.message)}</span> : null}
              </label>
            ) : null}
            <div className="kit-field">
              <span className="kit-field-label">Risk</span>
              <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label="Risk">
                {RISKS.map((r) => (
                  <button key={r.tier} type="button" role="radio" aria-checked={form.risk === r.tier}
                          onClick={() => setForm((f) => ({ ...f, risk: r.tier }))}>{r.label}</button>
                ))}
              </span>
            </div>
            <div className="kit-actions">
              <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-primary" data-testid="kit-custom-save"
                      disabled={busy || oauthOnly || !form.name.trim() || !form.url.trim()} onClick={() => void custom()}>
                {busy ? "Testing…" : "Save & test"}
              </GatedButton>
            </div>
          </div>
        )}
    </ModalShell>
  );
}
