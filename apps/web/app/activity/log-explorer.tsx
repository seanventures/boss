"use client";

/**
 * The Activity log: one fast explorer over everything BOSS wrote (activity-govern spec §4.2).
 *
 *   header   `Activity log · 47,210 lines in 24h · ● Live`, the range, Export
 *   bar      every filter as a token; the URL carries all of it
 *   views    All · Errors · Model calls · Deploys · Govern changes · Refusals
 *   pulse    lines per bucket, stacked by level; drag to zoom
 *   rail     LEVEL · STREAM · AGENT · RUN · PROCESS · STATUS with server counts
 *   list     windowed lines, newest first; `n new ↑` never moves a line under the reader
 *   drawer   one line, its facts, links, the lines around it
 *
 * Reads are server-side queries (keyset pages, SQL counts); the tail is an ETag poll that is a
 * 304 when nothing happened. Paused (the console's chip) stops every poll and every motion.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DotsThree, DownloadSimple, Funnel } from "@phosphor-icons/react";

import type { ActivityLogItem } from "../../lib/activity-log";
import { useConsoleLive } from "../../lib/console-live";
import { exportHref, fetchFacets, fetchHistogram, fetchKinds, fetchLogPage, fetchRow } from "../../lib/log-api";
import { useLogStream, usePolledRead } from "../../lib/log-stream";
import {
  RANGES, SCOPE_KEYS, VIEWS, apiParams, readLogParams, toggleToken, viewOf, writeLogParams,
  type LogRange, type LogState, type LogToken,
} from "../../lib/log-url";
import { showToast } from "../client-forms";
import { consoleHref } from "../url-intent";
import { FacetRail } from "./facet-rail";
import { LogDrawer } from "./log-drawer";
import { LogRows, lineOrder } from "./log-rows";
import { PulseStrip } from "./pulse-strip";
import { QueryBar } from "./query-bar";
import { plural } from "../../lib/factory-floor";
import { useAreaKeydown } from "../../lib/area-keys";

export const LOG_POLL_MS = 5000;
const PAGE = 100;
const NEXT_RANGE: Record<LogRange, LogRange | null> = { "15m": "1h", "1h": "24h", "24h": "7d", "7d": "30d", "30d": null };

function rangeWords(state: LogState): string {
  if (state.from) return "this range";
  return state.range ?? "24h";
}

function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function LogExplorer({ projectId, actor }: { projectId: string; actor: string }) {
  const live = useConsoleLive();
  // The server renders with no URL; the URL is read once mounted, and nothing is fetched before
  // it is, so the first paint matches the server's and no read is made for the wrong view.
  const [state, setState] = useState<LogState>(() => readLogParams(""));
  const [ready, setReady] = useState(false);
  const [facetKey, setFacetKey] = useState(() => JSON.stringify(apiParams(readLogParams(""))));
  useEffect(() => {
    const fromUrl = readLogParams(window.location.search);
    setState(fromUrl);
    setFacetKey(JSON.stringify(apiParams(fromUrl)));
    setReady(true);
  }, []);
  const [holdingScroll, setHoldingScroll] = useState(false);
  const [railOpen, setRailOpen] = useState(false);
  const [custom, setCustom] = useState<{ from: string; to: string } | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [labelsSeen, setLabelsSeen] = useState<Record<string, string>>({});
  const prevRange = useRef<LogState | null>(null);

  // THE URL IS THE STATE. Back/Forward re-read it; every change writes it (a scope change is a
  // new history entry, so Back leaves a step's log; a filter change replaces the entry).
  useEffect(() => {
    const on = () => setState(readLogParams(window.location.search));
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  const apply = useCallback((next: LogState, push = false) => {
    setState(next);
    try {
      const search = writeLogParams(next, window.location.search);
      const url = `${window.location.pathname}${search}`;
      if (push) window.history.pushState(null, "", url); else window.history.replaceState(null, "", url);
    } catch { /* a sandboxed frame without history keeps the in-memory state */ }
  }, []);
  const setTokens = useCallback((tokens: LogToken[], scopeChange = false) => {
    apply({ ...state, tokens, row: null }, scopeChange);
  }, [apply, state]);

  const params = useMemo(() => apiParams(state), [state]);
  const queryKey = JSON.stringify(params);

  // ── the pulse and the header count (one read, ETag-polled) ──
  const histogram = usePolledRead({
    key: `${projectId}|${queryKey}`, pollMs: state.from ? null : LOG_POLL_MS, paused: !live, enabled: ready,
    read: async (etag, signal) => {
      const got = await fetchHistogram(projectId, params, { etag, signal });
      return got.ok ? (got.notModified ? { ok: true as const, notModified: true, etag: got.etag }
        : { ok: true as const, data: got.data, etag: got.etag }) : { ok: false as const, error: got.error };
    },
  });

  // ── the rail (debounced: a burst of clicks asks once, and a stale answer is aborted) ──
  useEffect(() => {
    const t = setTimeout(() => setFacetKey(queryKey), 250);
    return () => clearTimeout(t);
  }, [queryKey]);
  const facets = usePolledRead({
    key: `${projectId}|${facetKey}`, pollMs: null, paused: !live, enabled: ready,
    read: async (etag, signal) => {
      const got = await fetchFacets(projectId, JSON.parse(facetKey) as Record<string, string>, { etag, signal });
      return got.ok ? (got.notModified ? { ok: true as const, notModified: true, etag: got.etag }
        : { ok: true as const, data: got.data, etag: got.etag }) : { ok: false as const, error: got.error };
    },
  });

  // ── the list ──
  const [queryError, setQueryError] = useState("");
  const [toTop, setToTop] = useState(0);
  const [toolsOpen, setToolsOpen] = useState(false);
  const findKinds = useCallback(async (prefix: string, signal: AbortSignal) => {
    const got = await fetchKinds(projectId, params, prefix, { signal });
    return got.ok && got.data ? got.data : [];
  }, [projectId, params]);
  const holding = holdingScroll || !!state.row;
  const stream = useLogStream<ActivityLogItem>({
    key: `${projectId}|${queryKey}`,
    idOf: (i) => i.id,
    pollMs: LOG_POLL_MS,
    paused: !live || !!state.from,
    holding,
    enabled: ready,
    first: async (signal) => {
      const got = await fetchLogPage(projectId, { ...params, limit: PAGE }, { signal });
      if (!got.ok) {
        setQueryError(got.status === 422 ? got.error : "");
        return { error: got.status === 422 ? "" : got.error };
      }
      if (!got.data) return { error: "" };
      setQueryError("");
      setLabelsSeen((l) => ({ ...l, ...got.data!.labels }));
      return { items: got.data.items, next: got.data.nextCursor ?? null, newest: got.data.newestCursor ?? null, etag: got.etag };
    },
    older: async (cursor, signal) => {
      const got = await fetchLogPage(projectId, { ...params, limit: PAGE, cursor }, { signal });
      if (!got.ok || !got.data) return { error: got.ok ? "" : got.error };
      setLabelsSeen((l) => ({ ...l, ...got.data!.labels }));
      return { items: got.data.items, next: got.data.nextCursor ?? null, newest: null };
    },
    newer: async (newest, etag, signal) => {
      const got = await fetchLogPage(projectId, { ...params, limit: PAGE, ...(newest ? { after: newest } : {}) }, { etag, signal });
      if (!got.ok) return { error: got.error };
      if (!got.data) return { items: [], newest, etag: got.etag, notModified: true };
      setLabelsSeen((l) => ({ ...l, ...got.data!.labels }));
      return { items: got.data.items, newest: got.data.items.length ? `${got.data.items[0].createdAt}|${got.data.items[0].id}` : newest,
               etag: got.etag, gap: got.data.gap };
    },
  });
  const items = stream.items;
  const labels = labelsSeen;

  // ── the open line (`row=`): from the page, else fetched; gone says so ──
  const [rowFetched, setRowFetched] = useState<{ id: string; item: ActivityLogItem | null } | null>(null);
  const openItem = useMemo(() => (state.row ? items?.find((i) => i.id === state.row) ?? null : null), [state.row, items]);
  useEffect(() => {
    if (!state.row || openItem || items === null) return;
    if (rowFetched?.id === state.row) return;
    let alive = true;
    void fetchRow(projectId, state.row).then((got) => {
      if (!alive) return;
      const found = got.ok && got.data ? got.data : null;
      if (found) setLabelsSeen((l) => ({ ...l, ...found.labels }));
      setRowFetched({ id: state.row!, item: found?.item ?? null });
    });
    return () => { alive = false; };
  }, [state.row, openItem, items, projectId, rowFetched]);
  const drawerItem = openItem ?? (rowFetched?.id === state.row ? rowFetched?.item ?? null : null);
  const drawerGone = !!state.row && !openItem && rowFetched?.id === state.row && !rowFetched.item;

  const order = useMemo(() => lineOrder(items ?? [], state.group === "run", labels), [items, state.group, labels]);
  const openRow = useCallback((id: string | null) => apply({ ...state, row: id }), [apply, state]);
  const step = useCallback((delta: number) => {
    if (!order.length) return;
    const at = state.row ? order.indexOf(state.row) : -1;
    const next = order[Math.min(order.length - 1, Math.max(0, at + delta))];
    if (next) openRow(next);
  }, [order, state.row, openRow]);

  // J/K (and arrows) move through lines; Enter opens; Esc closes.
  useAreaKeydown((e: KeyboardEvent) => {
    const target = e.target as HTMLElement | null;
    if (target?.closest("input, textarea, select, [contenteditable=true], .lx-drawer")) return;
    if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); step(1); }
    else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); step(-1); }
    else if (e.key === "Escape" && state.row) { e.preventDefault(); openRow(null); }
  });

  const setRange = (range: LogRange) => {
    prevRange.current = null; setToolsOpen(false); apply({ ...state, range, from: null, to: null });
  };
  const zoom = (from: string, to: string) => {
    if (!prevRange.current) prevRange.current = state;
    apply({ ...state, from, to, range: null });
  };
  const unzoom = () => {
    const back = prevRange.current;
    prevRange.current = null;
    if (back) apply({ ...state, range: back.range, from: back.from, to: back.to });
  };

  const download = async (format: "csv" | "ndjson") => {
    setExportOpen(false);
    setExporting(true);
    try {
      const r = await fetch(exportHref(projectId, params, format));
      if (!r.ok) throw new Error(`BOSS answered ${r.status}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `boss-log.${format}`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      if (r.headers.get("x-truncated") === "1") showToast("success", "First 50,000 lines exported");
    } catch (e) {
      showToast("error", String((e as Error)?.message ?? e));
    } finally {
      setExporting(false);
    }
  };

  const total = histogram.data?.total ?? null;
  const exact = histogram.data?.totalIsExact !== false;
  const chosenView = viewOf(state.tokens);
  const filtered = state.tokens.length > 0 || !!state.q;
  const stepToken = state.tokens.find((t) => t.key === "step");
  const tail = !live ? "paused" : holding ? "holding" : state.from ? "fixed" : "live";

  const empty = (() => {
    if (!items || items.length) return null;
    if (stepToken) {
      const wfr = state.tokens.find((t) => t.key === "wfr")?.value;
      const done = wfr ? labels[`${wfr}/${stepToken.value}@state`] === "finished" : false;
      return <span>{done ? "Nothing logged for this step" : "Nothing logged for this step yet"}</span>;
    }
    if (filtered) {
      return (<><span>Nothing matches</span>
        <button type="button" className="ag-btn" data-testid="lx-clear-filters"
                onClick={() => apply({ ...state, tokens: [], q: "", row: null })}>Clear filters</button></>);
    }
    const wider = state.range ? NEXT_RANGE[state.range] : "7d";
    if (!wider) {
      return (<><span>Nothing logged yet</span>
        <a className="ag-link" href={consoleHref(typeof window === "undefined" ? "" : window.location.search,
                                                  { area: "delivery_workflows" })}>Start work ↗</a></>);
    }
    return (<><span>No lines in this range</span>
      <button type="button" className="ag-btn" data-testid="lx-widen" onClick={() => setRange(wider as LogRange)}>Widen to {wider}</button></>);
  })();

  const footer = items && items.length ? (
    stream.next ? (
      <button type="button" className="ag-btn lx-older" data-testid="lx-older" disabled={stream.loadingOlder}
              onClick={() => stream.loadOlder()}>{stream.olderError ? "Retry" : "Load older"}</button>
    ) : (
      <span className="lx-end" title="No older lines in this range">
        Start of range
        {state.range && NEXT_RANGE[state.range] ? (
          <button type="button" className="ag-link" onClick={() => setRange(NEXT_RANGE[state.range!]!)}>
            Widen to {NEXT_RANGE[state.range]}
          </button>
        ) : null}
      </span>
    )
  ) : null;

  return (
    <div className="lx" data-testid="activity-log" data-holding={holding || undefined}>
      <header className="lx-head">
        <h2 className="lx-title-main">Activity log</h2>
        <span className="lx-head-count" data-testid="lx-count" title={exact ? undefined : "At least this many. Counting stopped early."}>
          {total === null ? "" : `${exact ? "" : "≥"}${plural(total, "line").replace(/^\d+/, total.toLocaleString("en-US"))} in ${rangeWords(state)}`}
        </span>
        <span className="lx-tail" data-state={tail} data-testid="lx-tail"
              title={tail === "paused" ? "Console is paused. Nothing updates."
                : tail === "holding" ? "New lines wait above. Nothing moves while you read." : undefined}>
          {tail === "paused" ? "Paused" : tail === "holding" ? "Holding" : tail === "live" ? "Live" : "Fixed range"}
        </span>
        <span className="lx-spacer" />
        <button type="button" className="ag-btn lx-filters-btn" data-testid="lx-filters" aria-expanded={railOpen}
                onClick={() => setRailOpen((o) => !o)}>
          <Funnel size={13} aria-hidden /> Filters{state.tokens.length ? ` (${state.tokens.length})` : ""}
        </button>
        <button type="button" className="ag-btn lx-more-btn" data-testid="lx-more" aria-expanded={toolsOpen}
                aria-label="Range and export" title="Range and export" onClick={() => setToolsOpen((o) => !o)}>
          <DotsThree size={16} weight="bold" aria-hidden />
        </button>
        <span className="lx-head-tools" data-open={toolsOpen || undefined} data-testid="lx-head-tools">
        <span className="ag-seg lx-range" role="group" aria-label="Range" title="Times are local. Hover any time for UTC.">
          {RANGES.map((r) => (
            <button key={r} type="button" aria-pressed={!state.from && state.range === r} data-testid={`lx-range-${r}`}
                    title={r === "30d" ? "Agent turns and app output keep 30 days" : undefined}
                    onClick={() => setRange(r)}>{r}</button>
          ))}
          <button type="button" aria-pressed={!!state.from} data-testid="lx-range-custom"
                  onClick={() => setCustom(custom ? null : { from: toLocalInput(state.from), to: toLocalInput(state.to) })}>
            {state.from ? "Custom" : "Custom…"}
          </button>
        </span>
        <span className="lx-export">
          <button type="button" className="ag-btn" data-testid="lx-export" aria-expanded={exportOpen} disabled={exporting}
                  title="Exports what matches now, up to 50,000 lines" onClick={() => setExportOpen((o) => !o)}>
            <DownloadSimple size={13} aria-hidden /> {exporting ? "Exporting…" : "Export"}
          </button>
          {exportOpen ? (
            <span className="lx-menu" role="menu">
              <button type="button" role="menuitem" data-testid="lx-export-csv" onClick={() => void download("csv")}>CSV</button>
              <button type="button" role="menuitem" data-testid="lx-export-ndjson" onClick={() => void download("ndjson")}>NDJSON</button>
            </span>
          ) : null}
        </span>
        </span>
      </header>
      {custom ? (
        <form className="lx-custom" data-testid="lx-custom" noValidate
              onSubmit={(e) => {
                e.preventDefault();
                const iso = (v: string) => { const d = v ? new Date(v) : null; return d && !Number.isNaN(d.getTime()) ? d.toISOString() : null; };
                const from = iso(custom.from);
                const to = iso(custom.to);
                if (from) apply({ ...state, from, to, range: null });
                setCustom(null);
              }}>
          <label>From <input type="datetime-local" value={custom.from} onChange={(e) => setCustom({ ...custom, from: e.target.value })} /></label>
          <label>To <input type="datetime-local" value={custom.to} onChange={(e) => setCustom({ ...custom, to: e.target.value })} /></label>
          <button type="submit" className="ag-btn ag-btn-primary">Apply</button>
        </form>
      ) : null}
      <QueryBar tokens={state.tokens} q={state.q} labels={labels} names={state.names} facets={facets.data}
                findKinds={findKinds}
                error={queryError || (state.badRange ? `That range isn't a date. Showing the last ${state.range ?? "24h"}.` : "")}
                onTokens={(tokens, scope) => setTokens(tokens, scope)}
                onQuery={(q) => apply({ ...state, q, row: null })}
                onAdd={(token) => setTokens(addToken(state.tokens, token), SCOPE_KEYS.has(token.key))} />
      <div className="lx-views" role="group" aria-label="Views" data-testid="lx-views">
        {VIEWS.map((v) => (
          <button key={v.key} type="button" className="lx-view" aria-pressed={chosenView?.key === v.key}
                  data-testid={`lx-view-${v.key}`} title={v.tip}
                  onClick={() => setTokens([...state.tokens.filter((t) => SCOPE_KEYS.has(t.key)), ...v.tokens])}>
            {v.label}
          </button>
        ))}
        <span className="lx-spacer" />
        <span className="ag-seg lx-group" role="group" aria-label="Group">
          <button type="button" aria-pressed={state.group !== "run"} onClick={() => apply({ ...state, group: null })}>Lines</button>
          <button type="button" aria-pressed={state.group === "run"} data-testid="lx-by-run"
                  onClick={() => apply({ ...state, group: "run" })}>By run</button>
        </span>
      </div>
      <div className="lx-body" data-rail-open={railOpen || undefined}>
        <FacetRail facets={facets.data} tokens={state.tokens} showVerbose={state.showVerbose}
                   onTokens={(tokens) => setTokens(tokens, false)}
                   onVerbose={(show) => apply({ ...state, showVerbose: show })} />
        <main className="lx-main">
          <PulseStrip histogram={histogram.data} loading={histogram.loading} onZoom={zoom} onReset={unzoom} />
          {stream.fresh.length ? (
            <button type="button" className="lx-new" data-testid="lx-new" title="Show the newest lines"
                    onClick={() => { stream.showFresh(); setHoldingScroll(false); setToTop((n) => n + 1); }}>{stream.fresh.length} new ↑</button>
          ) : null}
          {stream.error ? (
            <div className="ag-empty lx-banner" role="alert" data-testid="lx-unreachable">
              <span title={stream.error}>Can&apos;t reach BOSS</span>
              <button type="button" className="ag-btn" onClick={() => stream.reload()}>Retry</button>
            </div>
          ) : items === null ? (
            <div className="lx-skeleton" aria-busy="true" data-testid="lx-loading">
              {Array.from({ length: 12 }, (_, i) => <i key={i} />)}
            </div>
          ) : empty ? (
            <div className="ag-empty lx-empty" data-testid="lx-empty">{empty}</div>
          ) : (
            <LogRows items={items} labels={labels} byRun={state.group === "run"} selectedId={state.row}
                     arrived={stream.arrived} onOpen={(id) => openRow(id)} onHolding={setHoldingScroll}
                     onNearEnd={() => { if (stream.next && !stream.loadingOlder && !stream.olderError) stream.loadOlder(); }}
                     footer={footer} toTop={toTop} />
          )}
        </main>
        {state.row ? (
          <LogDrawer item={drawerItem} labels={labels} gone={drawerGone} projectId={projectId} viewer={actor}
                     onClose={() => openRow(null)}
                     onFilter={(token) => setTokens(toggleToken(state.tokens, token), SCOPE_KEYS.has(token.key))}
                     onNext={() => step(1)} onPrev={() => step(-1)} />
        ) : null}
      </div>
    </div>
  );
}

/** Add a token (replacing the same key's value where the key holds one value). */
export function addToken(tokens: LogToken[], token: LogToken): LogToken[] {
  return toggleToken(tokens.filter((t) => !(t.key === token.key && t.value === token.value)), token);
}
