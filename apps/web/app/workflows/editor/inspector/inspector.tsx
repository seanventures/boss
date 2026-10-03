"use client";

import React, { useCallback, useMemo, useState, type Dispatch } from "react";

import type { EditorAction, EditorState, Palette } from "../../../../lib/workflow-editor-types";
import { StepInspector } from "./step-inspector";
import { WorkflowSettings } from "./workflow-settings";

/**
 * The right-hand drawer: the selected step's properties, or the workflow's settings when nothing
 * is selected. Closes with × or Esc (an open popover takes its own Esc first).
 *
 * TWO PIECES OF STATE LIVE HERE, NOT IN THE STORE, because neither is part of the workflow:
 *  - grants made from this drawer, laid over the palette's holdings until the palette is next
 *    fetched, so `Lacks <skill>` disappears the moment Grant succeeds;
 *  - which inputs the editor wired (`auto` chips), per step.
 */
export function Inspector({ state, palette, dispatch, projectId, onClose, actor, usedFor, onSchedule }: {
  state: EditorState;
  palette: Palette;
  dispatch: Dispatch<EditorAction>;
  projectId: string;
  onClose(): void;
  /** The signed-in member, for Grant and Used for. Grant is disabled without one. */
  actor?: string;
  /** The work classes this workflow is mapped to, when the host knows them. */
  usedFor?: string[];
  /** Opens the host's schedule popover for this workflow. The row is hidden without it. */
  onSchedule?(workflowKey: string): void;
}) {
  const [grants, setGrants] = useState<Record<string, string[]>>({});
  const [autoWired, setAutoWired] = useState<Record<string, string[]>>({});

  const effective = useMemo<Palette>(() => (Object.keys(grants).length ? {
    ...palette,
    agents: palette.agents.map((a) => (grants[a.role_key]
      ? { ...a, skills: [...new Set([...a.skills, ...grants[a.role_key]])] } : a)),
  } : palette), [palette, grants]);

  const onGranted = useCallback((roleKey: string, skillId: string) =>
    setGrants((g) => ({ ...g, [roleKey]: [...(g[roleKey] ?? []), skillId] })), []);
  const onAutoWired = useCallback((stepId: string, names: string[] | ((was: string[]) => string[])) =>
    setAutoWired((w) => ({ ...w, [stepId]: typeof names === "function" ? names(w[stepId] ?? []) : names })), []);

  const step = state.selection ? state.spec.steps.find((s) => s.id === state.selection) : undefined;
  const readOnly = state.meta.readOnlyVersion != null;

  return (
    <aside className="wf-inspector" aria-label={step ? "Step" : "Workflow settings"}
           data-testid="wf-inspector"
           onKeyDown={(e) => {
             // A popover's Esc never arrives here (it stops it at the document), and an inline
             // editor that cancels on Esc stops its own; anything else closes the drawer.
             if (e.key !== "Escape" || e.defaultPrevented) return;
             e.preventDefault();
             onClose();
           }}>
      <fieldset className="wf-insp-body" disabled={readOnly}>
        {step ? (
          <StepInspector key={step.id} state={state} step={step} palette={effective} dispatch={dispatch}
                         projectId={projectId} actor={actor} autoWired={autoWired[step.id] ?? []}
                         onAutoWired={onAutoWired} onGranted={onGranted} onClose={onClose} />
        ) : (
          <WorkflowSettings state={state} dispatch={dispatch} projectId={projectId} actor={actor}
                            usedFor={usedFor} onSchedule={onSchedule} onClose={onClose} />
        )}
      </fieldset>
    </aside>
  );
}
