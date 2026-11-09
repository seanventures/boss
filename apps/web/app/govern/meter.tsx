"use client";

/**
 * The spend meter (spec §4.4.2), on every Govern tab: the binding period's money (solid) and
 * holds (hatched) against its cap, the four thresholds drawn at their real percentages, the
 * reset, and the other period beneath. `Included in plan` is a faint dotted line under the bars
 * that never fills them: a subscription call is not money and counts against no cap.
 */
import React from "react";

import { money, type Meter as MeterData, type Period } from "../../lib/govern";
import { consoleHref } from "../console-href";

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

/** Money spent this period, the same figure get_costs gives (included_spend.ledger_spend): what
 *  the caps count (settled, and running builds' money not settled yet), plus what no cap counts , 
 *  builds from before builds were metered, and self-fixes (their own budget), said apart only
 *  while there are any, and what subscription calls ran beside it, at list price, never money. */
function Spent({ ledger, fallbackIncluded }: { ledger?: MeterData["ledger"]; fallbackIncluded: number }) {
  const included = ledger ? ledger.included_usd_micros : fallbackIncluded;
  if (!ledger && included <= 0) return null;
  return (
    <div className="gv-included" data-testid="gv-spent">
      {ledger ? (
        <span data-testid="gv-spent-money"
              title={`On API keys: ${ledger.metered_calls} calls. Counted by caps ${money(ledger.under_cap_usd_micros + (ledger.in_flight_usd_micros ?? 0))}.`}>
          Spent {money(ledger.money_usd_micros)}
        </span>
      ) : null}
      {ledger && ledger.not_under_cap_usd_micros > 0 ? (
        <span data-testid="gv-spent-uncapped" title="Spent outside the caps: builds that ran before caps counted builds, and BOSS's self-fixes (they keep their own budget). New builds are under the caps.">
          {", "}Outside caps {money(ledger.not_under_cap_usd_micros)} ⓘ
        </span>
      ) : null}
      {included > 0 ? (
        <span data-testid="gv-included"
              title={ledger ? `Ran on a subscription (${ledger.included_calls} calls): an estimate at list price. Not money, not counted.`
                : "Ran on a subscription. Not money, not counted."}>
          {ledger ? `, Included in plan ~${money(included)}` : `Included in plan ${money(included)}`}
        </span>
      ) : null}
    </div>
  );
}

/** The holds no cap reset clears (an unreconciled model call or provider attempt), said with
 *  their decision. Shown under the meter and on Govern › Spend (BOSSAPP-807). */
export function LegacyHolds({ meter }: { meter: MeterData }) {
  const historicalHold = meter.legacy_financial_hold;
  const providerHold = meter.legacy_provider_hold;
  return (
    <>
      {historicalHold ? <div className="gv-legacy-hold" role="status" data-testid="gv-legacy-hold">
        <span>An earlier model call needs reconciliation. New paid work is held. Earlier estimate {money(historicalHold.original_estimate_usd_micros)};
          actual charge unknown. A cap reset will not clear this hold.</span>
        {historicalHold.decision_id ? <a className="ag-link" href={consoleHref({ area: "decisions", decision: historicalHold.decision_id })}>
          Open decision
        </a> : null}
      </div> : null}
      {providerHold ? <div className="gv-legacy-hold" role="status" data-testid="gv-legacy-provider-hold">
        <span>An earlier provider action may have been accepted. New paid work is held until that attempt is reconciled.
          Earlier estimate {money(providerHold.original_estimate_usd_micros)}; actual charge unknown.
          A cap reset will not clear this hold.</span>
        {providerHold.decision_id ? <a className="ag-link" href={consoleHref({ area: "decisions", decision: providerHold.decision_id })}>
          Review provider attempt
        </a> : null}
      </div> : null}
    </>
  );
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
  const historicalHold = meter.legacy_financial_hold;
  const providerHold = meter.legacy_provider_hold;
  const paidWorkHeld = Boolean(historicalHold || providerHold);
  const metaTip = [
    `Reserved ${money(meter.reserved_usd_micros)}, ${holds(meter.active_reservations_count)}`,
    `Burn ${money(meter.burn_rate_usd_micros_per_hour)}/h`,
    meter.time_to_cap_seconds !== null ? `Cap in ${capIn(meter.time_to_cap_seconds)}` : "",
  ].filter(Boolean).join(", ");
  return (
    <section className="gv-meter" data-testid="gv-meter" data-state={meter.state} title={metaTip}>
      <div className="gv-meter-line">
        <span className="gv-meter-period" data-testid="gv-meter-period">{periodWord(meter.period)}</span>
        <span className="gv-meter-amount" data-testid="gv-meter-amount"
              title={`What the cap counts: spent plus ${money(meter.reserved_usd_micros)} held by running work`}>
          {money(meter.committed_usd_micros)} <span className="gv-of">of</span> {money(meter.cap_usd_micros)}
        </span>
        <span className="gv-state" data-state={paidWorkHeld ? "held" : meter.state} data-testid="gv-meter-state">
          {paidWorkHeld ? "Paid work held" : meter.state_label}
        </span>
        <span className="gv-reset">resets {meter.resets_at_label}</span>
        <span className="gv-spacer" />
        <span className="gv-meta" data-testid="gv-meter-meta">
          <span>Reserved {money(meter.reserved_usd_micros)}, {holds(meter.active_reservations_count)}</span>
          <span>Burn {money(meter.burn_rate_usd_micros_per_hour)}/h</span>
          {meter.time_to_cap_seconds !== null ? <span>Cap in {capIn(meter.time_to_cap_seconds)}</span> : null}
        </span>
      </div>
      <LegacyHolds meter={meter} />
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
      <Spent ledger={meter.ledger} fallbackIncluded={meter.included_in_subscription_usd_micros} />
    </section>
  );
}
