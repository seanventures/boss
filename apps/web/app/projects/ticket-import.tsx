"use client";

/**
 * IMPORT WORK FROM A TICKET SYSTEM (BOSSAPP-1047 Phase C, wireframe T8), shown by the Work tab while
 * `tickets=import`. Pick a project in the ticket system → its epics with their tickets, plus
 * "Not in an epic" → tick epics or tickets → ONE "Import to Work". An epic comes with its children
 * (with_children). A ticket already in BOSS is greyed with a link to it. Nothing starts here: the
 * Work tab's "Ready to start" (T9) is where each is started.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { CaretDown, CaretRight, Check, MagnifyingGlass } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { writePxUrl } from "../../lib/project-experience";
import {
  TYPE_WORDS, importTickets, parseImport, readChildren, readEpics, readExternalProjects, readUnparented, searchTickets, workflowFor, workflowWords,
  type Connection, type ExternalProject, type Read, type Ticket, type TicketType,
} from "../../lib/tickets";
import { ProviderMono, TkEmpty, TkReadState, TkTag } from "../tickets/ticket-parts";
import { NoTicketSystem, browsable, closeImport, writeImported } from "./ticket-work";
import type { PxCtx } from "./project-page";
import "../tickets/ticket-import.css";

type Pick = { ticket: Ticket; with_children: boolean };
const TYPES: ("" | TicketType)[] = ["", "story", "task", "bug", "epic"];

export function TicketImportPage({ ctx, conns }: { ctx: PxCtx; conns: Read<Connection[]> | null }) {
  const pid = ctx.projectId;
  const project = ctx.overview.project.name;
  const list = conns && conns.ok ? browsable(conns.data) : [];
  const [connId, setConnId] = useState("");
  const conn = list.find((c) => c.id === connId) ?? list[0] ?? null;
  const label = conn?.label ?? "a ticket system";

  const crumb = (
    <nav className="tk-crumb" aria-label="Breadcrumb">
      <button type="button" className="tk-linkbtn" onClick={closeImport} data-testid="tk-imp-back">{project} › Work</button>
      <span aria-hidden> › </span><span>Import from a ticket system</span>
    </nav>
  );
  if (!conns || !conns.ok) {
    return <div className="tk-page" data-testid="tk-import">{crumb}<h1 className="tk-h1">Import work</h1>
      <TkReadState read={conns} what="Ticket systems" testid="tk-imp-conns" rows={3} /></div>;
  }
  if (!conn) return <div className="tk-page" data-testid="tk-import">{crumb}<h1 className="tk-h1">Import work</h1><NoTicketSystem testid="tk-imp-none" /></div>;
  return <ImportBody key={conn.id} ctx={ctx} conn={conn} conns={list} crumb={crumb} onConn={setConnId} label={label} project={project} pid={pid} />;
}

function ImportBody({ ctx, conn, conns, crumb, onConn, label, project, pid }: {
  ctx: PxCtx; conn: Connection; conns: Connection[]; crumb: React.ReactNode; onConn: (id: string) => void; label: string; project: string; pid: string;
}) {
  const [projects, setProjects] = useState<Read<ExternalProject[]> | null>(null);
  const [filter, setFilter] = useState("");
  const [picked, setPicked] = useState("");
  const [epics, setEpics] = useState<Read<Ticket[]> | null>(null);
  const [loose, setLoose] = useState<Read<Ticket[]> | null>(null);
  const [kids, setKids] = useState<Record<string, Read<Ticket[]> | null>>({});
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [openOnly, setOpenOnly] = useState(true);
  const [type, setType] = useState<"" | TicketType>("");
  const [q, setQ] = useState("");
  const [found, setFound] = useState<Read<Ticket[]> | null>(null);
  const [searching, setSearching] = useState(false);
  const [sel, setSel] = useState<Record<string, Pick>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const loadProjects = useCallback(() => { setProjects(null); void readExternalProjects(conn.id).then(setProjects); }, [conn.id]);
  useEffect(() => { loadProjects(); }, [loadProjects]);
  const mine = useMemo(() => new Set(conn.settings.mappings.filter((m) => m.boss_project_id === pid).map((m) => m.external_project)), [conn, pid]);
  const anyMap = useMemo(() => new Set(conn.settings.mappings.map((m) => m.external_project)), [conn]);
  // Pick the first project mapped onto this BOSS project once the list is in.
  useEffect(() => {
    if (!picked && projects?.ok) {
      const first = projects.data.find((p) => mine.has(p.key)) ?? projects.data[0];
      if (first) setPicked(first.key);
    }
  }, [projects, picked, mine]);
  const loadTree = useCallback(() => {
    if (!picked) return;
    setEpics(null); setLoose(null); setKids({}); setOpen({});
    void readEpics(conn.id, picked).then(setEpics);
    void readUnparented(conn.id, picked).then(setLoose);
  }, [conn.id, picked]);
  useEffect(() => { loadTree(); }, [loadTree]);

  const expand = (key: string) => {
    const now = !open[key];
    setOpen((o) => ({ ...o, [key]: now }));
    if (now && !(key in kids)) {
      setKids((k) => ({ ...k, [key]: null }));
      void readChildren(conn.id, key).then((r) => setKids((k) => ({ ...k, [key]: r })));
    }
  };
  const search = async () => {
    const words = q.trim();
    if (!words) { setFound(null); return; }
    setSearching(true);
    try { setFound(await searchTickets(conn.id, words, { ...(picked ? { project: picked } : {}), ...(openOnly ? { status: "open" } : {}), ...(type ? { type } : {}) })); }
    finally { setSearching(false); }
  };
  const shows = (t: Ticket) => (!openOnly || t.status_category !== "done") && (!type || t.type === type || t.type === "epic");
  const toggle = (t: Ticket) => setSel((s) => {
    const next = { ...s };
    if (next[t.key]) delete next[t.key];
    else next[t.key] = { ticket: t, with_children: t.type === "epic" };
    return next;
  });
  const parentPicked = (t: Ticket) => Boolean(t.parent_key && sel[t.parent_key]?.with_children);

  const picks = Object.values(sel);
  const nEpic = picks.filter((p) => p.ticket.type === "epic").length;
  const nTicket = picks.length - nEpic;
  const head = [nEpic ? `${nEpic} epic${nEpic === 1 ? "" : "s"}` : "", nTicket ? `${nTicket} ticket${nTicket === 1 ? "" : "s"}` : ""].filter(Boolean).join(" + ");

  const doImport = async () => {
    if (!picks.length || busy) return;
    setBusy(true); setErr("");
    try {
      const r = await importTickets(pid, conn.id, picks.map((p) => ({ key: p.ticket.key, with_children: p.with_children })));
      if (!r.ok) { setErr(r.error ?? "Nothing was imported."); return; }
      const done = parseImport(r.body);
      writeImported(pid, { at: Date.now(), connection_id: conn.id, label, projects: picked ? [picked] : [], items: done.imported });
      setSel({});
      closeImport();
      ctx.refreshWork();
    } finally { setBusy(false); }
  };

  const row = (t: Ticket, child: boolean) => {
    const inBoss = Boolean(t.in_boss);
    const viaEpic = child && parentPicked(t);
    const checked = Boolean(sel[t.key]) || viaEpic;
    const isEpic = t.type === "epic";
    return (
      <li key={t.key} className="tk-trow" data-child={child ? "" : undefined} data-picked={checked && !inBoss ? "" : undefined} data-testid={`tk-imp-row-${t.key}`}>
        {isEpic && !child ? (
          <button type="button" className="tk-caret" aria-expanded={Boolean(open[t.key])} aria-label={`${open[t.key] ? "Hide" : "Show"} the tickets of ${t.key}`}
                  onClick={() => expand(t.key)} data-testid={`tk-imp-expand-${t.key}`}>{open[t.key] ? <CaretDown size={12} /> : <CaretRight size={12} />}</button>
        ) : <span className="tk-caret" aria-hidden />}
        <input type="checkbox" aria-label={`Select ${t.key}`} checked={checked && !inBoss} disabled={inBoss || viaEpic}
               title={inBoss ? "Already in BOSS" : viaEpic ? "Comes with its epic" : undefined}
               onChange={() => toggle(t)} data-testid={`tk-imp-pick-${t.key}`} />
        <span className="tk-key" data-epic={isEpic ? "" : undefined}>{t.key}</span>
        <span className="tk-trow-t"><span data-user-text>{t.title}</span>
          <span className="tk-faint">{[TYPE_WORDS[t.type], t.priority || t.status, isEpic && t.children_count !== null ? `${t.children_count} tickets` : ""].filter(Boolean).join(" · ")}</span></span>
        {inBoss ? (
          <button type="button" className="tk-linkbtn tk-inboss" data-testid={`tk-imp-inboss-${t.key}`}
                  onClick={() => writePxUrl({ tickets: null, ...(t.in_boss?.run_id ? { drawer: `work:${t.in_boss.run_id}` } : {}) })}>In BOSS ↗</button>
        ) : isEpic ? <TkTag tone="violet">Epic</TkTag> : null}
      </li>
    );
  };

  const projList = projects?.ok ? [...projects.data].sort((x, y) => Number(mine.has(y.key)) - Number(mine.has(x.key)))
    .filter((p) => !filter.trim() || `${p.key} ${p.name}`.toLowerCase().includes(filter.trim().toLowerCase())) : [];

  return (
    <div className="tk-page" data-testid="tk-import">
      {crumb}
      <div className="tk-row tk-imp-head">
        <h1 className="tk-h1">Import work from {label}</h1>
        {conns.length > 1 ? (
          <label className="tk-pick"><span className="tk-sr">Ticket system</span>
            <select value={conn.id} onChange={(e) => onConn(e.target.value)} aria-label="Ticket system" data-testid="tk-imp-conn">
              {conns.map((c) => <option key={c.id} value={c.id}>{c.label} · {c.base_url.replace(/^https?:\/\//, "")}</option>)}
            </select></label>
        ) : <span className="tk-faint"><ProviderMono provider={conn.provider} size={16} /> {conn.label} · {conn.base_url.replace(/^https?:\/\//, "")}</span>}
        <span className="tk-spacer" />
        <form className="tk-search" role="search" onSubmit={(e) => { e.preventDefault(); void search(); }}>
          <MagnifyingGlass size={13} aria-hidden />
          <label htmlFor="tk-imp-q" className="tk-sr">Search all tickets</label>
          <input id="tk-imp-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search all tickets…" data-testid="tk-imp-q" />
          <button type="submit" className="tk-btn tk-btn-sm" data-testid="tk-imp-search" disabled={searching}>{searching ? "Searching…" : "Search"}</button>
        </form>
      </div>
      <ol className="tk-steps" aria-label="Steps">
        <li data-done={picked ? "" : undefined}>1 · Pick a {label} project{picked ? <Check size={11} weight="bold" aria-label="done" /> : null}</li>
        <li data-done={picks.length ? "" : undefined}>2 · Pick epics or tickets{picks.length ? <Check size={11} weight="bold" aria-label="done" /> : null}</li>
        <li>3 · Import into {project}</li>
      </ol>
      <div className="tk-imp-grid">
        <section className="tk-panel" aria-labelledby="tk-imp-jp">
          <h2 id="tk-imp-jp" className="tk-h2">{label} projects</h2>
          <label htmlFor="tk-imp-pf" className="tk-sr">Filter projects</label>
          <input id="tk-imp-pf" className="tk-input" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter projects" data-testid="tk-imp-filter" />
          {!projects || !projects.ok ? <TkReadState read={projects} what="Projects" onRetry={loadProjects} testid="tk-imp-projects" rows={4} />
            : !projList.length ? <TkEmpty title={filter ? "No project matches" : "No projects"} line={filter ? undefined : "BOSS's sign-in sees no projects here."} testid="tk-imp-projects-empty" />
            : (
              <div className="tk-projs" role="listbox" aria-label={`${label} projects`}>
                {projList.map((p) => (
                  <button key={p.key} type="button" role="option" aria-selected={picked === p.key} className="tk-proj" data-testid={`tk-imp-proj-${p.key}`}
                          onClick={() => { setPicked(p.key); setSel({}); setFound(null); }}>
                    <b>{p.key} · {p.name}</b>
                    <span className="tk-faint">{!anyMap.has(p.key) && anyMap.size ? "not mapped to a BOSS project"
                      : [p.epic_count !== null ? `${p.epic_count} epic${p.epic_count === 1 ? "" : "s"}` : "", p.open_count !== null ? `${p.open_count} open` : ""].filter(Boolean).join(" · ") || " "}</span>
                  </button>
                ))}
              </div>
            )}
        </section>
        <section className="tk-panel" aria-labelledby="tk-imp-tree">
          <div className="tk-row">
            <h2 id="tk-imp-tree" className="tk-h2">{found ? `Search: “${q.trim()}”` : picked ? `${picked} — epics and their tickets` : "Epics and tickets"}</h2>
            <span className="tk-spacer" />
            <button type="button" className="tk-optchip" aria-pressed={openOnly} onClick={() => setOpenOnly((v) => !v)} data-testid="tk-imp-open-only">Open only</button>
            <label className="tk-pick"><span className="tk-sr">Type</span>
              <select value={type} onChange={(e) => setType(e.target.value as "" | TicketType)} aria-label="Type" data-testid="tk-imp-type">
                {TYPES.map((t) => <option key={t || "any"} value={t}>{t ? TYPE_WORDS[t] : "Any type"}</option>)}
              </select></label>
          </div>
          {found ? (
            <>
              <button type="button" className="tk-linkbtn" onClick={() => { setFound(null); setQ(""); }} data-testid="tk-imp-clear">Back to {picked || "the project"}</button>
              {!found.ok ? <TkReadState read={found} what="Search" onRetry={() => void search()} testid="tk-imp-found" />
                : found.data.filter(shows).length ? <ul className="tk-tree">{found.data.filter(shows).map((t) => row(t, false))}</ul>
                : <TkEmpty title="Nothing found" line="Try other words, or turn off Open only." testid="tk-imp-found-empty" />}
            </>
          ) : !picked ? <TkEmpty title="Pick a project" line="Its epics and tickets show here." testid="tk-imp-pick-first" />
            : (
              <>
                {!epics || !epics.ok ? <TkReadState read={epics} what="Epics" onRetry={loadTree} testid="tk-imp-epics" rows={3} /> : (
                  <ul className="tk-tree" data-testid="tk-imp-epics">
                    {epics.data.filter(shows).map((e) => (
                      <React.Fragment key={e.key}>
                        {row(e, false)}
                        {open[e.key] ? (
                          kids[e.key] && kids[e.key]!.ok
                            ? (kids[e.key] as { ok: true; data: Ticket[] }).data.filter(shows).map((c) => row({ ...c, parent_key: c.parent_key || e.key }, true))
                            : <li className="tk-trow" data-child=""><TkReadState read={kids[e.key] ?? null} what="Tickets" testid={`tk-imp-kids-${e.key}`} rows={1} /></li>
                        ) : null}
                      </React.Fragment>
                    ))}
                  </ul>
                )}
                <div className="tk-group" aria-hidden>— Not in an epic{loose?.ok ? ` · ${loose.data.filter(shows).length}` : ""}</div>
                {!loose || !loose.ok ? <TkReadState read={loose} what="Tickets not in an epic" onRetry={loadTree} testid="tk-imp-loose" rows={2} />
                  : <ul className="tk-tree" data-testid="tk-imp-loose">{loose.data.filter(shows).map((t) => row(t, true))}</ul>}
                {epics?.ok && loose?.ok && !epics.data.filter(shows).length && !loose.data.filter(shows).length
                  ? <TkEmpty title="No open tickets here" line="Turn off Open only, or pick another project." testid="tk-imp-tree-empty" /> : null}
              </>
            )}
        </section>
        <section className="tk-panel tk-summary" aria-labelledby="tk-imp-sum" data-testid="tk-imp-summary">
          <h2 id="tk-imp-sum" className="tk-h2">{picks.length ? `Import ${head}` : "Nothing picked yet"}</h2>
          <ul className="tk-sumlist">
            {picks.map((p) => (
              <li key={p.ticket.key} data-testid={`tk-imp-sum-${p.ticket.key}`}>
                <b>{p.ticket.key} · {p.ticket.title}</b>
                <span className="tk-faint">{p.ticket.type === "epic" ? "epic + its tickets" : TYPE_WORDS[p.ticket.type]} → {workflowWords(workflowFor(p.ticket.type))}</span>
              </li>
            ))}
          </ul>
          <p className="tk-faint">Into: <b>{project}</b> · Keep in sync with {label} ✓</p>
          {err ? <p className="tk-error" role="alert" data-testid="tk-imp-err">{err}</p> : null}
          <GatedButton permission="work_tracker:write" projectId={pid} className="tk-btn" data-kind="primary" data-testid="tk-imp-go"
                       disabled={!picks.length || busy} onClick={() => void doImport()}>{busy ? "Importing…" : "Import to Work"}</GatedButton>
          <p className="tk-faint">Nothing starts yet. You pick what to start on the next screen.</p>
        </section>
      </div>
    </div>
  );
}
