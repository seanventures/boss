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
  type ProjectLimitRow, type ProjectLimitsView,
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
  const capsChanged = month !== start.month || day !== start.day || burst !== start.burst || choice !== start.choice;
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
    if (capsChanged) {
      const body: Record<string, unknown> = { project_id: projectId, actor };
      if (month !== start.month) body.monthly_cap_usd = monthN.value;
      if (day !== start.day) body.daily_cap_usd = dayN.value;
      if (burst !== start.burst) body.burst_usd_per_min = burstN.value;
      if (choice !== start.choice) body.at_limit = choice;
      const r = await governWrite("limits", body, view?.versions.budget);
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
    showToast("success", "Limits saved");
    onSaved();
    void loadRows();
  };
  const dirty = capsChanged || changedRows.length > 0;

  return (
    <form className="gv-limits" data-testid="gv-limits" noValidate
          onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <button type="button" className="ag-link gv-back" data-testid="gv-back" onClick={onBack}>← Spend</button>

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
