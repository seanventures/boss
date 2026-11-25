"use client";

/**
 * The MCP servers section of Skills & MCP (docs/design/SKILLS_AND_MCP_HOME.md).
 *
 * One row per server: status, tools, who has it, the last error in plain words, and Test, Edit,
 * Remove. Edit opens the server's drawer, which keeps every option it had (sign-in, headers, tool
 * policies, review, agents, data boundary). `⋯` holds the rarely used: Test all, Import, Export,
 * Gateways. A fresh install shows what to do next, never an empty box.
 */
import { GatedButton } from "../../components/gated";
import { ReadFailure } from "../../components/locked-panel";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { ArrowClockwise, DotsThree, PencilSimple, Plugs, Plus, Trash, Warning } from "@phosphor-icons/react";

import type { Ability } from "../../lib/kit-types";
import { mcpApi, type CrewMember, type McpServer } from "../../lib/kit-api";
import { showToast } from "../client-forms";
import { HolderStack, StatusPill } from "./ability-card";
import { GatewaysSheet } from "./gateways-sheet";
import { ImportSheet } from "./import-sheet";
import { plural, PROVENANCE, riskOf, shortError, TIP } from "./kit-copy";
import { McpDrawer, serverStatus } from "./mcp-drawer";
import { serverProvenance } from "./library";

/** How many rows show before "Show all". */
export const FIRST_ROWS = 6;

export function McpServersSection({ projectId, actor, crew, abilities, query, openId, onOpen, onAdd, version, onKitChanged }: {
  projectId: string; actor: string; crew: CrewMember[]; abilities: Ability[];
  /** The page's Find. */
  query: string;
  /** The server whose drawer is open (`&mcp=`). */
  openId: string | null;
  onOpen: (id: string | null) => void;
  /** Open Add MCP server (the page owns the sheet). */
  onAdd: () => void;
  /** Bumped by the page after an add: read the list again. */
  version: number;
  onKitChanged: () => void;
}) {
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [error, setError] = useState<{ text: string; status: number } | null>(null);
  const [testing, setTesting] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<string | null>(null);
  const [menu, setMenu] = useState(false);
  const [all, setAll] = useState(false);
  const [importing, setImporting] = useState(false);
  const [gateways, setGateways] = useState(false);
  // THE LATEST READ WINS: a slow first read must not put back rows a later one changed.
  const seq = useRef(0);

  const load = useCallback(async (check: "none" | "stale" | "now" = "stale") => {
    const mine = ++seq.current;
    const r = await mcpApi.list(projectId, check);
    if (mine !== seq.current) return null;
    if (!r.ok) { setError({ text: r.error, status: r.status ?? 0 }); return null; }
    setServers(r.data.items);
    setError(null);
    return r.data.items;
  }, [projectId]);

  useEffect(() => { void load(version ? "none" : "stale"); }, [load, version]);

  /** Probe now (the list's `check=now`) and say what this server answered. */
  const test = async (ids: string[]) => {
    setTesting(new Set(ids));
    const items = await load("now");
    setTesting(new Set());
    if (!items) { showToast("error", "Test failed, can't reach BOSS"); return; }
    const tested = items.filter((s) => ids.includes(s.id));
    const failed = tested.filter((s) => s.reachable === false);
    if (ids.length === 1 && tested[0]) {
      const s = tested[0];
      if (s.reachable === false) showToast("error", `${s.name}: ${shortError(s.last_error)}`);
      else showToast("success", `${s.name} connected, ${plural(s.tools.length, "tool")}`);
    } else {
      showToast(failed.length ? "error" : "success", failed.length ? `${plural(failed.length, "server")} failing` : "All servers connected");
    }
  };

  const remove = async (s: McpServer) => {
    setConfirm(null);
    const holders = (s.references?.agents ?? []).length;
    const r = await mcpApi.remove(projectId, actor, s.id, holders > 0);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `Removed ${s.name}`);
    if (openId === s.id) onOpen(null);
    void load("none"); onKitChanged();
  };

  const q = query.trim().toLowerCase();
  const shown = (servers ?? []).filter((s) => !q || `${s.name} ${s.description ?? ""} ${s.url}`.toLowerCase().includes(q));
  const rows = all || q ? shown : shown.slice(0, FIRST_ROWS);
  const open = (servers ?? []).find((s) => s.id === openId) ?? null;

  return (
    <section className="kit-lib kit-sec" aria-label="MCP servers" data-testid="kit-mcp-section" id="kit-mcp-section"
             data-drawer={open ? "open" : undefined}>
      <header className="kit-sec-head">
        <h2 className="kit-sec-title" data-tip="Servers whose tools your agents can call">
          <Plugs size={16} weight="duotone" aria-hidden="true" className="kit-icon" /> MCP servers
          {servers ? <span className="kit-sec-count" data-testid="kit-mcp-count">{servers.length}</span> : null}
        </h2>
        <span className="kit-spacer" />
        <span className="kit-confirm-wrap">
          <button type="button" className="kit-icon-btn" aria-label="More for MCP servers" data-tip="More"
                  data-testid="kit-mcp-more" aria-expanded={menu} onClick={() => setMenu((m) => !m)}><DotsThree size={18} /></button>
          {menu ? (
            <span className="kit-pop kit-menu" role="menu" data-testid="kit-mcp-menu">
              <button type="button" role="menuitem" data-testid="kit-mcp-test-all" disabled={!servers?.length}
                      onClick={() => { setMenu(false); void test((servers ?? []).map((s) => s.id)); }}>Test all</button>
              <button type="button" role="menuitem" data-testid="kit-lib-import" onClick={() => { setMenu(false); setImporting(true); }}>Import</button>
              <button type="button" role="menuitem" data-testid="kit-lib-export" onClick={() => { setMenu(false); exportServers(servers ?? []); }}>Export</button>
              <button type="button" role="menuitem" data-testid="kit-lib-gateways"
                      data-tip="Serve chosen servers to Claude Code, Cursor and other MCP clients"
                      onClick={() => { setMenu(false); setGateways(true); }}>Gateways</button>
            </span>
          ) : null}
        </span>
      </header>

      {error ? (
        <ReadFailure testId="kit-mcp-error" retryTestId="kit-mcp-retry" problem={error} onRetry={() => void load("none")} />
      ) : null}

      {servers === null ? (error ? null : <div className="kit-row kit-skeleton" aria-busy="true" />) : servers.length === 0 ? (
        <div className="kit-empty" data-testid="kit-mcp-empty">
          <Plugs size={28} weight="duotone" aria-hidden="true" className="kit-icon" />
          <span className="kit-empty-title">No MCP servers yet</span>
          <span className="kit-faint" data-tip="GitHub, Jira, Slack, your own tools: anything that speaks MCP">Connect your team&apos;s tools</span>
          <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-primary" data-testid="kit-mcp-empty-add"
                       onClick={onAdd}><Plus size={14} aria-hidden="true" /> Add MCP server</GatedButton>
        </div>
      ) : (
        <ul className="kit-list kit-mcp-list" data-testid="kit-mcp-list">
          {rows.length === 0 ? (
            <li className="kit-list-empty" data-testid="kit-mcp-nomatch"><span>No matches</span></li>
          ) : null}
          {rows.map((s) => {
            const holders = (s.references?.agents ?? []).map((a) => a.role_key);
            const status = testing.has(s.id) ? "testing" : serverStatus(s, holders.length);
            const prov = PROVENANCE[serverProvenance(s)];
            const risk = riskOf(s.risk_tier);
            return (
              <li key={s.id} className="kit-srv-row" data-testid={`kit-mcp-${s.name}`} data-open={openId === s.id || undefined}>
                <button type="button" className="kit-srv-name" data-tip={`${s.description ?? s.url} · ${risk.label}: ${risk.tip}`}
                        onClick={() => onOpen(s.id)} data-testid={`kit-mcp-open-${s.name}`}>
                  <span data-user-text>{s.name}</span>
                </button>
                <StatusPill status={status} tip={s.last_error ?? (status === "review" ? TIP.review : undefined)} />
                <span className="kit-chip kit-srv-prov" data-tip={prov.tip}>{prov.label}</span>
                <span className="kit-faint kit-srv-tools" data-testid={`kit-mcp-tools-${s.name}`}>{plural(s.tools.length, "tool")}</span>
                <span className="kit-srv-who"><HolderStack holders={holders} max={3} size={20} /></span>
                <span className="kit-srv-err" data-testid={`kit-mcp-err-${s.name}`}>
                  {s.reachable === false && s.last_error ? (
                    <span className="kit-err" data-tip={s.last_error}><Warning size={12} aria-hidden="true" /> {shortError(s.last_error)}</span>
                  ) : null}
                </span>
                <span className="kit-srv-acts">
                  <button type="button" className="kit-btn kit-btn-sm" aria-label={`Test ${s.name}`} data-tip="Ask the server now"
                          data-testid={`kit-mcp-test-${s.name}`} disabled={testing.size > 0} onClick={() => void test([s.id])}>
                    <ArrowClockwise size={14} aria-hidden="true" /><span className="kit-btn-word">Test</span>
                  </button>
                  <button type="button" className="kit-btn kit-btn-sm" aria-label={`Edit ${s.name}`} data-tip="Sign-in, tools, agents"
                          data-testid={`kit-mcp-edit-${s.name}`} onClick={() => onOpen(s.id)}>
                    <PencilSimple size={14} aria-hidden="true" /><span className="kit-btn-word">Edit</span>
                  </button>
                  <span className="kit-confirm-wrap">
                    <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-sm kit-btn-danger"
                                 aria-label={`Remove ${s.name}`} data-tip="Remove this server"
                                 data-testid={`kit-mcp-remove-${s.name}`} onClick={() => setConfirm(s.id)}>
                      <Trash size={14} aria-hidden="true" /><span className="kit-btn-word">Remove</span>
                    </GatedButton>
                    {confirm === s.id ? (
                      <span className="kit-pop" role="dialog" aria-label="Confirm remove" data-testid={`kit-mcp-remove-confirm-${s.name}`}>
                        <strong>Remove {s.name}?</strong>
                        {holders.length ? <span>Agents lose {s.name}</span> : null}
                        <span className="kit-pop-actions">
                          <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-danger kit-btn-sm"
                                       data-testid={`kit-mcp-remove-yes-${s.name}`} onClick={() => void remove(s)}>
                            <Trash size={14} aria-hidden="true" /> Remove
                          </GatedButton>
                          <button type="button" className="kit-btn kit-btn-sm" onClick={() => setConfirm(null)}>Cancel</button>
                        </span>
                      </span>
                    ) : null}
                  </span>
                </span>
              </li>
            );
          })}
          {!q && shown.length > FIRST_ROWS ? (
            <li className="kit-list-empty">
              <button type="button" className="kit-link" data-testid="kit-mcp-show-all" onClick={() => setAll((a) => !a)}>
                {all ? "Show fewer" : `Show all ${shown.length}`}
              </button>
            </li>
          ) : null}
        </ul>
      )}

      {open ? (
        <McpDrawer projectId={projectId} actor={actor} server={open} crew={crew} abilities={abilities}
                   onClose={() => onOpen(null)}
                   onChanged={(next) => {
                     if (next) setServers((list) => (list ?? []).map((x) => (x.id === next.id ? next : x)));
                     void load("none"); onKitChanged();
                   }}
                   onRemoved={() => { onOpen(null); void load("none"); onKitChanged(); }} />
      ) : null}
      {gateways ? <GatewaysSheet projectId={projectId} actor={actor} servers={servers ?? []} onClose={() => setGateways(false)} /> : null}
      {importing ? (
        <ImportSheet projectId={projectId} actor={actor} onClose={() => setImporting(false)}
                     onImported={() => { setImporting(false); void load("none"); onKitChanged(); }} />
      ) : null}
    </section>
  );
}

/** CONFIG AS CODE, never a secret: a server's sign-in is exported as its kind, not its token. */
export function exportServers(servers: McpServer[]): void {
  const data = { mcpServers: Object.fromEntries(servers.map((s) => [s.name, {
    type: "http", url: s.url, auth: { type: s.auth.type, header_name: s.auth.header_name },
    risk_tier: s.risk_tier, tool_policy: s.tool_policy }])) };
  download(data, "mcp.json");
}

export function download(data: unknown, name: string): void {
  try {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch { /* nothing to download into */ }
}
