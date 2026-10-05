"use client";

/**
 * Govern › Limits: every control as one row (spec §4.4.3) — the value the ENGINE will use, where
 * it came from, what enforces it, and when it last stopped something. Where the stored value
 * and the effective one differ, both are shown (`set 3 · min 6`), because a label that says
 * more than the engine does is the thing this screen exists to stop.
 *
 * Edits open in place with their effect before saving, are saved only by Save or Enter (never on
 * blur — a tab-away must not change a production limit), and carry the version the row showed:
 * a limit someone else changed meanwhile is refused (`Changed by … just now`), not overwritten.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { DotsThree } from "@phosphor-icons/react";

import {
  effectiveOf, governWrite, money, readNumber, rightsFor, type CapPreview as CapPreviewData, type Control, type Envelope,
  type GovernView, type TokenPolicy, type WriteResult,
} from "../../lib/govern";
import { logHref } from "../../lib/log-url";
import { showToast } from "../client-forms";
import { consoleHref } from "../url-intent";
import { CapPreview } from "./cap-preview";

const HELP: Record<string, string> = {
  daily_cap: "Work waits or stops when today's money reaches this",
  monthly_cap: "Work waits or stops when this month's money reaches this",
  burst: "Over this per minute, work waits its turn",
  pace: "Sets caps, jobs and workers together. Edit them after.",
  code_agent_max_concurrency: "Reviewers working at once on a build. BOSS uses at least 6.",
  code_agent_max_turns: "Steps one agent may take before it hands back",
  code_review_inrun_max_rounds: "Times reviewers and coder go back and forth",
  captain_leads_projects: "Adds the Engineering Captain's panel to every build",
  max_concurrent_jobs: "Seeded into job slots when the project starts",
  max_workers_total: "Upper bound for autoscaling",
};

const ENFORCED_TIP: Record<string, string> = {
  hard_cap: "BOSS refuses the reservation and asks you",
  waits: "Work pauses until money frees up. Costs nothing meanwhile.",
  preset: "Writes the caps, jobs and workers below",
  review_panel: "How many reviewers run at once",
  each_agent: "Each agent stops at this many steps",
  review_loop: "Rounds of review before a colder path",
  every_build: "Applied when each build starts",
  job_queue: "Job slots seeded at project start",
  scaling: "Autoscaling never goes past this",
  advisory: "Shapes alerts and tips. Doesn't stop model calls.",
};

const SOURCE_WORD: Record<string, string> = { default: "Default", set: "Set here", pace: "From pace" };

const CHIP_TIP: Record<string, string> = {
  soft_cap_pct: "Turns the meter yellow. Nothing stops.",
  auto_lite_mode_pct: "Build steps switch to low-cost models. Reviews never do.",
  freeze_provisioning_pct: "New infrastructure waits. Running servers scale down.",
  freeze_tier2_pct: "Tier 2+ work is refused at reservation",
};

/** How a value reads on the board. */
export function valueText(c: Control): string {
  if (c.value_effective === null || c.value_effective === undefined) return c.unit === "intensity" ? "Not set" : "—";
  switch (c.unit) {
    case "usd_micros": return money(c.value_effective);
    case "usd_micros_per_min": return money(c.value_effective, { perMin: true });
    case "switch": return c.value_effective ? "On" : "Off";
    default: return String(c.value_effective);
  }
}

/** The second line when the stored value is not what runs: `set 3 · min 6`, `set 0 · reads as 2`. */
export function noteText(c: Control): { text: string; tip: string } | null {
  if (c.note?.kind === "floor") return { text: `set ${c.note.stored} · min ${c.note.floor}`, tip: `Values under ${c.note.floor} behave like ${c.note.floor} today` };
  if (c.note?.kind === "reads_as") return { text: `set 0 · reads as ${c.note.reads_as}`, tip: `0 is read as ${c.note.reads_as} today` };
  if (c.note?.kind === "default") return { text: "(default)", tip: "BOSS's default. Edit to set your own." };
  return null;
}

type Failure = { text: string; tip?: string; reload?: boolean };

function failureOf(result: Extract<WriteResult, { ok: false }>, op: string): Failure {
  if (result.status === 412) return { text: `Changed by ${result.changedBy || "someone"} just now`, reload: true };
  if (result.status === 403) return { text: "You can't change this", tip: rightsFor(op) };
  if (result.status === 422 || result.status === 400) return { text: result.error };
  return { text: "Can't reach BOSS", tip: result.error };
}

function FailureLine({ failure, onReload }: { failure: Failure; onReload: () => void }) {
  return (
    <span className="gv-fail" role="alert" data-testid="gv-fail" title={failure.tip}>
      {failure.text}
      {failure.reload ? <button type="button" className="ag-link" onClick={onReload}>Reload</button> : null}
    </span>
  );
}

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}

/** The board's rows, in order, with the names the server gives them — so a board that can't
 *  reach BOSS still reads `Daily cap —`, never `daily_cap —` (D8). */
export const SPEND_ROWS: [string, string][] = [
  ["daily_cap", "Daily cap"], ["monthly_cap", "Monthly cap"], ["burst", "Burst"], ["pace", "Pace"]];
export const AGENT_ROWS: [string, string][] = [
  ["code_agent_max_concurrency", "Review lanes"], ["code_agent_max_turns", "Turn budget"],
  ["code_review_inrun_max_rounds", "Review rounds"], ["captain_leads_projects", "Captain leads"],
  ["max_concurrent_jobs", "Jobs at once"], ["max_workers_total", "Workers"]];
const ROW_NAMES: Record<string, string> = Object.fromEntries([...SPEND_ROWS, ...AGENT_ROWS]);

export function ControlBoard({ view, projectId, actor, unreachable, focus, onSaved }: {
  view: GovernView | null;
  projectId: string;
  actor: string;
  unreachable: boolean;
  focus: string | null;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [menu, setMenu] = useState<string | null>(null);
  const [pulsed, setPulsed] = useState<string | null>(null);
  const controls = useMemo(() => Object.fromEntries((view?.controls ?? []).map((c) => [c.key, c])), [view]);
  const focusRef = useRef<HTMLLIElement | null>(null);
  // The URL (who you are, org=) is read after mount so the server's links match the client's.
  const [search, setSearch] = useState("");
  useEffect(() => { setSearch(window.location.search); }, []);
  useEffect(() => { focusRef.current?.scrollIntoView?.({ block: "center" }); }, [focus, view === null]);

  const saved = (key: string, word: string) => {
    showToast("success", word);
    setEditing(null);
    setPulsed(key);
    setTimeout(() => setPulsed(null), 600);
    onSaved();
  };

  const row = (key: string) => {
    const c = controls[key] as Control | undefined;
    if (!c && !unreachable) return null;
    return (
      <ControlRow key={key} keyName={key} control={c ?? null} projectId={projectId} actor={actor}
                  disabled={unreachable} editing={editing === key} menuOpen={menu === key}
                  focused={focus === key} pulsed={pulsed === key}
                  refFor={focus === key ? focusRef : undefined}
                  onMenu={(open) => setMenu(open ? key : null)}
                  onEdit={(on) => { setEditing(on ? key : null); setMenu(null); }}
                  onSaved={(word) => saved(key, word)} onReload={onSaved} />
    );
  };

  return (
    <div className="gv-board" data-testid="gv-board">
      <section className="gv-sec" aria-labelledby="gv-sec-spend">
        <h3 id="gv-sec-spend" className="gv-sec-head">SPEND CAPS</h3>
        <ul className="gv-rows" role="list">
          {SPEND_ROWS.map(([key]) => row(key))}
        </ul>
      </section>
      <section className="gv-sec" aria-labelledby="gv-sec-rises">
        <h3 id="gv-sec-rises" className="gv-sec-head">WHEN SPEND RISES</h3>
        <div className="gv-chips" data-testid="gv-thresholds">
          {(view?.thresholds ?? []).map((t) => (
            <span key={t.key} className="gv-chip" data-passed={t.passed || undefined} data-testid={`gv-threshold-${t.key}`}
                  title={CHIP_TIP[t.key]}>
              {t.label} {Math.round(t.pct * 100)}%
            </span>
          ))}
        </div>
      </section>
      <section className="gv-sec" aria-labelledby="gv-sec-agents">
        <h3 id="gv-sec-agents" className="gv-sec-head">HOW HARD AGENTS WORK</h3>
        <ul className="gv-rows" role="list">
          {AGENT_ROWS.map(([key]) => row(key))}
        </ul>
      </section>
      <TokenGuides view={view} projectId={projectId} actor={actor} disabled={unreachable} onSaved={onSaved} />
      <section className="gv-sec gv-elsewhere" aria-label="Elsewhere">
        <h3 className="gv-sec-head">ELSEWHERE</h3>
        <span className="gv-links">
          <a className="ag-link" href={consoleHref(search, { area: "decisions" })} title="Which actions need a person">Approval rules ↗</a>
          <a className="ag-link" href={consoleHref(search, { area: "org" })} title="Who may change what">Access ↗</a>
          <a className="ag-link" href={consoleHref(search, { area: "pipeline" })} title="Which models each step may use">Model tiers ↗</a>
        </span>
      </section>
    </div>
  );
}

function ControlRow({ keyName, control: c, projectId, actor, disabled, editing, menuOpen, focused, pulsed,
                      refFor, onMenu, onEdit, onSaved, onReload }: {
  keyName: string; control: Control | null; projectId: string; actor: string; disabled: boolean;
  editing: boolean; menuOpen: boolean; focused: boolean; pulsed: boolean;
  refFor?: React.MutableRefObject<HTMLLIElement | null>;
  onMenu: (open: boolean) => void; onEdit: (on: boolean) => void; onSaved: (word: string) => void;
  onReload: () => void;
}) {
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (!editing) setFailure(null); }, [editing]);
  const name = c?.name ?? ROW_NAMES[keyName] ?? keyName;
  const note = c ? noteText(c) : null;
  const evidence = c?.evidence;

  const write = async (op: string, body: Record<string, unknown>, word: string) => {
    setBusy(true);
    setFailure(null);
    const r = await governWrite(op, { project_id: projectId, actor, ...body }, c?.version);
    setBusy(false);
    if (r.ok) onSaved(word); else setFailure(failureOf(r, op));
  };
  const useDefault = () => write("setting", { key: keyName, value: null }, `${name} back to default`);
  const editable = c?.editable_by === "self";
  const byPace = c?.editable_by === "pace" && (keyName === "daily_cap" || keyName === "burst");

  return (
    <li ref={refFor} className="gv-row" data-testid={`gv-control-${keyName}`} data-key={keyName}
        data-focused={focused || undefined} data-advisory={c?.advisory || undefined} data-editing={editing || undefined}>
      <span className="gv-name">
        <span title={HELP[keyName]}>{name}</span>
      </span>
      <span className="gv-value" data-pulsed={pulsed || undefined}>
        <span className="gv-value-main" data-testid={`gv-value-${keyName}`}>{c ? valueText(c) : "—"}</span>
        {note ? <span className="gv-value-note" data-testid={`gv-note-${keyName}`} title={note.tip}>{note.text}</span> : null}
        {c?.running_now ? (
          <span className="gv-value-note gv-running-now" data-testid={`gv-running-now-${keyName}`}
                title="Memory is low, so the review panel runs one seat at a time.">
            · running {c.running_now.value} now (memory)
          </span>
        ) : null}
      </span>
      <span className="gv-source">
        {c ? <span title={c.source === "pace" ? "Written by Pace" : undefined}>{SOURCE_WORD[c.source] ?? c.source}</span> : null}
        {c?.source === "set" && c.write?.path.startsWith("/v1/settings/") && !disabled ? (
          <button type="button" className="ag-link" data-testid={`gv-reset-${keyName}`} disabled={busy}
                  onClick={() => void useDefault()}>Use default</button>
        ) : null}
      </span>
      <span className="gv-enforced" data-advisory={c?.advisory || undefined}
            title={c ? ENFORCED_TIP[c.enforced.effect_key] : undefined}>{c?.enforced.word ?? ""}</span>
      <span className="gv-evidence">
        {evidence ? (
          <a className="ag-link" data-testid={`gv-evidence-${keyName}`} href={logHref({ kind: evidence.log.kind, range: "7d" })}
             title="Open the Log at these">{evidence.word}</a>
        ) : c ? <span title="BOSS doesn't record when this bites">—</span> : null}
      </span>
      <span className="gv-act">
        {editable || byPace ? (
          <span className="gv-more">
            <button type="button" className="ag-link gv-more-btn" aria-label={`More for ${name}`} aria-expanded={menuOpen}
                    disabled={disabled} data-testid={`gv-more-${keyName}`} onClick={() => onMenu(!menuOpen)}>
              <DotsThree size={16} weight="bold" aria-hidden />
            </button>
            {menuOpen ? (
              <span className="gv-menu" role="menu">
                <button type="button" role="menuitem" data-testid={`gv-edit-${keyName}`} disabled={byPace}
                        title={byPace ? "Today only Pace sets this. See Pace." : undefined}
                        onClick={() => onEdit(true)}>{byPace ? "Set by Pace" : "Edit"}</button>
              </span>
            ) : null}
          </span>
        ) : null}
      </span>
      {editing && c ? (
        <div className="gv-edit" data-testid={`gv-editor-${keyName}`}>
          {keyName === "pace" ? (
            <PaceEditor control={c} projectId={projectId} busy={busy}
                        onApply={(n) => void write("pace", { intensity: n }, `Pace set to ${n}`)}
                        onCancel={() => onEdit(false)} />
          ) : keyName === "monthly_cap" ? (
            <MonthlyCapEditor control={c} projectId={projectId} busy={busy}
                              onSave={(usd) => void write("monthly_cap", { monthly_cap_usd: usd }, `${name} saved`)}
                              onCancel={() => onEdit(false)} />
          ) : keyName === "captain_leads_projects" ? (
            <SwitchEditor control={c} busy={busy}
                          onSave={(on) => void write("setting", { key: keyName, value: on ? "1" : "0" }, `${name} saved`)}
                          onCancel={() => onEdit(false)} />
          ) : (
            <NumberEditor control={c} busy={busy}
                          onSave={(n) => void write("setting", { key: keyName, value: String(n) }, `${name} saved`)}
                          onCancel={() => onEdit(false)} />
          )}
          {failure ? <FailureLine failure={failure} onReload={() => { onReload(); onEdit(false); }} /> : null}
        </div>
      ) : failure ? <FailureLine failure={failure} onReload={onReload} /> : null}
    </li>
  );
}

function FieldError({ error }: { error: string | null }) {
  return error ? <span className="gv-field-error" role="alert" data-testid="gv-field-error">{error}</span> : null;
}

function Buttons({ busy, onCancel, label = "Save" }: { busy: boolean; onCancel: () => void; label?: string }) {
  return (
    <span className="gv-edit-buttons">
      <button type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="gv-save">{label}</button>
      <button type="button" className="ag-btn" onClick={onCancel} data-testid="gv-cancel">Cancel</button>
    </span>
  );
}

function NumberEditor({ control, busy, onSave, onCancel }: {
  control: Control; busy: boolean; onSave: (n: number) => void; onCancel: () => void;
}) {
  const start = control.value_stored ?? control.value_effective;
  const [text, setText] = useState(String(start ?? ""));
  const { value: n, error } = readNumber(text, { min: control.bounds?.min, max: control.bounds?.max, whole: true });
  const effective = n !== null ? effectiveOf(control, n) : null;
  return (
    <form className="gv-edit-form" noValidate onSubmit={(e) => { e.preventDefault(); if (n !== null) onSave(n); }}
          onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); onCancel(); } }}>
      <input className="gv-input" type="number" inputMode="numeric" autoFocus aria-label={control.name}
             min={control.bounds?.min ?? undefined} max={control.bounds?.max ?? undefined} step="any"
             aria-invalid={error ? true : undefined}
             value={text} onChange={(e) => setText(e.target.value)} data-testid="gv-input" />
      {control.bounds ? <span className="gv-bounds">{control.bounds.min}–{control.bounds.max}</span> : null}
      {n !== null && effective !== null ? (
        <span className="gv-preview-line" data-testid="gv-will-use">BOSS will use {effective}</span>
      ) : <FieldError error={error} />}
      <Buttons busy={busy || n === null} onCancel={onCancel} />
    </form>
  );
}

function SwitchEditor({ control, busy, onSave, onCancel }: {
  control: Control; busy: boolean; onSave: (on: boolean) => void; onCancel: () => void;
}) {
  const [on, setOn] = useState(Boolean(control.value_effective));
  return (
    <form className="gv-edit-form" noValidate onSubmit={(e) => { e.preventDefault(); onSave(on); }}
          onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); onCancel(); } }}>
      <button type="button" role="switch" aria-checked={on} className="gv-switch" data-testid="gv-switch"
              onClick={() => setOn(!on)}>{on ? "On" : "Off"}</button>
      <span className="gv-preview-line">BOSS will use {on ? "On" : "Off"}</span>
      <Buttons busy={busy} onCancel={onCancel} />
    </form>
  );
}

function MonthlyCapEditor({ control, projectId, busy, onSave, onCancel }: {
  control: Control; projectId: string; busy: boolean; onSave: (usd: number) => void; onCancel: () => void;
}) {
  const [text, setText] = useState(String(Number(control.value_effective ?? 0) / 1_000_000));
  const { value: dollars, error } = readNumber(text, { min: 0.01, max: 1_000_000, cents: true, money: true });
  const asked = useDebounced(dollars, 300);
  const [preview, setPreview] = useState<CapPreviewData | null>(null);
  useEffect(() => {
    if (asked === null) { setPreview(null); return; }
    let alive = true;
    void governWrite("cap_preview", { project_id: projectId, period: "month", cap_usd: asked }).then((r) => {
      if (alive && r.ok) setPreview(r.data as unknown as CapPreviewData);
    });
    return () => { alive = false; };
  }, [asked, projectId]);
  return (
    <form className="gv-edit-form gv-edit-wide" noValidate onSubmit={(e) => { e.preventDefault(); if (dollars !== null) onSave(dollars); }}
          onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); onCancel(); } }}>
      <label className="gv-money-input">$<input className="gv-input" type="number" inputMode="decimal" autoFocus
             aria-label="Monthly cap" min={0.01} max={1_000_000} step="any" value={text} data-testid="gv-input"
             aria-invalid={error ? true : undefined} onChange={(e) => setText(e.target.value)} /></label>
      <Buttons busy={busy || dollars === null} onCancel={onCancel} />
      <FieldError error={error} />
      {preview ? <CapPreview preview={preview} /> : null}
    </form>
  );
}

function PaceEditor({ control, projectId, busy, onApply, onCancel }: {
  control: Control; projectId: string; busy: boolean; onApply: (n: number) => void; onCancel: () => void;
}) {
  const [value, setValue] = useState(Number(control.value_effective ?? 50));
  const asked = useDebounced(value, 150);
  const [preview, setPreview] = useState<{ envelope: Envelope; preview: CapPreviewData | null } | null>(null);
  useEffect(() => {
    let alive = true;
    void governWrite("pace_preview", { project_id: projectId, intensity: asked }).then((r) => {
      if (alive && r.ok) setPreview(r.data as unknown as { envelope: Envelope; preview: CapPreviewData | null });
    });
    return () => { alive = false; };
  }, [asked, projectId]);
  const env = preview?.envelope;
  return (
    <form className="gv-edit-form gv-edit-wide" noValidate onSubmit={(e) => { e.preventDefault(); onApply(value); }}
          onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); onCancel(); } }}>
      <span className="gv-slider">
        <span className="gv-slider-end">Careful</span>
        <input type="range" min={0} max={100} step={1} value={value} aria-label="Pace" data-testid="gv-pace-slider"
               onChange={(e) => setValue(Number(e.target.value))} />
        <span className="gv-slider-end">Bold</span>
        <b className="gv-slider-value" data-testid="gv-pace-value-edit">{value}</b>
      </span>
      {env && env.operational_intensity === asked ? (
        <span className="gv-pace-preview" data-testid="gv-pace-preview"
              title="Exactly what Apply writes">
          <span>Daily {money(env.daily_cap_usd_micros)}</span> · <span>Monthly {money(env.monthly_cap_usd_micros)}</span>
          {" · "}<span>Burst {money(env.burst_cap_usd_micros_per_min, { perMin: true })}</span>
          {" · "}<span>Jobs {env.max_concurrent_jobs}</span> · <span>Workers {env.max_workers_total}</span>
        </span>
      ) : <span className="gv-pace-preview" aria-busy="true" />}
      <span className="gv-edit-buttons">
        <button type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="gv-pace-apply">Apply</button>
        <button type="button" className="ag-btn" onClick={onCancel}>Cancel</button>
      </span>
      {preview?.preview ? <CapPreview preview={preview.preview} /> : null}
    </form>
  );
}

function TokenGuides({ view, projectId, actor, disabled, onSaved }: {
  view: GovernView | null; projectId: string; actor: string; disabled: boolean; onSaved: () => void;
}) {
  const guides = view?.token_guides;
  const [editing, setEditing] = useState(false);
  const [stage, setStage] = useState("");
  const [cap, setCap] = useState("");
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState(false);
  const def = guides?.default ?? null;
  const steps: TokenPolicy[] = guides?.per_step ?? [];
  const stageWord = (s: string) => s.replace(/_/g, " ");
  // Exact whenever rounding would change it: a saved 48,500 never reads as 49k.
  const k = (n: number | null | undefined) => (!n ? "—"
    : n % 1_000_000 === 0 ? `${n / 1_000_000}M` : n % 1000 === 0 ? `${n / 1000}k` : n.toLocaleString("en-US"));
  const [allSteps, setAllSteps] = useState(false);
  // What was SET comes first (it is what someone changed), then the defaults.
  const capped = steps.filter((p) => p.max_total_tokens_per_call)
    .sort((a, b) => Number(b.source === "stored") - Number(a.source === "stored"));
  const shownSteps = allSteps ? capped : capped.slice(0, 3);
  const versionOf = (s: string) => {
    const p = s === "*" ? def : steps.find((x) => x.stage === s);
    return p?.source === "stored" && p.updated_at ? p.updated_at : "unset";
  };
  const save = async () => {
    if (!stage) { setFailure({ text: "Pick a stage" }); return; }
    const { value: n, error } = readNumber(cap, { min: 1000, whole: true });
    if (n === null) { setFailure({ text: error === "Enter a number" ? "Enter tokens per call" : `${error} tokens` }); return; }
    setBusy(true);
    setFailure(null);
    const r = await governWrite("token_cap", { project_id: projectId, actor, stage, max_total_tokens_per_call: Math.trunc(n) },
                                versionOf(stage));
    setBusy(false);
    if (r.ok) {
      showToast("success", `${stage === "*" ? "Project default" : stageWord(stage)} saved`);
      setEditing(false); setStage(""); setCap("");
      onSaved();
    } else setFailure(failureOf(r, "token_cap"));
  };
  return (
    <section className="gv-sec" aria-labelledby="gv-sec-tokens" data-advisory="true">
      <h3 id="gv-sec-tokens" className="gv-sec-head">
        TOKEN GUIDES <span className="gv-badge" title="Shapes alerts and tips. Doesn't stop model calls.">Advisory</span>
      </h3>
      <ul className="gv-rows" role="list">
        <li className="gv-row" data-advisory="true" data-testid="gv-control-token_default">
          <span className="gv-name">Project default</span>
          <span className="gv-value">
            <span className="gv-value-main" data-testid="gv-value-token_default">
              {def ? (<><span>{k(def.max_tokens_per_day)} tok/day</span> · <span>{money(def.max_cost_usd_micros_per_day)}/day</span>
                {" · "}<span>alert {Math.round((def.alert_at_pct ?? 0.8) * 100)}%</span></>) : "—"}
            </span>
          </span>
          <span className="gv-source">{def ? (def.source === "stored" ? "Set here" : "Default") : ""}</span>
          <span className="gv-enforced" data-advisory title="Shapes alerts and tips. Doesn't stop model calls.">Advisory</span>
          <span className="gv-evidence"><span title="BOSS doesn't record when this bites">—</span></span>
          <span className="gv-act" />
        </li>
        <li className="gv-row" data-advisory="true" data-testid="gv-control-token_per_step" data-editing={editing || undefined}>
          <span className="gv-name">Per step</span>
          <span className="gv-value">
            <span className="gv-value-main gv-steps" data-user-text
                  title="Tokens per model call, by step. Edit to change one.">
              {capped.length
                ? shownSteps.map((p, i) => (
                  <React.Fragment key={p.stage}>{i ? " · " : ""}<span data-testid={`gv-step-${p.stage}`}
                    data-set={p.source === "stored" || undefined}>{stageWord(p.stage)} {k(p.max_total_tokens_per_call)}</span></React.Fragment>))
                : "—"}
            </span>
            {capped.length > 3 ? (
              <button type="button" className="ag-link gv-steps-more" data-testid="gv-steps-more" aria-expanded={allSteps}
                      onClick={() => setAllSteps((a) => !a)}>{allSteps ? "Fewer" : `+${capped.length - 3} more`}</button>
            ) : null}
          </span>
          <span className="gv-source">{steps.some((p) => p.source === "stored") ? "Set here" : "Default"}</span>
          <span className="gv-enforced" data-advisory title="Shapes alerts and tips. Doesn't stop model calls.">Advisory</span>
          <span className="gv-evidence"><span title="BOSS doesn't record when this bites">—</span></span>
          <span className="gv-act">
            <button type="button" className="ag-btn" data-testid="gv-token-edit" disabled={disabled}
                    onClick={() => setEditing((e) => !e)}>Edit</button>
          </span>
          {editing ? (
            <form className="gv-edit gv-edit-form" noValidate data-testid="gv-token-form"
                  onSubmit={(e) => { e.preventDefault(); void save(); }}
                  onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); setEditing(false); } }}>
              <select className="gv-input" aria-label="Stage" value={stage} onChange={(e) => setStage(e.target.value)}
                      data-testid="gv-token-stage">
                <option value="">Choose a stage…</option>
                <option value="*">project default (*)</option>
                {steps.map((p) => <option key={p.stage} value={p.stage}>{p.stage}</option>)}
              </select>
              <input className="gv-input" type="number" min={1000} step="any" placeholder="e.g. 48000"
                     aria-label="Max tokens per call" value={cap} onChange={(e) => setCap(e.target.value)}
                     data-testid="gv-token-cap" />
              <span className="gv-edit-buttons">
                <button type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="gv-token-save">Save</button>
                <button type="button" className="ag-btn" onClick={() => setEditing(false)}>Cancel</button>
              </span>
              {failure ? <FailureLine failure={failure} onReload={() => { onSaved(); setEditing(false); }} /> : null}
            </form>
          ) : null}
        </li>
      </ul>
    </section>
  );
}
