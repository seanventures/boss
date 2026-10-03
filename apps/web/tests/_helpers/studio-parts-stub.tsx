/**
 * Stand-ins for the Workflow Studio parts the Workflows area mounts (app/workflows/studio-parts.ts),
 * for tests about the AREA — its runs, buttons, tabs and Library — rather than about the lane or
 * the editor, which have their own tests.
 *
 *   vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));
 *
 * The lane renders one button per step (data-step-id, data-phase, the meta line), so a test can
 * read what a run's lane was told and click a node. The editor renders the key it was opened on.
 * The API client is vi.fn()s a test points at fixtures with `stub.fetchLibrary.mockResolvedValue`.
 */
import React, { useEffect, useState } from "react";
import { vi } from "vitest";

import { stepView } from "../../lib/step-view";

import type {
  Palette, PaletteItem, Problem, Spec, WorkflowSummary,
} from "../../lib/workflow-editor-types";

export { NameInUseError, ProblemsError } from "../../lib/workflow-editor-types";
export type { CustomStepDef, Spec, WorkflowSummary } from "../../lib/workflow-editor-types";

// The run lane's pure helpers are the real ones: what a run draws is the rule under test.
export { runLaneSpec, runPalette } from "../../app/workflows/flow/run-lane";

export function FlowLane(p: { spec: Spec; palette?: Palette | null; mode: string; selection?: string | null;
  problems?: Problem[]; runState?: Record<string, { phase: string; meta: string }>;
  onSelect?(id: string | null): void; onInsert?(at: number, item: PaletteItem): void }) {
  return (
    <div data-testid="flow-lane" data-mode={p.mode}>
      {p.spec.steps.map((step) => (
        <button key={step.id} type="button" data-step-id={step.id}
                data-selected={p.selection === step.id || undefined}
                data-phase={p.runState?.[step.id]?.phase}
                onClick={() => p.onSelect?.(step.id)}>
          {p.palette ? stepView(step, p.palette).title : String(step.title ?? step.id)}
          <small>{p.runState?.[step.id]?.meta}</small>
        </button>
      ))}
    </div>
  );
}

export function MiniLane({ steps }: { steps: WorkflowSummary["steps"] }) {
  return <span data-testid="mini-lane">{steps.map((s) => s.title).join(" › ")}</span>;
}

export function WorkflowEditor(p: { projectId: string; workflowKey: string; onBack(): void;
  onStart(key: string): void }) {
  return (
    <div data-testid="workflow-editor" data-key={p.workflowKey}>
      <button type="button" data-testid="editor-back" onClick={p.onBack}>Library</button>
      <button type="button" data-testid="editor-start" onClick={() => p.onStart(p.workflowKey)}>Start</button>
    </div>
  );
}

export function DefineStepModal(p: { open: boolean; edit?: { step_key: string };
  onClose(created?: unknown): void }) {
  return p.open ? (
    <div role="dialog" data-testid="define-step-modal" data-edit={p.edit?.step_key ?? ""}>
      <button type="button" onClick={() => p.onClose({ step_key: "made" })}>Define</button>
    </div>
  ) : null;
}

export const fetchLibrary = vi.fn(async (_projectId: string): Promise<WorkflowSummary[]> => []);
export const deleteWorkflow = vi.fn(async (_projectId: string, _definitionId: string) =>
  ({ outcome: "deleted" as const }));
export const renameWorkflow = vi.fn(async (_projectId: string, _definitionId: string,
                                           _patch: { name?: string }) => undefined);
export const fetchSpec = vi.fn(async (_projectId: string, _key: string, _version?: number):
    Promise<{ spec: Spec; meta: never; versions: never[] }> => {
  throw new Error("no published spec in this test");
});

/** The definition a run ran, read through the stand-in `fetchSpec` (no palette: the area's
 *  own capabilities and roster name the steps). */
export function useRunDefinition(projectId: string, key: string | null | undefined,
                                 version: number | null | undefined): { spec: Spec | null; palette: Palette | null } {
  const [spec, setSpec] = useState<Spec | null>(null);
  useEffect(() => {
    if (!key || !version) return;
    let live = true;
    setSpec(null);
    fetchSpec(projectId, key, version).then((got) => { if (live) setSpec(got.spec); }, () => {});
    return () => { live = false; };
  }, [projectId, key, version]);
  return { spec, palette: null };
}
