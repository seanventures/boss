"use client";

/**
 * "Starts with": the start-form fields, as chips above the lane. A chip edits its title and
 * whether it is required, in place.
 *
 * input_schema exists in two shapes (see lib/workflow-inputs.ts). An edit keeps the loaded
 * shape whenever it can say the change; only a title or a required flag on the name-to-type
 * shape needs JSON Schema, and then the conversion is lossless (each type becomes `type`).
 */
import React, { useRef, useState } from "react";

import type { EditorAction } from "../../../lib/workflow-editor-types";
import { workflowInputFields } from "../../../lib/workflow-inputs";
import { Popover } from "./inspector/popover";

type Obj = Record<string, unknown>;
const asObj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});

function isJsonSchema(schema: Obj): boolean {
  return "properties" in schema || "required" in schema;
}

/** The schema with one field's title and required flag changed, in the loaded shape when possible. */
export function withField(schema: Obj | undefined, name: string,
                          patch: { title?: string; required?: boolean }): Obj {
  const raw = asObj(schema);
  const titled = patch.title !== undefined && patch.title.trim() !== "";
  if (!isJsonSchema(raw)) {
    if (!titled && !patch.required) return raw;
    const properties = Object.fromEntries(Object.keys(raw).map((k) => [k, { type: String(raw[k] || "string") }]));
    return withField({ properties, required: [] }, name, patch);
  }
  const props = { ...asObj(raw.properties) };
  const prop = { ...asObj(props[name]) };
  if (patch.title !== undefined) {
    if (titled) prop.title = patch.title.trim(); else delete prop.title;
  }
  props[name] = prop;
  const required = (Array.isArray(raw.required) ? raw.required.map(String) : []).filter((r) => r !== name);
  const keep = patch.required ?? (Array.isArray(raw.required) && raw.required.map(String).includes(name));
  return { ...raw, properties: props, required: keep ? [...required, name] : required };
}

export function StartsWith({ schema, dispatch, readOnly }: {
  schema: Obj | undefined;
  dispatch(a: EditorAction): void;
  readOnly?: boolean;
}) {
  const fields = workflowInputFields(schema);
  const [open, setOpen] = useState<string | null>(null);
  const anchors = useRef(new Map<string, HTMLButtonElement>());
  const anchor = useRef<HTMLElement | null>(null);
  anchor.current = open ? anchors.current.get(open) ?? null : null;
  if (!fields.length) return null;
  const editing = fields.find((f) => f.name === open);

  return (
    <div className="wf-starts" role="group" aria-label="Starts with" data-testid="wf-starts-with">
      <span className="wf-starts-label">Starts with</span>
      {fields.map((f) => (
        <button key={f.name} type="button" className="wf-chip wf-chip-btn" disabled={readOnly}
                ref={(el) => { if (el) anchors.current.set(f.name, el); else anchors.current.delete(f.name); }}
                data-tip={f.required ? `${f.name}, required` : f.name} aria-haspopup="dialog"
                onClick={() => setOpen(f.name)} data-testid={`wf-start-field-${f.name}`}>
          {f.label}{f.required ? <span className="wf-required-dot" aria-label="required" /> : null}
        </button>
      ))}
      <Popover anchor={anchor} open={!!editing}
               onClose={() => setOpen(null)} label="Start field" width={260}>
        {editing ? (
          <div className="wf-pop-form">
            <label className="wf-field">
              <span className="wf-label">Title</span>
              <input className="wf-input" defaultValue={editing.label} autoFocus
                     onBlur={(e) => {
                       if (e.target.value.trim() && e.target.value.trim() !== editing.label) {
                         dispatch({ type: "SET_INPUT_SCHEMA", schema: withField(schema, editing.name, { title: e.target.value }) });
                       }
                     }}
                     onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
            </label>
            <label className="wf-check">
              <input type="checkbox" checked={editing.required}
                     onChange={(e) => dispatch({ type: "SET_INPUT_SCHEMA",
                                                 schema: withField(schema, editing.name, { required: e.target.checked }) })} />
              <span>Required</span>
            </label>
          </div>
        ) : null}
      </Popover>
    </div>
  );
}
