"use client";

/**
 * Observability › Apps (spec §4.9): THE PULSE — one 24-hour strip per app, real probe results,
 * deploy pins dropped where each deploy landed; NOT WATCHED (scraped, no health target); the app
 * drawer (app-drawer.tsx) over `app=<target_id>`; and the header's `Ask BOSS` (ObsAskBoss).
 *
 *   PULSE · 24H                 00:00      06:00      12:00      18:00      now
 *   ● Down  api · prod   edge-2  ▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▼▯▯▯▯▯   91.3%  down 1h 4m  ↗
 *
 * Ordering is the server's (Down, Slow, No signal, Up, Not deployed; then name). The strip's
 * density is the server's too: narrow layouts ask for `cells=48`/`24` (obs-home) and the server
 * re-buckets. `?watch=<app>|<env>` — the workflow "Set up monitoring" step's link — opens that
 * app's drawer, or Find with its name when no row matches.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";

import { openDockAbout } from "../../lib/dock-subject";
import { deployHref, hostHref } from "../../lib/infra-view";
import { ms, pct, type PulseRow, type Pulse } from "../../lib/obs-view";
import { plainClick } from "../agents/live-util";
import { HEAD, SPAN, dur, leaveTo, obsAsks, timeTicks } from "./obs-util";
import type { ObsTabProps } from "./obs-home";
import { AppDrawer } from "./app-drawer";
import { PulseStrip, StripTable } from "./pulse-strip";
import { Empty, LiveRegion, Skeleton, StateWord, ZoneHead, useAnnouncer } from "../infra/kit";

// ── one row ─────────────────────────────────────────────────────────────────────────────────

function PulseRowView({ row, pulse, onOpen, changed, now }: {
  row: PulseRow; pulse: Pulse; onOpen: (id: string) => void; changed: boolean; now: number;
}) {
  const [table, setTable] = useState(false);
  const name = `${row.app} · ${row.env}`;
  const notDeployed = row.state === "not_deployed";
  const sinceMs = row.since ? Date.parse(row.since) : NaN;
  const right = row.state === "down" && Number.isFinite(sinceMs)
    ? <span className="obs-down" title={`Down since ${new Date(sinceMs).toLocaleString()}`}>down {dur((now - sinceMs) / 1000)}</span>
    : row.p95Ms !== null
      ? <span>p95 {ms(row.p95Ms)}</span>
      : notDeployed ? <span className="inf-faint">—</span>
        : <span className="inf-faint" title={row.state === "unknown" ? "No probes in this window" : "No latency recorded"}>
            {row.state === "unknown" ? "no probes" : "—"}</span>;
  return (
    <div className="obs-prow" data-testid="obs-pulse-row" data-target={row.targetId} data-state={row.state}>
      <span className="obs-prow-state"><StateWord w={{ word: row.word, tone: row.tone }} changed={changed} /></span>
      <button type="button" className="obs-prow-name" title={`Open ${name}`} onClick={() => onOpen(row.targetId)}>
        <span className="inf-trunc" data-copy="data" data-user-text>{row.app}</span>
        <span className="inf-faint" aria-hidden> · </span>
        <span className="inf-muted" data-copy="data" data-user-text>{row.env}</span>
      </button>
      <span className="obs-prow-host">
        {row.host ? (
          <a className="inf-link inf-trunc" href={hostHref(row.host.id)} title={`${row.host.name} in Infra`} data-copy="data"
             onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo(hostHref(row.host!.id)); } }}>{row.host.name}</a>
        ) : <span className="inf-faint">—</span>}
      </span>
      <span className="obs-prow-strip">
        {notDeployed ? (
          <span className="obs-nostrip" title="This target has no deploy yet">Not deployed</span>
        ) : (
          <PulseStrip cells={row.strip} fromIso={pulse.window.from} cellS={pulse.window.cellS} label={`${row.app} ${row.env}`}
                      span={SPAN[pulse.window.win] ?? pulse.window.win} testId="obs-pulse-strip"
                      pins={row.pins.map((p) => ({ ...p, href: p.ref ? deployHref(p.ref) : null }))}
                      onPin={(p) => { if (p.href) leaveTo(p.href); }} />
        )}
      </span>
      <span className="obs-prow-uptime inf-num" data-testid="obs-pulse-uptime"
            title={row.uptime === null ? "No probes in this window" : `Share of probes that answered, last ${SPAN[pulse.window.win] ?? pulse.window.win}`}>
        {notDeployed ? "—" : pct(row.uptime)}
      </span>
      <span className="obs-prow-p95 inf-num" data-testid="obs-pulse-p95">{right}</span>
      <span className="obs-prow-go">
        {!notDeployed ? (
          // One glyph per row (a word per row was a column of repeated text): its name is the
          // tooltip and the accessible label.
          <button type="button" className="obs-tbl" aria-pressed={table} data-testid="obs-pulse-table-toggle"
                  aria-label={table ? "View as chart" : "View as table"} title={table ? "View as chart" : "View as table"}
                  onClick={() => setTable((v) => !v)}>{table ? "▥" : "▤"}</button>
        ) : null}
        <button type="button" className="ag-link obs-go" aria-label={`Open ${name}`} data-testid="obs-app-open-row"
                onClick={() => onOpen(row.targetId)}>↗</button>
      </span>
      {table ? (
        <div className="obs-prow-table">
          <StripTable cells={row.strip} fromIso={pulse.window.from} cellS={pulse.window.cellS} testId="obs-pulse-table" />
        </div>
      ) : null}
    </div>
  );
}

// ── the tab ─────────────────────────────────────────────────────────────────────────────────

export function AppsTab(props: ObsTabProps) {
  const { pulse, url, go, query } = props;
  const data = pulse.data;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { setNow(Date.now()); }, [data]);

  // A state word that changed between polls rings its dot once and is announced (§5.2, §7).
  const [said, say] = useAnnouncer();
  const last = useRef<Map<string, string>>(new Map());
  const changed = useMemo(() => {
    const out = new Set<string>();
    if (!data) return out;
    const prev = last.current;
    for (const r of data.rows) {
      const was = prev.get(r.targetId);
      if (was !== undefined && was !== r.word) out.add(r.targetId);
    }
    return out;
  }, [data]);
  useEffect(() => {
    if (!data) return;
    for (const r of data.rows) {
      if (changed.has(r.targetId)) say(`${r.app} ${r.env} is ${r.word.toLowerCase()}`);
      last.current.set(r.targetId, r.word);
    }
  }, [data, changed]);

  // The legacy monitoring link: `watch=<app>|<env>` → that app's drawer, else Find with its name.
  const watched = useRef(false);
  useEffect(() => {
    if (!data || watched.current || typeof window === "undefined") return;
    const watch = new URLSearchParams(window.location.search).get("watch");
    if (!watch) return;
    watched.current = true;
    const [app, env] = watch.split("|");
    const hit = data.rows.find((r) => r.app === app && (!env || r.env === env));
    go(hit ? { app: hit.targetId, watch: null } : { q: app || null, watch: null }, "replace");
  }, [data, go]);

  if (pulse.loading && !data) return <Skeleton testId="obs-apps-loading" />;
  const rows = (data?.rows ?? []).filter((r) => {
    const q = (query || url.q).trim().toLowerCase();
    return !q || `${r.app} ${r.env} ${r.host?.name ?? ""}`.toLowerCase().includes(q);
  });
  const open = (id: string) => go({ app: id }, "push");
  const ticks = data ? timeTicks(data.window.from, data.window.to, data.window.win) : [];
  const head = HEAD[url.win] ?? "PULSE";

  return (
    <div className="obs-apps" data-testid="obs-apps">
      <LiveRegion text={said} />
      <section className="obs-pulse" aria-labelledby="obs-pulse-head">
        <div className="obs-prow obs-prow-head">
          <h3 id="obs-pulse-head" className="ag-zone-label obs-prow-headlabel">{head}</h3>
          <span className="obs-ticks" aria-hidden>
            {ticks.map((t, i) => <span key={i}>{t}</span>)}
          </span>
        </div>
        {!data || data.rows.length === 0 ? (
          <Empty testId="obs-apps-empty">
            Nothing deployed yet{" "}
            <a className="inf-link" href="?area=infra" data-testid="obs-apps-infra"
               onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo("?area=infra"); } }}>Infra ↗</a>
          </Empty>
        ) : rows.length === 0 ? (
          <Empty testId="obs-apps-nomatch">Nothing matches</Empty>
        ) : (
          <div className="obs-prows" role="list">
            {rows.map((r) => (
              <div role="listitem" key={r.targetId}>
                <PulseRowView row={r} pulse={data} onOpen={open} changed={changed.has(r.targetId)} now={now} />
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="obs-zone" aria-labelledby="obs-not-watched-head" data-testid="obs-not-watched">
        <ZoneHead id="obs-not-watched-head" label="NOT WATCHED" count={data?.notWatched.length ?? 0}
                  tip="Prometheus scrapes it, but no health check watches it" />
        {data && data.notWatched.length ? (
          <ul className="obs-list">
            {data.notWatched.map((n) => (
              <li key={`${n.app}|${n.env}`} className="obs-line">
                <span className="obs-strong" data-copy="data" data-user-text>{n.app} · {n.env}</span>
                {n.scrapeUrl ? <span className="inf-mono inf-trunc inf-faint" title={n.scrapeUrl} data-copy="data">{n.scrapeUrl}</span> : null}
                <span className="ag-spacer" />
                {n.dashboardUrl ? (
                  <a className="inf-link" href={n.dashboardUrl} target="_blank" rel="noreferrer noopener">Dashboard ↗</a>
                ) : <span className="inf-faint" title="Deploy the monitoring stack in Sources">No dashboard</span>}
              </li>
            ))}
          </ul>
        ) : <Empty>Every scraped app is watched</Empty>}
      </section>

      {url.app ? <AppDrawer {...props} targetId={url.app} row={data?.rows.find((r) => r.targetId === url.app) ?? null} /> : null}
    </div>
  );
}

// ── Ask BOSS (header) ───────────────────────────────────────────────────────────────────────

export function ObsAskBoss({ pulse }: ObsTabProps) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  const asks = obsAsks(pulse.data);
  return (
    <span className="inf-pop" ref={box}>
      <button type="button" className="ag-btn" aria-haspopup="menu" aria-expanded={open} data-testid="obs-ask"
              title="Ask BOSS about what is on this screen" onClick={() => setOpen((v) => !v)}>Ask BOSS</button>
      {open ? (
        <span className="inf-menu obs-ask-menu" role="menu" data-testid="obs-ask-menu">
          {asks.map((a) => (
            // The starter IS the question the dock will be prefilled with (data, editable there),
            // so it is shown whole and is not a tooltip.
            <button key={a.question} type="button" role="menuitem" data-testid="obs-ask-starter" data-user-text
                    onClick={() => { setOpen(false); openDockAbout({ subject: a.subject, question: a.question, role: null }); }}>
              <span className="obs-ask-q">{a.question}</span>
            </button>
          ))}
          <button type="button" role="menuitem" data-testid="obs-ask-open"
                  onClick={() => { setOpen(false); openDockAbout({ subject: null, question: "", role: null }); }}>
            Open BOSS here
          </button>
        </span>
      ) : null}
    </span>
  );
}
