"use client";

/**
 * "When it fails" (#512 phase 2): a step's tries, the wait between them, its time limit, and
 * where the run goes when it still fails.
 *
 * Every field writes one spec field (lib/workflow-failure.ts says which, and their ranges) and is
 * omitted when left empty: the engine's default is shown as the placeholder, never written. A
 * value out of range is said under its box, in words, and not written, so the spec only ever holds
 * values the engine accepts. "If it still fails, go to…" is the step's error path: it is set
 * through `onErrorPath` (the editor asks the validator first, as for any arrow on the canvas), so
 * a cycle or a stranded step is refused in the validator's words, never by a rule kept here.
 */
import React, { useEffect, useId, useRef, useState } from "react";

import { HelpTip } from "../../../help-tip";
import type { Palette, Problem, Spec, SpecStep, StepPatch } from "../../../../lib/workflow-editor-types";
import { stepView } from "../../../../lib/step-view";
import { errorTargetOf, loopBodies } from "../../../../lib/workflow-graph";
import {
  BACKOFF_DEFAULTS, DEFAULT_ATTEMPTS, TIME_UNITS, failureOf, readNumber, readTimeLimit, sayWaits,
  unitFor, unitSeconds, withBackoff, type FailureNumber, type TimeUnit,
} from "../../../../lib/workflow-failure";
import { FieldError } from "./block-settings";
import { Section } from "./step-inspector";

/** Step kinds the runtime steps through itself: nothing to retry or time out. The engine's
 *  CONTROL_KINDS (workflows.py) refuses failure settings on exactly these. */
const CONTROL_KINDS = new Set(["condition", "parallel", "join", "bounded_loop", "decision", "timer",
                               "subworkflow"]);

/** Whether a step has failure settings at all (a Branch, Do at the same time, its join, a Repeat,
 *  an approval, a wait and a sub-workflow are run by the runtime itself, not tried). */
export function hasFailureSettings(step: SpecStep): boolean {
  return !CONTROL_KINDS.has(step.kind);
}

/** The fields this section draws, so a problem on one shows on it. */
export const FAILURE_FIELDS = ["max_attempts", "retry_backoff", "timeout_seconds", "on_error"];

/** "Stop the run": the picker's value for no error path. */
const STOP = "";

/**
 * A number box that keeps what is typed. The store only ever holds a valid value, so the box
 * holds its own text: an out-of-range value stays on screen with its reason under it (and is not
 * written), and the box takes the stored value back when it changes underneath (undo, JSON).
 */
function NumberBox({ field, stored, placeholder, label, testId, onCommit, suffix }: {
  field: FailureNumber; stored: number | null; placeholder: string; label: string; testId: string;
  onCommit(value: number | null): void; suffix?: string;
}) {
  const id = useId();
  const [text, setText] = useState(stored === null ? "" : String(stored));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setText((typed) => {
      const read = readNumber(field, typed);
      return "value" in read && read.value === stored ? typed : stored === null ? "" : String(stored);
    });
    setError(null);
  }, [field, stored]);
  return (
    <span className="wf-fail-num">
      <label className="wf-label" htmlFor={id}>{label}</label>
      <span className="wf-fail-box">
        <input id={id} className="wf-input wf-num" inputMode="decimal" value={text} placeholder={placeholder}
               aria-invalid={error ? true : undefined} data-testid={testId}
               aria-describedby={error ? `${id}-error` : undefined}
               onChange={(e) => {
                 setText(e.target.value);
                 const read = readNumber(field, e.target.value);
                 if ("error" in read) { setError(read.error); return; }
                 setError(null);
                 if (read.value !== stored) onCommit(read.value);
               }} />
        {suffix ? <span className="wf-fail-unit">{suffix}</span> : null}
      </span>
      {error ? <span id={`${id}-error`} className="wf-field-error" role="alert" data-testid={`${testId}-error`}>{error}</span> : null}
    </span>
  );
}

/** The time limit: a number and its unit, stored as seconds. */
function TimeLimit({ stored, onCommit }: { stored: number | null; onCommit(value: number | null): void }) {
  const id = useId();
  const [unit, setUnit] = useState<TimeUnit>(unitFor(stored));
  const shown = (u: TimeUnit) => (stored === null ? "" : String(stored / unitSeconds(u)));
  const [text, setText] = useState(shown(unit));
  const [error, setError] = useState<string | null>(null);
  const typed = useRef({ text, unit });
  typed.current = { text, unit };
  useEffect(() => {
    const now = typed.current;
    const read = readTimeLimit(now.text, now.unit);
    if ("value" in read && read.value === stored) return;
    // A stored value the unit in use cannot say whole is shown in one that can.
    const u = stored !== null && stored % unitSeconds(now.unit) !== 0 ? unitFor(stored) : now.unit;
    setUnit(u);
    setText(stored === null ? "" : String(stored / unitSeconds(u)));
    setError(null);
  }, [stored]);
  const commit = (value: string, u: TimeUnit) => {
    const read = readTimeLimit(value, u);
    if ("error" in read) { setError(read.error); return; }
    setError(null);
    if (read.value !== stored) onCommit(read.value);
  };
  return (
    <div className="wf-field" data-testid="wf-fail-timeout-field">
      <span className="wf-fail-row">
        <label className="wf-label" htmlFor={id}>Time limit</label>
        <HelpTip label="Time limit" text="A try that runs longer than this fails, and counts as a try" />
      </span>
      <span className="wf-fail-box">
        <input id={id} className="wf-input wf-num" inputMode="decimal" value={text} placeholder="None"
               aria-invalid={error ? true : undefined} data-testid="wf-fail-timeout"
               aria-describedby={error ? `${id}-error` : undefined}
               onChange={(e) => { setText(e.target.value); commit(e.target.value, unit); }} />
        <select className="wf-select wf-select-sm" aria-label="Time limit unit" value={unit}
                data-testid="wf-fail-timeout-unit"
                onChange={(e) => { const u = e.target.value as TimeUnit; setUnit(u); commit(text, u); }}>
          {TIME_UNITS.map((u) => <option key={u.key} value={u.key}>{u.label}</option>)}
        </select>
      </span>
      {error ? <span id={`${id}-error`} className="wf-field-error" role="alert" data-testid="wf-fail-timeout-error">{error}</span> : null}
    </div>
  );
}

export function FailureSettings({ step, spec, palette, problems, onPatch, onErrorPath }: {
  step: SpecStep; spec: Spec; palette: Palette; problems: Problem[];
  onPatch(patch: StepPatch): void;
  /** Point the step's error path at `target` (null: Stop the run). */
  onErrorPath(target: string | null): void;
}) {
  const f = failureOf(step);
  const target = errorTargetOf(spec, step);
  const pickId = useId();
  // The engine refuses an error path on a Repeat's own step, and one into a join or a Repeat's
  // own step: those return to their block, not onward.
  const bodies = loopBodies(spec);
  const inLoop = bodies.has(step.id);
  const choices = spec.steps.filter((s) => s.id !== step.id && s.kind !== "join" && !bodies.has(s.id))
    .map((s) => ({ id: s.id, title: stepView(s, palette).title }));
  const backoff = (key: "initial_seconds" | "multiplier" | "max_seconds") => (value: number | null) =>
    onPatch({ retry_backoff: withBackoff(step, key, value) });
  return (
    <Section label="When it fails" testId="wf-sec-failure"
             tip="How many times it is tried, how long it waits between tries, and where the run goes if it still fails">
      <div className="wf-field">
        <NumberBox field="max_attempts" stored={f.maxAttempts} placeholder={String(DEFAULT_ATTEMPTS)}
                   label="Tries" testId="wf-fail-attempts" suffix="in all"
                   onCommit={(value) => onPatch({ max_attempts: value })} />
        <FieldError problems={problems} field="max_attempts" />
      </div>

      <div className="wf-field" data-testid="wf-fail-backoff">
        <span className="wf-sub-label">Wait between tries</span>
        <span className="wf-fail-grid">
          <NumberBox field="initial_seconds" stored={f.backoff.initial_seconds ?? null}
                     placeholder={String(BACKOFF_DEFAULTS.initial_seconds)} label="First wait"
                     testId="wf-fail-initial" suffix="s" onCommit={backoff("initial_seconds")} />
          <NumberBox field="multiplier" stored={f.backoff.multiplier ?? null}
                     placeholder={String(BACKOFF_DEFAULTS.multiplier)} label="Grows ×"
                     testId="wf-fail-multiplier" onCommit={backoff("multiplier")} />
          <NumberBox field="max_seconds" stored={f.backoff.max_seconds ?? null}
                     placeholder={String(BACKOFF_DEFAULTS.max_seconds)} label="Up to"
                     testId="wf-fail-max" suffix="s" onCommit={backoff("max_seconds")} />
        </span>
        <span className="wf-hint wf-fail-preview" data-testid="wf-fail-preview" aria-live="polite">
          {sayWaits(f.maxAttempts, f.backoff)}
        </span>
        <FieldError problems={problems} field="retry_backoff" />
      </div>

      <TimeLimit stored={f.timeoutSeconds} onCommit={(value) => onPatch({ timeout_seconds: value })} />
      <FieldError problems={problems} field="timeout_seconds" />

      {inLoop ? null : <div className="wf-field">
        <label className="wf-label" htmlFor={pickId}>If it still fails, go to…</label>
        <select id={pickId} className="wf-select" value={target ?? STOP} data-testid="wf-fail-on-error"
                onChange={(e) => onErrorPath(e.target.value === STOP ? null : e.target.value)}>
          <option value={STOP}>Stop the run</option>
          {choices.map((c) => <option key={c.id} value={c.id}>{c.title}</option>)}
        </select>
        <FieldError problems={problems} field="on_error" />
      </div>}
    </Section>
  );
}
