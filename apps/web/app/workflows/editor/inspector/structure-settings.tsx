"use client";

/**
 * The settings of a branch block: Branch (if / else), Do at the same time (and its Wait for
 * branches), Repeat until, For each.
 *
 * Every control is one action on the store — PATCH_STEP for a setting, ADD_BRANCH and
 * REMOVE_BRANCH for a parallel's branches, SELECT to go to a step — so undo, the lane and the
 * JSON follow. What a condition checks is built from parts (lib/workflow-expression.ts), never
 * typed as code; a condition the builder cannot show as written opens as JSON instead.
 */
import { Plus, X } from "@phosphor-icons/react";
import React, { useEffect, useId, useMemo, useState } from "react";

import type { Palette, Problem, Spec, SpecStep } from "../../../../lib/workflow-editor-types";
import {
  branchChain, DEFAULT_MAX_ITEMS, MAX_CONCURRENCY, MAX_ITEMS_LIMIT,
} from "../../../../lib/workflow-blocks";
import {
  OPS, expressionSources, fromExpression, kindOf, parseList, parseValue, pathLabel, takesOf,
  toExpression, valueText, type Condition, type ExprOp, type Rule, type Source, type ValueKind,
} from "../../../../lib/workflow-expression";
import { stepTitle } from "../../../../lib/step-view";
import { FieldError } from "./block-settings";
import { strings } from "./step-facts";

export type StructureProps = {
  step: SpecStep;
  spec: Spec;
  palette: Palette;
  problems: Problem[];
  onConfig(config: Record<string, unknown>): void;
  onSelect(id: string): void;
  onAddBranch(): void;
  onRemoveBranch(branch: string): void;
};

const OTHER = "__other__";

/** A step named by its title, as a button that opens it. */
function StepLink({ id, spec, palette, onSelect, testId }: {
  id: string; spec: Spec; palette: Palette; onSelect(id: string): void; testId?: string;
}) {
  const step = spec.steps.find((s) => s.id === id);
  if (!step) return <span className="wf-faint wf-mono">{id || "—"}</span>;
  return (
    <button type="button" className="wf-chip wf-chip-action" data-testid={testId}
            title="Open this step" onClick={() => onSelect(id)}>
      {stepTitle(step, palette)}
    </button>
  );
}

/* ── The expression builder ─────────────────────────────────────────────────────── */

function RuleRow({ rule, index, sources, onChange, onRemove }: {
  rule: Rule; index: number; sources: Source[]; onChange(rule: Rule): void; onRemove(): void;
}) {
  const n = index + 1;
  const known = sources.some((s) => s.path === rule.path);
  const [custom, setCustom] = useState(!known && rule.path !== "");
  const takes = takesOf(rule.op);
  const [kind, setKind] = useState<ValueKind>(kindOf(rule.value));
  const [text, setText] = useState(valueText(rule.value));
  const [bad, setBad] = useState(false);
  const source = sources.find((s) => s.path === rule.path);
  const listId = useId();
  // Follow the rule when it changes underneath (undo, JSON).
  useEffect(() => {
    setText((typed) => (valueText(parsedFrom(typed)) === valueText(rule.value) ? typed : valueText(rule.value)));
    setKind((k) => (rule.value === undefined || rule.value === "" ? k : kindOf(rule.value)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rule.value]);

  function parsedFrom(t: string, k: ValueKind = kind): unknown {
    return takes === "list" ? parseList(t, k) : parseValue(t, k);
  }
  function setValue(t: string, k: ValueKind = kind) {
    setText(t);
    const value = parsedFrom(t, k);
    setBad(value === null);
    if (value !== null) onChange({ ...rule, value });
  }

  return (
    <li className="wf-expr-rule" data-testid={`wf-expr-rule-${index}`}>
      <div className="wf-row">
        <select className="wf-select" aria-label={`Check ${n}: value to check`}
                data-testid={`wf-expr-source-${index}`}
                value={custom ? OTHER : rule.path}
                onChange={(e) => {
                  if (e.target.value === OTHER) { setCustom(true); return; }
                  setCustom(false);
                  onChange({ ...rule, path: e.target.value });
                }}>
          <option value="" disabled>Choose a value…</option>
          {(["Start form", "Steps"] as const).map((group) => {
            const rows = sources.filter((s) => s.group === group);
            return rows.length ? (
              <optgroup key={group} label={group}>
                {rows.map((s) => <option key={s.path} value={s.path}>{s.label}</option>)}
              </optgroup>
            ) : null;
          })}
          <option value={OTHER}>Other…</option>
        </select>
        <button type="button" className="wf-icon-btn" aria-label={`Remove check ${n}`}
                data-testid={`wf-expr-remove-${index}`} onClick={onRemove}>
          <X size={14} weight="bold" aria-hidden />
        </button>
      </div>
      {custom ? (
        <input className="wf-input wf-mono" aria-label={`Check ${n}: path`} value={rule.path}
               placeholder="steps.review.output.score" data-testid={`wf-expr-path-${index}`}
               title={rule.path ? pathLabel(rule.path) : undefined}
               onChange={(e) => onChange({ ...rule, path: e.target.value })} />
      ) : null}
      <div className="wf-row">
        <select className="wf-select wf-select-sm" aria-label={`Check ${n}: comparison`}
                data-testid={`wf-expr-op-${index}`} value={rule.op}
                onChange={(e) => {
                  const op = e.target.value as ExprOp;
                  const next = takesOf(op);
                  const value = next === "none" ? undefined
                    : next === "list" ? parseList(text, kind) : (parseValue(text.split(",")[0] ?? "", kind) ?? "");
                  onChange({ path: rule.path, op, ...(value === undefined ? {} : { value }) });
                }}>
          {OPS.map((o) => <option key={o.op} value={o.op}>{o.label}</option>)}
        </select>
        {takes === "none" ? null : kind === "yesno" && takes === "one" ? (
          <select className="wf-select wf-select-sm" aria-label={`Check ${n}: compare with`}
                  data-testid={`wf-expr-value-${index}`} value={rule.value === true ? "true" : "false"}
                  onChange={(e) => setValue(e.target.value)}>
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        ) : (
          <input className="wf-input" aria-label={`Check ${n}: compare with`} value={text}
                 aria-invalid={bad || undefined} data-testid={`wf-expr-value-${index}`}
                 list={source?.suggestions ? listId : undefined}
                 placeholder={takes === "list" ? "a, b, c" : "value"}
                 onChange={(e) => setValue(e.target.value)} />
        )}
        {takes === "none" ? null : (
          <select className="wf-select wf-select-sm" aria-label={`Check ${n}: kind of value`}
                  data-testid={`wf-expr-kind-${index}`} value={kind}
                  onChange={(e) => {
                    const k = e.target.value as ValueKind;
                    setKind(k);
                    setValue(k === "yesno" ? "true" : text, k);
                  }}>
            <option value="text">Text</option>
            <option value="number">Number</option>
            <option value="yesno">Yes / no</option>
          </select>
        )}
      </div>
      {source?.suggestions ? (
        <datalist id={listId}>{source.suggestions.map((s) => <option key={s} value={s} />)}</datalist>
      ) : null}
      {bad ? <span className="wf-field-error" role="alert">{kind === "number" ? "Not a number" : "Not valid"}</span> : null}
    </li>
  );
}

/** The comparisons as a list, all or any of them. A `not` around them belongs to the caller (a
 *  loop's Until / While); a negated expression handed in here opens as JSON. */
export function ExpressionBuilder({ expression, sources, onChange, problems, lead }: {
  expression: unknown; sources: Source[];
  onChange(expression: Record<string, unknown>): void; problems: Problem[];
  /** Says what the comparisons decide: "Take the yes path when". */
  lead: string;
}) {
  const parsed = useMemo(() => {
    const c = fromExpression(expression);
    return c && !c.negate ? c : null;
  }, [expression]);
  const [json, setJson] = useState(parsed === null);
  useEffect(() => { if (parsed === null) setJson(true); }, [parsed]);
  const condition: Condition = parsed ?? { match: "all", rules: [], negate: false };
  const set = (next: Partial<Condition>) => onChange(toExpression({ ...condition, ...next }));

  if (json) {
    return (
      <>
        <JsonExpression expression={expression} problems={problems} onChange={onChange} />
        {parsed ? (
          <button type="button" className="wf-link" data-testid="wf-expr-builder"
                  onClick={() => setJson(false)}>Use the builder</button>
        ) : <span className="wf-faint">Groups inside groups are edited here.</span>}
      </>
    );
  }
  const rules = condition.rules;
  return (
    <div className="wf-field wf-expr" data-testid="wf-expr">
      <div className="wf-row wf-expr-lead">
        <span className="wf-label">{lead}</span>
        {rules.length > 1 ? (
          <span className="wf-seg" role="group" aria-label="Which checks must pass">
            <button type="button" aria-pressed={condition.match === "all"} data-testid="wf-expr-all"
                    onClick={() => set({ match: "all" })}>all</button>
            <button type="button" aria-pressed={condition.match === "any"} data-testid="wf-expr-any"
                    onClick={() => set({ match: "any" })}>any</button>
          </span>
        ) : null}
        {rules.length > 1 ? <span className="wf-label">of these pass</span> : null}
      </div>
      {rules.length ? (
        <ol className="wf-expr-rules">
          {rules.map((rule, i) => (
            <RuleRow key={i} rule={rule} index={i} sources={sources}
                     onChange={(r) => set({ rules: rules.map((x, j) => (j === i ? r : x)) })}
                     onRemove={() => set({ rules: rules.filter((_, j) => j !== i) })} />
          ))}
        </ol>
      ) : <span className="wf-faint" data-testid="wf-expr-empty">Nothing to check yet.</span>}
      <FieldError problems={problems} field="expression" />
      <span className="wf-row">
        <button type="button" className="wf-link wf-link-icon" data-testid="wf-expr-add"
                onClick={() => set({ rules: [...rules, { path: sources[0]?.path ?? "", op: "eq", value: "" }] })}>
          <Plus size={12} weight="bold" aria-hidden /> Add a check
        </button>
        <span className="wf-spacer" />
        <button type="button" className="wf-link" data-testid="wf-expr-json"
                onClick={() => setJson(true)}>Edit as JSON</button>
      </span>
    </div>
  );
}

function JsonExpression({ expression, problems, onChange }: {
  expression: unknown; problems: Problem[]; onChange(e: Record<string, unknown>): void;
}) {
  const stored = JSON.stringify(expression ?? {});
  const [text, setText] = useState(stored);
  const [bad, setBad] = useState(false);
  useEffect(() => { setText(stored); setBad(false); }, [stored]);
  return (
    <label className="wf-field">
      <span className="wf-label">Condition (JSON)</span>
      <textarea className="wf-text wf-mono" rows={3} value={text} aria-invalid={bad || undefined}
                data-testid="wf-expr-json-text"
                onChange={(e) => {
                  setText(e.target.value);
                  try {
                    const parsed = JSON.parse(e.target.value);
                    const ok = parsed && typeof parsed === "object" && !Array.isArray(parsed);
                    setBad(!ok);
                    if (ok) onChange(parsed);
                  } catch { setBad(true); }
                }} />
      {bad ? <span className="wf-field-error" role="alert">Not valid JSON</span> : null}
      <FieldError problems={problems} field="expression" />
    </label>
  );
}

/* ── One per block ─────────────────────────────────────────────────────────────── */

export function ConditionSettings(p: StructureProps) {
  const { step, spec, palette, problems, onConfig, onSelect } = p;
  const sources = useMemo(() => expressionSources(spec, step.id, palette), [spec, step.id, palette]);
  return (
    <>
      <ExpressionBuilder expression={step.expression} sources={sources} problems={problems}
                         lead="Take the yes path when"
                         onChange={(expression) => onConfig({ expression })} />
      <div className="wf-field" data-testid="wf-paths">
        <span className="wf-label">Paths</span>
        <span className="wf-row"><span className="wf-path-tag">Yes</span>
          <StepLink id={String(step.on_true ?? "")} spec={spec} palette={palette} onSelect={onSelect}
                    testId="wf-path-yes" /></span>
        <FieldError problems={problems} field="on_true" />
        <span className="wf-row"><span className="wf-path-tag">No</span>
          <StepLink id={String(step.on_false ?? "")} spec={spec} palette={palette} onSelect={onSelect}
                    testId="wf-path-no" /></span>
        <FieldError problems={problems} field="on_false" />
        <span className="wf-faint">Add steps to a path with the + on the lane.</span>
      </div>
    </>
  );
}

export function ParallelSettings(p: StructureProps) {
  const { step, spec, palette, problems, onSelect, onAddBranch, onRemoveBranch } = p;
  const branches = strings(step.branches);
  const join = String(step.join ?? "");
  return (
    <>
      <div className="wf-field" data-testid="wf-branches">
        <span className="wf-label">Branches</span>
        <ul className="wf-branch-list">
          {branches.map((b, i) => {
            const removable = branches.length > 2 && branchChain(spec, step.id, b) !== null;
            return (
              <li key={b} className="wf-row">
                <StepLink id={b} spec={spec} palette={palette} onSelect={onSelect} testId={`wf-branch-${i}`} />
                {removable ? (
                  <button type="button" className="wf-icon-btn" aria-label={`Remove branch ${i + 1}`}
                          data-testid={`wf-branch-remove-${i}`} onClick={() => onRemoveBranch(b)}>
                    <X size={14} weight="bold" aria-hidden />
                  </button>
                ) : null}
              </li>
            );
          })}
        </ul>
        <FieldError problems={problems} field="branches" />
        <button type="button" className="wf-link wf-link-icon" data-testid="wf-branch-add" onClick={onAddBranch}>
          <Plus size={12} weight="bold" aria-hidden /> Add a branch
        </button>
      </div>
      <div className="wf-field">
        <span className="wf-label">Then</span>
        <StepLink id={join} spec={spec} palette={palette} onSelect={onSelect} testId="wf-branch-join" />
        <FieldError problems={problems} field="join" />
      </div>
    </>
  );
}

export function JoinSettings(p: StructureProps) {
  const { step, spec, palette, problems, onConfig, onSelect } = p;
  const policy = step.success_policy === "any" ? "any" : "all";
  return (
    <>
      <div className="wf-field">
        <span className="wf-label" id={`wf-join-${step.id}`}>Go on when</span>
        <span className="wf-seg" role="group" aria-labelledby={`wf-join-${step.id}`}>
          <button type="button" aria-pressed={policy === "all"} data-testid="wf-join-all"
                  onClick={() => onConfig({ success_policy: "all" })}>Every branch is done</button>
          <button type="button" aria-pressed={policy === "any"} data-testid="wf-join-any"
                  onClick={() => onConfig({ success_policy: "any" })}>The first is done</button>
        </span>
        <FieldError problems={problems} field="success_policy" />
      </div>
      <div className="wf-field">
        <span className="wf-label">Waits for</span>
        <div className="wf-chips">
          {strings(step.wait_for).map((w) => (
            <StepLink key={w} id={w} spec={spec} palette={palette} onSelect={onSelect} />
          ))}
        </div>
        <FieldError problems={problems} field="wait_for" />
      </div>
    </>
  );
}

export function LoopSettings(p: StructureProps) {
  const { step, spec, palette, problems, onConfig, onSelect } = p;
  const sources = useMemo(() => expressionSources(spec, step.id, palette), [spec, step.id, palette]);
  const parsed = fromExpression(step.expression);
  const filled = !!parsed && parsed.rules.length > 0;
  // An empty loop reads as Until: "repeat until it passes" is what people mean. Once it checks
  // something, the stored expression says which (Until is the engine's `not`).
  const [chosen, setChosen] = useState(true);
  const until = filled ? parsed!.negate : chosen;
  const inner = filled && parsed!.negate ? toExpression({ ...parsed!, negate: false }) : step.expression;
  const store = (expression: Record<string, unknown>, asUntil: boolean) => {
    const c = fromExpression(expression);
    onConfig({ expression: asUntil && c && c.rules.length && !c.negate ? { not: expression } : expression });
  };
  const flip = (next: boolean) => {
    setChosen(next);
    if (filled) store(toExpression({ ...parsed!, negate: false }), next);
  };
  const max = Number(step.max_iterations ?? 3);
  return (
    <>
      <div className="wf-field">
        <span className="wf-label">Repeat</span>
        <StepLink id={String(step.body ?? "")} spec={spec} palette={palette} onSelect={onSelect}
                  testId="wf-loop-body" />
        <FieldError problems={problems} field="body" />
      </div>
      <span className="wf-seg" role="group" aria-label="Repeat until or while">
        <button type="button" aria-pressed={until} data-testid="wf-loop-until"
                disabled={parsed === null} onClick={() => flip(true)}>Until</button>
        <button type="button" aria-pressed={!until} data-testid="wf-loop-while"
                disabled={parsed === null} onClick={() => flip(false)}>While</button>
      </span>
      <ExpressionBuilder expression={inner} sources={sources} problems={problems}
                         lead={until ? "Stop repeating when" : "Keep repeating while"}
                         onChange={(expression) => store(expression, until)} />
      <label className="wf-field wf-field-inline">
        <span className="wf-label">At most</span>
        <input className="wf-input wf-num" type="number" min={1} max={100} value={max}
               data-testid="wf-loop-max" aria-describedby={`wf-loop-max-${step.id}`}
               onChange={(e) => {
                 const n = Math.round(Number(e.target.value));
                 if (Number.isFinite(n)) onConfig({ max_iterations: Math.min(100, Math.max(1, n)) });
               }} />
        <span className="wf-faint">times</span>
      </label>
      <span className="wf-faint" id={`wf-loop-max-${step.id}`}>
        Checked before each round. Still going after the last one: the run stops and asks you.
      </span>
      <FieldError problems={problems} field="max_iterations" />
    </>
  );
}

/* ── For each ───────────────────────────────────────────────────────────────────── */

/**
 * A For each: which list it goes through (`items`, picked from what the start form and earlier
 * steps make — the condition builder's sources — or typed as a path), the step it runs for each
 * item, at most how many items, and how many at a time. A list longer than the limit is refused
 * when the run gets there, never cut short.
 */
export function ForEachSettings(p: StructureProps) {
  const { step, spec, palette, problems, onConfig, onSelect } = p;
  const sources = useMemo(() => expressionSources(spec, step.id, palette), [spec, step.id, palette]);
  const items = typeof step.items === "string" ? step.items : "";
  const literal = Array.isArray(step.items);
  const known = sources.some((s) => s.path === items);
  const [custom, setCustom] = useState(!known && items !== "");
  const max = Number(step.max_items ?? DEFAULT_MAX_ITEMS);
  const atOnce = Number(step.concurrency ?? 1);
  const whole = (raw: string, low: number, high: number) => {
    const n = Math.round(Number(raw));
    return Number.isFinite(n) ? Math.min(high, Math.max(low, n)) : null;
  };
  return (
    <>
      <div className="wf-field">
        <span className="wf-label">For each item in</span>
        {literal ? (
          <span className="wf-faint" data-testid="wf-foreach-literal">
            {`A fixed list of ${(step.items as unknown[]).length} — edit it in JSON`}
          </span>
        ) : (
          <>
            <select className="wf-select" aria-label="The list to go through" data-testid="wf-foreach-items"
                    value={custom ? OTHER : items}
                    onChange={(e) => {
                      if (e.target.value === OTHER) { setCustom(true); return; }
                      setCustom(false);
                      onConfig({ items: e.target.value });
                    }}>
              <option value="" disabled>Choose a list…</option>
              {(["Start form", "Steps"] as const).map((group) => {
                const rows = sources.filter((s) => s.group === group);
                return rows.length ? (
                  <optgroup key={group} label={group}>
                    {rows.map((s) => <option key={s.path} value={s.path}>{s.label}</option>)}
                  </optgroup>
                ) : null;
              })}
              <option value={OTHER}>Other…</option>
            </select>
            {custom ? (
              <input className="wf-input wf-mono" aria-label="The list's path" value={items}
                     placeholder="steps.research.output.urls" data-testid="wf-foreach-items-path"
                     title={items ? pathLabel(items) : undefined}
                     onChange={(e) => onConfig({ items: e.target.value })} />
            ) : null}
          </>
        )}
        <FieldError problems={problems} field="items" />
      </div>
      <div className="wf-field">
        <span className="wf-label">Run</span>
        <StepLink id={String(step.body ?? "")} spec={spec} palette={palette} onSelect={onSelect}
                  testId="wf-foreach-body" />
        <span className="wf-faint">once per item. It reads the item as its input <code>item</code>.</span>
        <FieldError problems={problems} field="body" />
      </div>
      <label className="wf-field wf-field-inline">
        <span className="wf-label">At most</span>
        <input className="wf-input wf-num" type="number" min={1} max={MAX_ITEMS_LIMIT} value={max}
               data-testid="wf-foreach-max"
               onChange={(e) => {
                 const n = whole(e.target.value, 1, MAX_ITEMS_LIMIT);
                 if (n !== null) onConfig({ max_items: n });
               }} />
        <span className="wf-faint">items</span>
      </label>
      <FieldError problems={problems} field="max_items" />
      <label className="wf-field wf-field-inline">
        <span className="wf-label">At a time</span>
        <input className="wf-input wf-num" type="number" min={1} max={MAX_CONCURRENCY} value={atOnce}
               data-testid="wf-foreach-at-once"
               onChange={(e) => {
                 const n = whole(e.target.value, 1, MAX_CONCURRENCY);
                 if (n !== null) onConfig({ concurrency: n });
               }} />
      </label>
      <FieldError problems={problems} field="concurrency" />
      <span className="wf-faint">
        A longer list stops the run and says so; it is never cut short. If an item fails, the For
        each fails, and Retry runs the failed items again.
      </span>
    </>
  );
}
