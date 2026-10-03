"use client";

import { useEffect, useRef, type Dispatch } from "react";

import { checkSpec } from "../../../../lib/workflow-editor-types";
import type { EditorAction, EditorState, Spec } from "../../../../lib/workflow-editor-types";

export const AUTO_CHECK_MS = 600;

/**
 * Re-check the draft 600ms after the last edit, so problems appear on the nodes while the
 * operator works rather than only when they press Save.
 *
 * Writes nothing (`/check` is read-only on the server). Skipped while the JSON is broken, for an
 * empty workflow, and for a spec already checked. A reply that arrives after a newer edit is
 * dropped: only the answer for the spec on screen may set the problems.
 */
export function useAutoCheck(state: EditorState, dispatch: Dispatch<EditorAction>, projectId: string): void {
  const lastChecked = useRef<Spec | null>(null);
  const latest = useRef<Spec>(state.spec);
  latest.current = state.spec;

  const definitionId = state.meta.is_catalogue
    ? (state.meta.copy?.definition_id ?? null) : state.meta.definition_id;

  useEffect(() => {
    if (state.jsonError || !state.spec.steps.length || !projectId) return;
    if (lastChecked.current === state.spec) return;
    const spec = state.spec;
    const timer = setTimeout(() => {
      lastChecked.current = spec;
      checkSpec(projectId, spec, definitionId).then((result) => {
        if (latest.current === spec) dispatch({ type: "SET_PROBLEMS", result, spec });
      }, () => {
        // A failed background check says nothing: Save runs its own and reports there.
        lastChecked.current = null;
      });
    }, AUTO_CHECK_MS);
    return () => clearTimeout(timer);
  }, [state.spec, state.jsonError, projectId, definitionId, dispatch]);
}
