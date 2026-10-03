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
 * The functions and components at the bottom are STUBS: their signatures are the contract and
 * each throws until its owning stream lands the real one in its own module (named beside it).
 * Nothing in the shipped console calls them yet.
 */
import type { Dispatch, JSX, ReactNode } from "react";

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
export type Palette = { capabilities: Capability[]; customSteps: CustomStepDef[]; blocks: BlockDef[];
  agents: RosterAgent[]; humanRole: string; riskFloor: number;
  bindable: { skills: { id: string; name: string }[];
              mcp_servers: { id: string; name: string; tools: { name: string }[] }[] } };

/* ── Library ──────────────────────────────────────────────────────────────────────── */

export type WorkflowSummary = { definition_id: string; workflow_key: string; name: string; description?: string;
  is_catalogue: boolean; version: number | null; step_count: number;
  steps: { id: string; title: string; capability?: string; captain?: string; approval?: string }[];
  forked_from_workflow_key?: string | null; forked_from_name?: string | null; run_count: number;
  last_run?: { status: string; at: string } | null; schedule?: unknown; used_for?: string[] };

/* ── Problems / check / publish ───────────────────────────────────────────────────── */

export type Problem = { step_id: string | null; field: string | null; message: string };
export type CheckResult = { ok: boolean; problems: Problem[];
  summary: { steps: number; agents: string[]; approvals: string[]; typical_minutes: number | null;
             needs: string[] } };
export type PublishRequest = { definition_id: string | null; name: string; description?: string; spec: Spec;
  from_workflow_key?: string | null; update_copy_id?: string | null };
export type PublishResult =
  | { unchanged: true }
  | { definition: { id: string; workflow_key: string; name: string }; version: number; created: boolean };

/** `publishSpec` on a 422: the server's problems, each pinned to a step and field where known. */
export class ProblemsError extends Error {
  constructor(public readonly problems: Problem[]) {
    super(problems[0]?.message ?? "Workflow has problems");
    this.name = "ProblemsError";
  }
}

/** `publishSpec` (and rename) on a 409: an own workflow already has that name. */
export class NameInUseError extends Error {
  constructor(public readonly suggestedName?: string | null) {
    super("Name in use");
    this.name = "NameInUseError";
  }
}

/* ── Graph (S1 implements; S2 renders) ────────────────────────────────────────────── */

export type EdgeKind = "next" | "yes" | "no" | "branch" | "join" | "loop";
export type GraphEdge = { from: string; to: string; kind: EdgeKind };
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
export type EditorState = { spec: Spec; loaded: Spec | null; meta: EditorMeta; loadedMeta: EditorMeta;
  selection: string | null; problems: Problem[]; summary: CheckResult["summary"] | null;
  past: Spec[]; future: Spec[]; jsonText: string | null;
  jsonError: { line: number; message: string } | null };
export type EditorAction =
  | { type: "LOAD"; spec: Spec; meta: EditorMeta }
  | { type: "SET_SPEC"; spec: Spec; fromJson?: boolean }
  | { type: "SET_JSON_TEXT"; text: string }
  /** `at` is an index in linear order; -1 is the end. */
  | { type: "INSERT"; at: number; step: SpecStep }
  | { type: "MOVE"; id: string; to: number }
  | { type: "REMOVE"; id: string }
  | { type: "DUPLICATE"; id: string }
  | { type: "PATCH_STEP"; id: string; patch: StepPatch }
  | { type: "RENAME_ID"; from: string; to: string }
  | { type: "SET_META"; patch: Partial<EditorMeta> }
  | { type: "SET_INPUT_SCHEMA"; schema: Record<string, unknown> }
  | { type: "SELECT"; id: string | null }
  | { type: "SET_PROBLEMS"; result: CheckResult }
  | { type: "UNDO" } | { type: "REDO" } | { type: "SAVED"; spec: Spec; meta: EditorMeta };
export type StepPatch = { title?: string; captain?: string | null;
  instructions?: { mode: "ask" | "fixed"; text?: string };
  skills?: string[]; mcp?: string[]; approval?: "auto" | "manual"; approvalMore?: Record<string, unknown>;
  input?: { name: string; source: InputSource | null }; produces?: string[];
  config?: Record<string, unknown>; max_attempts?: number };
export type InputSource = { from_input: string } | { from_step: string; field: string } | { value: unknown };

/* ── Canvas (S2) ──────────────────────────────────────────────────────────────────── */

export type PaletteItem = { capability?: string; customStep?: string; agentTask?: true; block?: BlockDef["kind"] };

/* ── Stubs ────────────────────────────────────────────────────────────────────────── */

function notImplemented(name: string): never {
  throw new Error(`${name}: not implemented`);
}

// S1 · lib/workflow-graph.ts
export function edgesOf(spec: Spec): GraphEdge[] { return notImplemented("edgesOf"); }
/** Every step has ≤1 next, and there is no parallel/condition/join/loop/wait_for. */
export function isLinear(spec: Spec): boolean { return notImplemented("isLinear"); }
/** Longest-path ranks from entry_step, siblings stacked; ported from v2 WorkflowsScreen. */
export function layoutFlow(spec: Spec): FlowLayout { return notImplemented("layoutFlow"); }
/** Topological order used by index-based INSERT/MOVE. */
export function linearOrder(spec: Spec): string[] { return notImplemented("linearOrder"); }
export function upstreamOf(spec: Spec, id: string): string[] { return notImplemented("upstreamOf"); }
/** Rewrites next, entry_step, from_step, wait_for, on_true/false, branches, join and body. */
export function renameStepId(spec: Spec, from: string, to: string): Spec {
  return notImplemented("renameStepId");
}
/** Mirrors builtin.py _wire_inputs: nearest upstream producer, else from_input when declared. */
export function wireInputs(spec: Spec, palette: Palette, ids?: string[]):
    { spec: Spec; wired: { step_id: string; input: string; source: InputSource }[] } {
  return notImplemented("wireInputs");
}
/** Splices next; refuses (returns the same spec) across branch boundaries. */
export function insertStep(spec: Spec, at: number, step: SpecStep): Spec {
  return notImplemented("insertStep");
}
export function moveStep(spec: Spec, id: string, to: number): Spec { return notImplemented("moveStep"); }
/** Reconnects predecessor → successor. */
export function removeStep(spec: Spec, id: string): Spec { return notImplemented("removeStep"); }
export function canMove(spec: Spec, id: string, to: number): boolean { return notImplemented("canMove"); }

// S1 · lib/step-view.ts
export function stepView(step: SpecStep, palette: Palette): StepView { return notImplemented("stepView"); }
export function newStepFor(item: PaletteItem, spec: Spec, palette: Palette): SpecStep {
  return notImplemented("newStepFor");
}

// S1 · lib/workflow-editor-store.ts
export function editorReducer(s: EditorState, a: EditorAction): EditorState {
  return notImplemented("editorReducer");
}
export function useWorkflowEditor(key: string | "new", projectId: string): {
  state: EditorState; dispatch: (a: EditorAction) => void; palette: Palette | null; dirty: boolean;
  loading: boolean;
} {
  return notImplemented("useWorkflowEditor");
}
export function lineOfStep(text: string, id: string): number | null { return notImplemented("lineOfStep"); }
export function stepAtLine(text: string, line: number): string | null { return notImplemented("stepAtLine"); }

// S1 · lib/workflow-api.ts (all through the /api/delivery-workflows proxy, which adds the actor)
export function fetchPalette(projectId: string): Promise<Palette> { return notImplemented("fetchPalette"); }
export function fetchLibrary(projectId: string): Promise<WorkflowSummary[]> {
  return notImplemented("fetchLibrary");
}
export function fetchSpec(projectId: string, key: string, version?: number): Promise<{
  spec: Spec; meta: EditorMeta; versions: { version: number; published_at: string; is_active: boolean }[];
}> {
  return notImplemented("fetchSpec");
}
export function checkSpec(projectId: string, spec: Spec, definitionId?: string | null): Promise<CheckResult> {
  return notImplemented("checkSpec");
}
/** Throws ProblemsError on 422 and NameInUseError on 409. */
export function publishSpec(projectId: string, req: PublishRequest): Promise<PublishResult> {
  return notImplemented("publishSpec");
}
export function renameWorkflow(projectId: string, definitionId: string,
                               patch: { name?: string; description?: string }): Promise<void> {
  return notImplemented("renameWorkflow");
}
export function deleteWorkflow(projectId: string, definitionId: string):
    Promise<{ outcome: "deleted" | "retired" }> {
  return notImplemented("deleteWorkflow");
}
export function draftFromDescription(projectId: string, description: string):
    Promise<{ name: string; spec: Spec }> {
  return notImplemented("draftFromDescription");
}

// S2 · app/workflows/flow/*, app/workflows/editor/*
export function StepNode(p: { view: StepView; mode: "design" | "run" | "dot"; selected?: boolean;
  problem?: string;
  run?: { phase: "done" | "moving" | "needs_you" | "failed" | "future" | "skipped"; meta: string };
  onSelect?(): void }): JSX.Element {
  return notImplemented("StepNode");
}
export function FlowLane(p: { spec: Spec; palette: Palette | null; mode: "design" | "run" | "preview";
  density?: "auto" | "compact"; selection?: string | null; problems?: Problem[];
  runState?: Record<string, { phase: string; meta: string }>; onSelect?(id: string | null): void;
  onInsert?(at: number, item: PaletteItem): void; onMove?(id: string, to: number): void;
  canMove?(id: string, to: number): boolean; renderGapAction?(at: number): ReactNode }): JSX.Element {
  return notImplemented("FlowLane");
}
/** The library card rail. */
export function MiniLane(p: { steps: WorkflowSummary["steps"] }): JSX.Element {
  return notImplemented("MiniLane");
}
export function WorkflowEditor(p: { projectId: string; workflowKey: string | "new"; onBack(): void;
  onStart(key: string): void }): JSX.Element {
  return notImplemented("WorkflowEditor");
}

// S3 · app/workflows/editor/inspector/*, json/*, save/*
/** Step properties, or the workflow settings when nothing is selected. */
export function Inspector(p: { state: EditorState; palette: Palette; dispatch: Dispatch<EditorAction>;
  projectId: string; onClose(): void }): JSX.Element {
  return notImplemented("Inspector");
}
export function JsonView(p: { state: EditorState; palette: Palette | null;
  dispatch: Dispatch<EditorAction> }): JSX.Element {
  return notImplemented("JsonView");
}
export function useSaveFlow(p: { state: EditorState; dispatch: Dispatch<EditorAction>; projectId: string;
  onNavigate(key: string): void }): {
  save(): Promise<void>; check(): Promise<void>; saving: boolean; checking: boolean; popover: ReactNode;
  label: "Save" | "Save as my copy";
} {
  return notImplemented("useSaveFlow");
}
export function DefineStepModal(p: { projectId: string; edit?: CustomStepDef; open: boolean;
  onClose(created?: CustomStepDef): void }): JSX.Element {
  return notImplemented("DefineStepModal");
}
/** Re-checks the draft after an 800ms pause in editing. */
export function useAutoCheck(state: EditorState, dispatch: Dispatch<EditorAction>, projectId: string): void {
  notImplemented("useAutoCheck");
}
