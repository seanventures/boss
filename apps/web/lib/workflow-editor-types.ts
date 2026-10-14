/**
 * Workflow Studio: the shared contract between the editor's streams.
 *
 * Frozen in phase 0 so the model (S1), canvas (S2), inspector (S3) and library/IA (S4) work can
 * proceed in parallel against one set of shapes. After phase 0 every change to a type here goes
 * through S1.
 *
 * THE SPEC IS THE STATE. The editor holds the workflow spec itself, not a projection of it, so
 * the Visual and JSON views can never drift and keys this console does not know survive a save.
 * That is why `Spec` and `SpecStep` are open records with a few known fields.
 *
 * The model's functions (S1) are re-exported at the bottom from their own modules, so a stream
 * may import them from here or from lib/workflow-graph.ts, lib/step-view.ts,
 * lib/workflow-editor-store.ts and lib/workflow-api.ts directly. The lane's components are
 * re-exported too; the editor shell and the inspector, JSON and save parts are imported from their
 * own modules (named at the bottom), because they import this file.
 */

/* ── Spec ─────────────────────────────────────────────────────────────────────────── */

export type Spec = Record<string, unknown> & {
  entry_step?: string;
  steps: SpecStep[];
  input_schema?: Record<string, unknown>;
};
export type SpecStep = Record<string, unknown> & {
  id: string; kind: string; title?: string;
  next?: string | string[]; captain?: string;
  binding?: Record<string, unknown>; approval?: Record<string, unknown>;
};

/* ── Palette / roster (GET view=capabilities + custom_steps + skills assignments + roles) ── */

export type Capability = { key: string; label: string; summary: string; risk_tier: number;
  required_inputs: string[]; optional_inputs: string[]; produces: string[]; skills: string[];
  typical_minutes?: number; is_gate?: boolean; approval_forced_manual?: boolean; captain?: string };
export type CustomStepDef = { step_key: string; label: string; summary?: string;
  binding_kind: "skill" | "mcp_tool"; skill_id?: string; mcp_server_id?: string; mcp_tool_name?: string;
  inputs: { name: string; required: boolean }[]; produces: string[]; risk_tier: number;
  usable: boolean; unusable_reason?: string; approval_forced_manual?: boolean };
export type BlockDef = { kind: "decision" | "timer" | "subworkflow"; label: string; summary: string;
  defaults: Record<string, unknown>;
  configure: { key: string; label: string; type: string; required?: boolean; max?: number }[] };
export type RosterAgent = { role_key: string; name: string; skills: string[]; mcp: string[] };
/** A branch block the server says the runtime runs (`canvas_required` blocks of view=capabilities):
 *  placed whole by lib/workflow-blocks.ts `placeBlock`, never configured from a `configure` list. */
export type ControlDef = { kind: "condition" | "parallel" | "bounded_loop"; label: string; summary: string };
export type Palette = { capabilities: Capability[]; customSteps: CustomStepDef[]; blocks: BlockDef[];
  /** Absent from palettes built before branch blocks could be placed: none are offered then. */
  controls?: ControlDef[];
  agents: RosterAgent[]; humanRole: string; riskFloor: number;
  bindable: { skills: { id: string; name: string }[];
              mcp_servers: { id: string; name: string; tools: { name: string }[] }[] } };

/* ── Library ──────────────────────────────────────────────────────────────────────── */

export type WorkflowSummary = { definition_id: string; workflow_key: string; name: string; description?: string;
  is_catalogue: boolean; version: number | null; step_count: number;
  steps: { id: string; title: string; capability?: string; captain?: string; approval?: string }[];
  forked_from_workflow_key?: string | null; forked_from_name?: string | null; run_count: number;
  last_run?: { status: string; at: string } | null; schedule?: unknown; used_for?: string[];
  /** The running version's id: what a "Run another workflow" step is pinned to. Null when
   *  nothing is published yet. */
  active_version_id?: string | null };

/* ── Problems / check / publish ───────────────────────────────────────────────────── */

export type Problem = { step_id: string | null; field: string | null; message: string;
  /** "warning": the step cannot run yet (an ability not set up), but the workflow saves. */
  severity?: "warning" | "error";
  /** Where the gap is closed (kit.setup_fix): a console link and the button's words. */
  fix_label?: string; fix_href?: string };
export type CheckResult = { ok: boolean; problems: Problem[];
  summary: { steps: number; agents: string[]; approvals: string[]; typical_minutes: number | null;
             needs: string[] } };
export type PublishRequest = { definition_id: string | null; name: string; description?: string; spec: Spec;
  from_workflow_key?: string | null; update_copy_id?: string | null };
export type PublishResult =
  | { unchanged: true }
  | { definition: { id: string; workflow_key: string; name: string }; version: number; created: boolean };

// ProblemsError (422, `problems: Problem[]`) and NameInUseError (409, `suggestedName`) live in
// lib/workflow-api.ts, which throws them, and are re-exported below.

/* ── Graph (S1 implements; S2 renders) ────────────────────────────────────────────── */

export type EdgeKind = "next" | "yes" | "no" | "branch" | "join" | "loop";
export type GraphEdge = { from: string; to: string; kind: EdgeKind };
/** One arrow the canvas draws out of a step, as something that can be pointed elsewhere: an edge
 *  (`to` its target), or a flow's end (`kind` "end", `to` null) — a step with no way out, whose
 *  arrow runs to End. */
export type FlowLink = { from: string; to: string | null; kind: EdgeKind | "end" };
export type LaidOutNode = { id: string; column: number; row: number; rowsInColumn: number };
export type FlowLayout = { nodes: LaidOutNode[]; edges: GraphEdge[]; columns: number; maxRows: number;
  linear: boolean; terminal: string[] };

/* ── Display (S1 implements; S2/S3/S4 consume) ────────────────────────────────────── */

export type StepView = { id: string; title: string;
  type: "builtin" | "agent" | "custom" | "gate" | "wait" | "subworkflow" | "parallel" | "condition"
        | "join" | "loop" | "other";
  /** A capability key, or the type fallback. */
  icon: string;
  agent: { kind: "named" | "auto" | "you" | "person"; role_key?: string; label: string; tip: string };
  badges: { approval: boolean; forced: boolean; inputs: number; unwired: string[]; skills: number;
            mcp: number; lacks: string[] } };

/* ── Editor store (S1 implements; S2/S3 dispatch) ─────────────────────────────────── */

export type EditorMeta = { definition_id: string | null; workflow_key: string | null; name: string;
  description: string; is_catalogue: boolean; from_workflow_key: string | null; version: number | null;
  copy?: { definition_id: string; workflow_key: string; name: string } | null;
  readOnlyVersion?: number | null };
/** `loaded` is null for a workflow never saved (new, or an unsaved duplicate): any step makes it
 *  dirty. `jsonText` is null unless someone is typing in the JSON view; the view shows
 *  `specText(spec)` otherwise. `wired` names, per step, the inputs the editor wired itself
 *  (the inspector's `auto` chip); setting one by hand removes it. */
export type EditorState = { spec: Spec; loaded: Spec | null; meta: EditorMeta; loadedMeta: EditorMeta;
  selection: string | null; problems: Problem[]; summary: CheckResult["summary"] | null;
  /** The spec `problems`/`summary` were checked against (by identity: every edit makes a new
   *  spec). When it is not `spec`, the last check describes an older draft and says nothing
   *  about this one, so nothing may show it as passing. */
  checkedSpec?: Spec | null;
  past: Spec[]; future: Spec[]; jsonText: string | null;
  jsonError: { line: number; column?: number; message: string } | null;
  wired?: Record<string, string[]> };
export type EditorAction =
  | { type: "LOAD"; spec: Spec; meta: EditorMeta }
  | { type: "SET_SPEC"; spec: Spec; fromJson?: boolean }
  | { type: "SET_JSON_TEXT"; text: string }
  /** `at` is the index the new step will have in `linearOrder`; -1 is the end. With `palette`,
   *  its required inputs are wired where it lands (useWorkflowEditor's dispatch adds it). */
  | { type: "INSERT"; at: number; step: SpecStep; palette?: Palette }
  /** `to` is the index the step is left at in `linearOrder` (array-move): Alt+→ is index + 1;
   *  a drop on gap g of the order as drawn is `g > index ? g - 1 : g`. */
  | { type: "MOVE"; id: string; to: number }
  /** A step put down off the lane's grid, by `offset` from its place on it (null: back on it).
   *  Drawing only: kept in the spec's optional `layout` (lib/workflow-layout.ts). */
  | { type: "PLACE"; id: string; offset: { dx: number; dy: number } | null }
  /** Every step back on the grid (the spec's `layout` goes). */
  | { type: "RESET_LAYOUT" }
  /** Auto-wire: unset required inputs of `ids` (default every step), by the server's rule. */
  | { type: "WIRE"; palette: Palette; ids?: string[] }
  /** A whole branch block placed at gap `at` (any gap INSERT takes); its head is selected. */
  | { type: "PLACE_BLOCK"; at: number; block: ControlDef["kind"] }
  /** REMOVE on a block's head (a condition, parallel or loop) removes the whole block. */
  | { type: "REMOVE"; id: string }
  | { type: "ADD_BRANCH"; id: string }
  | { type: "REMOVE_BRANCH"; id: string; branch: string }
  /** An arrow pointed at another step (`target`; null for End): lib/workflow-graph.ts
   *  `rewireLink`. Refused (no change) where that refuses; the editor asks the validator first
   *  (lib/workflow-rewire.ts). */
  | { type: "REWIRE"; link: FlowLink; target: string | null }
  | { type: "DUPLICATE"; id: string }
  | { type: "PATCH_STEP"; id: string; patch: StepPatch }
  | { type: "RENAME_ID"; from: string; to: string }
  | { type: "SET_META"; patch: Partial<EditorMeta> }
  | { type: "SET_INPUT_SCHEMA"; schema: Record<string, unknown> }
  | { type: "SELECT"; id: string | null }
  | { type: "SET_PROBLEMS"; result: CheckResult; /** The spec that was checked; the one on screen if omitted. */ spec?: Spec }
  | { type: "UNDO" } | { type: "REDO" }
  /** A save landed: `spec` and `meta` are what was published, the new baseline. Edits made
   *  while the request was in flight are KEPT (the draft stays dirty with them): the spec is
   *  never replaced, and a name or description that differs from `sent` (the meta the request
   *  was made from; default the current meta) keeps the operator's newer value. */
  | { type: "SAVED"; spec: Spec; meta: EditorMeta; sent?: EditorMeta };
export type StepPatch = { title?: string; captain?: string | null;
  instructions?: { mode: "ask" | "fixed"; text?: string };
  skills?: string[]; mcp?: string[]; approval?: "auto" | "manual"; approvalMore?: Record<string, unknown>;
  input?: { name: string; source: InputSource | null }; produces?: string[];
  config?: Record<string, unknown>; max_attempts?: number };
export type InputSource = { from_input: string } | { from_step: string; field: string } | { value: unknown };

/* ── Canvas (S2) ──────────────────────────────────────────────────────────────────── */

export type PaletteItem = { capability?: string; customStep?: string; agentTask?: true; block?: BlockDef["kind"];
  control?: ControlDef["kind"] };

/* ── Implementations ────────────────────────────────────────────────────────────────────────── */

// S1 · the model, from its own modules.
export {
  edgesOf, isLinear, layoutFlow, linearOrder, upstreamOf, renameStepId, wireInputs, insertStep,
  moveStep, removeStep, canMove, canRemove, contractOf, declaredInputs,
} from "./workflow-graph";
export { stepView, newStepFor, stepTitle, stepType, freeStepId, TYPE_CHIPS } from "./step-view";
export {
  editorReducer, useWorkflowEditor, lineOfStep, stepAtLine, parseSpecText, specText, isDirty,
  emptyEditorState, newMeta, type RestoreOffer,
} from "./workflow-editor-store";
export {
  fetchPalette, fetchLibrary, fetchSpec, checkSpec, publishSpec, renameWorkflow, deleteWorkflow,
  draftFromDescription, ProblemsError, NameInUseError,
} from "./workflow-api";
export { applyStepPatch, inputSourceOf, deepEqual, TITLE_MAX } from "./workflow-spec";

// S2 · the canvas, from its own modules. WorkflowEditor is imported from
// app/workflows/editor/workflow-editor.tsx directly: it composes the inspector, which imports
// this file, and a re-export here would make that a cycle.
export { StepNode } from "../app/workflows/flow/step-node";
export { FlowLane } from "../app/workflows/flow/flow-lane";
export { MiniLane } from "../app/workflows/flow/mini-lane";

// S3 · the inspector, JSON view, save flow and define-step dialog live in
// app/workflows/editor/{inspector,json,save}/ and app/workflows/editor/define-step-modal.tsx and
// are imported from there: each imports this file, so a re-export here would be a cycle.
