"use client";

/**
 * THE SCORECARD AS PICTURES (design §11.7): acceptance and noise as rings, time to repair and
 * cost per item as one number each, decisions per day as small stacked bars by level (A0 → A3
 * in one hue, light to dark, so "more autonomy" reads as "darker"). Computed by the API from the
 * ledger, never estimated; each mark's exact number is in its tooltip.
 */
import React from "react";

import { acceptance, minutesWords, money, microsUsd, type Outcomes, type Scorecard } from "../../lib/ambient";

function Ring({ pct, label, tip, good, testId }: { pct: number | null; label: string; tip: string; good: "high" | "low" | "none"; testId: string }) {
  const r = 17;
  const c = 2 * Math.PI * r;
  const v = pct === null ? 0 : Math.max(0, Math.min(100, pct));
  const tone = pct === null || good === "none" ? "quiet" : good === "high" ? (v >= 80 ? "ok" : v >= 50 ? "warn" : "bad") : (v <= 10 ? "ok" : v <= 30 ? "warn" : "bad");
  return (
    <div className="aa-tile" title={tip} data-testid={testId}>
      <svg viewBox="0 0 44 44" className="aa-ring" data-tone={tone} aria-hidden>
        <circle cx="22" cy="22" r={r} className="aa-ring-bg" />
        <circle cx="22" cy="22" r={r} className="aa-ring-fg" strokeDasharray={`${(v / 100) * c} ${c}`} transform="rotate(-90 22 22)" />
      </svg>
      <span className="aa-tile-v">{pct === null ? "—" : `${Math.round(v)}%`}</span>
      <span className="aa-tile-k">{label}</span>
    </div>
  );
}

function Big({ value, label, tip, testId, children }: { value: string; label: string; tip: string; testId: string; children?: React.ReactNode }) {
  return (
    <div className="aa-tile" title={tip} data-testid={testId}>
      <span className="aa-tile-big">{value}</span>
      {children}
      <span className="aa-tile-k">{label}</span>
    </div>
  );
}

const LEVELS = ["a0", "a1", "a2", "a3"] as const;

export function DecisionBars({ card }: { card: Scorecard }) {
  const rows = card.decisions_per_day.slice(-7);
  const max = Math.max(1, ...rows.map((r) => r.total));
  return (
    <div className="aa-tile aa-tile-wide" data-testid="aa-score-decisions" title="Decisions per day by autonomy level">
      <span className="aa-bars" aria-hidden>
        {rows.map((r) => (
          <span key={r.day} className="aa-bar" title={`${r.day}: ${r.total} decisions · A0 ${r.a0} · A1 ${r.a1} · A2 ${r.a2} · A3 ${r.a3}`}
                data-testid="aa-bar">
            {LEVELS.map((l) => r[l] ? <span key={l} className="aa-seg" data-level={l} style={{ height: `${(r[l] / max) * 100}%` }} /> : null)}
          </span>
        ))}
      </span>
      <span className="aa-legend" aria-label="Levels">
        {LEVELS.map((l) => <span key={l} className="aa-key" data-level={l}>{l.toUpperCase()}</span>)}
      </span>
      <span className="aa-tile-k">Decisions / day</span>
    </div>
  );
}

/** What happened AFTER agents decided (BOSSAPP-912): merged, reverted, roadmap taken, BOSS-started. */
export function OutcomeTiles({ out, days }: { out: Outcomes | null; days: number }) {
  if (!out) return null;
  const r = out.roadmap;
  return (
    <>
      <Big value={String(out.merged)} label="Merged" testId="aa-score-merged"
           tip={`Agent changes merged over ${days} days · ${out.reverted} reverted or caused an incident.`} />
      <Ring pct={out.revert_pct} label="Reverted" good="low" testId="aa-score-reverted"
            tip={`Merged agent changes later reverted or tied to an incident (${out.reverted} of ${out.merged + out.reverted}). Lower is better.`} />
      <Ring pct={r.acceptance_pct} label="Roadmap taken" good="high" testId="aa-score-roadmap"
            tip={`Recommendations a person accepted: ${r.accepted} accepted · ${r.rejected} rejected · ${r.ignored} ignored · ${r.recurred} recurred after acceptance.`} />
      <Ring pct={out.boss_started.pct} label="BOSS-started" good="none" testId="aa-score-boss-started"
            tip={`Work items BOSS opened itself: ${out.boss_started.boss} of ${out.boss_started.all}.`} />
    </>
  );
}

export function ScorecardStrip({ card }: { card: Scorecard | null }) {
  if (!card) return null;
  const acc = acceptance(card);
  const env = card.cost.envelope_today;
  const used = env.cap_usd_micros ? Math.min(1, (env.spent_usd_micros + env.held_usd_micros) / env.cap_usd_micros) : 0;
  const mttr = card.incident_mttr_minutes.n ? card.incident_mttr_minutes : card.mttr_minutes;
  return (
    <section className="aa-score" aria-label={`Scorecard, last ${card.days} days`} data-testid="aa-scorecard">
      <Ring pct={acc} label="Accepted" good="high" testId="aa-score-accepted"
            tip={`Agent decisions a person kept or that worked, over ${card.days} days. Target 80% to earn more autonomy.`} />
      <Ring pct={card.noise_rate === null ? null : card.noise_rate * 100} label="Noise" good="low" testId="aa-score-noise"
            tip="Items dismissed ÷ items ended. Lower is better; target ≤ 10%." />
      <Big value={minutesWords(mttr.median)} label={card.incident_mttr_minutes.n ? "Incident MTTR" : "Time to fix"} testId="aa-score-mttr"
           tip={`Median from signal to resolved (${mttr.n} items); p90 ${minutesWords(mttr.p90)}.`} />
      <Big value={money(card.cost.per_resolved_item_usd)} label="Cost / item" testId="aa-score-cost"
           tip={`Spent ${money(card.cost.spent_usd)} over ${card.days} days. Today ${money(microsUsd(env.spent_usd_micros))} of ${money(microsUsd(env.cap_usd_micros))}.`}>
        <span className="aa-envbar" aria-hidden><span style={{ width: `${used * 100}%` }} data-tone={used > 0.9 ? "bad" : used > 0.7 ? "warn" : "ok"} /></span>
      </Big>
      <Big value={minutesWords(card.time_to_human_decision_minutes.median)} label="You answer in" testId="aa-score-wait"
           tip={`Median time a Needs-you item waited for a person (${card.time_to_human_decision_minutes.n}).`} />
      <OutcomeTiles out={card.outcomes} days={card.days} />
      <DecisionBars card={card} />
    </section>
  );
}
