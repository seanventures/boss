"use client";

/**
 * SETUP › TICKET SYSTEMS (BOSSAPP-1047, wireframe T1): the one place a ticket system is
 * connected and configured.
 *
 * ONE ORG-WIDE LIST, FILTERED PER PROJECT (operator, 2026-11-15: "I think we should show the jira
 * irrespective of the project - inside the project we can filter for that project, this is the
 * global setting"). Two views of the same connections:
 *   * This project — what this project can use (its own, or shared with the org), each with the
 *     keys that map here; the org's other connections show greyed with "Share it" (one press to
 *     the switch) or whom to ask. Never "No ticket system yet" while the org has one.
 *   * All in org — every connection of the org, wherever it was added: site, where it was added,
 *     whose token BOSS uses, mappings, sharing and status; managed here by whoever may manage it
 *     at its home (integration:manage there, which the API answers per row as `can_manage`).
 *
 * SHARING IS ONE CONTROL ON THE ROW (operator: "what is option just dont makes sense, also the
 * position is horible"): a Sharing pill in its own column — "Shared with org" or "Only <project>"
 * — whose press asks once, in the whose-token words of PR #137, and then switches it.
 *
 * Self-loading, like the gateways and pools rows. Tokens never reach this page: the API answers
 * `last4` and a fingerprint only, and those only to a person who manages the connection.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { ArrowSquareOut, Buildings, FolderSimple, Globe, LockSimple, Plus, ShareNetwork, ShieldCheck, Sparkle } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { SectionLabel } from "../section-label";
import { NotSharedLine, ProviderMono, TkEmpty, TkReadState } from "../tickets/ticket-parts";
import {
  disconnectTicketSystem, notSharedOf, pickupWords, readOrgConnections, readOrgName, readTicketSetup, saveTicketSettings,
  shareWords, testTicketSystem, writesWords, type Connection, type Read,
} from "../../lib/tickets";
import { AddTicketSystem } from "./add-ticket-system";
import { RowMenu } from "./parts";

export const CONNECTORS_DOC = "https://github.com/seanventures/boss/blob/main/docs/infra/TICKET_CONNECTORS.md";

const host = (url: string) => url.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
const STATUS: Record<Connection["status"], { word: string; tone: string }> = {
  connected: { word: "Connected", tone: "green" }, disconnected: { word: "Disconnected", tone: "red" }, not_connected: { word: "Needs a test", tone: "amber" },
  not_shared: { word: "Not shared here", tone: "amber" },
};
type View = "here" | "all";

/** "LEND → this project": a connection's mappings in words (all of its projects when none). */
export function mapWords(c: Connection, projectId: string, names: Record<string, string>): string[] {
  // No mapping: everything goes to the connection's HOME project (this one, or the project an
  // org-level connection was added from).
  const home = c.home_project && c.home_project.id !== projectId ? c.home_project.name : "this project";
  if (!c.settings.mappings.length) return [c.settings.project_keys.length ? `${c.settings.project_keys.join(", ")} → ${home}` : `All projects → ${home}`];
  return c.settings.mappings.map((m) => `${m.external_project} → ${m.boss_project_id === projectId ? "this project" : names[m.boss_project_id] ?? m.boss_project_id}${m.default ? " (default)" : ""}`);
}

/** The ticket-system project keys whose tickets land in `projectId` ("KAN, SAM1"); every key of a
 *  project's own connection with no mapping (everything comes here); "" when none do. */
export function mapsHere(c: Connection, projectId: string): string {
  const keys = c.settings.mappings.filter((m) => m.boss_project_id === projectId).map((m) => m.external_project);
  if (keys.length) return keys.join(", ");
  const homeHere = !c.home_project || c.home_project.id === projectId;
  if (!c.settings.mappings.length && homeHere && !c.managed_from) return c.settings.project_keys.length ? c.settings.project_keys.join(", ") : "All";
  return "";
}

/** Where the connection lives, from this project's point of view. */
const homeName = (c: Connection, projectId: string) =>
  c.home_project && c.home_project.id !== projectId ? c.home_project.name : "this project";

/** One line for Setup's collapsed section, about THIS project: "Jira · shared · KAN here", or
 *  "Jira · shared · maps elsewhere" when none of its keys land here (never a count of mappings
 *  that says nothing about this project). */
export function ticketSummary(usable: Connection[], hidden: Connection[], projectId: string): string {
  if (!usable.length && hidden.length) {
    const c = hidden[0];
    return `${c.label} in ${c.home_project?.name ?? "another project"} · not shared here`;
  }
  if (!usable.length) return "None yet";
  const c = usable.find((x) => mapsHere(x, projectId)) ?? usable[0];
  const shared = c.reach === "org" || c.settings.share_with_org;
  const here = mapsHere(c, projectId);
  const more = usable.length > 1 ? ` · +${usable.length - 1} more` : "";
  return `${c.label} · ${shared ? "shared" : "this project only"} · ${here ? `${here === "All" ? "all keys" : here} here` : "maps elsewhere"}${more}`;
}

/** The ticket-system project keys a connection knows (its mappings, then its project list). */
export const keysOf = (c: Connection): string[] =>
  [...new Set([...c.settings.mappings.map((m) => m.external_project), ...c.settings.project_keys])].filter(Boolean);

const readParam = (key: string): string => {
  try { return new URLSearchParams(window.location.search).get(key) ?? ""; } catch { return ""; }
};

export function TicketSystemsRow({ projectId, focused = false, onSummary }: {
  projectId: string; focused?: boolean; onSummary?: (words: string) => void;
}) {
  const [view, setView] = useState<View>(() => (typeof window !== "undefined" && readParam("tk") === "all" ? "all" : "here"));
  const [read, setRead] = useState<Read<Connection[]> | null>(null);
  const [projectName, setProjectName] = useState("");
  const [orgRead, setOrgRead] = useState<Read<Connection[]> | null>(null);
  const [said, setSaid] = useState<{ text: string; tone: "green" | "red" } | null>(null);
  const [drawer, setDrawer] = useState<{ mode: "add" } | { mode: "edit"; connection: Connection } | null>(null);
  const [confirm, setConfirm] = useState<Connection | null>(null);
  /** The connection whose sharing is being asked (from a "Share it" link, or the Sharing pill). */
  const [asking, setAsking] = useState<{ c: Connection; on: boolean } | null>(null);
  const [spot, setSpot] = useState<string>(() => (typeof window !== "undefined" ? readParam("share") : ""));
  const manage = useGate("integration:manage", projectId);
  const [orgName, setOrgName] = useState("");
  useEffect(() => { void readOrgName().then(setOrgName); }, []);

  const load = useCallback(async () => {
    const r = await readTicketSetup(projectId);
    if (r.ok) setProjectName(r.data.projectName);
    setRead(r.ok ? { ok: true, data: r.data.connections } : r);
  }, [projectId]);
  const loadOrg = useCallback(async () => { setOrgRead(await readOrgConnections(projectId)); }, [projectId]);
  useEffect(() => { setRead(null); void load(); }, [load]);
  useEffect(() => { if (view === "all") { setOrgRead(null); void loadOrg(); } }, [view, loadOrg]);
  const reload = () => { void load(); if (view === "all") void loadOrg(); };

  const all = read?.ok ? read.data : [];
  // Another project's connection NOT shared with this one: shown greyed, never usable here.
  const hidden = all.filter((c) => c.reach === "unshared");
  const items = all.filter((c) => c.reach !== "unshared");
  const orgItems = orgRead?.ok ? orgRead.data : [];
  useEffect(() => { if (read?.ok) onSummary?.(ticketSummary(items, hidden, projectId)); }, [read]); // eslint-disable-line react-hooks/exhaustive-deps

  // "Share it" landed here (?tk=all&share=<id>): ask that connection's sharing once it is listed.
  const asked = useRef("");
  useEffect(() => {
    if (!spot || view !== "all" || asked.current === spot) return;
    const c = orgItems.find((x) => x.id === spot);
    if (!c) return;
    asked.current = spot;
    if (c.can_manage && !c.settings.share_with_org) setAsking({ c, on: true });
    window.setTimeout(() => document.querySelector(`[data-testid="tk-system-${spot.replace(/"/g, "")}"]`)
      ?.scrollIntoView?.({ behavior: "smooth", block: "center" }), 80);
  }, [spot, view, orgItems]);

  const switchView = (next: View, share = "") => {
    setView(next);
    setSaid(null); setAsking(null); setConfirm(null);
    asked.current = "";
    setSpot(share);
    try {
      const url = new URL(window.location.href);
      if (next === "all") url.searchParams.set("tk", "all"); else url.searchParams.delete("tk");
      if (share) url.searchParams.set("share", share); else url.searchParams.delete("share");
      window.history.replaceState(window.history.state, "", url.toString());
    } catch { /* the view still switches */ }
  };

  /** The Sharing pill's one effect, after its confirm (integration:manage at the home). */
  const share = async (c: Connection, on: boolean) => {
    setSaid(null); setAsking(null);
    const r = await saveTicketSettings(c.id, { share_with_org: on });
    setSaid(r.ok ? { text: on ? `${c.label} is shared with the whole org.` : `${c.label} is used by ${homeName(c, projectId)} only.`, tone: "green" }
                 : { text: r.error ?? "Not saved.", tone: "red" });
    if (r.ok) reload();
  };
  const test = async (c: Connection) => {
    setSaid(null);
    const r = await testTicketSystem(c.id);
    const t = (r.body ?? {}) as { test?: { ok?: boolean; account?: string; project_keys?: string[] } };
    // The guide's promise: "You should see your name and the projects you can see."
    const keys = Array.isArray(t.test?.project_keys) ? t.test.project_keys : [];
    const seen = keys.length ? ` · sees ${keys.length} project${keys.length === 1 ? "" : "s"}: ${keys.slice(0, 6).join(", ")}${keys.length > 6 ? "…" : ""}` : "";
    setSaid(r.ok ? { text: `${c.label} works${t.test?.account ? ` · signed in as ${t.test.account}` : ""}${seen}.`, tone: "green" }
                 : { text: r.error ?? "The test failed.", tone: "red" });
    if (r.ok) reload();
  };
  /** "Map KAN here": that key's tickets land in this project (a mirror here; manage at its home). */
  const mapHere = async (c: Connection, key: string) => {
    setSaid(null);
    const others = c.settings.mappings.filter((m) => m.external_project !== key);
    const was = c.settings.mappings.find((m) => m.external_project === key);
    const r = await saveTicketSettings(c.id, { mappings: [...others, { external_project: key, boss_project_id: projectId, default: Boolean(was?.default) }] });
    setSaid(r.ok ? { text: `${key} tickets now land in ${projectName || "this project"}.`, tone: "green" } : { text: r.error ?? "Not saved.", tone: "red" });
    if (r.ok) reload();
  };
  const disconnect = async (c: Connection) => {
    setConfirm(null);
    const r = await disconnectTicketSystem(c.id);
    setSaid(r.ok ? { text: `${c.label} disconnected. Its work items keep their links, with sync paused.`, tone: "green" }
                 : { text: r.error ?? "Not disconnected.", tone: "red" });
    if (r.ok) reload();
  };

  /** Sharing, one control per row: a pill that says the state and, pressed, asks to switch it. */
  const sharePill = (c: Connection, inAll: boolean) => {
    if (c.status === "disconnected") return <span className="tk-faint">—</span>;
    if (c.managed_from) {
      // A copy here of a connection shared from its home: used here, managed there.
      return <span className="tk-pill" data-tone="green" data-testid={`tk-shared-${c.id}`}
                   title="Shared from the project it was added in; its settings live there.">
        <Globe size={12} aria-hidden /> Shared</span>;
    }
    const home = c.home_project && c.home_project.id !== projectId ? c.home_project.name : "this project";
    if (!inAll && c.reach === "org") {
      return <span className="tk-pill" data-tone="green" data-testid={`tk-shared-${c.id}`}
                   title="Connected once for the whole organisation: every project can read and import its tickets. Settings live where it was added.">
        <Globe size={12} aria-hidden /> Shared</span>;
    }
    const on = c.settings.share_with_org;
    const can = inAll ? c.can_manage : manage.allowed;
    const owners = c.home_project?.owners ?? [];
    const label = on ? "Shared with org" : inAll ? "Not shared" : "This project only";
    return (
      <button type="button" className="tk-pill tk-pill-btn" data-tone={on ? "green" : "grey"} data-testid={`tk-share-${c.id}`}
              data-spot={spot === c.id || undefined} aria-pressed={on} disabled={!can}
              title={can ? (on ? "Every project in the org can use it. Press to keep it to its own project."
                               : `Only ${home} uses it. Press to share: ${shareWords(orgName, c.account)}`)
                         : `Ask ${owners.length ? owners.join(" or ") : `an owner of ${home}`} to change who can use it.`}
              onClick={() => { setSaid(null); setConfirm(null); setAsking({ c, on: !on }); }}>
        {on ? <Globe size={12} aria-hidden /> : <LockSimple size={12} aria-hidden />} {label}
      </button>
    );
  };

  const menu = (c: Connection, can: boolean) => {
    const managed = Boolean(c.managed_from);
    const fromElsewhere = view === "here" && c.reach === "org";
    return (
      <RowMenu label={`${c.label} actions`} testid={`tk-menu-${c.id}`} items={[
        { label: "Test", testid: `tk-test-${c.id}`, disabled: !can || c.status === "disconnected", onSelect: () => void test(c) },
        { label: "Settings", testid: `tk-edit-${c.id}`, disabled: !can || managed, onSelect: () => setDrawer({ mode: "edit", connection: c }) },
        { label: "Disconnect", testid: `tk-off-${c.id}`, danger: true, disabled: !can || c.status === "disconnected" || managed || fromElsewhere,
          onSelect: () => setConfirm(c) },
      ]} />
    );
  };

  const nameCell = (c: Connection) => (
    <span role="cell" className="tk-system-name">
      <ProviderMono provider={c.provider} size={26} />
      <span className="tk-stack" style={{ gap: 0 }}>
        <b>{c.label}</b>
        <span className="tk-faint" title={c.account ? `BOSS acts as ${c.account}${c.last4 ? ` · token …${c.last4}` : ""}` : undefined}>
          {host(c.base_url) || "—"}</span>
      </span>
    </span>
  );
  const statusCell = (c: Connection) => {
    const st = STATUS[c.status];
    return (
      <span role="cell" className="tk-cell" data-label="Status">
        <span className="tk-status" data-tone={st.tone}><i aria-hidden /> {st.word}</span>
        {c.bindings?.paused ? <span className="tk-faint">{c.bindings.paused} paused</span> : null}
      </span>
    );
  };

  const hereTable = items.length ? (
    <div className="tk-systems" data-view="here" role="table" aria-label="Ticket systems this project uses" data-testid="tk-systems">
      <div className="tk-systems-head" role="row">
        <span role="columnheader">System</span>
        <span role="columnheader" title="Ticket-system projects whose tickets land in this BOSS project">Maps here</span>
        <span role="columnheader" title="What BOSS writes back, and what it does with a new ticket">Sync</span>
        <span role="columnheader">Sharing</span><span role="columnheader">Status</span><span />
      </div>
      {items.map((c) => {
        const here = mapsHere(c, projectId);
        return (
          <div key={c.id} className="tk-system" role="row" data-testid={`tk-system-${c.id}`} data-status={c.status}>
            {nameCell(c)}
            <span role="cell" className="tk-cell" data-label="Maps here" data-testid={`tk-maps-${c.id}`} title={mapWords(c, projectId, {}).join(" · ")}>
              {here ? <span className="tk-mono-text">{here}</span> : <span className="tk-faint">None · lands in {homeName(c, projectId)}</span>}
              {c.managed_from ? <span className="tk-faint">Managed from another project</span> : null}
            </span>
            <span role="cell" className="tk-cell" data-label="Sync">
              <span title="What BOSS writes back to the ticket">{writesWords(c.settings.write_back)}</span>
              <span className="tk-faint" title="What BOSS does with a new ticket" data-testid={`tk-pickup-${c.id}`}>{pickupWords(c.settings)}</span>
            </span>
            <span role="cell" className="tk-cell" data-label="Sharing">
              {sharePill(c, false)}
              {c.reach === "org" && c.home_project ? <span className="tk-faint" data-testid={`tk-shared-from-${c.id}`}>added in {c.home_project.name}</span> : null}
            </span>
            {statusCell(c)}
            <span role="cell" className="tk-cell-menu">{menu(c, manage.allowed && c.reach !== "org")}</span>
          </div>
        );
      })}
    </div>
  ) : null;

  const allTable = orgItems.length ? (
    <div className="tk-systems" data-view="all" role="table" aria-label="Every ticket system in the org" data-testid="tk-org-systems">
      <div className="tk-systems-head" role="row">
        <span role="columnheader">System</span><span role="columnheader">Added in</span>
        <span role="columnheader">Projects → BOSS project</span><span role="columnheader">Sharing</span>
        <span role="columnheader">Status</span><span />
      </div>
      {orgItems.map((c) => (
        <div key={c.id} className="tk-system" role="row" data-testid={`tk-system-${c.id}`} data-status={c.status}
             data-spot={spot === c.id || undefined} data-usable={c.usable_here || undefined}>
          {nameCell(c)}
          <span role="cell" className="tk-cell" data-label="Added in" data-testid={`tk-home-${c.id}`}>
            <span className="tk-home"><FolderSimple size={13} aria-hidden /> {c.home_project?.id === projectId ? "This project" : c.home_project?.name ?? "—"}</span>
            {c.account ? <span className="tk-faint" title="BOSS reads and writes tickets as this account (its token)">as {c.account}</span> : null}
          </span>
          <span role="cell" className="tk-cell" data-label="Projects">
            {mapWords(c, projectId, c.project_names).map((m) => <span key={m} className="tk-mono-text">{m}</span>)}
          </span>
          <span role="cell" className="tk-cell" data-label="Sharing">
            {sharePill(c, true)}
            {!c.can_manage && !c.settings.share_with_org && !c.usable_here && notSharedOf(c) ? (
              <NotSharedLine info={notSharedOf(c)!} projectId={projectId} compact testid={`tk-org-ask-${c.id}`} />
            ) : null}
          </span>
          {statusCell(c)}
          <span role="cell" className="tk-cell-menu">{menu(c, c.can_manage)}</span>
        </div>
      ))}
    </div>
  ) : null;

  // THE EMPTY STATE NEVER HIDES THE ORG'S CONNECTION: the first unshared one is the headline.
  const firstHidden = hidden[0] ?? null;
  const live = items.filter((c) => c.status !== "disconnected");
  const here = projectName || "this project";
  // WHICH PROJECT (operator: "When I click on New work - which project it would take you?"):
  // a connection whose keys land HERE gets "New work in <this project>"; one whose keys all land
  // elsewhere says where, opens New work THERE, and offers "Map KAN here" to whoever may.
  const landsHere = live.find((c) => mapsHere(c, projectId));
  const elsewhere = landsHere ? null : live[0] ?? null;
  const hereKey = landsHere ? mapsHere(landsHere, projectId).split(", ").find((k) => /^[A-Z][A-Z0-9_]+$/.test(k)) : undefined;
  const awayKeys = elsewhere ? keysOf(elsewhere).slice(0, 3) : [];
  const awayHome = elsewhere?.home_project;

  return (
    <section className="su-row tk-setup" data-testid="su-row-ticket_systems" aria-label="Ticket systems" id="su-ticket_systems"
             data-focused={focused || undefined}>
      <div className="su-row-label">
        <SectionLabel as="span" tip="Connect once for the org. Each project uses the ones shared with it.">Ticket systems</SectionLabel>
      </div>
      <div className="su-row-body su-stack">
        <div className="tk-row">
          <div className="tk-seg" role="tablist" aria-label="Which ticket systems">
            <button type="button" role="tab" aria-selected={view === "here"} data-testid="tk-view-here" onClick={() => switchView("here")}
                    title="The ticket systems this project can use, and the keys that land here">
              <FolderSimple size={13} aria-hidden /> This project</button>
            <button type="button" role="tab" aria-selected={view === "all"} data-testid="tk-view-all" onClick={() => switchView("all")}
                    title="Every ticket system in the org, wherever it was added">
              <Buildings size={13} aria-hidden /> All in org</button>
          </div>
          <span className="tk-spacer" />
          <GatedButton permission="integration:manage" projectId={projectId} className="su-btn su-btn-sm" data-testid="tk-add"
                       title="Jira, Plane, GitHub Issues, Linear or Azure DevOps" onClick={() => setDrawer({ mode: "add" })}>
            <Plus size={13} aria-hidden /> Add ticket system</GatedButton>
        </div>

        {view === "here" ? (
          <>
            <TkReadState read={read} what="Ticket systems" onRetry={() => { setRead(null); void load(); }} testid="tk-systems" />
            {read?.ok && !items.length && firstHidden && notSharedOf(firstHidden) ? (
              <div className="tk-empty tk-empty-org" data-testid="tk-systems-org-only">
                <span className="tk-empty-ico" aria-hidden><ShareNetwork size={18} weight="duotone" /></span>
                <b data-testid="tk-org-headline">{firstHidden.label} is connected in {firstHidden.home_project?.name ?? "another project"}, not shared here yet</b>
                <NotSharedLine info={notSharedOf(firstHidden)!} projectId={projectId} compact testid={`tk-unshared-${firstHidden.id}`}
                               onShare={(id) => switchView("all", id)} />
                {manage.allowed ? <button type="button" className="tk-linkbtn tk-secondary" onClick={() => setDrawer({ mode: "add" })} data-testid="tk-add-empty"
                                          title="Connect a different ticket system for this project">or add another ticket system</button> : null}
              </div>
            ) : null}
            {read?.ok && !items.length && !firstHidden ? (
              <TkEmpty title="No ticket system yet" line="BOSS reads tickets, writes progress back" testid="tk-systems-empty">
                {manage.allowed ? <button type="button" className="tk-btn" data-kind="primary" onClick={() => setDrawer({ mode: "add" })} data-testid="tk-add-empty">
                  <Plus size={13} aria-hidden /> Add ticket system</button> : null}
              </TkEmpty>
            ) : null}
            {hereTable}
            {items.length && hidden.length ? (
              <ul className="tk-list tk-greyed" data-testid="tk-unshared" aria-label="In the org, not shared here">
                {hidden.filter((c) => notSharedOf(c)).map((c) => (
                  <li key={c.id} data-testid={`tk-unshared-row-${c.id}`}>
                    <ProviderMono provider={c.provider} size={22} />
                    <NotSharedLine info={notSharedOf(c)!} projectId={projectId} testid={`tk-unshared-${c.id}`} onShare={(id) => switchView("all", id)} />
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        ) : (
          <>
            <TkReadState read={orgRead} what="The org's ticket systems" onRetry={() => { setOrgRead(null); void loadOrg(); }} testid="tk-org" />
            {orgRead?.ok && !orgItems.length ? (
              <TkEmpty title="No ticket system in the org yet" line="Connect one, then share it with every project" testid="tk-org-empty" />
            ) : null}
            {allTable}
          </>
        )}

        {asking ? (
          <div className="tk-confirm" data-kind="share" role="alertdialog" aria-label={asking.on ? `Share ${asking.c.label} with the whole org?` : `Stop sharing ${asking.c.label}?`}
               data-testid="tk-share-confirm">
            <span>{asking.on ? shareWords(orgName, asking.c.account)
              : `Only ${homeName(asking.c, projectId)} will use ${asking.c.label}. Other projects stop reading tickets through it.`}</span>
            <button type="button" className="su-btn su-btn-sm su-btn-primary" data-testid="tk-share-yes" onClick={() => void share(asking.c, asking.on)}>
              {asking.on ? <><Globe size={13} aria-hidden /> Share with org</> : "Stop sharing"}</button>
            <button type="button" className="su-btn su-btn-sm" onClick={() => setAsking(null)} data-testid="tk-share-no">Cancel</button>
          </div>
        ) : null}
        {confirm ? (
          <div className="tk-confirm" role="alertdialog" aria-label={`Disconnect ${confirm.label}?`} data-testid="tk-off-confirm">
            <span title={confirm.bindings.total ? `${confirm.bindings.total} work item${confirm.bindings.total === 1 ? "" : "s"} keep their links, with sync paused.` : "Nothing is deleted in the ticket system."}>Disconnect {confirm.label}? Links stay, sync pauses.</span>
            <button type="button" className="su-btn su-btn-sm su-btn-danger" data-testid="tk-off-yes" onClick={() => void disconnect(confirm)}>Disconnect</button>
            <button type="button" className="su-btn su-btn-sm" onClick={() => setConfirm(null)} data-testid="tk-off-no">Keep it</button>
          </div>
        ) : null}
        {said ? <div role="status" className={said.tone === "red" ? "tk-error" : "tk-ok"} data-testid="tk-said">{said.text}</div> : null}

        {/* ONE DOOR, NOT FOUR (operator: "When I click on New work - what is the expectation - which
            project it would take you?"). The four "Where tickets show up" cards linked three times
            to the same page and showed a made-up key. One line now names the project. */}
        {view === "here" && landsHere ? (
          <div className="tk-try" data-testid="tk-where">
            <Sparkle size={14} aria-hidden />
            <span>Paste {hereKey ? <>a <b className="tk-mono-text" data-testid="tk-where-key">{hereKey}</b> key</> : "a ticket key"} or a ticket link into New work, or ask BOSS to implement it.</span>
            <a className="tk-linkbtn" href={`?area=project&project_id=${encodeURIComponent(projectId)}&tab=work&compose=1`} data-testid="tk-where-new"
               title={`Opens New work in ${here}.`}>New work in {here} →</a>
          </div>
        ) : null}
        {view === "here" && elsewhere && awayKeys.length ? (
          <div className="tk-try" data-testid="tk-where-elsewhere">
            <Sparkle size={14} aria-hidden />
            <span data-testid="tk-where-lands"><b className="tk-mono-text">{awayKeys.join(", ")}</b> tickets land in {awayHome?.name ?? "another project"}.</span>
            {awayHome && !awayHome.hidden && awayHome.id ? (
              <a className="tk-linkbtn" href={`?area=project&project_id=${encodeURIComponent(awayHome.id)}&tab=work&compose=1`} data-testid="tk-where-there"
                 title={`Opens New work in ${awayHome.name}, where these tickets belong.`}>New work in {awayHome.name} →</a>
            ) : null}
            {elsewhere.can_manage && manage.allowed ? (
              <button type="button" className="su-btn su-btn-sm" data-testid="tk-map-here" onClick={() => void mapHere(elsewhere, awayKeys[0])}
                      title={`${awayKeys[0]} tickets will land in ${here} instead.`}>Map {awayKeys[0]} here</button>
            ) : null}
          </div>
        ) : null}
        <div className="tk-faint tk-vault"><ShieldCheck size={13} aria-hidden /> <span title="Tokens are stored in BOSS's vault. Agents never see them; BOSS makes every call for them.">Tokens stay in BOSS&apos;s vault</span>{" "}
          <a className="tk-linkbtn" href={CONNECTORS_DOC} target="_blank" rel="noreferrer noopener" style={{ fontSize: "inherit" }}>How adapters work <ArrowSquareOut size={11} aria-hidden /></a></div>
      </div>
      {drawer ? (
        <AddTicketSystem projectId={drawer.mode === "edit" ? (drawer.connection.home_project?.id ?? drawer.connection.project_id ?? projectId) : projectId}
                         connection={drawer.mode === "edit" ? drawer.connection : null}
                         onClose={() => setDrawer(null)}
                         onSaved={(words) => { setDrawer(null); setSaid({ text: words, tone: "green" }); reload(); }}
                         onChanged={() => reload()} />
      ) : null}
    </section>
  );
}
