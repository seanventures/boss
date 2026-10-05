"use client";

/**
 * Observability › Models (spec §4.10): every model call BOSS made — tokens, time, errors,
 * retries and metered spend — by model, stage, agent or runner, over the window.
 *
 *   Calls 1,284 ╱╲_  Tokens 4.1M in · 610k out ╱‾╲  Spend $3.12 est. _╱╲  p95 18 s ‾╲_  Errors 2.1% · 27 _╱
 *   [Tokens|Calls|Spend|Latency]                                          by [Model|Stage|Agent|Runner]
 *   <the token stream: top 5 + Other>
 *   MODELS 6   Name · Calls · In · Out · Spend · Avg · p95 · Errors · Retries · trend
 *   FAILED CALLS · ROUTING
 *
 * The group and the metric live in the URL (`group=`, `metric=`); a band or a row selects
 * `model=<group>:<key>` and opens its drawer. Spend is metered spend only: never `$0` — work
 * that ran on a subscription reads `Included in plan` / `Included`. There is no provider column
 * because llm_calls has none; Runner is the engine.
 */
import React, { useMemo, useState } from "react";

import { buildHref, logsHref } from "../../lib/infra-view";
import {
  GROUPS, METRICS, compact, ms, pct, useModels, usd, type Group, type Metric, type ModelGroupRow, type Models,
} from "../../lib/obs-view";
import { RunFace } from "../agents/run-face";
import { plainClick } from "../agents/live-util";
import type { ObsTabProps } from "./obs-home";
import { GroupDrawer, groupLabel, readModelParam, spendWord } from "./group-drawer";
import { leaveTo } from "./obs-util";
import { Routing } from "./routing";
import { Spark } from "./spark";
import { TokenStream, slotOf } from "./token-stream";
import { Empty, Skeleton, ZoneHead } from "../infra/kit";
import { useLocalTime } from "../../lib/local-time";

// ── the stat strip ──────────────────────────────────────────────────────────────────────────

function Stat({ id, label, tip, value, spark, testId }: { id: string; label: string; tip?: string; value: React.ReactNode; spark: number[]; testId: string }) {
  return (
    <div className="obs-stat" data-testid={testId}>
      <span className="obs-stat-k" title={tip}>{label}</span>
      <span className="obs-stat-v inf-num" data-testid={`${testId}-value`}>{value}</span>
      <Spark values={spark} label={`${label} over the window`} testId={`obs-spark-${id}`} />
    </div>
  );
}

export function StatStrip({ m }: { m: Models }) {
  const t = m.totals;
  const spend = t.spendMicros > 0
    ? <span title={t.estimated ? "Some prices were estimated, not billed" : "Metered spend"}>{usd(t.spendMicros)}{t.estimated ? <span className="obs-est"> est.</span> : null}</span>
    : t.calls > 0
      ? <span title="Ran on a subscription. No metered spend." data-testid="obs-included">Included in plan</span>
      : <span className="inf-faint">—</span>;
  return (
    <div className="obs-stats" data-testid="obs-stats">
      <Stat id="calls" testId="obs-stat-calls" label="Calls" value={t.calls.toLocaleString("en-US")} spark={m.spark.calls} />
      <Stat id="tokens" testId="obs-stat-tokens" label="Tokens" value={<>{compact(t.tokensIn)} in · {compact(t.tokensOut)} out</>} spark={m.spark.tokens} />
      <Stat id="spend" testId="obs-stat-spend" label="Spend" value={spend} spark={m.spark.spend} />
      <Stat id="p95" testId="obs-stat-p95" label="p95" value={ms(t.p95Ms)} spark={m.spark.p95} />
      <Stat id="errors" testId="obs-stat-errors" label="Errors" tip="Calls that failed after retries, of all calls"
            value={t.calls ? <>{pct(t.failed / t.calls)} · {t.failed.toLocaleString("en-US")}</> : "—"} spark={m.spark.errors} />
    </div>
  );
}

// ── the table ───────────────────────────────────────────────────────────────────────────────

type SortKey = "name" | "calls" | "in" | "out" | "spend" | "avg" | "p95" | "errors" | "retries";
const COLS: { key: SortKey; label: string; tip?: string; cls?: string }[] = [
  { key: "name", label: "Name" }, { key: "calls", label: "Calls" }, { key: "in", label: "In", cls: "obs-c-in" },
  { key: "out", label: "Out", cls: "obs-c-out" }, { key: "spend", label: "Spend" }, { key: "avg", label: "Avg", cls: "obs-c-avg" },
  { key: "p95", label: "p95" }, { key: "errors", label: "Errors", tip: "Calls that failed after retries, of all calls" },
  { key: "retries", label: "Retries", cls: "obs-c-retries" },
];
const sortVal = (r: ModelGroupRow, k: SortKey): number | string => ({
  name: r.label.toLowerCase(), calls: r.calls, in: r.tokensIn, out: r.tokensOut, spend: r.spendMicros,
  avg: r.avgMs ?? -1, p95: r.p95Ms ?? -1, errors: r.calls ? r.failed / r.calls : 0, retries: r.retries,
})[k];

export function sortRows(rows: ModelGroupRow[], key: SortKey, dir: 1 | -1): ModelGroupRow[] {
  return [...rows].sort((a, b) => {
    const x = sortVal(a, key);
    const y = sortVal(b, key);
    if (x !== y) return (x < y ? -1 : 1) * dir;
    // The default order: Spend, then Calls.
    return (b.spendMicros - a.spendMicros) || (b.calls - a.calls);
  });
}

function GroupTable({ m, group, selected, onSelect, slots }: {
  m: Models; group: Group; selected: string | null; onSelect: (key: string) => void; slots: Map<string, string>;
}) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "spend", dir: -1 });
  const rows = useMemo(() => sortRows(m.groups, sort.key, sort.dir), [m.groups, sort]);
  const head = GROUPS.find((g) => g.key === group)?.head ?? "MODELS";
  const by = (k: SortKey) => setSort((s) => ({ key: k, dir: s.key === k ? (s.dir === 1 ? -1 : 1) : k === "name" ? 1 : -1 }));
  return (
    <section className="obs-zone" aria-labelledby="obs-models-table-head">
      <ZoneHead id="obs-models-table-head" label={head} count={m.groups.length} />
      {rows.length === 0 ? <Empty>No model calls yet</Empty> : (
        <div className="inf-table-wrap">
          <table className="inf-table obs-gtable" data-testid="obs-models-table">
            <thead>
              <tr>
                {COLS.map((c) => (
                  <th key={c.key} scope="col" className={c.cls}
                      aria-sort={sort.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                    <button type="button" className="obs-sort" title={c.tip} data-testid={`obs-sort-${c.key}`} onClick={() => by(c.key)}>
                      {c.label}{sort.key === c.key ? (sort.dir === 1 ? " ↑" : " ↓") : ""}
                    </button>
                  </th>
                ))}
                <th scope="col" className="obs-c-trend"><span className="ag-sr">Trend</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const sp = spendWord(r.spendMicros, r.included, r.estimated);
                const slot = slots.get(r.key);
                return (
                  <tr key={r.key} data-testid="obs-group-row" data-key={r.key} data-open={selected === r.key ? "1" : undefined}
                      className="obs-grow" tabIndex={0} onClick={() => onSelect(r.key)}
                      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(r.key); } }}>
                    <td>
                      <span className="obs-gname">
                        <span className="obs-swatch" data-slot={slot ?? "none"} aria-hidden />
                        {group === "role" ? <RunFace roleKey={r.roleKey} variant={r.roleKey ? "agent" : "boss"} size={20} /> : null}
                        <span className="inf-trunc" title={r.label} data-copy="data" data-user-text>{groupLabel(r, group)}</span>
                        {group === "engine" && r.guest ? <span className="obs-chip" title="A guest runner">guest</span> : null}
                      </span>
                    </td>
                    <td>{r.calls.toLocaleString("en-US")}</td>
                    <td className="obs-c-in">{compact(r.tokensIn)}</td>
                    <td className="obs-c-out">{compact(r.tokensOut)}</td>
                    <td title={sp.tip} data-testid="obs-group-spend">{sp.text}</td>
                    <td className="obs-c-avg">{ms(r.avgMs)}</td>
                    <td>{ms(r.p95Ms)}</td>
                    <td className={r.failed ? "obs-bad" : undefined}>{r.calls ? pct(r.failed / r.calls) : "—"}</td>
                    <td className="obs-c-retries">{r.retries}</td>
                    <td className="obs-c-trend"><Spark values={r.trend} label={`${r.label} trend`} slot={slot} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ── FAILED CALLS ────────────────────────────────────────────────────────────────────────────

function FailedCalls({ m, since }: { m: Models; since: string | null }) {
  const lt = useLocalTime();
  return (
    <section className="obs-zone" aria-labelledby="obs-failed-head" data-testid="obs-failed">
      <ZoneHead id="obs-failed-head" label="FAILED CALLS" count={m.totals.failed}>
        <a className="inf-link" href={logsHref("llm_call", null, since)} data-testid="obs-failed-logs"
           onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo(logsHref("llm_call", null, since)); } }}>Logs ↗</a>
      </ZoneHead>
      {m.failed.length === 0 ? <Empty>No failed calls</Empty> : (
        <ul className="obs-list">
          {m.failed.map((f, i) => (
            <li key={`${f.at}-${i}`} className="obs-line" data-testid="obs-failed-row">
              <span className="inf-num inf-muted" title={lt.full(f.at)}>
                {lt.hhmm(Date.parse(String(f.at)))}
              </span>
              <RunFace roleKey={f.roleKey} variant={f.roleKey ? "agent" : "boss"} size={20} />
              <span data-copy="data">{f.stage ?? "—"}</span>
              <span className="inf-mono inf-faint inf-trunc" data-copy="data">{f.model ?? "—"}</span>
              <span className="obs-bad">{f.errorWord}</span>
              {f.retries ? <span className="inf-faint inf-num" title="Retries">↻{f.retries}</span> : null}
              <span className="ag-spacer" />
              {f.runId ? (
                <a className="inf-link" href={buildHref(f.runId)} title="Open the build that made this"
                   onClick={(e) => { if (plainClick(e)) { e.preventDefault(); leaveTo(buildHref(f.runId!)); } }}>Build ↗</a>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ── the tab ─────────────────────────────────────────────────────────────────────────────────

export function ModelsTab({ projectId, url, go }: ObsTabProps) {
  const models = useModels(projectId, url.win, url.group, url.metric);
  const m = models.data;
  const sel = readModelParam(url.model);
  const selected = sel && sel.group === url.group ? sel.key : null;
  const slots = useMemo(() => {
    const out = new Map<string, string>();
    (m?.series.bands ?? []).forEach((b, i) => out.set(b.key, slotOf(b.key, i, b.label)));
    return out;
  }, [m]);
  const select = (key: string) => go({ model: `${url.group}:${key}` }, "push");
  const setGroup = (g: Group) => go({ group: g === "model" ? null : g, model: null }, "replace");
  const setMetric = (k: Metric) => go({ metric: k === "tokens" ? null : k }, "replace");

  if (models.loading && !m) return <Skeleton testId="obs-models-loading" />;
  if (!m) return <Empty testId="obs-models-error">Can&apos;t load model calls</Empty>;
  const empty = m.totals.calls === 0 && m.groups.length === 0;

  return (
    <div className="obs-models" data-testid="obs-models">
      {empty ? (
        <span className="inf-empty obs-models-none" title="Calls appear when an agent thinks" data-testid="obs-models-empty">No model calls yet</span>
      ) : null}
      <StatStrip m={m} />
      <div className="obs-controls">
        <span className="inf-seg" role="radiogroup" aria-label="Metric" data-testid="obs-metric">
          {METRICS.map((x) => (
            <button key={x.key} type="button" role="radio" aria-checked={url.metric === x.key} className="inf-seg-btn"
                    data-testid={`obs-metric-${x.key}`} onClick={() => setMetric(x.key)}>{x.label}</button>
          ))}
        </span>
        <span className="ag-spacer" />
        <span className="inf-faint obs-by">by</span>
        <span className="inf-seg" role="radiogroup" aria-label="Group by" data-testid="obs-group">
          {GROUPS.map((g) => (
            <button key={g.key} type="button" role="radio" aria-checked={url.group === g.key} className="inf-seg-btn"
                    data-testid={`obs-group-${g.key}`} title={g.key === "engine" ? "The engine that made the call" : undefined}
                    onClick={() => setGroup(g.key)}>{g.label}</button>
          ))}
        </span>
      </div>
      <TokenStream buckets={m.series.buckets} bands={m.series.bands} metric={url.metric} selected={selected} onSelect={select} />
      <GroupTable m={m} group={url.group} selected={selected} onSelect={select} slots={slots} />
      <FailedCalls m={m} since={m.window.from} />
      <Routing projectId={projectId} rows={m.routing.rows} total={m.routing.total} options={m.routing.filters} />
      {sel ? (
        <GroupDrawer projectId={projectId} group={sel.group} keyName={sel.key} since={m.window.from}
                     row={sel.group === url.group ? m.groups.find((g) => g.key === sel.key) ?? null : null}
                     onClose={() => go({ model: null }, "push")} />
      ) : null}
    </div>
  );
}
