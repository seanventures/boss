"use client";

/**
 * Add MCP server (docs/design/SKILLS_AND_MCP_HOME.md): ONE SCREEN, ONE COMPONENT.
 *
 * Skills & MCP, the agent page, the create-agent wizard and the workflow step editor all open
 * this sheet; there is no second form. Pick a Popular server (or Browse all connectors), or paste
 * a URL. Test connection asks the server what it offers and stores NOTHING (POST
 * /v1/mcp/servers/test). Give to agents picks who may use it. Add server stores it and grants it.
 *
 * A TOKEN IS A VAULT REFERENCE. A typed token travels to the API once, is written to the secret
 * store there and kept on the server row only as its reference and last four.
 *
 * A COMMAND IS NOT RUN FROM HERE. `npx …`, `uvx …` or `docker run …` is a local (stdio) server: BOSS
 * runs one only from a launch template an Owner/Admin made (Browse all › Create template).
 */
import { GatedButton, useGate } from "../../components/gated";
import React, { useEffect, useMemo, useState } from "react";
import { ArrowLeft, Check, Lock, MagnifyingGlass, PlugsConnected, Terminal, X } from "@phosphor-icons/react";

import type { McpCatalogueEntry } from "../../lib/kit-types";
import { kitApi, mcpApi, toolOverrides, type Connector, type CrewMember, type McpAuthInput } from "../../lib/kit-api";
import { AgentAvatar } from "../agent-avatar";
import { showToast } from "../client-forms";
import { ConnectorsTab } from "./connectors-tab";
import { agentName, plural, shortError } from "./kit-copy";
import { ModalShell } from "./overlay";

const RISKS = [{ tier: 0, label: "Reads" }, { tier: 2, label: "Writes" }, { tier: 3, label: "Public" }];
/** Agent chips shown before "All N" (a crew can be 30 agents). */
const AGENTS_SHOWN = 8;
const NONE: string[] = [];
const AUTH_WORD: Record<McpAuthInput["type"], string> = { none: "None", bearer: "Token", header: "Header", oauth: "OAuth" };

/** What was typed in "URL or command": a remote server, a stdio template URL, or a command. */
export function targetKind(text: string): "empty" | "http" | "stdio" | "command" {
  const t = text.trim();
  if (!t) return "empty";
  if (/^https?:\/\//i.test(t)) return "http";
  if (/^stdio:\/\//i.test(t)) return "stdio";
  return "command";
}

/** A name from a URL: `https://mcp.linear.app/mcp` is `linear`. */
export function nameFromUrl(text: string): string {
  try {
    const host = new URL(text.trim()).hostname.toLowerCase();
    const parts = host.split(".").filter((p) => !["mcp", "www", "api", "app", "com", "io", "dev", "net", "org", "ai"].includes(p));
    return (parts[0] ?? host.split(".")[0] ?? "").replace(/[^a-z0-9_-]/g, "").slice(0, 40);
  } catch { return ""; }
}

type TestState = { ok: boolean; tools: { name: string; description?: string }[]; error: string | null } | null;

export function AddMcpSheet({ projectId, actor, onClose, onAdded, defaultAgents = NONE, grants = true, crew: givenCrew }: {
  projectId: string; actor: string; onClose: () => void;
  /** Called once the server is stored (and granted): its id and name. */
  onAdded: (serverId: string, name?: string) => void;
  /** Agents whose chip starts on (the agent page's agent, the step's agent). */
  defaultAgents?: string[];
  /** False where the agent does not exist yet (the wizard): no Give to agents row. */
  grants?: boolean;
  crew?: CrewMember[];
}) {
  const [items, setItems] = useState<McpCatalogueEntry[] | null>(null);
  const [entry, setEntry] = useState<McpCatalogueEntry | null>(null);
  const [connector, setConnector] = useState<Connector | null>(null);
  const [browse, setBrowse] = useState(false);
  const [form, setForm] = useState({ name: "", target: "", auth: "none" as McpAuthInput["type"], header: "", secret: "",
                                     risk: 0, clientId: "" });
  const [named, setNamed] = useState(false);
  const [test, setTest] = useState<TestState>(null);
  const [testing, setTesting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ field: string | null; message: string } | null>(null);
  const [crew, setCrew] = useState<CrewMember[]>(givenCrew ?? []);
  const [agents, setAgents] = useState<Set<string>>(() => new Set(defaultAgents));
  // Giving a server to agents is agent_ability:assign: a viewer who may add servers but not
  // grant sees the chips greyed with the reason, and nothing is granted.
  const assignGate = useGate("agent_ability:assign");
  const [everyone, setEveryone] = useState(false);

  useEffect(() => {
    void mcpApi.catalogue(projectId).then((r) => setItems(r.ok ? r.data.items : []));
  }, [projectId]);
  useEffect(() => {
    if (!grants || givenCrew) return;
    void kitApi.load(projectId, "none").then((r) => { if (r.ok) setCrew(r.data.crew ?? []); });
  }, [projectId, grants, givenCrew]);

  const kind = targetKind(form.target);
  const set = (change: Partial<typeof form>) => {
    setForm((f) => ({ ...f, ...change }));
    // ANYTHING THAT CHANGES WHAT IS REACHED voids the last test: Add waits for a new one.
    if ("target" in change || "auth" in change || "secret" in change || "header" in change) setTest(null);
    setError(null);
  };
  const setTarget = (target: string) => {
    set({ target, ...(named ? {} : { name: nameFromUrl(target) }) });
    setEntry(null); setConnector(null);
  };

  const pickEntry = (e: McpCatalogueEntry) => {
    setEntry(e); setConnector(null); setNamed(true); setTest(null); setError(null);
    setForm((f) => ({ ...f, name: e.name, target: e.url, auth: e.auth, header: e.header_name ?? "", secret: "",
                      risk: e.risk_default }));
  };
  const pickConnector = (c: Connector) => {
    const p = c.prefill;
    setConnector(c); setEntry(null); setNamed(true); setTest(null); setError(null); setBrowse(false);
    setForm({ name: p.name, target: p.url, auth: p.auth.type, header: p.auth.header_name ?? "", secret: "",
              risk: p.risk_tier, clientId: "" });
  };

  const auth = (): McpAuthInput => ({ type: form.auth, ...(form.auth === "header" ? { header_name: form.header } : {}),
                                      ...(form.secret ? { secret: form.secret } : {}) });

  const runTest = async () => {
    setTesting(true); setError(null);
    const t = await mcpApi.test(projectId, actor, form.target.trim(), auth());
    setTesting(false);
    if (!t.ok || !t.data.ok) {
      const message = t.ok ? String(t.data.error ?? "Can't reach server") : t.error;
      setTest({ ok: false, tools: [], error: message });
      setError({ field: form.secret ? "secret" : "url", message });
      return;
    }
    setTest({ ok: true, tools: t.data.tools, error: null });
  };

  const grantAll = async (serverId: string, name: string) => {
    if (!grants || !assignGate.allowed) return;
    const failed: string[] = [];
    for (const role of agents) {
      const r = await mcpApi.grant(projectId, actor, serverId, role);
      if (!r.ok) failed.push(agentName(role));
    }
    if (failed.length) showToast("error", `Not given to ${failed.join(", ")}`);
    else if (agents.size) showToast("success", `Gave ${name} to ${plural(agents.size, "agent")}`);
  };

  const add = async () => {
    const name = form.name.trim();
    setBusy(true); setError(null);
    if (form.auth === "oauth") {
      // OAUTH (BOSSAPP-846): nothing to test before someone signs in. Store, then open the
      // provider's sign-in in a new tab; the server's row says when it is connected.
      const oauth: McpAuthInput = { type: "oauth", ...(form.clientId.trim() ? { oauth: { client_id: form.clientId.trim() } } : {}),
                                    ...(form.secret ? { secret: form.secret } : {}) };
      const r = await mcpApi.register(projectId, actor, { name, url: form.target.trim(), risk_tier: form.risk, auth: oauth,
                                                          provenance: connector ? connector.prefill.provenance : { source: "custom" } });
      if (!r.ok) { setBusy(false); setError({ field: r.field ?? "name", message: r.error }); showToast("error", shortError(r.error)); return; }
      await grantAll(r.data.id, name);
      const s = await mcpApi.signIn(projectId, actor, r.data.id, "org");
      setBusy(false);
      if (s.ok && typeof window !== "undefined") {
        window.open(s.data.redirect, "_blank", "noopener,noreferrer");
        showToast("success", `Added ${name}, finish signing in`);
      } else {
        showToast("error", `Added ${name}, sign in from Edit`);
      }
      onAdded(r.data.id, name);
      return;
    }
    if (entry) {
      // The curated catalogue's own add: it tests and stores in one call, as its entry says.
      const r = await mcpApi.addFromCatalogue(projectId, actor, entry.key, entry.auth === "none" ? null : form.secret);
      if (!r.ok) { setBusy(false); setError({ field: r.field ?? "secret", message: r.error }); showToast("error", `Test failed, ${shortError(r.error)}`); return; }
      await grantAll(r.data.server.id, entry.name);
      setBusy(false);
      showToast("success", `Added ${entry.label}, ${plural(r.data.test?.tools?.length ?? r.data.server?.tools?.length ?? 0, "tool")}`);
      onAdded(r.data.server.id, r.data.server.name ?? entry.name);
      return;
    }
    const found = (test?.tools ?? []).map((x) => x.name);
    const hints = connector ? toolOverrides(connector.prefill, found) : { tiers: {}, policies: {} };
    const r = await mcpApi.register(projectId, actor, {
      name, url: form.target.trim(), risk_tier: form.risk, transport: kind === "stdio" ? "stdio" : "http", auth: auth(),
      ...(connector ? { description: connector.prefill.description, data_boundary: connector.prefill.data_boundary,
                        tool_risk_overrides: hints.tiers } : {}),
      provenance: connector ? connector.prefill.provenance : { source: "custom" } });
    if (!r.ok) { setBusy(false); setError({ field: r.field ?? "name", message: r.error }); showToast("error", shortError(r.error)); return; }
    // The connector's default policies, best effort: a tool the floor holds at Ask stays Ask.
    for (const [tool, policy] of Object.entries(hints.policies)) {
      await mcpApi.policy(projectId, actor, r.data.id, tool, String(policy));
    }
    await grantAll(r.data.id, name);
    setBusy(false);
    showToast("success", `Added ${name}, ${plural(found.length, "tool")}`);
    onAdded(r.data.id, name);
  };

  // The agents passed in first (they start on), then the crew; the order never moves on a press.
  const ordered = useMemo(() => [...crew.filter((c) => defaultAgents.includes(c.role_key)),
                                  ...crew.filter((c) => !defaultAgents.includes(c.role_key))], [crew, defaultAgents]);
  const shownCount = AGENTS_SHOWN + defaultAgents.length;
  const popular = useMemo(() => (items ?? []).filter((e) => !e.added).slice(0, 8), [items]);
  const canTest = (kind === "http" || kind === "stdio") && form.auth !== "oauth" && !testing && !busy
    && (form.auth === "none" || Boolean(form.secret)) && (form.auth !== "header" || Boolean(form.header.trim()));
  const canAdd = !busy && Boolean(form.name.trim()) && (kind === "http" || kind === "stdio")
    && (form.auth === "oauth" || test?.ok === true);

  return (
    <ModalShell label="Add MCP server" testId="kit-add-mcp" onClose={onClose} wide over="dialog">
      <header className="kit-modal-head">
        <h3>{browse ? "Browse connectors" : "Add MCP server"}</h3>
        <button type="button" className="kit-icon-btn" aria-label="Close" data-tip="Close" onClick={onClose}><X size={16} /></button>
      </header>
      {browse ? (
        <>
          <button type="button" className="kit-btn kit-btn-sm kit-add-back" data-testid="kit-add-back" onClick={() => setBrowse(false)}>
            <ArrowLeft size={14} aria-hidden="true" /> Back
          </button>
          <ConnectorsTab projectId={projectId} actor={actor} onPick={pickConnector} />
        </>
      ) : (
        <div className="kit-conn kit-add" data-testid="kit-custom-mcp">
          <div className="kit-field">
            <span className="kit-field-label" data-tip="Curated servers; pick one to fill the form">Popular</span>
            <div className="kit-chips" data-testid="kit-catalogue">
              {popular.map((e) => (
                <button key={e.key} type="button" className="kit-chip kit-chip-btn" aria-pressed={entry?.key === e.key}
                        data-tip={e.description} data-testid={`kit-cat-${e.key}`} onClick={() => pickEntry(e)}>
                  {e.auth !== "none" ? <Lock size={11} aria-label="Needs a token" /> : null}{e.label}
                </button>
              ))}
              <button type="button" className="kit-chip kit-chip-btn" data-testid="kit-add-browse"
                      data-tip="Every connector, by category" onClick={() => setBrowse(true)}>
                <MagnifyingGlass size={11} aria-hidden="true" /> Browse all
              </button>
            </div>
          </div>
          {connector ? (
            <div className="kit-note" data-testid="kit-custom-picked">
              <strong data-user-text>{connector.name}</strong>
              <span className="kit-faint" data-tip={connector.setup}>Connector</span>
              {connector.token_url ? <a className="kit-link" href={connector.token_url} target="_blank" rel="noreferrer noopener"
                                        data-tip={connector.scopes_tip ? `Needs: ${connector.scopes_tip}` : undefined}>Get a token ↗</a> : null}
            </div>
          ) : null}
          <label className="kit-field">
            <span className="kit-field-label" data-tip="A remote server's URL, or a local server's launch command">URL or command</span>
            <input type="text" value={form.target} spellCheck={false} placeholder="https://mcp.example.com/mcp" data-testid="kit-custom-url"
                   aria-invalid={error?.field === "url" || undefined} onChange={(e) => setTarget(e.target.value)} />
            {kind === "command" ? (
              <span className="kit-faint kit-add-cmd" data-testid="kit-add-command"
                    data-tip="BOSS runs a local server only from a reviewed launch template, in its own container">
                <Terminal size={12} aria-hidden="true" /> Runs from a template ·{" "}
                <button type="button" className="kit-link" onClick={() => setBrowse(true)}>Browse all</button>
              </span>
            ) : null}
          </label>
          <label className="kit-field">
            <span className="kit-field-label">Name</span>
            <input type="text" value={form.name} spellCheck={false} data-testid="kit-custom-name"
                   aria-invalid={error?.field === "name" || undefined}
                   onChange={(e) => { setNamed(true); set({ name: e.target.value }); }} />
            {error?.field === "name" ? <span className="kit-field-err" role="alert" data-tip={error.message}>{shortError(error.message)}</span> : null}
          </label>
          {!entry ? (
            <div className="kit-field">
              <span className="kit-field-label">Sign-in</span>
              <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label="Sign-in">
                {(["none", "bearer", "header", "oauth"] as const).map((t) => (
                  <button key={t} type="button" role="radio" aria-checked={form.auth === t} data-testid={`kit-custom-auth-${t}`}
                          data-tip={t === "oauth" ? "Sign in with the provider's account; tokens stay in BOSS's vault" : undefined}
                          onClick={() => set({ auth: t })}>{AUTH_WORD[t]}</button>
                ))}
              </span>
            </div>
          ) : null}
          {form.auth === "header" && !entry ? (
            <label className="kit-field">
              <span className="kit-field-label">Header name</span>
              <input type="text" value={form.header} spellCheck={false} placeholder="X-API-Key" data-testid="kit-custom-header"
                     onChange={(e) => set({ header: e.target.value })} />
            </label>
          ) : null}
          {form.auth === "bearer" || form.auth === "header" ? (
            <label className="kit-field">
              <span className="kit-field-label">Token</span>
              <input type="password" value={form.secret} autoComplete="new-password" data-testid="kit-custom-secret"
                     aria-invalid={error?.field === "secret" || undefined}
                     onChange={(e) => set({ secret: e.target.value })} />
              <span className="kit-faint kit-vault" data-testid="kit-add-vault"
                    data-tip="Written to the secret store; only a reference and the last four are kept">
                <Lock size={12} aria-hidden="true" /> Saved to the vault
                {entry?.token_url ? <> · <a className="kit-link" href={entry.token_url} target="_blank" rel="noreferrer noopener"
                                            data-tip={entry.scopes_tip ? `Needs scopes: ${entry.scopes_tip}` : undefined}>Get a token ↗</a></> : null}
              </span>
            </label>
          ) : null}
          <details className="kit-more" data-testid="kit-add-advanced">
            <summary>Advanced</summary>
            <div className="kit-field">
              <span className="kit-field-label" data-tip="What the server's tools can do; sets when agents must ask">Risk</span>
              <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label="Risk">
                {RISKS.map((r) => (
                  <button key={r.tier} type="button" role="radio" aria-checked={form.risk === r.tier} data-testid={`kit-add-risk-${r.tier}`}
                          onClick={() => set({ risk: r.tier })}>{r.label}</button>
                ))}
              </span>
            </div>
            {form.auth === "oauth" ? (
              <div data-testid="kit-custom-oauth-client">
                <label className="kit-field">
                  <span className="kit-field-label" data-tip="Only when the provider does not register apps itself">Your own OAuth app: client ID</span>
                  <input type="text" value={form.clientId} spellCheck={false} data-testid="kit-custom-client-id"
                         onChange={(e) => set({ clientId: e.target.value })} />
                </label>
                <label className="kit-field">
                  <span className="kit-field-label">Client secret</span>
                  <input type="password" value={form.secret} autoComplete="new-password" data-testid="kit-custom-client-secret"
                         onChange={(e) => set({ secret: e.target.value })} />
                </label>
              </div>
            ) : null}
          </details>

          {form.auth !== "oauth" ? (
            <div className="kit-actions">
              <GatedButton permission="mcp_server:review" type="button" className="kit-btn" data-testid="kit-add-test"
                           disabled={!canTest} onClick={() => void runTest()}>
                {testing ? <span className="kit-spin" aria-hidden="true" /> : <PlugsConnected size={14} aria-hidden="true" />}
                {testing ? "Testing…" : "Test connection"}
              </GatedButton>
              {test?.ok ? (
                <span className="kit-pill" data-tone="green" data-testid="kit-add-ok">
                  <Check size={12} aria-hidden="true" /> Connected · {plural(test.tools.length, "tool")}
                </span>
              ) : test && !test.ok ? (
                <span className="kit-field-err" role="alert" data-tip={test.error ?? undefined} data-testid="kit-add-error">
                  {shortError(test.error)}
                </span>
              ) : null}
            </div>
          ) : null}
          {test?.ok && test.tools.length ? (
            <div className="kit-chips" data-testid="kit-add-tools" aria-label="Tools found">
              {test.tools.slice(0, 12).map((t) => (
                <span key={t.name} className="kit-chip" data-tip={t.description || t.name} data-user-text>{t.name}</span>
              ))}
              {test.tools.length > 12 ? <span className="kit-faint">+{test.tools.length - 12}</span> : null}
            </div>
          ) : null}

          {grants ? (
            <div className="kit-field">
              <span className="kit-field-label" data-tip="Who may use this server's tools; change it any time">Give to agents</span>
              <div className="kit-chips" data-testid="kit-add-agents">
                {(everyone ? ordered : ordered.slice(0, shownCount)).map((c) => {
                  const on = agents.has(c.role_key);
                  return (
                    <button key={c.role_key} type="button" className="kit-chip kit-chip-btn kit-agent-chip" aria-pressed={on}
                            disabled={!assignGate.allowed} title={assignGate.allowed ? undefined : assignGate.reason}
                            data-tip={c.display_name ?? c.role_key} data-testid={`kit-add-agent-${c.role_key}`}
                            onClick={() => setAgents((s) => { const n = new Set(s); if (on) n.delete(c.role_key); else n.add(c.role_key); return n; })}>
                      <AgentAvatar agent={c.role_key} size={16} aiBadge={false} />{agentName(c.role_key)}
                    </button>
                  );
                })}
                {!crew.length ? <span className="kit-faint">No agents yet</span> : null}
                {!everyone && crew.length > shownCount ? (
                  <button type="button" className="kit-link" data-testid="kit-add-agents-all" onClick={() => setEveryone(true)}>
                    All {crew.length}
                  </button>
                ) : null}
              </div>
            </div>
          ) : null}

          <div className="kit-modal-foot">
            <button type="button" className="kit-btn" onClick={onClose}>Cancel</button>
            <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-primary" data-testid="kit-add-save"
                         disabled={!canAdd} data-tip={canAdd ? undefined : form.auth === "oauth" ? "Name and URL first" : "Test the connection first"}
                         onClick={() => void add()}>
              {busy ? <span className="kit-spin" aria-hidden="true" /> : <Check size={14} aria-hidden="true" />}
              {busy ? "Adding…" : form.auth === "oauth" ? "Add & sign in" : "Add server"}
            </GatedButton>
          </div>
        </div>
      )}
    </ModalShell>
  );
}
