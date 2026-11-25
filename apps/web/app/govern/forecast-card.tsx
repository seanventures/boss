"use client";

/**
 * Govern › Spend › This month (BOSSAPP-1295): for the whole org, one team or this project — spent,
 * the month-end forecast against the scope's monthly cap, the run-rate, a running total per day,
 * and the costliest runs (each opens its build). A preview team on a fixed monthly allowance sees
 * where it will land before it gets there.
 *
 * Reads GET /v1/budget-policies/scoped/forecast and /teams (budget:read at org). Writes nothing:
 * a cap is set in Limits ("$2,000 pilot" adds a team's).
 */
import React, { useEffect, useState } from "react";
import { ArrowSquareOut, CaretDown, CaretUp, ChartLineUp, Robot, Wrench } from "@phosphor-icons/react";

import { money } from "../../lib/govern";
import { DailyBars } from "./cap-preview";
import { daysFor, resetDay } from "./spend-home";
import type { Team } from "./scoped-budgets";

export type ForecastRun = {
  run_id: string; kind: "build" | "agent"; project_id: string; title: string | null;
  money_usd_micros: number; calls: number; started_at: string | null;
};
export type Forecast = {
  scope_type: "org" | "team" | "project"; scope_id: string; label: string;
  period_start: string; period_end: string; days_elapsed: number; days_left: number;
  spent_usd_micros: number; run_rate_usd_micros_per_day: number; forecast_usd_micros: number;
  cap_usd_micros: number | null; warn_pct: number; hard_stop: boolean | null;
  /** Where the cap comes from: a scoped monthly budget, or (a project) its own monthly cap. */
  cap_source?: "scoped_budget" | "project_budget" | null;
  state: "at_cap" | "over_forecast" | "near" | "on_track" | "no_cap";
  daily: { day: string; money_usd_micros: number }[];
  by_project: { id: string; name: string; money_usd_micros: number }[];
  run_count: number; avg_run_usd_micros: number; runs: ForecastRun[];
};

const enc = encodeURIComponent;
const SHOWN = 5;

/** The badge: tone + words, from the server's one-word state. */
export function forecastBadge(f: Pick<Forecast, "state" | "forecast_usd_micros">): { tone: string; text: string } {
  switch (f.state) {
    case "at_cap": return { tone: "red", text: "At the cap" };
    case "over_forecast": return { tone: "yellow", text: `Heading over, ${money(f.forecast_usd_micros)}` };
    case "near": return { tone: "yellow", text: "Near the cap" };
    case "on_track": return { tone: "green", text: "On track" };
    default: return { tone: "unknown", text: "No monthly cap" };
  }
}

/** A run's console page: a build opens on Builds; an agent run has no page of its own ("" = none). */
export function runHref(r: Pick<ForecastRun, "run_id" | "kind">): string {
  return r.kind === "build" ? `/?area=code_tasks&run=${enc(r.run_id)}` : "";
}

type Choice = { value: string; label: string };

export function ForecastCard({ projectId }: { projectId: string }) {
  const [teams, setTeams] = useState<Team[]>([]);
  const [choice, setChoice] = useState<string>("org");
  const [data, setData] = useState<Forecast | null>(null);
  const [error, setError] = useState<string>("");
  const [all, setAll] = useState(false);

  useEffect(() => {
    let live = true;
    void fetch(`/v1/budget-policies/scoped/teams?project_id=${enc(projectId)}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null)).then((b) => { if (live) setTeams(b?.teams ?? []); })
      .catch(() => undefined);
    return () => { live = false; };
  }, [projectId]);

  useEffect(() => {
    let live = true;
    const [scope, id] = choice === "org" ? ["org", ""] : choice === "project" ? ["project", projectId] : ["team", choice.slice(5)];
    setError("");
    void fetch(`/v1/budget-policies/scoped/forecast?project_id=${enc(projectId)}&scope_type=${scope}&scope_id=${enc(id)}`,
               { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json().catch(() => null);
        if (!live) return;
        if (r.ok && body && typeof body.spent_usd_micros === "number") {
          setData({ ...body, daily: body.daily ?? [], runs: body.runs ?? [], by_project: body.by_project ?? [] } as Forecast);
        }
        else if (!r.ok) { setData(null); setError(body?.detail || "Can't read the forecast"); }
        else setError("Can't read the forecast");
      })
      .catch(() => { if (live) { setData(null); setError("Can't reach BOSS"); } });
    return () => { live = false; };
  }, [choice, projectId]);

  const choices: Choice[] = [{ value: "org", label: "Whole org" }, { value: "project", label: "This project" },
    ...teams.map((t) => ({ value: `team:${t.id}`, label: `Team ${t.name}` }))];
  const badge = data ? forecastBadge(data) : null;
  const cap = data?.cap_usd_micros ?? null;
  const today = new Date().toISOString().slice(0, 10);
  const days = data ? daysFor("month", data.daily.map((d) => ({ ...d, included_usd_micros: 0 })), today) : [];
  const runs = data ? (all ? data.runs : data.runs.slice(0, SHOWN)) : [];
  const pct = (n: number) => `${Math.max(0, Math.min(100, (n / (cap || 1)) * 100)).toFixed(2)}%`;

  return (
    <section className="gv-card gv-fc" aria-labelledby="gv-fc-h" data-testid="gv-forecast-card">
      <div className="gv-fc-head">
        <h3 id="gv-fc-h" className="gv-sec-head"><ChartLineUp size={16} aria-hidden />THIS MONTH</h3>
        <span className="gv-spacer" />
        <select className="gv-input" aria-label="Whose spend" value={choice} data-testid="gv-fc-scope"
                onChange={(e) => { setChoice(e.target.value); setAll(false); }}>
          {choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
        </select>
      </div>
      {error ? <span className="gv-muted" role="alert" data-testid="gv-fc-error">{error}</span>
        : !data ? <span className="gv-skel gv-skel-short" aria-busy="true" /> : (
        <>
          <div className="gv-fc-stats" data-testid="gv-fc-stats">
            <span className="gv-fc-stat"><span className="gv-fc-k">Spent</span>
              <b data-testid="gv-fc-spent">{money(data.spent_usd_micros)}</b></span>
            <span className="gv-fc-stat" title="Spent so far + today's daily average x days left">
              <span className="gv-fc-k">Forecast</span><b data-testid="gv-fc-forecast">{money(data.forecast_usd_micros)}</b></span>
            <span className="gv-fc-stat"><span className="gv-fc-k" title={data.cap_source === "project_budget" ? "The project's monthly cap in Limits" : undefined}>
                {data.hard_stop ? "Hard cap" : data.cap_source === "project_budget" ? "Project cap" : "Cap"}</span>
              <b data-testid="gv-fc-cap">{cap ? money(cap) : "None"}</b></span>
            <span className="gv-state gv-fc-badge" data-state={badge!.tone} data-testid="gv-fc-badge"
                  title={cap ? (data.hard_stop ? "At the cap new work is refused; nobody is asked mid-build" : "Warns only") : "Set one in Limits"}>
              {badge!.text}</span>
          </div>
          {cap ? (
            <div className="gv-bar gv-hero-bar" data-tone={badge!.tone} data-testid="gv-fc-bar" role="img"
                 aria-label={`${money(data.spent_usd_micros)} of ${money(cap)}, forecast ${money(data.forecast_usd_micros)}`}>
              <span className="gv-bar-fill" style={{ width: pct(data.spent_usd_micros) }} />
              {data.forecast_usd_micros > data.spent_usd_micros ? (
                <span className="gv-forecast" title={`Forecast ${money(data.forecast_usd_micros)}`}
                      style={{ left: pct(Math.min(data.forecast_usd_micros, cap)) }} />
              ) : null}
            </div>
          ) : null}
          <div className="gv-fc-sub">
            <span>{money(data.run_rate_usd_micros_per_day)}/day</span>
            <span data-testid="gv-fc-runs-line">{data.run_count} run{data.run_count === 1 ? "" : "s"}, avg {money(data.avg_run_usd_micros)}</span>
            <span>Resets {resetDay(data.period_end)}</span>
          </div>
          <DailyBars daily={days} cap={cap} cumulative testid="gv-fc-chart" highlight={today}
                     capLabel={cap ? `cap ${money(cap)}` : undefined} />
          <h4 className="gv-sec-head">COSTLIEST RUNS</h4>
          {runs.length ? (
            <ul className="gv-fc-runs" data-testid="gv-fc-runs">
              {runs.map((r) => (
                <li key={r.run_id} data-testid="gv-fc-run">
                  {r.kind === "build" ? <Wrench size={14} aria-hidden /> : <Robot size={14} aria-hidden />}
                  {runHref(r) ? (
                    <a className="ag-link gv-fc-run-name" href={runHref(r)} title={`${r.run_id}, ${r.calls} model calls`} data-user-text>
                      {r.title || "Build"}<ArrowSquareOut size={12} aria-hidden /></a>
                  ) : <span className="gv-fc-run-name" title={`${r.run_id}, ${r.calls} model calls`} data-user-text>{r.title || "Agent run"}</span>}
                  <span className="gv-hbar-value">{money(r.money_usd_micros)}</span>
                </li>
              ))}
            </ul>
          ) : <span className="gv-muted" data-testid="gv-fc-runs-empty">No paid runs this month</span>}
          {data.runs.length > SHOWN ? (
            <button type="button" className="ag-btn gv-fc-more" data-testid="gv-fc-more" aria-expanded={all}
                    onClick={() => setAll(!all)}>
              {all ? <CaretUp size={14} aria-hidden /> : <CaretDown size={14} aria-hidden />}
              {all ? "Fewer" : `All ${data.runs.length}`}
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}
