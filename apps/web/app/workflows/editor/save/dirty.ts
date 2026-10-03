/**
 * Whether the draft differs from what was loaded — the one test Save, Start and the leave guard
 * share, so "Save is disabled" and "Save first?" can never disagree.
 *
 * Deep, key-order-insensitive equality: the JSON view reformats and reorders, and a workflow
 * whose text moved but whose meaning did not has nothing to save.
 */
import type { EditorMeta, EditorState } from "../../../../lib/workflow-editor-types";

export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null || typeof a !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    const bb = b as unknown[];
    return a.length === bb.length && a.every((v, i) => deepEqual(v, bb[i]));
  }
  const ao = a as Record<string, unknown>;
  const bo = b as Record<string, unknown>;
  const ak = Object.keys(ao).filter((k) => ao[k] !== undefined);
  const bk = Object.keys(bo).filter((k) => bo[k] !== undefined);
  return ak.length === bk.length && ak.every((k) => deepEqual(ao[k], bo[k]));
}

/** The fields of the meta a Save writes: name and description. Selection-ish fields
 *  (readOnlyVersion, copy) are not edits. */
export function metaDirty(meta: EditorMeta, loaded: EditorMeta): boolean {
  return meta.name.trim() !== loaded.name.trim() || (meta.description ?? "") !== (loaded.description ?? "");
}

export function specDirty(state: Pick<EditorState, "spec" | "loaded">): boolean {
  return state.loaded === null ? state.spec.steps.length > 0 : !deepEqual(state.spec, state.loaded);
}

export function isDirty(state: EditorState): boolean {
  return specDirty(state) || metaDirty(state.meta, state.loadedMeta);
}
