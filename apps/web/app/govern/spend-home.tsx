"use client";

/**
 * Govern › Spend, the Govern landing (BOSSAPP-807): how much has been spent, on which model and
 * which project, and what happens at the cap, in one look. A hero (money of the cap, a badge,
 * one bar, one line), the days, and two short lists. Everything deeper (by agent, by step,
 * savings, top drivers) stays one click away in "Where the money went".
 *
 * Reads: the Limits view the screen already polls (the caps, the meter, the at-limit choice)
 * and GET /v1/govern/spend for the chosen period, nothing else.
 */
import React from "react";

import {
  modelName, money, type DailySpend, readGovern, type AtLimit, type GovernView, type Meter, type ModelSpendRow, type Period,
  type ProjectSpendRow, type SpendView,
} from "../../lib/govern";
import { usePolledRead } from "../../lib/log-stream";
import { DailyBars } from "./cap-preview";
import { LegacyHolds } from "./meter";
import { ReconciliationCard } from "./reconciliation-card";

export type SpendPeriod = "today" | "week" | "month";
export const SPEND_PERIODS: { key: SpendPeriod; label: string }[] = [
  { key: "today", label: "Today" }, { key: "week", label: "Week" }, { key: "month", label: "Month" }];
const WINDOW: Record<SpendPeriod, string> = { today: "day", week: "7d", month: "month" };

/** THE money spent in a period (the ledger, the same figure every Govern read says). */
export const spentOf = (p: Period | null | undefined) => Number(p?.spent_usd_micros ?? p?.actual_usd_micros ?? 0);
const heldOf = (p: Period | null | undefined) => Number(p?.held_usd_micros ?? p?.reserved_usd_micros ?? 0);

const day = (iso: string) => new Date(`${iso}T00:00:00Z`);
const isoDay = (d: Date) => d.toISOString().slice(0, 10);
const shortDay = (iso: string) => day(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

/** One bar per day of the chosen period: the whole month (days to come are empty), the last
 *  7 days, or today. Days without spend are $0 bars, so the bars sit at their dates. */
export function daysFor(period: SpendPeriod, daily: DailySpend[], today: string): DailySpend[] {
  if (!today) return [];
  const known = new Map(daily.map((d) => [d.day, d]));
  const want: string[] = [];
  if (period === "month") {
    const first = day(`${today.slice(0, 7)}-01`);
    for (let d = new Date(first); d.getUTCMonth() === first.getUTCMonth(); d.setUTCDate(d.getUTCDate() + 1)) want.push(isoDay(d));
  } else if (period === "week") {
    for (let i = 6; i >= 0; i -= 1) { const d = day(today); d.setUTCDate(d.getUTCDate() - i); want.push(isoDay(d)); }
  } else want.push(today);
  return want.map((d) => known.get(d) ?? { day: d, money_usd_micros: 0, included_usd_micros: 0 });
}

const AT_CAP: Record<AtLimit, string> = {
  pause: "At the cap, new work paused",
  cheaper: "At the cap, cheaper models",
  stop: "At the cap, stopped",
};

const BADGE_TIP: Record<string, string> = {
  green: "At today's burn this period stays under its cap",
  yellow: "Past the heads-up line, or forecast over the cap",
  red: "A cap is reached. Limits says what happens now.",
  unknown: "Set a cap in Limits",
};

const OTHER_TIP = "Money no model call explains: tools, provider charges, unattributed";

export type Badge = { tone: "green" | "yellow" | "red" | "unknown"; text: string };

function periodOf(meter: Meter | null | undefined, which: "day" | "month"): Period | null {
  return meter?.periods?.find((p) => p.period === which) ?? null;
}

/** "Dec 1" from a reset time (UTC: budget periods turn over at 00:00 UTC). */
export function resetDay(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/** The hero's badge: at the cap (by the at-limit choice), near it, or on track with a forecast. */
export function badgeOf(meter: Meter | null | undefined, period: Period | null, atLimit: AtLimit): Badge {
  const capped = (meter?.periods ?? []).filter((p) => p.cap_usd_micros > 0);
  if (!period || !period.cap_usd_micros) return { tone: "unknown", text: "No cap set" };
  if (capped.some((p) => spentOf(p) >= p.cap_usd_micros)) return { tone: "red", text: AT_CAP[atLimit] };
  const forecast = period.forecast_usd_micros ?? spentOf(period);
  const soft = Number(meter?.soft_cap_pct ?? 0.7);
  const counted = (spentOf(period) + heldOf(period)) / period.cap_usd_micros;
  if (counted >= soft || forecast > period.cap_usd_micros) return { tone: "yellow", text: "Near the cap" };
  return { tone: "green", text: `On track, forecast ${money(forecast)}` };
}

function HeroBar({ period, tone }: { period: Period; tone: Badge["tone"] }) {
  const cap = period.cap_usd_micros;
  const pct = (n: number) => `${Math.max(0, Math.min(100, (n / cap) * 100)).toFixed(2)}%`;
  const spent = spentOf(period);
  const held = heldOf(period);
  const forecast = Math.max(period.forecast_usd_micros ?? 0, spent);
  return (
    <div className="gv-bar gv-hero-bar" data-tone={tone} data-testid="gv-hero-bar" role="img"
         aria-label={`${money(spent)} spent, ${money(held)} held, of ${money(cap)}, forecast ${money(forecast)}`}>
      <span className="gv-bar-fill" data-testid="gv-hero-fill" style={{ width: pct(spent) }} />
      {held > 0 ? (
        <span className="gv-bar-held" data-testid="gv-hero-held" title={`Held ${money(held)} by running work`}
              style={{ left: pct(spent), width: `calc(${pct(Math.min(spent + held, cap))} - ${pct(spent)})` }} />
      ) : null}
      {forecast > spent ? (
        <span className="gv-forecast" data-testid="gv-hero-forecast" title={`Forecast ${money(forecast)}`}
              style={{ left: pct(Math.min(forecast, cap)) }} />
      ) : null}
    </div>
  );
}

function BarRows({ rows: all, testid, empty, mine }: {
  rows: { id: string; name: string; money: number; included: number }[]; testid: string; empty: string; mine?: string;
}) {
  // Only what spent (or ran included) in the window is listed: a zero row is a catalogue entry.
  const rows = all.filter((r) => r.money > 0 || r.included > 0 || r.id === mine);
  const peak = Math.max(1, ...rows.map((r) => r.money || r.included));
  if (!rows.length) return <span className="gv-muted" data-testid={`${testid}-empty`}>{empty}</span>;
  return (
    <>
    <ul className="gv-hbars" data-testid={testid}>
      {rows.map((r) => (
        <li key={r.id} data-testid={`${testid}-row`} data-mine={r.id === mine || undefined}>
          <span className="gv-hbar-name" data-user-text title={r.id === "other" ? OTHER_TIP : r.id === mine ? `${r.id}, this project` : r.id}>{r.name}</span>
          <span className="gv-hbar-track" aria-hidden="true">
            <span className="gv-hbar-fill" data-included={!r.money || undefined}
                  style={{ width: `${Math.max(2, ((r.money || r.included) / peak) * 100).toFixed(1)}%` }} />
          </span>
          <span className="gv-hbar-value">
            {r.money ? money(r.money) : r.included ? <span className="gv-muted" title="Ran on a subscription. Not money.">Included</span> : money(0)}
          </span>
        </li>
      ))}
    </ul>
    </>
  );
}

export function SpendHome({ view, viewError, onRetry, projectId, paused, period, onDetail }: {
  view: GovernView | null;
  viewError: string;
  onRetry: () => void;
  projectId: string;
  paused: boolean;
  period: SpendPeriod;
  onDetail: () => void;
}) {
  const read = usePolledRead<SpendView>({
    key: `${projectId}|${WINDOW[period]}`, pollMs: 60_000, paused,
    read: async (etag, signal) => {
      const got = await readGovern<SpendView>(projectId, "spend", { window: WINDOW[period] }, { etag, signal });
      return got.ok ? (got.notModified ? { ok: true as const, notModified: true, etag: got.etag }
        : { ok: true as const, data: got.data, etag: got.etag }) : { ok: false as const, error: got.error };
    },
  });
  const s = read.data;
  const projects: ProjectSpendRow[] = s?.by_project ?? [];
  const meter = view?.meter ?? null;
  const atLimit: AtLimit = view?.at_limit?.value ?? "pause";
  const day = periodOf(meter, "day");
  const month = periodOf(meter, "month");
  const shown = period === "today" ? day : period === "month" ? month : null;
  const badge = badgeOf(meter, shown ?? month, atLimit);
  const big = shown ? spentOf(shown) : (s?.money_usd_micros ?? 0);
  const held = heldOf(shown ?? day);
  const word = period === "today" ? "today" : period === "week" ? "this week" : "this month";
  const other = period === "today" ? month : day;
  const included = Number(s?.included_calls ?? 0);
  const resets = period === "today" ? (day?.resets_at_label ?? "") : resetDay((month ?? day)?.resets_at);
  const daily = s?.daily ?? [];
  const today = daily.length ? daily[daily.length - 1].day : "";
  const days = daysFor(period, daily, today);
  const dailyCap = day?.cap_usd_micros || s?.daily_cap_usd_micros || 0;
  // By model is THIS project's, adding up to the hero; By project is every project you can see.
  const models: ModelSpendRow[] = s?.by_model_spent ?? s?.by_model ?? [];
  const projectName = projects.find((p) => p.id === projectId)?.name ?? "";

  return (
    <div className="gv-home" data-testid="gv-home">
      <section className="gv-hero" data-testid="gv-hero" aria-label="Spend">
        {!view ? (
          viewError ? (
            <span className="gv-unreachable" role="alert"><span title={viewError}>Can&apos;t reach BOSS</span>
              <button type="button" className="ag-btn" onClick={onRetry}>Retry</button></span>
          ) : <span className="gv-meter-skel" aria-busy="true" />
        ) : (
          <>
            <div className="gv-hero-line">
              <span className="gv-hero-scope" data-testid="gv-hero-scope" data-user-text
                    title="Caps are per project. By project shows every project you can see.">
                {projectName ? `This project, ${projectName}` : "This project"}
              </span>
              <span className="gv-hero-break" />
              <span className="gv-hero-amount" data-testid="gv-hero-amount">{money(big)}</span>
              <span className="gv-hero-of" data-testid="gv-hero-of">
                {shown && shown.cap_usd_micros ? `of ${money(shown.cap_usd_micros)} ${word}` : word}
              </span>
              <span className="gv-spacer" />
              <span className="gv-state gv-hero-badge" data-state={badge.tone} data-testid="gv-hero-badge"
                    title={BADGE_TIP[badge.tone]}>{badge.text}</span>
            </div>
            {shown && shown.cap_usd_micros ? <HeroBar period={shown} tone={badge.tone} /> : null}
            <div className="gv-hero-sub" data-testid="gv-hero-sub">
              {other && other.cap_usd_micros ? (
                <span>{money(spentOf(other))} {other.period === "day" ? "today" : "this month"} of {money(other.cap_usd_micros)}</span>
              ) : null}
              {held > 0 ? (
                <span data-testid="gv-hero-held-line" title="Reserved by running work. The cap counts it; it is not spent.">
                  {money(held)} held
                </span>
              ) : null}
              {included > 0 ? (
                <span title="Subscription calls are not money and count against no cap">
                  {included.toLocaleString("en-US")} subscription call{included === 1 ? "" : "s"}, included, $0
                </span>
              ) : null}
              {resets ? <span>Resets {resets}</span> : null}
            </div>
            <LegacyHolds meter={view.meter} />
          </>
        )}
      </section>

      {read.error && !s ? (
        <div className="ag-empty" role="alert"><span title={read.error}>Can&apos;t reach BOSS</span>
          <button type="button" className="ag-btn" onClick={read.reload}>Retry</button></div>
      ) : !s ? (
        <div className="gv-skel" aria-busy="true" />
      ) : (
        <>
          <section className="gv-card" aria-labelledby="gv-days-h" data-testid="gv-days">
            <h3 id="gv-days-h" className="gv-sec-head">EACH DAY</h3>
            <DailyBars daily={days} cap={dailyCap || null} testid="gv-days-chart" highlight={today} capOnScaleAlways
                       capLabel={dailyCap ? `daily cap ${money(dailyCap)}` : undefined} />
            {days.length > 1 ? (
              <span className="gv-axis" data-testid="gv-days-axis" aria-hidden="true">
                <span>{shortDay(days[0].day)}</span>
                <span>{shortDay(days[Math.floor((days.length - 1) / 2)].day)}</span>
                <span>{shortDay(days[days.length - 1].day)}</span>
              </span>
            ) : days.length ? <span className="gv-axis" data-testid="gv-days-axis" aria-hidden="true"><span>{shortDay(days[0].day)}</span></span> : null}
          </section>
          {/* BOSSAPP-864: estimated, reconciled to invoices, seat fees. */}
          <ReconciliationCard projectId={projectId} />
          <div className="gv-pair">
            <section className="gv-card" aria-labelledby="gv-bymodel-h">
              <div className="gv-sec-head"><h3 id="gv-bymodel-h" className="gv-sec-head">BY MODEL</h3><span className="gv-badge">this project</span></div>
              <BarRows testid="gv-home-models" empty="No model calls"
                       rows={models.map((r) => ({ id: r.id, name: modelName(r.id), money: r.money_usd_micros, included: r.included_usd_micros }))} />
            </section>
            <section className="gv-card" aria-labelledby="gv-byproject-h">
              <div className="gv-sec-head"><h3 id="gv-byproject-h" className="gv-sec-head">BY PROJECT</h3><span className="gv-badge">all you can see</span></div>
              <BarRows testid="gv-home-projects" empty="No spend yet" mine={projectId}
                       rows={projects.map((r) => ({ id: r.id, name: r.name, money: r.money_usd_micros, included: r.included_usd_micros }))} />
              <button type="button" className="ag-link gv-detail-link" data-testid="gv-where" onClick={onDetail}
                      title="By agent, by step, savings and top drivers">Where the money went</button>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
