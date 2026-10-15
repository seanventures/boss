"use client";

import React, { type Dispatch } from "react";

import type { EditorAction, EditorState, FlowLink, Palette } from "../../../../lib/workflow-editor-types";
import { jsonGate } from "../json/json-view";
import { LockProvider } from "./editable";
import { StepInspector } from "./step-inspector";
import { WorkflowSettings } from "./workflow-settings";

/** Why nothing in the drawer can be edited right now, or null when it can. */
export function lockReason(state: EditorState): string | null {
  if (state.meta.readOnlyVersion != null) return `Viewing v${state.meta.readOnlyVersion}`;
  const gate = jsonGate(state);
  return gate.blocked ? gate.tip : null;
}

/**
 * The right-hand drawer: the selected step's properties, or the workflow's settings when nothing
 * is selected. Closes with × or Esc (an open popover takes its own Esc first).
 *
 * IT KEEPS NO WORKFLOW STATE OF ITS OWN. Which inputs the editor wired is the store's `wired`;
 * what each agent holds is the host's palette, refetched after a Grant — so the node, the agent
 * picker and the Grant row read one source and cannot disagree.
 */
export function Inspector({ state, palette, dispatch, projectId, onClose, refreshPalette, actor, usedFor,
                            onSchedule, onRewire }: {
  state: EditorState;
  palette: Palette;
  dispatch: Dispatch<EditorAction>;
  projectId: string;
  onClose(): void;
  /** Refetch the palette (useWorkflowEditor's `refreshPalette`): after a Grant, what the agent
   *  holds changed everywhere the editor draws it. */
  refreshPalette(): Promise<void>;
  /** The signed-in member, for Grant and Used for. Grant is disabled without one. */
  actor?: string;
  /** The work classes this workflow is mapped to, when the host knows them. */
  usedFor?: string[];
  /** Opens the host's schedule popover for this workflow. The row is hidden without it. */
  onSchedule?(workflowKey: string): void;
  /** An arrow pointed elsewhere through the editor's check (a step's error path, from its settings). */
  onRewire?(link: FlowLink, target: string | null): void;
}) {
  const step = state.selection ? state.spec.steps.find((s) => s.id === state.selection) : undefined;
  const locked = lockReason(state);

  return (
    <aside className="wf-inspector" aria-label={step ? "Step" : "Workflow settings"}
           data-testid="wf-inspector" data-locked={locked !== null || undefined}
           onKeyDown={(e) => {
             // A popover's Esc never arrives here (it stops it at the document), and an inline
             // editor that cancels on Esc stops its own; anything else closes the drawer.
             if (e.key !== "Escape" || e.defaultPrevented) return;
             e.preventDefault();
             onClose();
           }}>
      <LockProvider value={locked}>
        <div className="wf-insp-body">
          {step ? (
            <StepInspector key={step.id} state={state} step={step} palette={palette} dispatch={dispatch}
                           projectId={projectId} actor={actor} refreshPalette={refreshPalette}
                           onClose={onClose} onRewire={onRewire} />
          ) : (
            <WorkflowSettings state={state} dispatch={dispatch} projectId={projectId} actor={actor}
                              usedFor={usedFor} onSchedule={onSchedule} onClose={onClose} />
          )}
        </div>
      </LockProvider>
    </aside>
  );
}
