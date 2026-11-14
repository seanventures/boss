"use client";

/**
 * TICKETS ON THE WORK TAB (BOSSAPP-1047 Phase C, wireframes T5 and T9).
 *
 *  T5  the "Import from Jira" action beside New work; "Waiting for BOSS" — open tickets carrying a
 *      connection's pick-up label, not yet in BOSS (GET /v1/projects/{id}/tickets/waiting), each
 *      with ONE action (Start, or Plan first for an epic); a ticket chip with its sync state on
 *      every work row named after a ticket ("LEND-142 …"), read lazily for the rows in view.
 *  T9  after an import: "Ready to start" — an epic card listing its tickets with ONE Start major
 *      feature, and single cards with the workflow picked from the type and Start.
 *
 * The import page itself (T8) is ticket-import.tsx, shown by the Work tab while `tickets=import`.
 */
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { ArrowRight, DownloadSimple, Kanban, X } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { writePxUrl, type WorkItem } from "../../lib/project-experience";
import {
  TYPE_WORDS, codeWords, importAndStart, readConnections, readTicket, readWaiting, startFromTicket, syncWords, workflowFor, workflowWords,
  type Connection, type Read, type Ticket, type TypedError,
} from "../../lib/tickets";
import { ProviderMono, TicketChip, TkEmpty, TkReadState, TkTag, WorkflowPick } from "../tickets/ticket-parts";
import type { PxCtx } from "./project-page";
import "../tickets/ticket-import.css";

// ── the `tickets` URL param (tickets=import opens T8) ──────────────────────────────────────────
function readParam(): string {
  try { return new URLSearchParams(window.location.search).get("tickets") ?? ""; } catch { return ""; }
}
/** `tickets=import` while the Import page is open; re-read on Back/Forward and on writePxUrl. */
export function useTicketsParam(): string {
  const [v, setV] = useState("");
  useEffect(() => {
    const read = () => setV(readParam());
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  return v;
}
export const openImport = () => writePxUrl({ tickets: "import" });
export const closeImport = () => writePxUrl({ tickets: null });

// ── the project's connections, read once for the tab ───────────────────────────────────────────
export function useTicketConnections(projectId: string, demo = false): { read: Read<Connection[]> | null; reload: () => void } {
  const [read, setRead] = useState<Read<Connection[]> | null>(null);
  const reload = useCallback(() => {
    if (demo) { setRead({ ok: true, data: [] }); return; }
    setRead(null);
    void readConnections(projectId).then(setRead);
  }, [projectId, demo]);
  useEffect(() => { reload(); }, [reload]);
  return { read, reload };
}
export const browsable = (cs: Connection[]) => cs.filter((c) => c.status === "connected" && c.capabilities.browse !== false);

/** "LEND-142 Second reviewer…" → "LEND-142". */
const KEY = /^([A-Z][A-Z0-9_]*-\d+)\b/;
export function ticketKeyOf(label: string): string {
  return KEY.exec(label.trim())?.[1] ?? "";
}
/** The connection a key belongs to: one whose known project keys hold its prefix; with no keys
 *  known, the only connected one. */
export function connectionForKey(key: string, cs: Connection[]): Connection | null {
  const prefix = key.split("-")[0];
  const live = cs.filter((c) => c.status === "connected");
  const owner = live.find((c) => c.settings.project_keys.includes(prefix));
  if (owner) return owner;
  const unknown = live.filter((c) => !c.settings.project_keys.length);
  return unknown.length === 1 && live.length === 1 ? unknown[0] : null;
}

// ── T5: row chips, through a context the Work tab provides ─────────────────────────────────────
export type RowChip = { provider: string; key: string; url: string; sync: string; tone?: "green" | "amber" | "red" };
const ChipsCtx = createContext<Record<string, RowChip>>({});
const ROW_LIMIT = 12;

/** Reads, for at most 12 rows in view (running, needs you, planned) named after a ticket, the
 *  ticket's status and the binding's sync state: one read per row, lazily. */
export function useRowChips(items: WorkItem[], cs: Connection[]): Record<string, RowChip> {
  const wanted = useMemo(() => items
    .filter((w) => w.state === "running" || w.state === "needs_you" || w.state === "planned" || w.state === "stalled")
    .map((w) => ({ id: w.id, key: ticketKeyOf(w.label) }))
    .filter((x) => x.key)
    .map((x) => ({ ...x, conn: connectionForKey(x.key, cs) }))
    .filter((x): x is { id: string; key: string; conn: Connection } => Boolean(x.conn))
    .slice(0, ROW_LIMIT), [items, cs]);
  const sig = wanted.map((x) => `${x.id}:${x.key}`).join(",");
  const [chips, setChips] = useState<Record<string, RowChip>>({});
  useEffect(() => {
    let live = true;
    for (const x of wanted) {
      void readTicket(x.conn.id, x.key).then((r) => {
        if (!live) return;
        const chip: RowChip = r.ok
          ? { provider: x.conn.provider, key: x.key, url: r.data.url,
              sync: syncWords(x.conn.label, r.data.status, r.data.in_boss?.sync_status ?? ""),
              tone: r.data.in_boss?.sync_status === "failed" ? "red" : r.data.in_boss?.sync_status === "paused" ? "amber" : "green" }
          : { provider: x.conn.provider, key: x.key, url: "", sync: `${x.conn.label}: not read`, tone: "amber" };
        setChips((c) => ({ ...c, [x.id]: chip }));
      });
    }
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);
  return chips;
}

export function RowChipsProvider({ chips, children }: { chips: Record<string, RowChip>; children: React.ReactNode }) {
  return <ChipsCtx.Provider value={chips}>{children}</ChipsCtx.Provider>;
}
/** The row's ticket chip (nothing for a row that is no ticket's). */
export function RowTicket({ w }: { w: WorkItem }) {
  const c = useContext(ChipsCtx)[w.id];
  if (!c) return null;
  return <TicketChip provider={c.provider} ticketKey={c.key} url={c.url || undefined} sync={c.sync} tone={c.tone} testid={`tk-row-chip-${w.id}`} />;
}

// ── T5: the Import action ───────────────────────────────────────────────────────────────────
export function ImportAction({ ctx, conns }: { ctx: PxCtx; conns: Connection[] }) {
  const can = browsable(conns);
  if (!can.length) return null;
  const label = can.length === 1 ? can[0].label : "a ticket system";
  return (
    <GatedButton permission="work_tracker:write" projectId={ctx.projectId} className="tk-btn" data-testid="tk-import-open"
                 title="Browse its projects and epics, and bring tickets in as work" onClick={openImport}>
      <DownloadSimple size={14} aria-hidden /> Import from {label}
    </GatedButton>
  );
}

// ── T5: Waiting for BOSS ─────────────────────────────────────────────────────────────────────
export function WaitingSection({ ctx, conns }: { ctx: PxCtx; conns: Connection[] }) {
  const pid = ctx.projectId;
  const [read, setRead] = useState<Read<{ tickets: Ticket[]; errors: TypedError[] }> | null>(null);
  const [busy, setBusy] = useState("");
  const [said, setSaid] = useState<{ text: string; tone: "red" | "green" } | null>(null);
  const startGate = useGate("workflow_run:start", pid);
  const load = useCallback(() => { if (!ctx.demo) void readWaiting(pid).then(setRead); }, [pid, ctx.demo]);
  useEffect(() => { load(); }, [load]);
  const live = conns.filter((c) => c.status === "connected" && c.settings.pickup.label && c.settings.inbound.mode !== "off");
  if (!live.length || !read) return null;
  if (!read.ok) {
    if (read.missing) return null;
    return <section className="tk-sec" data-testid="tk-waiting"><TkReadState read={read} what="Waiting for BOSS" onRetry={load} testid="tk-waiting" /></section>;
  }
  const { tickets, errors } = read.data;
  if (!tickets.length && !errors.length) return null;
  const c = live[0];
  const mapped = c.settings.mappings.filter((m) => m.boss_project_id === pid).map((m) => m.external_project);
  const start = async (t: Ticket) => {
    if (!startGate.allowed) return;
    setBusy(t.key); setSaid(null);
    try {
      const r = await importAndStart(pid, t, workflowFor(t.type));
      if (!r.ok) { setSaid({ text: r.error ?? `${t.key} did not start.`, tone: "red" }); return; }
      setSaid({ text: `Started ${t.key} as ${workflowWords(workflowFor(t.type)).toLowerCase()}.${r.note ? ` ${r.note}` : ""}`, tone: "green" });
      load(); ctx.refreshWork();
    } finally { setBusy(""); }
  };
  const labelOf = (id?: string) => conns.find((x) => x.id === id)?.label ?? "The ticket system";
  return (
    <section className="tk-sec" data-testid="tk-waiting" aria-labelledby="tk-waiting-h">
      <div className="tk-sec-head">
        <h2 id="tk-waiting-h">Waiting for BOSS · <span className="tk-muted">{c.label}{mapped.length ? ` ${mapped.join(", ")}` : ""}, label “{c.settings.pickup.label}”</span> · {tickets.length}</h2>
        <span className="tk-spacer" />
        <a className="tk-linkbtn" href="?area=setup&focus=ticket_systems" data-testid="tk-waiting-rule"
           title={c.settings.pickup.mode === "auto_start" ? "Tickets with this label start on their own" : "BOSS asks you before starting a labelled ticket"}>Change the rule</a>
      </div>
      {errors.map((e, i) => (
        <p key={i} className="tk-error" role="alert" data-testid="tk-waiting-err">{labelOf(e.connection_id)}: {codeWords(e.error, e.message)}</p>
      ))}
      <ul className="tk-list">
        {tickets.map((t) => (
          <li key={`${t.connection_id}:${t.key}`} className="tk-item" data-testid={`tk-waiting-${t.key}`}>
            <ProviderMono provider={t.provider} size={18} />
            {t.url ? <a className="tk-key" href={t.url} target="_blank" rel="noreferrer noopener">{t.key}</a> : <b className="tk-key">{t.key}</b>}
            <span className="tk-item-title" data-user-text>{t.title}</span>
            <TkTag tone={t.type === "bug" ? "red" : t.type === "epic" ? "violet" : "blue"}>{TYPE_WORDS[t.type]}</TkTag>
            {t.priority ? <TkTag tone={/high|urgent|critical|blocker/i.test(t.priority) ? "amber" : "idle"}>{t.priority}</TkTag> : null}
            <GatedButton permission="work_tracker:write" projectId={pid} className="tk-btn tk-btn-sm" data-kind="primary" data-testid={`tk-waiting-start-${t.key}`}
                         disabled={Boolean(busy) || !startGate.allowed} title={startGate.allowed ? `Starts it as ${workflowWords(workflowFor(t.type)).toLowerCase()}` : startGate.reason}
                         onClick={() => void start(t)}>
              {busy === t.key ? "Starting…" : t.type === "epic" ? "Plan first" : "Start"}
            </GatedButton>
          </li>
        ))}
      </ul>
      {said ? <p className={said.tone === "red" ? "tk-error" : "tk-ok"} role={said.tone === "red" ? "alert" : "status"} data-testid="tk-waiting-said">{said.text}</p> : null}
    </section>
  );
}

// ── T9: what was just imported ─────────────────────────────────────────────────────────────────
export type ImportedRecord = {
  at: number; connection_id: string; label: string; projects: string[];
  items: { key: string; work_item_id: string; parent_work_item_id: string | null }[];
};
const STORE = (pid: string) => `boss.tickets.imported.${pid}`;
export function readImported(pid: string): ImportedRecord | null {
  try {
    const raw = window.sessionStorage.getItem(STORE(pid));
    if (!raw) return null;
    const x = JSON.parse(raw) as ImportedRecord;
    return x && Array.isArray(x.items) && x.items.length ? x : null;
  } catch { return null; }
}
export function writeImported(pid: string, rec: ImportedRecord | null): void {
  try {
    if (!rec || !rec.items.length) window.sessionStorage.removeItem(STORE(pid));
    else window.sessionStorage.setItem(STORE(pid), JSON.stringify(rec));
  } catch { /* private window: the cards live as long as the page */ }
  try { window.dispatchEvent(new Event("boss:tickets-imported")); } catch { /* non-browser */ }
}

const READ_LIMIT = 12;

export function ImportedSection({ ctx }: { ctx: PxCtx }) {
  const pid = ctx.projectId;
  const [rec, setRec] = useState<ImportedRecord | null>(null);
  const [info, setInfo] = useState<Record<string, Ticket | null>>({});
  const [busy, setBusy] = useState("");
  const [said, setSaid] = useState<{ text: string; tone: "red" | "green" } | null>(null);
  const [picks, setPicks] = useState<Record<string, string>>({});
  const startGate = useGate("workflow_run:start", pid);
  useEffect(() => {
    const read = () => setRec(readImported(pid));
    read();
    window.addEventListener("boss:tickets-imported", read);
    return () => window.removeEventListener("boss:tickets-imported", read);
  }, [pid]);
  const sig = rec ? rec.items.map((i) => i.key).join(",") : "";
  useEffect(() => {
    if (!rec) return;
    let live = true;
    for (const it of rec.items.slice(0, READ_LIMIT)) {
      void readTicket(rec.connection_id, it.key).then((r) => { if (live) setInfo((m) => ({ ...m, [it.key]: r.ok ? r.data : null })); });
    }
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);
  if (!rec) return null;

  const byWi = new Map(rec.items.map((i) => [i.work_item_id, i]));
  const epics = rec.items.filter((i) => info[i.key]?.type === "epic" || rec.items.some((c) => c.parent_work_item_id === i.work_item_id));
  const epicIds = new Set(epics.map((e) => e.work_item_id));
  const singles = rec.items.filter((i) => !epicIds.has(i.work_item_id) && !(i.parent_work_item_id && byWi.has(i.parent_work_item_id)));
  const ready = epics.length + singles.length;
  const drop = (ids: string[]) => {
    const next = { ...rec, items: rec.items.filter((i) => !ids.includes(i.work_item_id) && !ids.includes(i.parent_work_item_id ?? "")) };
    writeImported(pid, next.items.length ? next : null);
  };
  const start = async (workItemId: string, key: string, workflow: string) => {
    if (!startGate.allowed) return;
    setBusy(workItemId); setSaid(null);
    try {
      const r = await startFromTicket(workItemId, { project_id: pid, workflow_key: workflow });
      if (!r.ok) { setSaid({ text: r.error ?? `${key} did not start.`, tone: "red" }); return; }
      const note = typeof (r.body as Record<string, unknown> | null)?.note === "string" ? ` ${(r.body as Record<string, string>).note}` : "";
      setSaid({ text: `Started ${key} as ${workflowWords(workflow).toLowerCase()}.${note}`, tone: "green" });
      drop([workItemId]);
      ctx.refreshWork();
    } finally { setBusy(""); }
  };
  // "1 epic and 2 tickets": an epic's own tickets come with it, so they are not counted again (T9).
  const nEpics = epics.length, nTickets = singles.length;
  const head = [nEpics ? `${nEpics} epic${nEpics === 1 ? "" : "s"}` : "", nTickets ? `${nTickets} ticket${nTickets === 1 ? "" : "s"}` : ""].filter(Boolean).join(" and ");
  const title = (k: string) => info[k]?.title ?? (k in info ? "" : "Reading…");
  return (
    <section className="tk-sec" data-testid="tk-imported" aria-labelledby="tk-ready-h">
      <p className="tk-banner" role="status" data-testid="tk-imported-banner">
        ✓ Imported {head} from {rec.label}{rec.projects.length ? ` ${rec.projects.join(", ")}` : ""}. Pick what to start.
        <span className="tk-spacer" />
        <button type="button" className="tk-linkbtn" onClick={() => writeImported(pid, null)} data-testid="tk-imported-dismiss" aria-label="Dismiss"><X size={12} aria-hidden /> Dismiss</button>
      </p>
      <h2 id="tk-ready-h" className="tk-h2">Ready to start · {ready}</h2>
      <div className="tk-cards">
        {epics.map((e) => {
          const kids = rec.items.filter((c) => c.parent_work_item_id === e.work_item_id);
          return (
            <article key={e.work_item_id} className="tk-icard" data-epic="" data-testid={`tk-imported-${e.key}`} aria-labelledby={`tk-e-${e.work_item_id}`}>
              <div className="tk-row"><span className="tk-key">{e.key}</span><TkTag tone="violet">Epic</TkTag><span className="tk-spacer" /><TkTag tone="blue">Major feature</TkTag></div>
              <h3 id={`tk-e-${e.work_item_id}`} className="tk-ititle" data-user-text>{title(e.key)}</h3>
              {kids.length ? <ul className="tk-kids">{kids.map((k) => <li key={k.work_item_id}><b className="tk-mono-text">{k.key}</b> <span data-user-text>{title(k.key)}</span></li>)}</ul> : null}
              <p className="tk-faint">Plan first: BOSS writes one plan for {kids.length ? `the ${kids.length} ticket${kids.length === 1 ? "" : "s"}` : "the epic"}, you approve, then one PR per ticket.</p>
              <GatedButton permission="work_tracker:write" projectId={pid} className="tk-btn" data-kind="primary" data-testid={`tk-imported-start-${e.key}`}
                           disabled={Boolean(busy) || !startGate.allowed} title={startGate.allowed ? undefined : startGate.reason}
                           onClick={() => void start(e.work_item_id, e.key, "major_feature")}>
                {busy === e.work_item_id ? "Starting…" : "Start major feature"}<ArrowRight size={12} aria-hidden /></GatedButton>
            </article>
          );
        })}
        {singles.map((s) => {
          const t = info[s.key];
          const wf = picks[s.key] ?? workflowFor(t?.type ?? "other");
          return (
            <article key={s.work_item_id} className="tk-icard" data-testid={`tk-imported-${s.key}`}>
              <div className="tk-row"><span className="tk-key">{s.key}</span>{t ? <TkTag tone={t.type === "bug" ? "red" : "blue"}>{TYPE_WORDS[t.type]}</TkTag> : null}</div>
              <h3 className="tk-ititle" data-user-text>{title(s.key)}</h3>
              <div className="tk-row">
                <WorkflowPick value={wf} onChange={(v) => setPicks((p) => ({ ...p, [s.key]: v }))} testid={`tk-imported-wf-${s.key}`} />
                <span className="tk-spacer" />
                <GatedButton permission="work_tracker:write" projectId={pid} className="tk-btn" data-kind="primary" data-testid={`tk-imported-start-${s.key}`}
                             disabled={Boolean(busy) || !startGate.allowed} title={startGate.allowed ? undefined : startGate.reason}
                             onClick={() => void start(s.work_item_id, s.key, wf)}>{busy === s.work_item_id ? "Starting…" : "Start"}</GatedButton>
              </div>
            </article>
          );
        })}
      </div>
      {said ? <p className={said.tone === "red" ? "tk-error" : "tk-ok"} role={said.tone === "red" ? "alert" : "status"} data-testid="tk-imported-said">{said.text}</p> : null}
    </section>
  );
}

// ── the Work tab's ticket band: Import, Waiting for BOSS, Ready to start ──────────────────────
export function TicketWorkBand({ ctx, conns }: { ctx: PxCtx; conns: Read<Connection[]> | null }) {
  if (!conns || !conns.ok) return null;
  return (
    <>
      {browsable(conns.data).length ? <div className="tk-row tk-actions" data-testid="tk-work-actions"><span className="tk-spacer" /><ImportAction ctx={ctx} conns={conns.data} /></div> : null}
      <ImportedSection ctx={ctx} />
      <WaitingSection ctx={ctx} conns={conns.data} />
    </>
  );
}

/** No ticket system yet (the Import page's empty state). */
export function NoTicketSystem({ testid = "tk-none" }: { testid?: string }) {
  return (
    <TkEmpty title="No ticket system connected" line="Connect Jira, Plane, GitHub Issues or another once; then import epics and tickets here." testid={testid}>
      <a className="tk-btn" href="?area=setup&focus=ticket_systems" data-testid={`${testid}-setup`}><Kanban size={13} aria-hidden /> Open Setup › Ticket systems</a>
    </TkEmpty>
  );
}
