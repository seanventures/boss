"use client";

/**
 * SETUP › TICKET SYSTEMS (BOSSAPP-1047, wireframe T1): the one place a ticket system is
 * connected and configured. Each connection says, in one line each: which of its projects feed
 * which BOSS project, what BOSS writes back, the new-ticket rule (a ticket labelled `boss` is
 * asked about first unless the system is set to start automatically) and its status. Add opens
 * the drawer (T2, add-ticket-system.tsx); a connection's ⋯ re-tests it, opens its settings in the
 * same drawer, or disconnects it (its work items keep their bindings, with sync paused).
 * "Where tickets show up" links to the four doors a connected system opens. Skills & MCP's File
 * tickets points here (skills/connection-section.tsx), so there is one surface to configure.
 *
 * Self-loading, like the gateways and pools rows. Tokens never reach this page: the API answers
 * `last4` and a fingerprint only.
 */
import React, { useCallback, useEffect, useState } from "react";
import { ArrowSquareOut, ChatCircleDots, GitPullRequest, Kanban, Plus, ShieldCheck, Sparkle } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { SectionLabel } from "../section-label";
import { ProviderMono, TkEmpty, TkReadState } from "../tickets/ticket-parts";
import {
  disconnectTicketSystem, pickupWords, readConnections, testTicketSystem, writesWords,
  type Connection, type Read,
} from "../../lib/tickets";
import { AddTicketSystem } from "./add-ticket-system";
import { RowMenu } from "./parts";

export const CONNECTORS_DOC = "https://github.com/seanventures/boss/blob/main/docs/infra/TICKET_CONNECTORS.md";

const host = (url: string) => url.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
const STATUS: Record<Connection["status"], { word: string; tone: string }> = {
  connected: { word: "Connected", tone: "green" }, disconnected: { word: "Disconnected", tone: "red" }, not_connected: { word: "Needs a test", tone: "amber" },
};

/** "LEND → this project": a connection's mappings in words (all of its projects when none). */
function mapWords(c: Connection, projectId: string, names: Record<string, string>): string[] {
  if (!c.settings.mappings.length) return [c.settings.project_keys.length ? `${c.settings.project_keys.join(", ")} → this project` : "All projects → this project"];
  return c.settings.mappings.map((m) => `${m.external_project} → ${m.boss_project_id === projectId ? "this project" : names[m.boss_project_id] ?? m.boss_project_id}${m.default ? " (default)" : ""}`);
}

export function TicketSystemsRow({ projectId, focused = false }: { projectId: string; focused?: boolean }) {
  const [read, setRead] = useState<Read<Connection[]> | null>(null);
  const [said, setSaid] = useState<{ text: string; tone: "green" | "red" } | null>(null);
  const [drawer, setDrawer] = useState<{ mode: "add" } | { mode: "edit"; connection: Connection } | null>(null);
  const [confirm, setConfirm] = useState<Connection | null>(null);
  const manage = useGate("integration:manage", projectId);

  const load = useCallback(async () => { setRead(await readConnections(projectId)); }, [projectId]);
  useEffect(() => { setRead(null); void load(); }, [load]);

  const test = async (c: Connection) => {
    setSaid(null);
    const r = await testTicketSystem(c.id);
    const t = (r.body ?? {}) as { test?: { ok?: boolean; account?: string } };
    setSaid(r.ok ? { text: `${c.label} works${t.test?.account ? ` · signed in as ${t.test.account}` : ""}.`, tone: "green" }
                 : { text: r.error ?? "The test failed.", tone: "red" });
    if (r.ok) void load();
  };
  const disconnect = async (c: Connection) => {
    setConfirm(null);
    const r = await disconnectTicketSystem(c.id);
    setSaid(r.ok ? { text: `${c.label} disconnected. Its work items keep their links, with sync paused.`, tone: "green" }
                 : { text: r.error ?? "Not disconnected.", tone: "red" });
    if (r.ok) void load();
  };

  const items = read?.ok ? read.data : [];
  const live = items.filter((c) => c.status !== "disconnected");
  const names: Record<string, string> = {};
  return (
    <section className="su-row tk-setup" data-testid="su-row-ticket_systems" aria-label="Ticket systems" id="su-ticket_systems"
             data-focused={focused || undefined}>
      <div className="su-row-label">
        <SectionLabel as="span" tip="Connect once. Then paste a ticket anywhere, or ask BOSS to implement it.">Ticket systems</SectionLabel>
      </div>
      <div className="su-row-body su-stack">
        <div className="tk-row">
          <span className="su-faint" title="Connect once. Then paste a ticket anywhere, or ask BOSS to implement it.">Connect once, then paste any ticket</span>
          <span className="tk-spacer" />
          <GatedButton permission="integration:manage" projectId={projectId} className="su-btn su-btn-sm" data-testid="tk-add"
                       title="Jira, Plane, GitHub Issues, Linear or Azure DevOps" onClick={() => setDrawer({ mode: "add" })}>
            <Plus size={13} aria-hidden /> Add ticket system</GatedButton>
        </div>
        <TkReadState read={read} what="Ticket systems" onRetry={() => { setRead(null); void load(); }} testid="tk-systems" />
        {read?.ok && !items.length ? (
          <TkEmpty title="No ticket system yet" line="BOSS reads tickets, writes progress back" testid="tk-systems-empty">
            {manage.allowed ? <button type="button" className="tk-btn" data-kind="primary" onClick={() => setDrawer({ mode: "add" })} data-testid="tk-add-empty">
              <Plus size={13} aria-hidden /> Add ticket system</button> : null}
          </TkEmpty>
        ) : null}
        {items.length ? (
          <div className="tk-systems" role="table" aria-label="Connected ticket systems" data-testid="tk-systems">
            <div className="tk-systems-head" role="row">
              <span role="columnheader">System</span><span role="columnheader">Projects → BOSS project</span>
              <span role="columnheader">Writes back</span><span role="columnheader">New tickets</span><span role="columnheader">Status</span><span />
            </div>
            {items.map((c) => {
              const st = STATUS[c.status];
              const managed = Boolean(c.managed_from);
              return (
                <div key={c.id} className="tk-system" role="row" data-testid={`tk-system-${c.id}`} data-status={c.status}>
                  <span role="cell" className="tk-system-name">
                    <ProviderMono provider={c.provider} size={26} />
                    <span className="tk-stack" style={{ gap: 0 }}>
                      <b>{c.label}</b>
                      <span className="tk-faint" title={c.account ? `BOSS acts as ${c.account}${c.last4 ? ` · token …${c.last4}` : ""}` : undefined}>
                        {host(c.base_url) || "—"}</span>
                    </span>
                  </span>
                  <span role="cell" className="tk-cell" data-label="Projects">
                    {mapWords(c, projectId, names).map((m) => <span key={m} className="tk-mono-text">{m}</span>)}
                    {managed ? <span className="tk-faint">Managed from another project</span> : null}
                  </span>
                  <span role="cell" className="tk-cell" data-label="Writes back">{writesWords(c.settings.write_back)}</span>
                  <span role="cell" className="tk-cell" data-label="New tickets" data-testid={`tk-pickup-${c.id}`}>{pickupWords(c.settings)}</span>
                  <span role="cell" className="tk-cell" data-label="Status">
                    <span className="tk-status" data-tone={st.tone}><i aria-hidden /> {st.word}</span>
                    {c.bindings.paused ? <span className="tk-faint">{c.bindings.paused} paused</span> : null}
                  </span>
                  <span role="cell" className="tk-cell-menu">
                    <RowMenu label={`${c.label} actions`} testid={`tk-menu-${c.id}`} items={[
                      { label: "Test", testid: `tk-test-${c.id}`, disabled: !manage.allowed || c.status === "disconnected", onSelect: () => void test(c) },
                      { label: "Settings", testid: `tk-edit-${c.id}`, disabled: !manage.allowed || managed, onSelect: () => setDrawer({ mode: "edit", connection: c }) },
                      { label: "Disconnect", testid: `tk-off-${c.id}`, danger: true, disabled: !manage.allowed || c.status === "disconnected" || managed,
                        onSelect: () => setConfirm(c) },
                    ]} />
                  </span>
                </div>
              );
            })}
          </div>
        ) : null}
        {confirm ? (
          <div className="tk-confirm" role="alertdialog" aria-label={`Disconnect ${confirm.label}?`} data-testid="tk-off-confirm">
            <span title={confirm.bindings.total ? `${confirm.bindings.total} work item${confirm.bindings.total === 1 ? "" : "s"} keep their links, with sync paused.` : "Nothing is deleted in the ticket system."}>Disconnect {confirm.label}? Links stay, sync pauses.</span>
            <GatedButton permission="integration:manage" projectId={projectId} className="su-btn su-btn-sm su-btn-danger" data-testid="tk-off-yes"
                         onClick={() => void disconnect(confirm)}>Disconnect</GatedButton>
            <button type="button" className="su-btn su-btn-sm" onClick={() => setConfirm(null)} data-testid="tk-off-no">Keep it</button>
          </div>
        ) : null}
        {said ? <div role="status" className={said.tone === "red" ? "tk-error" : "tk-ok"} data-testid="tk-said">{said.text}</div> : null}

        {live.length ? (
          <div className="tk-where" aria-label="Where tickets show up" data-testid="tk-where">
            <span className="tk-lbl">Where tickets show up</span>
            <div className="tk-where-grid">
              <a href={`?area=project&project_id=${encodeURIComponent(projectId)}&tab=work&compose=1`} data-testid="tk-where-new">
                <Sparkle size={16} aria-hidden /><b>New work</b><span>Paste a key or link</span></a>
              <a href="?area=agent" data-testid="tk-where-ask"><ChatCircleDots size={16} aria-hidden /><b>Ask BOSS</b><span>“Implement LEND-142”</span></a>
              <a href={`?area=project&project_id=${encodeURIComponent(projectId)}&tab=work`} data-testid="tk-where-work">
                <Kanban size={16} aria-hidden /><b>Project › Work</b><span>Tickets waiting for BOSS</span></a>
              <a href={`?area=project&project_id=${encodeURIComponent(projectId)}&tab=work`} data-testid="tk-where-runs">
                <GitPullRequest size={16} aria-hidden /><b>Runs and PRs</b><span>Status and comments sync back</span></a>
            </div>
          </div>
        ) : null}
        <div className="tk-faint tk-vault"><ShieldCheck size={13} aria-hidden /> <span title="Tokens are stored in BOSS's vault. Agents never see them; BOSS makes every call for them.">Tokens stay in BOSS&apos;s vault</span>{" "}
          <a className="tk-linkbtn" href={CONNECTORS_DOC} target="_blank" rel="noreferrer noopener" style={{ fontSize: "inherit" }}>How adapters work <ArrowSquareOut size={11} aria-hidden /></a></div>
      </div>
      {drawer ? (
        <AddTicketSystem projectId={projectId} connection={drawer.mode === "edit" ? drawer.connection : null}
                         onClose={() => setDrawer(null)}
                         onSaved={(words) => { setDrawer(null); setSaid({ text: words, tone: "green" }); void load(); }}
                         onChanged={() => void load()} />
      ) : null}
    </section>
  );
}
