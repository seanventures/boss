"use client";

/**
 * Govern › Limits in plain words (BOSSAPP-807): how much BOSS may spend (per month, per day,
 * per minute), what happens when a limit is reached, and an optional monthly limit per
 * project — then one Save. Before saving, the last 30 days are replayed against the typed caps
 * (`cap_preview`, the board's own replay). Everything else (pace, thresholds, token guides,
 * review rounds, Captain) stays on the board, one click away under "Advanced".
 *
 * Writes: `limits` (POST /v1/govern/limits — only the fields changed, with the budget version
 * as If-Match) and `project_limit` per changed project (a project budget envelope).
 */
import React, { useEffect, useMemo, useRef, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  governWrite, money, readGovern, readNumber, type AtLimit, type CapPreview as CapPreviewData, type GovernView,
  type Envelope, type ProjectLimitRow, type ProjectLimitsView,
} from "../../lib/govern";
import { showToast } from "../client-forms";
import { FailureLine, failureOf, type Failure } from "./control-board";
import { resetDay } from "./spend-home";

export const AT_LIMIT_CHOICES: { key: AtLimit; label: string; hint: string }[] = [
  { key: "pause", label: "Pause new work and ask me", hint: "Running builds stop at next step" },
  { key: "cheaper", label: "Switch to cheaper models", hint: "Keep going, lower quality" },
  { key: "stop", label: "Stop everything", hint: "Nothing runs until the next period" },
];

const dollars = (micros: number | null | undefined) =>
  micros === null || micros === undefined ? "" : String(Math.round(Number(micros) / 10_000) / 100);
const MONEY = { min: 0.01, max: 1_000_000, cents: true, money: true } as const;
const BURST = { min: 0, max: 1_000_000, cents: true, money: true } as const;

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}

/** Days the typed caps would have paused work on, from the server's replays (month and day). */
function usePausedDays(projectId: string, month: number | null, day: number | null): number | null {
  const m = useDebounced(month, 300);
  const d = useDebounced(day, 300);
  const [n, setN] = useState<number | null>(null);
  useEffect(() => {
    let alive = true;
    const ask = (period: "month" | "day", cap: number | null) => (cap === null ? Promise.resolve(null)
      : governWrite("cap_preview", { project_id: projectId, period, cap_usd: cap })
        .then((r) => (r.ok ? (r.data as unknown as CapPreviewData) : null)));
    void Promise.all([ask("month", m), ask("day", d)]).then((got) => {
      if (!alive) return;
      const known = got.filter((p): p is CapPreviewData => !!p && !p.not_enough_history);
      setN(known.length ? new Set(known.flatMap((p) => p.stopped_days ?? [])).size : null);
    });
    return () => { alive = false; };
  }, [projectId, m, d]);
  return n;
}

function MoneyField({ id, label, value, onChange, error, hint, testid, inputRef, perMin }: {
  id: string; label: string; value: string; onChange: (v: string) => void; error: string | null; hint: string;
  testid: string; inputRef?: React.Ref<HTMLInputElement>; perMin?: boolean;
}) {
  return (
    <div className="gv-limit-row" data-testid={`gv-limit-${testid}`}>
      <label className="gv-limit-label" htmlFor={id}>{label}</label>
      <span className="gv-money-input">$<input id={id} ref={inputRef} className="gv-input" type="number" inputMode="decimal"
             step="any" value={value} aria-invalid={error ? true : undefined} data-testid={`gv-input-${testid}`}
             onChange={(e) => onChange(e.target.value)} />{perMin ? <span className="gv-muted">/min</span> : null}</span>
      <span className="gv-limit-hint" data-testid={`gv-hint-${testid}`}>{error ? <span className="gv-field-error" role="alert">{error}</span> : hint}</span>
    </div>
  );
}

export function LimitsScreen({ view, projectId, actor, unreachable, focus, onSaved, onAdvanced, onBack }: {
  view: GovernView | null;
  projectId: string;
  actor: string;
  unreachable: boolean;
  focus: string | null;
  onSaved: () => void;
  onAdvanced: () => void;
  onBack: () => void;
}) {
  const controls = useMemo(() => Object.fromEntries((view?.controls ?? []).map((c) => [c.key, c])), [view]);
  const start = useMemo(() => ({
    month: dollars(controls.monthly_cap?.value_effective),
    day: dollars(controls.daily_cap?.value_effective),
    burst: dollars(controls.burst?.value_effective),
    choice: (view?.at_limit?.value ?? "pause") as AtLimit,
  }), [controls, view?.at_limit?.value]);
  const [month, setMonth] = useState(start.month);
  const [day, setDay] = useState(start.day);
  const [burst, setBurst] = useState(start.burst);
  const [choice, setChoice] = useState<AtLimit>(start.choice);
  // The fields follow the server until someone types (a poll never overwrites an edit).
  const touched = useRef(false);
  useEffect(() => {
    if (touched.current) return;
    setMonth(start.month); setDay(start.day); setBurst(start.burst); setChoice(start.choice);
  }, [start]);
  const edit = <T,>(set: (v: T) => void) => (v: T) => { touched.current = true; set(v); };

  // PACE, the first card (operator, 2026-11-04: "I like the pace"): the board's own control and
  // ops. Moving it fills the caps below with exactly what Pace writes (pace_preview); they can
  // still be fine-tuned, and Save writes Pace first, then any cap changed from what it set.
  const startPace = view?.pace?.value ?? null;
  const [pace, setPace] = useState<number | null>(null);
  const shownPace = pace ?? startPace ?? 50;
  const askedPace = useDebounced(pace, 150);
  const [envelope, setEnvelope] = useState<Envelope | null>(null);
  const [paceBase, setPaceBase] = useState<{ month: string; day: string; burst: string } | null>(null);
  useEffect(() => {
    const ask = askedPace ?? startPace;
    if (ask === null) { setEnvelope(null); return; }
    let alive = true;
    void governWrite("pace_preview", { project_id: projectId, intensity: ask }).then((r) => {
      if (!alive || !r.ok) return;
      const env = (r.data as unknown as { envelope: Envelope }).envelope;
      setEnvelope(env);
      if (askedPace !== null) {
        const base = { month: dollars(env.monthly_cap_usd_micros), day: dollars(env.daily_cap_usd_micros),
                       burst: dollars(env.burst_cap_usd_micros_per_min) };
        setPaceBase(base);
        setMonth(base.month); setDay(base.day); setBurst(base.burst);
      }
    });
    return () => { alive = false; };
  }, [askedPace, startPace, projectId]);
  const paceMoved = pace !== null && pace !== startPace;

  const [rows, setRows] = useState<ProjectLimitRow[] | null>(null);
  const [rowText, setRowText] = useState<Record<string, string>>({});
  const loadRows = React.useCallback(async () => {
    const got = await readGovern<ProjectLimitsView>(projectId, "project_limits");
    if (got.ok && !got.notModified) {
      setRows(got.data.projects ?? []);
      setRowText(Object.fromEntries((got.data.projects ?? []).map((r) => [r.project_id, dollars(r.limit_usd_micros)])));
    } else if (!got.ok) setRows([]);
  }, [projectId]);
  useEffect(() => { void loadRows(); }, [loadRows]);

  const monthN = readNumber(month, MONEY);
  const dayN = readNumber(day, MONEY);
  const burstN = readNumber(burst, BURST);
  const rowErrors = Object.fromEntries((rows ?? []).map((r) => {
    const t = (rowText[r.project_id] ?? "").trim();
    return [r.project_id, t === "" ? null : readNumber(t, MONEY).error];
  }));
  const changedRows = (rows ?? []).filter((r) => (rowText[r.project_id] ?? "").trim() !== dollars(r.limit_usd_micros));
  // After Pace moved, a cap is "changed" when it differs from what Pace sets.
  const from = paceMoved && paceBase ? paceBase : start;
  const capsChanged = month !== from.month || day !== from.day || burst !== from.burst || choice !== start.choice;
  // Only what was changed is sent, so only what was changed has to be valid; its reason shows.
  const monthErr = month !== start.month ? monthN.error : null;
  const dayErr = day !== start.day ? dayN.error : null;
  const burstErr = burst !== start.burst ? burstN.error : null;
  const invalid = Boolean(monthErr || dayErr || burstErr || Object.values(rowErrors).some(Boolean));
  const paused = usePausedDays(projectId, monthN.value, dayN.value);

  const monthPeriod = view?.meter.periods.find((p) => p.period === "month");
  const dayPeriod = view?.meter.periods.find((p) => p.period === "day");
  const monthRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => { if (focus === "monthly_cap" && view) monthRef.current?.focus?.(); }, [focus, view === null]);

  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const save = async () => {
    if (invalid || busy) return;
    setBusy(true);
    setFailure(null);
    if (paceMoved) {
      const r = await governWrite("pace", { project_id: projectId, actor, intensity: pace }, view?.versions.budget);
      if (!r.ok) { setBusy(false); setFailure(failureOf(r, "pace")); return; }
    }
    if (capsChanged) {
      const body: Record<string, unknown> = { project_id: projectId, actor };
      if (month !== from.month) body.monthly_cap_usd = monthN.value;
      if (day !== from.day) body.daily_cap_usd = dayN.value;
      if (burst !== from.burst) body.burst_usd_per_min = burstN.value;
      if (choice !== start.choice) body.at_limit = choice;
      // Pace just moved the budget's version; this save follows it in the same press.
      const r = await governWrite("limits", body, paceMoved ? null : view?.versions.budget);
      if (!r.ok) { setBusy(false); setFailure(failureOf(r, "limits")); return; }
    }
    for (const row of changedRows) {
      const t = (rowText[row.project_id] ?? "").trim();
      const r = await governWrite("project_limit", { project_id: row.project_id, actor,
                                                     monthly_limit_usd: t === "" ? null : Number(t) });
      if (!r.ok) { setBusy(false); setFailure(failureOf(r, "project_limit")); return; }
    }
    setBusy(false);
    touched.current = false;
    setPace(null); setPaceBase(null);
    showToast("success", "Limits saved");
    onSaved();
    void loadRows();
  };
  const dirty = paceMoved || capsChanged || changedRows.length > 0;

  return (
    <form className="gv-limits" data-testid="gv-limits" noValidate
          onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <button type="button" className="ag-link gv-back" data-testid="gv-back" onClick={onBack}>← Spend</button>

      <section className="gv-card" aria-labelledby="gv-pace-h" data-testid="gv-pace-card">
        <h3 id="gv-pace-h" className="gv-card-title">How fast BOSS works</h3>
        <span className="gv-slider">
          <span className="gv-slider-end">Careful</span>
          <input type="range" min={0} max={100} step={1} value={shownPace} aria-label="Pace" data-testid="gv-limits-pace"
                 onChange={(e) => { touched.current = true; setPace(Number(e.target.value)); }} />
          <span className="gv-slider-end">Bold</span>
          <b className="gv-slider-value" data-testid="gv-limits-pace-value">{startPace === null && pace === null ? "Not set" : shownPace}</b>
        </span>
        {envelope ? (
          <span className="gv-pace-preview" data-testid="gv-limits-pace-sets" title="Exactly what Pace writes. Fine-tune the caps below.">
            Sets {money(envelope.monthly_cap_usd_micros)}/month · {money(envelope.daily_cap_usd_micros)}/day
            {" · "}{money(envelope.burst_cap_usd_micros_per_min, { perMin: true })} burst
            {" · "}{envelope.max_concurrent_jobs} jobs · {envelope.max_workers_total} workers
          </span>
        ) : null}
      </section>

      <section className="gv-card" aria-labelledby="gv-howmuch-h" data-testid="gv-howmuch">
        <h3 id="gv-howmuch-h" className="gv-card-title">How much BOSS may spend</h3>
        <MoneyField id="gv-month" label="Per month" testid="month" value={month} onChange={edit(setMonth)} inputRef={monthRef}
                    error={monthErr}
                    hint={`Used ${money(monthPeriod?.spent_usd_micros ?? monthPeriod?.actual_usd_micros)}${monthPeriod?.resets_at ? ` · resets ${resetDay(monthPeriod.resets_at)}` : ""}`} />
        <MoneyField id="gv-day" label="Per day" testid="day" value={day} onChange={edit(setDay)}
                    error={dayErr} hint={`Used ${money(dayPeriod?.spent_usd_micros ?? dayPeriod?.actual_usd_micros)} today`} />
        <MoneyField id="gv-burst" label="Burst per minute" testid="burst" value={burst} onChange={edit(setBurst)} perMin
                    error={burstErr} hint="Stops a runaway loop fast" />
        <span className="gv-muted" data-testid="gv-included-note"
              title="Subscription use is included: $0, never counted against a limit">Subscriptions (Claude, Codex) never count here</span>
      </section>

      <section className="gv-card" aria-labelledby="gv-atlimit-h" data-testid="gv-atlimit">
        <h3 id="gv-atlimit-h" className="gv-card-title">When a limit is reached</h3>
        <div role="radiogroup" aria-labelledby="gv-atlimit-h" className="gv-radios">
          {AT_LIMIT_CHOICES.map((c) => (
            <label key={c.key} className="gv-radio" data-checked={choice === c.key || undefined}>
              <input type="radio" name="gv-at-limit" value={c.key} checked={choice === c.key}
                     data-testid={`gv-atlimit-${c.key}`} onChange={() => edit(setChoice)(c.key)} />
              <span className="gv-radio-label">{c.label}</span>
              <span className="gv-radio-hint">{c.hint}</span>
            </label>
          ))}
        </div>
      </section>

      <section className="gv-card" aria-labelledby="gv-perproject-h" data-testid="gv-perproject">
        <h3 id="gv-perproject-h" className="gv-card-title">Per project <span className="gv-muted">(optional)</span></h3>
        {rows === null ? <span className="gv-skel gv-skel-short" aria-busy="true" /> : rows.length ? (
          <ul className="gv-proj-rows">
            {rows.map((r) => (
              <li key={r.project_id} className="gv-limit-row" data-testid={`gv-proj-${r.project_id}`}>
                <label className="gv-limit-label" htmlFor={`gv-proj-in-${r.project_id}`} data-user-text>{r.name}</label>
                <span className="gv-money-input">$<input id={`gv-proj-in-${r.project_id}`} className="gv-input" type="number"
                       inputMode="decimal" step="any" placeholder="No limit" value={rowText[r.project_id] ?? ""}
                       aria-invalid={rowErrors[r.project_id] ? true : undefined} data-testid={`gv-proj-input-${r.project_id}`}
                       onChange={(e) => setRowText((t) => ({ ...t, [r.project_id]: e.target.value }))} /></span>
                <span className="gv-limit-hint">
                  {rowErrors[r.project_id] ? <span className="gv-field-error" role="alert">{rowErrors[r.project_id]}</span>
                    : `${money(r.used_usd_micros)} used`}
                </span>
              </li>
            ))}
          </ul>
        ) : <span className="gv-muted">No projects</span>}
      </section>

      <div className="gv-save-row">
        {paused !== null ? (
          <span className="gv-paused-line" data-warn={paused > 0 || undefined} data-testid="gv-paused-days"
                title="Replays the last 30 days of real spend">
            {paused > 0 ? `With these limits, last month would have paused work on ${paused} day${paused === 1 ? "" : "s"}`
              : "With these limits, last month would have paused work on no days"}
          </span>
        ) : null}
        <span className="gv-spacer" />
        <GatedButton permission="budget:manage" type="submit" className="ag-btn ag-btn-primary" data-testid="gv-save-limits"
                     disabled={unreachable || busy || invalid || !dirty}>Save limits</GatedButton>
      </div>
      {failure ? <FailureLine failure={failure} onReload={() => { touched.current = false; onSaved(); }} /> : null}
      <button type="button" className="ag-link gv-advanced" data-testid="gv-advanced" onClick={onAdvanced}>
        Advanced: pace, token guides, review rounds
      </button>
    </form>
  );
}
