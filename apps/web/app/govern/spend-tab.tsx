"use client";

/**
 * Govern › Spend (spec §4.4.4): where money went, over ONE window for every figure on the tab —
 * the six screens that each defined "spend" their own way are one definition here. Money
 * excludes subscription calls; what those ran is shown beside it as `Included`, never in it.
 *
 * By agent and by model (D4, parity with the old card): money, with `Included` beside it —
 * the old card added subscription runs at list price into "spend". Spent vs cap links on to
 * Observability › Models (infra-observability spec §6.7) for the deeper model view.
 */
import React, { useState } from "react";

import { modelName, money, readGovern, tip14, type ModelSpendRow, type SpendView } from "../../lib/govern";
import { nameOf } from "../agents/run-face";
import { usePolledRead } from "../../lib/log-stream";
import { consoleHref } from "../url-intent";
import { DailyBars } from "./cap-preview";
import { useIdentityVersion } from "../../lib/use-agent-identities";


const WINDOWS = ["24h", "7d", "30d"] as const;

function SpendRows({ rows, label, testid, nameFor }: {
  rows: ModelSpendRow[]; label: string; testid: string; nameFor: (id: string) => string;
}) {
  const anyIncluded = rows.some((r) => r.included_usd_micros > 0);
  return (
    <section className="gv-card" aria-label={label} data-testid={testid}>
      <h3 className="gv-sec-head">{label.toUpperCase()}</h3>
      {rows.length ? (
        <ul className="gv-list gv-spend-rows">
          <li className="gv-spend-head" aria-hidden="true">
            <span />
            <span>Calls</span><span>Money</span>{anyIncluded ? <span title="Ran on a subscription. Not money.">Included</span> : null}
          </li>
          {rows.map((r) => (
            <li key={r.id}>
              <span data-user-text title={r.id}>{nameFor(r.id)}</span>
              <span>{r.calls.toLocaleString("en-US")}</span>
              <span>{money(r.money_usd_micros)}</span>
              {anyIncluded ? <span className="gv-muted">{r.included_usd_micros ? money(r.included_usd_micros) : "—"}</span> : null}
            </li>
          ))}
        </ul>
      ) : <span className="gv-muted">No model calls</span>}
    </section>
  );
}

function pctText(v: unknown): string {
  return typeof v === "number" && Number.isFinite(v) ? `${Math.round(v * 100)}%` : "—";
}

export function SpendTab({ projectId, paused }: { projectId: string; paused: boolean }) {
  useIdentityVersion(); // names land when the install's agents load
  const [win, setWin] = useState<(typeof WINDOWS)[number]>("7d");
  const read = usePolledRead<SpendView>({
    key: `${projectId}|${win}`, pollMs: 60_000, paused,
    read: async (etag, signal) => {
      const got = await readGovern<SpendView>(projectId, "spend", { window: win }, { etag, signal });
      return got.ok ? (got.notModified ? { ok: true as const, notModified: true, etag: got.etag }
        : { ok: true as const, data: got.data, etag: got.etag }) : { ok: false as const, error: got.error };
    },
  });
  const s = read.data;
  const search = typeof window === "undefined" ? "" : window.location.search;
  const card = s?.scorecard ?? {};
  const cacheRatio = card.cache_ratio as number | null | undefined;
  const noSpend = s && s.money_usd_micros === 0 && s.included_usd_micros === 0 && !(Number(card.calls) > 0);

  return (
    <div className="gv-spend" data-testid="gv-spend">
      <div className="gv-spend-bar">
        <span className="ag-seg" role="group" aria-label="Window">
          {WINDOWS.map((w) => (
            <button key={w} type="button" aria-pressed={win === w} data-testid={`gv-window-${w}`} onClick={() => setWin(w)}>{w}</button>
          ))}
        </span>
        {s ? (
          <span className="gv-spend-total" data-testid="gv-spend-total">
            <span>{money(s.money_usd_micros)} spent</span>
            {s.included_usd_micros > 0 ? <span title="Ran on a subscription. Not money, not counted."> · Included {money(s.included_usd_micros)}</span> : null}
          </span>
        ) : null}
      </div>
      {read.error && !s ? (
        <div className="ag-empty" role="alert"><span title={read.error}>Can&apos;t reach BOSS</span>
          <button type="button" className="ag-btn" onClick={read.reload}>Retry</button></div>
      ) : !s ? (
        <div className="gv-skel" aria-busy="true" />
      ) : noSpend ? (
        <div className="ag-empty" data-testid="gv-spend-empty">No model spend yet</div>
      ) : (
        <>
          <section className="gv-card" aria-labelledby="gv-daily-h">
            <h3 id="gv-daily-h" className="gv-sec-head">DAILY SPEND</h3>
            <DailyBars daily={s.daily} cap={s.daily_cap_usd_micros || null} />
          </section>
          <section className="gv-card gv-models" aria-labelledby="gv-models-h" data-testid="gv-models-link">
            <h3 id="gv-models-h" className="gv-sec-head">MODEL SPEND</h3>
            <span className="gv-models-line">
              <span>{money(s.spent_vs_cap.committed_usd_micros)} of {money(s.spent_vs_cap.cap_usd_micros)}</span>
              <span className="gv-state" data-state={s.spent_vs_cap.state}>{s.spent_vs_cap.state_label}</span>
              <a className="ag-link" href={consoleHref(search, { area: "observability", tab: "models" })}
                 title="Spend by agent and by model">Models ↗</a>
            </span>
          </section>
          <div className="gv-pair">
            <SpendRows rows={s.by_agent ?? []} label="By agent" testid="gv-by-agent"
                       nameFor={(id) => (id === "unknown" ? "Unknown" : nameOf(id).name)} />
            <SpendRows rows={s.by_model ?? []} label="By model" testid="gv-by-model" nameFor={modelName} />
          </div>
          <div className="gv-tiles">
            <section className="gv-tile" data-testid="gv-cache">
              <span className="gv-tile-label">CACHE</span>
              <b>Cache reads {pctText(cacheRatio)}</b>
              {typeof cacheRatio === "number" && cacheRatio < 0.3 ? (
                <span className="gv-low" title="Under 30% of input came from cache">Low</span>
              ) : null}
              <span className="gv-tile-sub" title="Fresh and cached input tokens">
                {Number(card.prompt_tokens ?? 0).toLocaleString()} fresh · {Number(card.cache_read_tokens ?? 0).toLocaleString()} cached
              </span>
            </section>
            <section className="gv-tile" data-testid="gv-scorecard">
              <span className="gv-tile-label">SCORECARD</span>
              <span>Calls {Number(card.calls ?? 0).toLocaleString()}</span>
              <span title="Calls that failed">Failed {Number(card.failed_calls ?? 0).toLocaleString()}</span>
              <span title="Money spent on failed calls">Failed burn {money(Number(card.failed_burn_usd_micros ?? 0))}</span>
              <span title="Average tokens per call">Avg {Math.round(Number(card.avg_tokens_per_call ?? 0)).toLocaleString()} tok</span>
              <span title="Money per successful call">Per ok call {money(Number(card.cost_per_ok_call_usd_micros ?? 0))}</span>
            </section>
          </div>
          {Array.isArray(card.by_stage) && card.by_stage.length ? (
            <section className="gv-card" aria-labelledby="gv-stage-h">
              <h3 id="gv-stage-h" className="gv-sec-head">By step</h3>
              <ul className="gv-list">
                {(card.by_stage as Record<string, unknown>[]).slice(0, 8).map((st) => (
                  <li key={String(st.stage)}>
                    <span data-user-text>{String(st.stage).replace(/_/g, " ")}</span>
                    <span>{Number(st.calls ?? 0)} calls</span>
                    <span>{money(Number(st.cost_usd_micros ?? 0))}</span>
                    <span title="Cache reads">{pctText(st.cache_ratio)}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
          <section className="gv-card" aria-labelledby="gv-save-h" data-testid="gv-savings">
            <h3 id="gv-save-h" className="gv-sec-head">SAVINGS</h3>
            {s.savings.length ? (
              <ul className="gv-list">
                {s.savings.map((x) => (
                  <li key={x.kind}>
                    <span className="gv-sev" data-sev={x.severity}>{x.severity ?? "info"}</span>
                    <span className="gv-finding" data-user-text title={tip14(x.finding)}>{x.finding}</span>
                    {Number((x as { potential_saving_usd_micros?: number }).potential_saving_usd_micros ?? 0) > 0 ? (
                      <span>Save ≈ {money(Number((x as { potential_saving_usd_micros?: number }).potential_saving_usd_micros))}</span>
                    ) : null}
                    {x.action ? <span className="gv-action" title={tip14(x.action)}>What to do</span> : null}
                  </li>
                ))}
              </ul>
            ) : <span className="gv-muted">No savings found</span>}
          </section>
          <section className="gv-card" aria-labelledby="gv-drivers-h" data-testid="gv-drivers">
            <h3 id="gv-drivers-h" className="gv-sec-head">TOP DRIVERS</h3>
            {s.top_drivers.length ? (
              <ul className="gv-list">
                {s.top_drivers.map((d) => (
                  <li key={d.id}><span data-user-text title={d.id}>{modelName(d.id)}</span><span>{money(d.amount_usd_micros)}</span></li>
                ))}
              </ul>
            ) : <span className="gv-muted">No money spent</span>}
          </section>
        </>
      )}
    </div>
  );
}
