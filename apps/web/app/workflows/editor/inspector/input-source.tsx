"use client";

import React from "react";

import type { InputSource } from "../../../../lib/workflow-editor-types";

export type Upstream = { id: string; title: string; fields: string[] };

const encode = (source: InputSource | null): string => {
  if (!source) return "unset";
  if ("from_input" in source) return "form";
  if ("from_step" in source) return `step:${source.from_step}:${source.field}`;
  return "fixed";
};

/**
 * Where one input's value comes from: `Start form`, `<Step> ▸ field`, `Fixed`, or `Not set`.
 *
 * A native select styled as a pill — one decision, keyboard and screen-reader complete — with
 * at most one small box beside it for the part a list cannot hold: the start-form field name,
 * the fixed value, or a field an earlier step returns but does not declare.
 */
export function InputSourceRow({ name, required, source, upstream, formNames, auto, unwired, onChange }: {
  name: string;
  required: boolean;
  source: InputSource | null;
  /** Earlier steps only, each with the fields it declares it makes. */
  upstream: Upstream[];
  /** The start form's field names, offered for `Start form`. */
  formNames: string[];
  /** The editor wired this one (Auto-wire, or on insert). */
  auto: boolean;
  /** Required and not connected to anything. */
  unwired: boolean;
  onChange(next: InputSource | null): void;
}) {
  const value = encode(source);
  const stepPick = source && "from_step" in source ? source : null;
  const known = stepPick
    ? upstream.find((u) => u.id === stepPick.from_step)?.fields.includes(stepPick.field)
    : true;

  const pick = (next: string) => {
    if (next === "unset") return onChange(null);
    if (next === "form") return onChange({ from_input: name });
    if (next === "fixed") return onChange({ value: "" });
    const [, stepId, ...field] = next.split(":");
    onChange({ from_step: stepId, field: field.join(":") });
  };

  const optionsFor = (u: Upstream) => (u.fields.length
    ? u.fields.map((f) => <option key={`${u.id}:${f}`} value={`step:${u.id}:${f}`}>{`${u.title} ▸ ${f}`}</option>)
    : [<option key={`${u.id}:`} value={`step:${u.id}:`}>{`${u.title} ▸ …`}</option>]);

  return (
    <div className="wf-input-row" data-testid={`wf-input-${name}`} data-unwired={unwired || undefined}>
      <span className="wf-input-name" title={required ? `${name} (required)` : name}>
        {required ? <span className="wf-dot" data-amber={unwired || undefined} aria-label="required" /> : null}
        <span className="wf-mono">{name}</span>
        {auto ? <span className="wf-chip wf-chip-auto" title="Wired by the editor">auto</span> : null}
      </span>
      <select className="wf-pill-select" value={value} aria-label={`Source of ${name}`}
              data-testid={`wf-input-source-${name}`}
              onChange={(e) => pick(e.target.value)}>
        <option value="form">Start form</option>
        {upstream.length ? (
          <optgroup label="Earlier steps">
            {upstream.flatMap(optionsFor)}
            {stepPick && !known ? (
              <option value={value}>{`${upstream.find((u) => u.id === stepPick.from_step)?.title
                ?? stepPick.from_step} ▸ ${stepPick.field || "…"}`}</option>
            ) : null}
          </optgroup>
        ) : stepPick ? <option value={value}>{`${stepPick.from_step} ▸ ${stepPick.field || "…"}`}</option> : null}
        <option value="fixed">Fixed</option>
        <option value="unset">Not set</option>
      </select>
      {source && "from_input" in source ? (
        <input className="wf-input-extra wf-mono" value={source.from_input} list={`wf-form-${name}`}
               aria-label={`Start form field for ${name}`} title="Start form field"
               onChange={(e) => onChange({ from_input: e.target.value })} />
      ) : null}
      {source && "value" in source ? (
        <input className="wf-input-extra" value={String(source.value ?? "")} placeholder="Value"
               aria-label={`Fixed value for ${name}`} data-testid={`wf-input-fixed-${name}`}
               onChange={(e) => onChange({ value: e.target.value })} />
      ) : null}
      {stepPick && (!stepPick.field || !known) ? (
        <input className="wf-input-extra wf-mono" value={stepPick.field} placeholder="field"
               aria-label={`Field supplying ${name}`}
               onChange={(e) => onChange({ from_step: stepPick.from_step, field: e.target.value })} />
      ) : null}
      {source && "from_input" in source ? (
        <datalist id={`wf-form-${name}`}>{formNames.map((f) => <option key={f} value={f} />)}</datalist>
      ) : null}
    </div>
  );
}
