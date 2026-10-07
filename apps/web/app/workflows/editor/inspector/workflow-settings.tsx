"use client";

import { useGate } from "../../../../components/gated";
import React, { useState, type Dispatch } from "react";
import { GearSix, X } from "@phosphor-icons/react";

import { TOAST_EVENT, apiWrite } from "../../../../lib/api-write";
import { NameInUseError, isDirty, renameWorkflow } from "../../../../lib/workflow-editor-types";
import type { EditorAction, EditorState } from "../../../../lib/workflow-editor-types";
import { Editable } from "./editable";
import { Section } from "./step-inspector";
import {
  STEP_ID, formCanRequire, formFields, humanise, withFormFields, type FieldType, type FormField,
} from "./step-facts";

/** The work classes a workflow can be used for, in the operator's words (workflow_catalogue). */
export const WORK_CLASSES: { key: string; label: string }[] = [
  { key: "project.new", label: "New projects" },
  { key: "feature.major", label: "Major features" },
  { key: "feature.minor", label: "Small features" },
  { key: "bugfix", label: "Bug fixes" },
  { key: "any", label: "Any other work" },
];

const TYPES: { key: FieldType; label: string }[] = [
  { key: "text", label: "Text" }, { key: "long", label: "Long text" },
  { key: "number", label: "Number" }, { key: "yesno", label: "Yes / no" },
];

function toast(type: "success" | "error", message: string) {
  try { window.dispatchEvent(new CustomEvent(TOAST_EVENT, { detail: { type, message } })); } catch { /* */ }
}

/**
 * The workflow's own settings: Name, Description, Start form, Used for, Schedule, Based on.
 *
 * NAME AND DESCRIPTION DO NOT MAKE A VERSION. On a published workflow with nothing else changed
 * they are PATCHed when the field is left, and the draft's baseline moves with them, so Save
 * stays disabled. With other unsaved edits they ride along with the next Save instead — a rename
 * must never mark unsaved step edits as saved.
 */
export function WorkflowSettings({ state, dispatch, projectId, actor, usedFor, onSchedule, onClose }: {
  state: EditorState;
  dispatch: Dispatch<EditorAction>;
  projectId: string;
  actor?: string;
  usedFor?: string[];
  onSchedule?(workflowKey: string): void;
  onClose(): void;
}) {
  const { meta, loadedMeta } = state;
  const standard = meta.is_catalogue;
  const published = Boolean(meta.definition_id) && !standard;
  const [nameError, setNameError] = useState("");
  const [busy, setBusy] = useState(false);

  const commitMeta = async () => {
    // Unsaved edits other than these two fields: the rename rides along with the next Save.
    if (!published || !meta.definition_id || isDirty({ ...state, meta: state.loadedMeta })) return;
    const patch: { name?: string; description?: string } = {};
    if (meta.name.trim() !== loadedMeta.name) patch.name = meta.name.trim();
    if (meta.description !== loadedMeta.description) patch.description = meta.description;
    if (!Object.keys(patch).length) return;
    if (patch.name === "") { setNameError("Name required"); return; }
    setBusy(true);
    try {
      await renameWorkflow(projectId, meta.definition_id, patch);
      setNameError("");
      // Only the names moved: the spec baseline stays, and a name typed while this was in
      // flight is kept as the next change.
      dispatch({ type: "SAVED", spec: state.loaded ?? state.spec, meta: { ...meta, name: meta.name.trim() }, sent: meta });
    } catch (e) {
      if (e instanceof NameInUseError) setNameError("Name in use");
      else toast("error", String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="wf-insp-settings" data-testid="wf-workflow-settings">
      <header className="wf-insp-head">
        <span className="wf-insp-icon"><GearSix size={20} weight="bold" /></span>
        <div className="wf-insp-names"><span className="wf-insp-heading">Settings</span></div>
        <button type="button" className="wf-icon-btn" aria-label="Close" title="Close (Esc)" onClick={onClose}>
          <X size={16} weight="bold" />
        </button>
      </header>

      <label className="wf-field" title={standard ? "Shipped with BOSS. Saving makes your own copy." : undefined}>
        <span className="wf-label">Name</span>
        <Editable>
          <input className="wf-input" value={meta.name} readOnly={standard} maxLength={120}
                 aria-invalid={nameError ? true : undefined} data-testid="wf-settings-name"
                 onChange={(e) => { setNameError(""); dispatch({ type: "SET_META", patch: { name: e.target.value } }); }}
                 onBlur={() => void commitMeta()} />
        </Editable>
        {nameError ? <span className="wf-field-error" role="alert">{nameError}</span> : null}
      </label>
      <label className="wf-field">
        <span className="wf-label">Description</span>
        <Editable>
          <textarea className="wf-text" rows={2} value={meta.description} readOnly={standard}
                    data-testid="wf-settings-description" disabled={busy}
                    onChange={(e) => dispatch({ type: "SET_META", patch: { description: e.target.value } })}
                    onBlur={() => void commitMeta()} />
        </Editable>
      </label>

      <StartForm state={state} dispatch={dispatch} />

      {meta.workflow_key && meta.definition_id ? (
        <UsedFor projectId={projectId} actor={actor} workflowKey={meta.workflow_key} usedFor={usedFor} />
      ) : null}

      {onSchedule && meta.workflow_key && meta.definition_id && !standard ? (
        <Section label="Schedule">
          <button type="button" className="wf-btn" onClick={() => onSchedule(meta.workflow_key!)}>Schedule…</button>
        </Section>
      ) : null}

      {meta.from_workflow_key ? (
        <Section label="Based on">
          <a className="wf-link" data-testid="wf-based-on"
             href={`/?area=delivery_workflows&wf=${encodeURIComponent(meta.from_workflow_key)}`}>
            {humanise(meta.from_workflow_key)}
          </a>
        </Section>
      ) : null}
    </div>
  );
}

function StartForm({ state, dispatch }: { state: EditorState; dispatch: Dispatch<EditorAction> }) {
  const fields = formFields(state.spec);
  const canRequire = formCanRequire(state.spec);
  const [adding, setAdding] = useState<string | null>(null);
  const [addError, setAddError] = useState("");
  const write = (next: FormField[]) =>
    dispatch({ type: "SET_INPUT_SCHEMA", schema: withFormFields(state.spec, next) });
  const change = (name: string, patch: Partial<FormField>) =>
    write(fields.map((f) => (f.name === name ? { ...f, ...patch } : f)));

  const commitAdd = () => {
    const name = (adding ?? "").trim();
    if (!name) { setAdding(null); return; }
    if (!STEP_ID.test(name)) { setAddError("Letters, numbers, . _ - only"); return; }
    if (fields.some((f) => f.name === name)) { setAddError("Already a field"); return; }
    write([...fields, { name, title: "", type: "text", required: false }]);
    setAdding(null);
    setAddError("");
  };

  return (
    <Section label="Start form" tip="Asked for when someone starts a run" testId="wf-start-form">
      {fields.length === 0 && adding === null ? <span className="wf-faint">No fields</span> : null}
      {fields.map((f) => (
        <div key={f.name} className="wf-form-row" data-testid={`wf-form-${f.name}`}>
          <span className="wf-mono wf-form-name" title={f.title || f.name}>{f.name}</span>
          <select className="wf-select wf-select-sm" value={f.type} aria-label={`Type of ${f.name}`}
                  onChange={(e) => change(f.name, { type: e.target.value as FieldType })}>
            {TYPES.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}
          </select>
          <label className="wf-check" title={canRequire ? undefined : "This form's shape can't mark fields required"}>
            <input type="checkbox" checked={f.required} disabled={!canRequire}
                   data-testid={`wf-form-required-${f.name}`}
                   onChange={(e) => change(f.name, { required: e.target.checked })} />
            <span>Required</span>
          </label>
          <button type="button" className="wf-icon-btn" aria-label={`Remove ${f.name}`}
                  onClick={() => write(fields.filter((x) => x.name !== f.name))}><X size={12} weight="bold" /></button>
        </div>
      ))}
      {adding !== null ? (
        <div className="wf-form-row">
          <input className="wf-input wf-mono" autoFocus value={adding} placeholder="field_name"
                 aria-label="New field name" aria-invalid={addError ? true : undefined}
                 data-testid="wf-form-new"
                 onChange={(e) => { setAdding(e.target.value); setAddError(""); }}
                 onBlur={commitAdd}
                 onKeyDown={(e) => {
                   if (e.key === "Enter") { e.preventDefault(); commitAdd(); }
                   if (e.key === "Escape") { e.stopPropagation(); setAdding(null); setAddError(""); }
                 }} />
          {addError ? <span className="wf-field-error" role="alert">{addError}</span> : null}
        </div>
      ) : (
        <button type="button" className="wf-link" data-testid="wf-form-add" onClick={() => setAdding("")}>+ Field</button>
      )}
    </Section>
  );
}

/** Point a class of work at this workflow (`/v1/delivery-workflows/apply`), toasting the outcome. */
export async function applyWorkClass({ projectId, actor, workflowKey, workClass }: {
  projectId: string; actor?: string; workflowKey: string; workClass: string;
}): Promise<boolean> {
  try {
    const r = await apiWrite("/api/delivery-workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "apply", project_id: projectId, workflow_key: workflowKey,
                             work_class: workClass, ...(actor ? { actor } : {}) }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) { toast("error", String(body?.error ?? `HTTP ${r.status}`)); return false; }
    toast("success", `Used for ${workClassLabel(workClass).toLowerCase()}`);
    return true;
  } catch (e) {
    toast("error", String((e as Error)?.message ?? e));
    return false;
  }
}

export const workClassLabel = (key: string) => WORK_CLASSES.find((c) => c.key === key)?.label ?? key;

function UsedFor({ projectId, actor, workflowKey, usedFor }: {
  projectId: string; actor?: string; workflowKey: string; usedFor?: string[];
}) {
  const [value, setValue] = useState(usedFor?.[0] ?? "");
  const [busy, setBusy] = useState(false);
  const applyGate = useGate("workflow:publish");
  const apply = async (workClass: string) => {
    setValue(workClass);
    if (!workClass) return;
    setBusy(true);
    const ok = await applyWorkClass({ projectId, actor, workflowKey, workClass });
    setBusy(false);
    if (!ok) setValue(usedFor?.[0] ?? "");
  };
  return (
    <Section label="Used for" tip="Which new work starts this workflow">
      <select {...applyGate.props} className="wf-select" value={value} disabled={(busy) || !applyGate.allowed} data-testid="wf-used-for"
              onChange={(e) => void apply(e.target.value)}>
        <option value="">Not used by default</option>
        {WORK_CLASSES.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
      </select>
    </Section>
  );
}

