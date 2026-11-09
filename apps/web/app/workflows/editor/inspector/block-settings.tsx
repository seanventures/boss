"use client";

import React, { useEffect, useState } from "react";

import { fetchLibrary } from "../../../../lib/workflow-editor-types";
import type {
  BlockDef, Palette, Problem, SpecStep, StepView, WorkflowSummary,
} from "../../../../lib/workflow-editor-types";
import { asObj, bindingOf } from "./step-facts";
import {
  ConditionSettings, ForEachSettings, JoinSettings, LoopSettings, ParallelSettings, type StructureProps,
} from "./structure-settings";

const libraryCache = new Map<string, Promise<WorkflowSummary[]>>();

/** The project's workflows, fetched once per project while the editor is open. A "Run another
 *  workflow" step is pinned to a VERSION (`active_version_id`), not to a key. */
export function useLibrary(projectId: string, wanted: boolean): WorkflowSummary[] | null {
  const [rows, setRows] = useState<WorkflowSummary[] | null>(null);
  useEffect(() => {
    if (!wanted) return;
    let live = true;
    let pending = libraryCache.get(projectId);
    if (!pending) {
      pending = fetchLibrary(projectId);
      pending.catch(() => libraryCache.delete(projectId));
      libraryCache.set(projectId, pending);
    }
    pending.then((r) => { if (live) setRows(r); }, () => { if (live) setRows([]); });
    return () => { live = false; };
  }, [projectId, wanted]);
  return rows;
}

const UNITS = [
  { key: "min", seconds: 60 },
  { key: "h", seconds: 3600 },
  { key: "d", seconds: 86400 },
] as const;

function splitDuration(seconds: number): { amount: number; unit: (typeof UNITS)[number]["key"] } {
  if (seconds > 0 && seconds % 86400 === 0) return { amount: seconds / 86400, unit: "d" };
  if (seconds > 0 && seconds % 3600 === 0) return { amount: seconds / 3600, unit: "h" };
  return { amount: Math.round((seconds / 60) * 100) / 100, unit: "min" };
}

export function FieldError({ problems, field }: { problems: Problem[]; field: string }) {
  const hit = problems.find((p) => p.field === field);
  if (!hit) return null;
  return (
    <span className="wf-field-error" role="alert" title={hit.message}>
      {hit.message}
      {/* NO DEAD END: the gap names where it is closed (a deploy step with no target opens Infra
          with the App target sheet). A new tab, so the unsaved workflow is not lost. */}
      {hit.fix_href ? (
        <>, <a href={hit.fix_href} target="_blank" rel="noreferrer" data-testid="wf-problem-fix">{hit.fix_label ?? "Set it up"}</a></>
      ) : null}
    </span>
  );
}

/** The step fields BlockSettings draws a control for, and so where a problem on one shows. */
export function blockFieldKeys(view: StepView, block: BlockDef | undefined): string[] {
  if (view.type === "condition") return ["expression", "on_true", "on_false"];
  if (view.type === "parallel") return ["branches", "join"];
  if (view.type === "join") return ["wait_for", "success_policy"];
  if (view.type === "loop") return ["body", "expression", "max_iterations"];
  if (view.type === "foreach") return ["items", "body", "max_items", "concurrency"];
  return block?.configure.length ? block.configure.map((c) => c.key)
    : view.type === "gate" ? ["question", "guidance", "approver_role"]
    : view.type === "wait" ? ["wait_seconds"]
    : view.type === "subworkflow" ? ["workflow_version_id"] : [];
}

/**
 * The settings of a Flow block, Approval gate, Wait, Run another workflow, rendered from the
 * block's own `configure` list, so a field the server requires is a field the inspector offers;
 * the labels are the copy guide's, not the server's. A branch block (If / else, Parallel, Join,
 * Repeat) draws its own settings (structure-settings.tsx).
 */
export function BlockSettings({ step, view, block, palette, projectId, workflowKey, problems, onConfig,
                                structure }: {
  step: SpecStep;
  view: StepView;
  block: BlockDef | undefined;
  palette: Palette;
  projectId: string;
  /** The workflow being edited, which cannot call itself. */
  workflowKey: string | null;
  problems: Problem[];
  onConfig(config: Record<string, unknown>): void;
  /** The workflow and the moves a branch block's settings make. */
  structure?: Omit<StructureProps, "step" | "palette" | "problems" | "onConfig">;
}) {
  const library = useLibrary(projectId, view.type === "subworkflow");

  if (structure && ["condition", "parallel", "join", "loop", "foreach"].includes(view.type)) {
    const props: StructureProps = { ...structure, step, palette, problems, onConfig };
    if (view.type === "condition") return <ConditionSettings {...props} />;
    if (view.type === "parallel") return <ParallelSettings {...props} />;
    if (view.type === "join") return <JoinSettings {...props} />;
    if (view.type === "foreach") return <ForEachSettings {...props} />;
    return <LoopSettings {...props} />;
  }

  const fields = blockFieldKeys(view, block);
  const conf = (key: string) => block?.configure.find((c) => c.key === key);

  return (
    <>
      {fields.map((key) => {
        if (key === "question") {
          const value = String(step.question ?? "");
          const max = conf(key)?.max ?? 400;
          const empty = !value.trim();
          return (
            <label key={key} className="wf-field">
              <span className="wf-label">Question <span className="wf-req" aria-hidden>*</span></span>
              <textarea className="wf-text" rows={2} maxLength={max} value={value} required
                        aria-invalid={empty || problems.some((p) => p.field === key) || undefined}
                        placeholder="What is being approved?" data-testid="wf-gate-question"
                        onChange={(e) => onConfig({ question: e.target.value })} />
              {empty && !problems.some((p) => p.field === key)
                ? <span className="wf-field-error" role="alert">Required</span> : null}
              <FieldError problems={problems} field={key} />
            </label>
          );
        }
        if (key === "guidance") {
          return (
            <label key={key} className="wf-field">
              <span className="wf-label">Guidance</span>
              <textarea className="wf-text" rows={2} value={String(step.guidance ?? "")}
                        placeholder="What to check" maxLength={conf(key)?.max ?? 1000}
                        onChange={(e) => onConfig({ guidance: e.target.value })} />
              <FieldError problems={problems} field={key} />
            </label>
          );
        }
        if (key === "approver_role") {
          return (
            <label key={key} className="wf-field">
              <span className="wf-label">Approver</span>
              <ApproverSelect value={String(step.approver_role ?? asObj(step.approval).approver_role ?? "")}
                              palette={palette} onChange={(v) => onConfig({ approver_role: v })} />
              <FieldError problems={problems} field={key} />
            </label>
          );
        }
        if (key === "wait_seconds") {
          return <WaitField key={key} seconds={Number(step.wait_seconds ?? 3600)} problems={problems}
                            onChange={(s) => onConfig({ wait_seconds: s })} />;
        }
        if (key === "workflow_version_id") {
          const pinned = String(bindingOf(step).workflow_version_id ?? step.workflow_version_id ?? "");
          const rows = (library ?? []).filter((w) => w.workflow_key !== workflowKey);
          const chosen = rows.find((w) => w.active_version_id === pinned);
          return (
            <label key={key} className="wf-field">
              <span className="wf-label">Workflow</span>
              <span className="wf-row">
                <select className="wf-select" value={chosen?.active_version_id ?? (pinned ? "__pinned" : "")}
                        disabled={library === null} data-testid="wf-subworkflow"
                        onChange={(e) => onConfig({ workflow_version_id: e.target.value || null })}>
                  <option value="">{library === null ? "Loading…" : "Choose a workflow"}</option>
                  {pinned && !chosen ? <option value="__pinned" disabled>Pinned version</option> : null}
                  {rows.map((w) => (
                    <option key={w.definition_id} value={w.active_version_id ?? ""}
                            disabled={!w.active_version_id}
                            title={w.active_version_id ? undefined : "Not published yet"}>
                      {w.name}{w.is_catalogue ? ", Standard" : ""}
                    </option>
                  ))}
                </select>
                {chosen?.version ? <span className="wf-chip" title="Runs this version">v{chosen.version}</span> : null}
              </span>
              <FieldError problems={problems} field={key} />
            </label>
          );
        }
        const c = conf(key);
        const raw = step[key];
        return (
          <label key={key} className="wf-field">
            <span className="wf-label">{c?.label ?? key}</span>
            <input className="wf-input" type={c?.type === "number" ? "number" : "text"}
                   value={raw === undefined || raw === null ? "" : String(raw)}
                   onChange={(e) => onConfig({ [key]: c?.type === "number"
                     ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value })} />
            <FieldError problems={problems} field={key} />
          </label>
        );
      })}
    </>
  );
}

/** `A person` first, a gate exists so that a person looks, then the project's agents. */
export function ApproverSelect({ value, palette, onChange, testId }: {
  value: string; palette: Palette; onChange(v: string): void; testId?: string;
}) {
  const human = palette.humanRole || "human";
  const known = value === "" || value === human || palette.agents.some((a) => a.role_key === value);
  return (
    <select className="wf-select" value={value || human} data-testid={testId}
            onChange={(e) => onChange(e.target.value)}>
      <option value={human}>A person</option>
      {palette.agents.filter((a) => a.role_key !== human).map((a) => (
        <option key={a.role_key} value={a.role_key}>{a.name || a.role_key}</option>
      ))}
      {!known ? <option value={value}>{value}</option> : null}
    </select>
  );
}

function WaitField({ seconds, problems, onChange }: {
  seconds: number; problems: Problem[]; onChange(seconds: number): void;
}) {
  const split = splitDuration(Number.isFinite(seconds) ? seconds : 3600);
  const [unit, setUnit] = useState(split.unit);
  // Follow the step when it changes underneath (undo, JSON), unless the unit still fits.
  useEffect(() => {
    const per = UNITS.find((u) => u.key === unit)!.seconds;
    if (seconds % per !== 0) setUnit(splitDuration(seconds).unit);
  }, [seconds, unit]);
  const per = UNITS.find((u) => u.key === unit)!.seconds;
  return (
    <div className="wf-field">
      <span className="wf-label">Wait</span>
      <span className="wf-row">
        <input className="wf-input wf-num" type="number" min={0} step="any"
               value={Math.round((seconds / per) * 100) / 100} aria-label="Wait for"
               data-testid="wf-wait-amount"
               onChange={(e) => onChange(Math.max(0, Math.round(Number(e.target.value || 0) * per)))} />
        <span className="wf-seg" role="group" aria-label="Unit">
          {UNITS.map((u) => (
            <button key={u.key} type="button" aria-pressed={u.key === unit}
                    onClick={() => {
                      // Keep the number, change what it means: "2 h" → "2 d".
                      const amount = Math.round((seconds / per) * 100) / 100;
                      setUnit(u.key);
                      onChange(Math.round(amount * u.seconds));
                    }}>{u.key}</button>
          ))}
        </span>
      </span>
      <FieldError problems={problems} field="wait_seconds" />
    </div>
  );
}
