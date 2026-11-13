"use client";

/**
 * Add MCP › Connectors (BOSSAPP-849): the whole connector catalog, searchable, with category
 * chips. Picking one hands its pre-fill to the Custom form (`onPick`); nothing is stored here.
 * A stdio connector needs its launch template first — an Owner/Admin creates it with one press
 * (host:exec, BOSSAPP-850) from the same card.
 */
import React, { useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { mcpApi, type Connector, type ConnectorCatalog } from "../../lib/kit-api";
import { showToast } from "../client-forms";
import { shortError } from "./kit-copy";

const AUTH_LABEL: Record<string, string> = { none: "No key", bearer: "Token", header: "Header key", oauth: "OAuth" };

export function ConnectorsTab({ projectId, actor, onPick }: {
  projectId: string; actor: string; onPick: (connector: Connector) => void;
}) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [data, setData] = useState<ConnectorCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let live = true;
    const t = setTimeout(() => {
      void mcpApi.connectors(projectId, query.trim(), category).then((r) => {
        if (!live) return;
        if (r.ok) { setData(r.data); setError(null); } else setError(r.error);
      });
    }, 150);
    return () => { live = false; clearTimeout(t); };
  }, [projectId, query, category, nonce]);

  const createTemplate = async (c: Connector) => {
    if (!c.prefill.stdio_template) return;
    setBusy(c.key);
    const r = await mcpApi.createStdioTemplate(projectId, actor, c.prefill.stdio_template);
    setBusy(null);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `Template ${r.data.name} created`);
    setNonce((n) => n + 1);
  };

  return (
    <div className="kit-connectors" data-testid="kit-connectors">
      <label className="kit-search">
        <input type="search" value={query} placeholder={`Search ${data?.total ?? ""} connectors…`} aria-label="Search connectors"
               data-testid="kit-connectors-search" onChange={(e) => setQuery(e.target.value)} />
      </label>
      <div className="kit-chips" role="radiogroup" aria-label="Category" data-testid="kit-connectors-categories">
        <button type="button" role="radio" aria-checked={category === ""} className="kit-chip"
                onClick={() => setCategory("")}>All</button>
        {(data?.categories ?? []).map((c) => (
          <button key={c.key} type="button" role="radio" aria-checked={category === c.key} className="kit-chip"
                  data-testid={`kit-connectors-cat-${c.key}`} onClick={() => setCategory(category === c.key ? "" : c.key)}>
            {c.key} <span className="kit-faint">{c.count}</span>
          </button>
        ))}
      </div>
      {error ? <p className="kit-field-err" role="alert">{shortError(error)}</p> : null}
      <div className="kit-catalogue">
        {(data?.items ?? []).map((c) => (
          <article key={c.key} className="kit-cat-card" data-testid={`kit-connector-${c.key}`}>
            <div className="kit-card-row">
              <h4 className="kit-card-label" data-tip={c.description}>{c.name}</h4>
              <span className="kit-chip" data-tip={c.risk_notes}>Tier {c.default_tier}</span>
            </div>
            <div className="kit-card-row">
              <span className="kit-chip" data-tip={c.transport === "stdio" ? "Runs in its own sandboxed container" : "Reached over HTTP"}>
                {c.transport === "stdio" ? "Local (stdio)" : "Remote"}</span>
              <span className="kit-chip" data-tip={c.auth_kind === "oauth" && c.prefill.oauth?.token_fallback
                ? "OAuth sign-in; a token works meanwhile" : undefined}>{AUTH_LABEL[c.auth_kind] ?? c.auth_kind}</span>
              <span className="kit-spacer" />
              {c.added ? <span className="kit-faint">Added</span> : c.transport === "stdio" && !c.template_ready ? (
                <GatedButton permission="host:exec" type="button" className="kit-btn kit-btn-sm" disabled={busy === c.key}
                             data-testid={`kit-connector-template-${c.key}`} data-tip="Runs a command on BOSS's host: Owner or Admin"
                             onClick={() => void createTemplate(c)}>{busy === c.key ? "Creating…" : "Create template"}</GatedButton>
              ) : (
                <button type="button" className="kit-btn kit-btn-sm kit-btn-primary" data-testid={`kit-connector-use-${c.key}`}
                        onClick={() => onPick(c)}>Use</button>
              )}
            </div>
          </article>
        ))}
        {data && data.items.length === 0 ? <p className="kit-faint">No connector matches.</p> : null}
      </div>
    </div>
  );
}
