/**
 * The Workflow Studio editor's state: the spec being edited, and every edit as one action.
 *
 * THE SPEC IS THE STATE. Visual and JSON are two views of `state.spec`; neither keeps a model
 * of its own, so they cannot drift and keys this console does not know survive a save. A
 * visual edit is an action applied to the spec (lib/workflow-graph.ts for structure,
 * `applyStepPatch` for properties); a JSON edit replaces the spec once the text parses.
 *
 * WHAT THE REDUCER GUARANTEES
 *   - An edit that changes nothing returns the same state: no undo entry, no dirty flag.
 *   - Undo/redo restore the exact prior spec (100 states).
 *   - Broken JSON never reaches the spec: `jsonError` says where, the spec stays as it was,
 *     and visual edits are refused until the text parses (or is undone) so typing is not lost.
 *   - A step the canvas places is wired where it lands (INSERT with a palette), by the server's
 *     rule, and the inputs it wired are remembered in `wired` for the inspector's `auto` chip.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { fetchPalette, fetchSpec } from "./workflow-api";
import type {
  EditorAction, EditorMeta, EditorState, Palette, Spec, SpecStep,
} from "./workflow-editor-types";
import {
  duplicateStep, insertStep, moveStep, removeStep, renameStepId, wireInputs,
} from "./workflow-graph";
import { freeStepId } from "./step-view";
import { applyStepPatch, deepEqual, inputsReadBy, mergeInputSchema } from "./workflow-spec";

const HISTORY = 100;
const STEP_ID = /^[A-Za-z0-9._-]+$/;

type Obj = Record<string, unknown>;

export const NEW_WORKFLOW_NAME = "Untitled workflow";

export function newMeta(patch: Partial<EditorMeta> = {}): EditorMeta {
  return { definition_id: null, workflow_key: null, name: NEW_WORKFLOW_NAME, description: "",
           is_catalogue: false, from_workflow_key: null, version: null, copy: null,
           readOnlyVersion: null, ...patch };
}

/** The state before anything has loaded: an empty, unsaved workflow. */
export function emptyEditorState(meta: EditorMeta = newMeta()): EditorState {
  return { spec: { steps: [] }, loaded: null, meta, loadedMeta: meta, selection: null,
           problems: [], summary: null, past: [], future: [], jsonText: null, jsonError: null,
           wired: {} };
}

/** The meta fields an operator edits; the rest describe where the workflow came from. */
const EDITED_META: (keyof EditorMeta)[] = ["name", "description", "from_workflow_key"];

/** Whether Save has anything to save: the spec or the edited meta differ from what loaded. */
export function isDirty(state: EditorState): boolean {
  if (state.jsonError) return true;
  if (EDITED_META.some((k) => !deepEqual(state.meta[k] ?? null, state.loadedMeta[k] ?? null))) {
    return true;
  }
  if (state.loaded === null) return state.spec.steps.length > 0;
  return !deepEqual(state.spec, state.loaded);
}

/** The spec as the JSON view shows it when nobody is typing in it. */
export function specText(spec: Spec): string {
  return JSON.stringify(spec, null, 2);
}

/* ── Reducer ─────────────────────────────────────────────────────────────────────── */

const stepIds = (spec: Spec) => new Set(spec.steps.map((s) => s.id));

/** Names the steps newly read from the start form, added to it (offered, not required). */
function declareNewReads(before: Spec, after: Spec): Spec {
  const had = new Set(inputsReadBy(before.steps));
  const added = inputsReadBy(after.steps).filter((name) => !had.has(name));
  if (!added.length) return after;
  const schema = mergeInputSchema(after.input_schema, added);
  return deepEqual(schema, after.input_schema ?? {}) ? after : { ...after, input_schema: schema };
}

/** A new spec, with the old one on the undo stack. Problems and marks for gone steps go. */
function commit(s: EditorState, spec: Spec, extra: Partial<EditorState> = {}): EditorState {
  if (spec === s.spec || deepEqual(spec, s.spec)) {
    return Object.keys(extra).length ? { ...s, ...extra } : s;
  }
  const ids = stepIds(spec);
  const wired = Object.fromEntries(Object.entries(extra.wired ?? s.wired ?? {})
    .filter(([id]) => ids.has(id)));
  const selection = extra.selection !== undefined ? extra.selection : s.selection;
  return {
    ...s,
    jsonText: null,
    jsonError: null,
    ...extra,
    spec,
    past: [...s.past, s.spec].slice(-HISTORY),
    future: [],
    wired,
    problems: (extra.problems ?? s.problems).filter((p) => !p.step_id || ids.has(p.step_id)),
    selection: selection !== null && ids.has(selection) ? selection : null,
  };
}

/** Restore a spec from history, keeping the selection only if that step is there. */
function travel(s: EditorState, spec: Spec, past: Spec[], future: Spec[]): EditorState {
  const ids = stepIds(spec);
  return { ...s, spec, past, future, jsonText: null, jsonError: null,
           selection: s.selection && ids.has(s.selection) ? s.selection : null,
           problems: s.problems.filter((p) => !p.step_id || ids.has(p.step_id)) };
}

/** Actions that change the spec from the visual side: refused while the JSON is broken. */
const VISUAL_EDITS = new Set<EditorAction["type"]>([
  "SET_SPEC", "INSERT", "MOVE", "REMOVE", "DUPLICATE", "PATCH_STEP", "RENAME_ID",
  "SET_INPUT_SCHEMA", "WIRE",
]);

export function editorReducer(s: EditorState, a: EditorAction): EditorState {
  if (s.jsonError && VISUAL_EDITS.has(a.type) && !(a.type === "SET_SPEC" && a.fromJson)) return s;
  switch (a.type) {
    case "LOAD":
      // A workflow with no key has never been saved (new, or an unsaved duplicate): nothing
      // was loaded, so any step at all is something to save.
      return { ...emptyEditorState(a.meta), spec: a.spec,
               loaded: a.meta.workflow_key === null ? null : a.spec };

    case "SAVED":
      return { ...s, spec: a.spec, loaded: a.spec, meta: a.meta, loadedMeta: a.meta, problems: [],
               jsonText: null, jsonError: null };

    case "SET_SPEC":
      return commit(s, a.spec, a.fromJson ? { jsonText: s.jsonText } : {});

    case "SET_JSON_TEXT": {
      const parsed = parseSpecText(a.text);
      if ("error" in parsed) return { ...s, jsonText: a.text, jsonError: parsed.error };
      if (deepEqual(parsed.spec, s.spec)) return { ...s, jsonText: a.text, jsonError: null };
      return commit(s, parsed.spec, { jsonText: a.text, jsonError: null });
    }

    case "INSERT": {
      const step: SpecStep = stepIds(s.spec).has(a.step.id)
        ? { ...a.step, id: freeStepId(s.spec, a.step.id) } : a.step;
      const inserted = insertStep(s.spec, a.at, step);
      if (inserted === s.spec) return s;
      let spec = inserted;
      let wired = s.wired ?? {};
      if (a.palette) {
        const result = wireInputs(spec, a.palette, [step.id]);
        spec = result.spec;
        if (result.wired.length) {
          wired = { ...wired, [step.id]: result.wired.map((w) => w.input) };
        }
      }
      return commit(s, declareNewReads(s.spec, spec), { selection: step.id, wired });
    }

    case "WIRE": {
      const result = wireInputs(s.spec, a.palette, a.ids);
      if (!result.wired.length) return s;
      const wired = { ...(s.wired ?? {}) };
      for (const w of result.wired) wired[w.step_id] = [...(wired[w.step_id] ?? []), w.input];
      return commit(s, declareNewReads(s.spec, result.spec), { wired });
    }

    case "MOVE":
      return commit(s, moveStep(s.spec, a.id, a.to));

    case "REMOVE":
      return commit(s, removeStep(s.spec, a.id),
                    s.selection === a.id ? { selection: null } : {});

    case "DUPLICATE": {
      const copyId = freeStepId(s.spec, a.id);
      const spec = duplicateStep(s.spec, a.id, copyId);
      return spec === s.spec ? s : commit(s, spec, { selection: copyId });
    }

    case "PATCH_STEP": {
      const index = s.spec.steps.findIndex((step) => step.id === a.id);
      if (index < 0) return s;
      const before = s.spec.steps[index];
      const after = applyStepPatch(before, a.patch);
      if (after === before) return s;
      const steps = [...s.spec.steps];
      steps[index] = after;
      // An input the operator set by hand is no longer one the editor wired.
      const wired = a.patch.input && s.wired?.[a.id]
        ? { ...s.wired, [a.id]: s.wired[a.id].filter((n) => n !== a.patch.input!.name) }
        : s.wired;
      return commit(s, declareNewReads(s.spec, { ...s.spec, steps }), { wired });
    }

    case "RENAME_ID": {
      if (!STEP_ID.test(a.to)) return s;
      const spec = renameStepId(s.spec, a.from, a.to);
      if (spec === s.spec) return s;
      const wired = { ...(s.wired ?? {}) };
      if (wired[a.from]) { wired[a.to] = wired[a.from]; delete wired[a.from]; }
      return commit(s, spec, {
        wired,
        selection: s.selection === a.from ? a.to : s.selection,
        problems: s.problems.map((p) => (p.step_id === a.from ? { ...p, step_id: a.to } : p)),
      });
    }

    case "SET_META":
      return { ...s, meta: { ...s.meta, ...a.patch } };

    case "SET_INPUT_SCHEMA": {
      const empty = !Object.keys(a.schema).length;
      if (empty && !("input_schema" in s.spec)) return s;
      return commit(s, { ...s.spec, input_schema: a.schema });
    }

    case "SELECT":
      return a.id === s.selection || (a.id !== null && !stepIds(s.spec).has(a.id))
        ? s : { ...s, selection: a.id };

    case "SET_PROBLEMS":
      return { ...s, problems: a.result.problems, summary: a.result.summary };

    case "UNDO":
      // With broken JSON on screen, the first undo puts the text back to the spec it shows.
      if (s.jsonError) return { ...s, jsonText: null, jsonError: null };
      if (!s.past.length) return s;
      return travel(s, s.past[s.past.length - 1], s.past.slice(0, -1),
                    [s.spec, ...s.future].slice(0, HISTORY));

    case "REDO":
      if (s.jsonError || !s.future.length) return s;
      return travel(s, s.future[0], [...s.past, s.spec].slice(-HISTORY), s.future.slice(1));

    default:
      return s;
  }
}

/* ── JSON text: parse with positions ─────────────────────────────────────────────── */

type StepSpan = { id: string | null; start: number; end: number; idAt: number };
type Located = { spans: StepSpan[]; error: { at: number; message: string } | null };

class JsonFault extends Error {
  constructor(public readonly at: number, message: string) { super(message); }
}

/**
 * A JSON reader that knows WHERE things are: each step object's span and the offset of its
 * `"id"`, and the offset of the first syntax error. Browsers word JSON.parse errors
 * differently and Safari gives no position at all, so the line in "Line 14: Unexpected token }"
 * comes from here. Spans found before an error are kept, so the caret still finds its step in
 * a half-typed document.
 */
function locate(text: string): Located {
  const spans: StepSpan[] = [];
  const n = text.length;
  let i = 0;
  const unexpected = (at: number): never => {
    throw new JsonFault(at, at >= n ? "Unexpected end of JSON" : `Unexpected token ${text[at]}`);
  };
  const ws = () => { while (i < n && " \t\n\r".includes(text[i])) i += 1; };
  const string = (): string => {
    const start = i;
    i += 1;
    let out = "";
    while (i < n) {
      const c = text[i];
      if (c === "\"") { i += 1; return out; }
      if (c === "\\") {
        const e = text[i + 1];
        if (e === "u") {
          if (!/^[0-9a-fA-F]{4}$/.test(text.slice(i + 2, i + 6))) throw new JsonFault(i, "Bad escape in string");
          out += String.fromCharCode(parseInt(text.slice(i + 2, i + 6), 16));
          i += 6;
          continue;
        }
        const simple: Record<string, string> = { "\"": "\"", "\\": "\\", "/": "/", b: "\b",
                                                 f: "\f", n: "\n", r: "\r", t: "\t" };
        if (e === undefined || !(e in simple)) throw new JsonFault(i, "Bad escape in string");
        out += simple[e];
        i += 2;
        continue;
      }
      if (c < " ") throw new JsonFault(i, c === "\n" ? "Unterminated string" : "Bad character in string");
      out += c;
      i += 1;
    }
    throw new JsonFault(start, "Unterminated string");
  };
  const NUMBER = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
  const value = (path: (string | number)[]): unknown => {
    ws();
    if (i >= n) unexpected(i);
    const c = text[i];
    if (c === "{") return object(path);
    if (c === "[") return array(path);
    if (c === "\"") return string();
    if (c === "-" || (c >= "0" && c <= "9")) {
      NUMBER.lastIndex = i;
      const m = NUMBER.exec(text);
      if (!m || !m[0] || m[0] === "-") unexpected(m?.[0] === "-" ? i + 1 : i);
      i += m![0].length;
      return Number(m![0]);
    }
    for (const [word, v] of [["true", true], ["false", false], ["null", null]] as const) {
      if (text.startsWith(word, i)) { i += word.length; return v; }
    }
    return unexpected(i);
  };
  const object = (path: (string | number)[]): undefined => {
    const start = i;
    const isStep = path.length === 2 && path[0] === "steps";
    let id: string | null = null;
    let idAt = start;
    i += 1;
    ws();
    const close = () => { if (isStep) spans.push({ id, start, end: i, idAt }); };
    if (text[i] === "}") { i += 1; close(); return undefined; }
    for (;;) {
      ws();
      if (text[i] !== "\"") unexpected(i);
      const keyAt = i;
      const key = string();
      ws();
      if (text[i] !== ":") unexpected(i);
      i += 1;
      const v = value([...path, key]);
      if (isStep && key === "id" && typeof v === "string") { id = v; idAt = keyAt; }
      ws();
      if (text[i] === ",") { i += 1; continue; }
      if (text[i] === "}") { i += 1; close(); return undefined; }
      unexpected(i);
    }
  };
  const array = (path: (string | number)[]): undefined => {
    i += 1;
    ws();
    if (text[i] === "]") { i += 1; return undefined; }
    for (let index = 0; ; index += 1) {
      value([...path, index]);
      ws();
      if (text[i] === ",") { i += 1; continue; }
      if (text[i] === "]") { i += 1; return undefined; }
      unexpected(i);
    }
  };
  try {
    value([]);
    ws();
    if (i < n) unexpected(i);
    return { spans, error: null };
  } catch (fault) {
    if (fault instanceof JsonFault) return { spans, error: { at: fault.at, message: fault.message } };
    throw fault;
  }
}

let lastLocated: { text: string; located: Located } | null = null;
function located(text: string): Located {
  if (lastLocated?.text !== text) lastLocated = { text, located: locate(text) };
  return lastLocated.located;
}

function lineAt(text: string, offset: number): number {
  let line = 1;
  for (let k = 0; k < offset && k < text.length; k += 1) if (text[k] === "\n") line += 1;
  return line;
}

function columnAt(text: string, offset: number): number {
  return offset - (text.lastIndexOf("\n", offset - 1) + 1) + 1;
}

/** The line of a step's `"id": "<id>"` in the JSON text (1-based), or null. */
export function lineOfStep(text: string, id: string): number | null {
  const span = located(text).spans.find((s) => s.id === id);
  return span ? lineAt(text, span.idAt) : null;
}

/** The id of the step whose object contains `line` (1-based), or null. */
export function stepAtLine(text: string, line: number): string | null {
  for (const span of located(text).spans) {
    if (lineAt(text, span.start) <= line && line <= lineAt(text, Math.max(span.start, span.end - 1))) {
      return span.id;
    }
  }
  return null;
}

/** The text as a spec, or where and why it is not one. */
export function parseSpecText(text: string):
    { spec: Spec } | { error: { line: number; column: number; message: string } } {
  const at = (offset: number, message: string) =>
    ({ error: { line: lineAt(text, offset), column: columnAt(text, offset), message } });
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (e) {
    const fault = located(text).error;
    return fault ? at(fault.at, fault.message) : at(0, String((e as Error)?.message ?? e));
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return at(0, "A workflow is an object with steps");
  }
  const steps = (value as Obj).steps;
  if (!Array.isArray(steps)) return at(0, "steps must be a list");
  const spans = located(text).spans;
  const seen = new Set<string>();
  for (const [index, step] of steps.entries()) {
    const offset = spans[index]?.start ?? 0;
    if (!step || typeof step !== "object" || Array.isArray(step)) {
      return at(offset, `Step ${index + 1} must be an object`);
    }
    const id = (step as Obj).id;
    if (typeof id !== "string" || !id.trim()) return at(offset, `Step ${index + 1} needs an id`);
    if (seen.has(id)) return at(spans[index]?.idAt ?? offset, `Duplicate step id ${id}`);
    seen.add(id);
  }
  return { spec: value as Spec };
}

/* ── The hook ────────────────────────────────────────────────────────────────────── */

type StoredDraft = { spec: Spec; meta: Pick<EditorMeta, "name" | "description" | "from_workflow_key">;
                     baseVersion: number | null; savedAt: string };

/** An unsaved draft found from an earlier visit, offered back (spec 2.8 "Restore"). */
export type RestoreOffer = {
  savedAt: string;
  /** The version published since the draft was made, when the workflow moved on. */
  publishedSince: number | null;
  apply(): void;
  discard(): void;
};

const draftKey = (projectId: string, key: string) => `boss:wf-draft:${projectId}:${key}`;

function readDraft(storageKey: string): StoredDraft | null {
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredDraft;
    return parsed && Array.isArray(parsed.spec?.steps) ? parsed : null;
  } catch { return null; }
}

function writeDraft(storageKey: string, draft: StoredDraft | null) {
  try {
    if (draft) window.localStorage.setItem(storageKey, JSON.stringify(draft));
    else window.localStorage.removeItem(storageKey);
  } catch { /* storage full, blocked or absent: the draft is a convenience only */ }
}

/**
 * Load a workflow (or start a new one) and edit it.
 *
 * Returns the state and `dispatch`, plus the palette (null until it loads), whether there is
 * anything to save, a load error, the offer to restore an unsaved draft from an earlier visit,
 * and `refreshPalette` (after defining a step or granting a skill).
 *
 * `dispatch` attaches the palette to INSERT, so a placed step is wired where it lands.
 * A beforeunload guard is held while there are unsaved changes.
 */
export function useWorkflowEditor(key: string | "new", projectId: string,
                                  options: { version?: number } = {}): {
  state: EditorState; dispatch: (a: EditorAction) => void; palette: Palette | null;
  dirty: boolean; loading: boolean; error: string | null; restore: RestoreOffer | null;
  refreshPalette(): Promise<void>;
} {
  const [state, rawDispatch] = useReducer(editorReducer, undefined, () => emptyEditorState());
  const [palette, setPalette] = useState<Palette | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [offer, setOffer] = useState<StoredDraft | null>(null);
  const paletteRef = useRef<Palette | null>(null);
  paletteRef.current = palette;
  const stateRef = useRef(state);
  stateRef.current = state;
  const version = options.version;
  const storageKey = draftKey(projectId, key);
  const storageKeyRef = useRef(storageKey);
  storageKeyRef.current = storageKey;
  // The draft key `state` belongs to: set when a load lands (or when what is in memory already
  // is that workflow), cleared when another starts loading. A draft is written only under it,
  // so a failed or still-pending load can neither erase this key's draft nor file another
  // workflow's spec under it.
  const stateKeyRef = useRef<string | null>(null);

  const refreshPalette = useCallback(async () => {
    try { setPalette(await fetchPalette(projectId)); } catch (e) {
      setError(String((e as Error)?.message ?? e));
    }
  }, [projectId]);

  useEffect(() => { void refreshPalette(); }, [refreshPalette]);

  useEffect(() => {
    // Just saved a new workflow or a copy, and the URL moved to its key: what is in memory IS
    // that workflow's running version. Reloading would only flash a spinner. An older version
    // on screen ("Viewing v2") is not the running one, so going back to it loads.
    const current = stateRef.current;
    if (key !== "new" && current.meta.workflow_key === key && current.loaded && !version
        && !current.meta.readOnlyVersion && !isDirty(current)) {
      stateKeyRef.current = storageKey;
      setLoading(false);
      return;
    }
    let live = true;
    stateKeyRef.current = null;
    // Nothing of the previous workflow stays on screen (or in a draft) while this one loads.
    rawDispatch({ type: "LOAD", spec: { steps: [] }, meta: newMeta() });
    setLoading(true);
    setError(null);
    setOffer(null);
    const load = key === "new"
      ? Promise.resolve({ spec: { steps: [] } as Spec, meta: newMeta() })
      : fetchSpec(projectId, key, version);
    load.then(({ spec, meta }) => {
      if (!live) return;
      rawDispatch({ type: "LOAD", spec, meta });
      stateKeyRef.current = storageKey;
      const stored = meta.readOnlyVersion ? null : readDraft(storageKey);
      if (stored && !deepEqual(stored.spec, spec)) setOffer(stored);
      else if (stored) writeDraft(storageKey, null);
      setLoading(false);
    }).catch((e) => {
      if (!live) return;
      setError(String((e as Error)?.message ?? e));
      setLoading(false);
    });
    return () => { live = false; };
  }, [key, projectId, version, storageKey]);

  const dirty = useMemo(() => isDirty(state), [state]);

  // Keep the unsaved draft in this browser, a convenience only. Paused while an earlier draft
  // is being offered back, so loading cannot overwrite the thing being offered; and written
  // only once this key's workflow has loaded. While the JSON is broken the draft is the last
  // spec that parsed, so earlier edits are not lost to half-typed text.
  useEffect(() => {
    if (loading || offer || state.meta.readOnlyVersion || stateKeyRef.current !== storageKey) return;
    const specDirty = isDirty({ ...state, jsonError: null });
    const timer = setTimeout(() => {
      writeDraft(storageKey, specDirty ? {
        spec: state.spec,
        meta: { name: state.meta.name, description: state.meta.description,
                from_workflow_key: state.meta.from_workflow_key },
        baseVersion: state.loadedMeta.version,
        savedAt: new Date().toISOString(),
      } : null);
    }, 400);
    return () => clearTimeout(timer);
  }, [state, loading, offer, storageKey]);

  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  const dispatch = useCallback((action: EditorAction) => {
    // Saved: the draft kept under the key it was edited as ("new", or the standard a copy was
    // made from) is spent. The URL moves to the saved key, so the effect above would miss it.
    if (action.type === "SAVED") writeDraft(storageKeyRef.current, null);
    if (action.type === "INSERT" && !action.palette && paletteRef.current) {
      rawDispatch({ ...action, palette: paletteRef.current });
    } else {
      rawDispatch(action);
    }
  }, []);

  const restore = useMemo<RestoreOffer | null>(() => offer ? {
    savedAt: offer.savedAt,
    publishedSince: offer.baseVersion !== state.loadedMeta.version ? state.loadedMeta.version : null,
    apply: () => {
      rawDispatch({ type: "SET_SPEC", spec: offer.spec });
      rawDispatch({ type: "SET_META", patch: offer.meta });
      setOffer(null);
    },
    discard: () => { writeDraft(storageKey, null); setOffer(null); },
  } : null, [offer, state.loadedMeta.version, storageKey]);

  return { state, dispatch, palette, dirty, loading, error, restore, refreshPalette };
}
