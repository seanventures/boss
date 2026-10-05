"use client";

/**
 * The spend meter (spec §4.4.2), on every Govern tab: the binding period's money (solid) and
 * holds (hatched) against its cap, the four thresholds drawn at their real percentages, the
 * reset, and the other period beneath. `Included in plan` is a faint dotted line under the bars
 * that never fills them: a subscription call is not money and counts against no cap.
 */
import React from "react";

import { money, type Meter as MeterData, type Period } from "../../lib/govern";

const TICK_LABEL: Record<string, string> = {
  soft_cap_pct: "Heads-up", auto_lite_mode_pct: "Cheaper builds",
  freeze_provisioning_pct: "No new servers", freeze_tier2_pct: "Risky work stops",
};

const holds = (n: number) => `${n} hold${n === 1 ? "" : "s"}`;
const pct = (n: number) => `${Math.max(0, Math.min(100, n * 100)).toFixed(2)}%`;

function Bar({ cap, actual, reserved, ticks, forecast, forecastTip, thin, testid }: {
  cap: number; actual: number; reserved: number; ticks?: { key: string; at: number }[]; forecast?: number;
  forecastTip?: string; thin?: boolean; testid: string;
}) {
  const a = cap > 0 ? actual / cap : 0;
  const r = cap > 0 ? reserved / cap : 0;
  return (
    <div className="gv-bar" data-thin={thin || undefined} data-testid={testid}
         role="img" aria-label={`${money(actual + reserved)} of ${money(cap)}`}>
      <span className="gv-bar-fill" data-testid={`${testid}-fill`} style={{ width: pct(a) }} />
      {reserved > 0 ? (
        <span className="gv-bar-held" data-testid={`${testid}-held`} title={`Reserved ${money(reserved)}`}
              style={{ left: pct(a), width: pct(Math.min(r, 1 - Math.min(a, 1))) }} />
      ) : null}
      {(ticks ?? []).map((t) => (
        <span key={t.key} className="gv-tick" data-testid={`gv-tick-${t.key}`} data-at={t.at}
              title={`${TICK_LABEL[t.key]} ${Math.round(t.at * 100)}%`} style={{ left: pct(t.at) }} />
      ))}
      {forecast && forecast > 0 && cap > 0 ? (
        <span className="gv-forecast" data-testid="gv-forecast" title={forecastTip}
              style={{ left: pct(Math.min(forecast / cap, 1)) }} />
      ) : null}
    </div>
  );
}

function periodWord(p: string): string {
  return p === "month" ? "THIS MONTH" : "TODAY";
}

function capIn(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return "";
  if (seconds <= 0) return "now";
  const h = Math.floor(seconds / 3600);
  return h >= 1 ? `${h}h` : `${Math.max(1, Math.round(seconds / 60))}m`;
}

export function GovernMeter({ meter, error, onRetry }: { meter: MeterData | null; error: string; onRetry: () => void }) {
  if (!meter) {
    return (
      <section className="gv-meter" data-testid="gv-meter" aria-busy={!error || undefined}>
        {error ? (
          <span className="gv-unreachable" role="alert">
            <span title={error}>Can&apos;t reach BOSS</span>
            <button type="button" className="ag-btn" onClick={onRetry}>Retry</button>
          </span>
        ) : <span className="gv-meter-skel" />}
      </section>
    );
  }
  const other: Period[] = meter.periods.filter((p) => p.period !== meter.period);
  const ticks = (["soft_cap_pct", "auto_lite_mode_pct", "freeze_provisioning_pct", "freeze_tier2_pct"] as const)
    .map((key) => ({ key, at: Number(meter[key] ?? 0) }));
  const forecast = meter.period === "day" ? meter.forecasted_24h_usd_micros : 0;
  const metaTip = [
    `Reserved ${money(meter.reserved_usd_micros)} · ${holds(meter.active_reservations_count)}`,
    `Burn ${money(meter.burn_rate_usd_micros_per_hour)}/h`,
    meter.time_to_cap_seconds !== null ? `Cap in ${capIn(meter.time_to_cap_seconds)}` : "",
  ].filter(Boolean).join(" · ");
  return (
    <section className="gv-meter" data-testid="gv-meter" data-state={meter.state} title={metaTip}>
      <div className="gv-meter-line">
        <span className="gv-meter-period" data-testid="gv-meter-period">{periodWord(meter.period)}</span>
        <span className="gv-meter-amount" data-testid="gv-meter-amount">
          {money(meter.committed_usd_micros)} <span className="gv-of">of</span> {money(meter.cap_usd_micros)}
        </span>
        <span className="gv-state" data-state={meter.state} data-testid="gv-meter-state">{meter.state_label}</span>
        <span className="gv-reset">resets {meter.resets_at_label}</span>
        <span className="gv-spacer" />
        <span className="gv-meta" data-testid="gv-meter-meta">
          <span>Reserved {money(meter.reserved_usd_micros)} · {holds(meter.active_reservations_count)}</span>
          <span>Burn {money(meter.burn_rate_usd_micros_per_hour)}/h</span>
          {meter.time_to_cap_seconds !== null ? <span>Cap in {capIn(meter.time_to_cap_seconds)}</span> : null}
        </span>
      </div>
      <Bar cap={meter.cap_usd_micros} actual={meter.actual_usd_micros} reserved={meter.reserved_usd_micros}
           ticks={ticks} forecast={forecast} testid="gv-bar"
           forecastTip={`Spend over ${meter.burn_window_hours}h plus reservations, projected`} />
      {other.map((p) => (
        <div key={p.period} className="gv-meter-other" data-testid={`gv-meter-${p.period}`}>
          <span className="gv-other-word">{p.period === "month" ? "This month" : "Today"}</span>
          <span className="gv-other-amount">{money(p.committed_usd_micros)} <span className="gv-of">of</span> {money(p.cap_usd_micros)}</span>
          <Bar cap={p.cap_usd_micros} actual={p.actual_usd_micros} reserved={p.reserved_usd_micros} thin testid={`gv-bar-${p.period}`} />
        </div>
      ))}
      {meter.included_in_subscription_usd_micros > 0 ? (
        <div className="gv-included" data-testid="gv-included" title="Ran on a subscription. Not money, not counted.">
          Included in plan {money(meter.included_in_subscription_usd_micros)}
        </div>
      ) : null}
    </section>
  );
}
