/**
 * The Workflow Studio's one door to the API: every read and write the editor and library make,
 * through the /api/delivery-workflows proxy (which adds the signed-in actor's credentials).
 *
 * Each function returns the editor's own shapes (lib/workflow-editor-types.ts), normalised here
 * once, so no component parses a raw API row. Writes go through apiWrite, so a double press
 * sends once (tests/every-write-goes-through-api-write.test.ts).
 *
 * A refusal is an Error carrying the API's own sentence. Two refusals are typed because the
 * editor acts on them: 422 with problems → ProblemsError (each problem pinned to a step and a
 * field), 409 name clash → NameInUseError (with the name the API suggests instead).
 */
import { apiWrite } from "./api-write";
import type {
  BlockDef, Capability, ControlDef, CheckResult, CustomStepDef, EditorMeta, Palette, Problem,
  PublishRequest, PublishResult, RosterAgent, Spec, WorkflowSummary,
} from "./workflow-editor-types";

/** `publishSpec` (and `checkSpec`) on a 422: the problems, each pinned to a step and field. */
export class ProblemsError extends Error {
  constructor(public readonly problems: Problem[]) {
    super(problems[0]?.message ?? "Workflow has problems");
    this.name = "ProblemsError";
  }
}

/** `publishSpec` or `renameWorkflow` on a 409: an own workflow already has that name. */
export class NameInUseError extends Error {
  constructor(public readonly suggestedName?: string | null) {
    super("Name in use");
    this.name = "NameInUseError";
  }
}

const ROUTE = "/api/delivery-workflows";

type Obj = Record<string, unknown>;

const asObj = (value: unknown): Obj =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Obj) : {};
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const strings = (value: unknown): string[] => list(value).map(String).filter(Boolean);
const str = (value: unknown): string => (typeof value === "string" ? value : "");

/** The body of a response, whatever it is; a non-JSON body becomes `{error: text}`. */
async function bodyOf(r: Response): Promise<Obj> {
  const raw = await r.text();
  try { return asObj(JSON.parse(raw)); } catch { return { error: raw }; }
}

/** The API's problems from a refusal: top level, or inside `detail`. */
function problemsIn(body: Obj): Problem[] | null {
  const raw = Array.isArray(body.problems) ? body.problems
    : Array.isArray(asObj(body.detail).problems) ? asObj(body.detail).problems : null;
  return raw ? (raw as unknown[]).map(toProblem) : null;
}

function toProblem(raw: unknown): Problem {
  const p = asObj(raw);
  // Severity is kept: a warning ("Needs Deploy set up") is shown but blocks neither Save nor a
  // link (use-save-flow, lib/workflow-rewire.ts). So is where its gap is fixed.
  return {
    step_id: p.step_id == null || p.step_id === "" ? null : String(p.step_id),
    field: p.field == null || p.field === "" ? null : String(p.field),
    message: str(p.message) || String(raw ?? ""),
    ...(p.severity === "warning" || p.severity === "error" ? { severity: p.severity } : {}),
    ...(str(p.fix_label) ? { fix_label: str(p.fix_label) } : {}),
    ...(str(p.fix_href) ? { fix_href: str(p.fix_href) } : {}),
  };
}

/** Turn a failed response into the error the editor acts on. */
function refusal(status: number, body: Obj): Error {
  const problems = problemsIn(body);
  if (status === 422 && problems?.length) return new ProblemsError(problems);
  const message = str(body.error) || str(body.detail) || `API returned ${status}`;
  if (status === 409 && /name in use/i.test(message)) {
    const suggested = body.suggested_name ?? asObj(body.detail).suggested_name;
    return new NameInUseError(typeof suggested === "string" ? suggested : null);
  }
  const error = new Error(message);
  (error as Error & { status?: number }).status = status;
  return error;
}

async function getView(view: string, params: Record<string, string | undefined>): Promise<Obj> {
  const query = new URLSearchParams({ view });
  for (const [key, value] of Object.entries(params)) if (value) query.set(key, value);
  const r = await fetch(`${ROUTE}?${query}`, { cache: "no-store" });
  const body = await bodyOf(r);
  if (!r.ok) throw refusal(r.status, body);
  return body;
}

async function send(method: "POST" | "PATCH" | "DELETE", payload: Obj | null,
                    query?: Record<string, string>): Promise<Obj> {
  const url = query ? `${ROUTE}?${new URLSearchParams(query)}` : ROUTE;
  const r = await apiWrite(url, {
    method,
    ...(payload ? { headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload) } : {}),
  });
  const body = await bodyOf(r);
  if (!r.ok) throw refusal(r.status, body);
  return body;
}

/* ── Palette ─────────────────────────────────────────────────────────────────────── */

/** The inspector's words for each block setting (spec section 4), whatever the API calls them. */
const SETTING_LABELS: Record<string, string> = {
  question: "Question", guidance: "Guidance", approver_role: "Approver",
  wait_seconds: "Wait", workflow_version_id: "Workflow",
};
const SETTING_RULES: Record<string, { required?: boolean; max?: number }> = {
  question: { required: true, max: 400 }, guidance: { max: 1000 },
  workflow_version_id: { required: true },
};
const PLACEABLE: BlockDef["kind"][] = ["decision", "timer", "subworkflow"];

function toCapability(raw: unknown): Capability {
  const c = asObj(raw);
  return {
    key: str(c.key), label: str(c.label) || str(c.key), summary: str(c.summary),
    risk_tier: Number(c.risk_tier ?? 0),
    required_inputs: strings(c.required_inputs), optional_inputs: strings(c.optional_inputs),
    produces: strings(c.produces), skills: strings(c.skills),
    ...(typeof c.typical_minutes === "number" ? { typical_minutes: c.typical_minutes } : {}),
    is_gate: Boolean(c.is_gate), approval_forced_manual: Boolean(c.approval_forced_manual),
    ...(str(c.captain) ? { captain: str(c.captain) } : {}),
    ...(str(c.role_agent) ? { role_agent: str(c.role_agent) } : {}),
    ...(c.needs_model === true ? { needs_model: true } : {}),
    ...(c.is_code === true ? { is_code: true } : {}),
  };
}

function toCustomStep(raw: unknown): CustomStepDef {
  const c = asObj(raw);
  return {
    step_key: str(c.step_key), label: str(c.label) || str(c.step_key),
    ...(str(c.summary) ? { summary: str(c.summary) } : {}),
    binding_kind: c.binding_kind === "mcp_tool" ? "mcp_tool" : "skill",
    ...(str(c.skill_id) ? { skill_id: str(c.skill_id) } : {}),
    ...(str(c.mcp_server_id) ? { mcp_server_id: str(c.mcp_server_id) } : {}),
    ...(str(c.mcp_tool_name) ? { mcp_tool_name: str(c.mcp_tool_name) } : {}),
    inputs: list(c.inputs).map((i) => {
      const input = asObj(i);
      return { name: str(input.name) || String(i), required: input.required !== false };
    }).filter((i) => i.name),
    produces: strings(c.produces), risk_tier: Number(c.risk_tier ?? 0),
    usable: c.usable !== false,
    ...(str(c.unusable_reason) ? { unusable_reason: str(c.unusable_reason) } : {}),
    approval_forced_manual: Boolean(c.approval_forced_manual),
  };
}

function toBlock(raw: unknown): BlockDef | null {
  const b = asObj(raw);
  const kind = str(b.kind) as BlockDef["kind"];
  if (!PLACEABLE.includes(kind) || b.canvas_required) return null;
  return {
    kind, label: str(b.label), summary: str(b.summary), defaults: asObj(b.defaults),
    configure: list(b.configure).map((f) => {
      const field = asObj(f);
      const key = str(field.key) || str(field.field);
      return { key, label: SETTING_LABELS[key] ?? str(field.label), type: str(field.type) || "text",
               ...SETTING_RULES[key] };
    }).filter((f) => f.key),
  };
}

const CONTROL_KINDS: ControlDef["kind"][] = ["condition", "parallel", "bounded_loop", "for_each"];
const CONTROL_LABELS: Record<ControlDef["kind"], string> = {
  condition: "Branch", parallel: "Do at the same time", bounded_loop: "Repeat until",
  for_each: "For each",
};

/** A branch block the runtime runs. The server marks these `canvas_required` — they need a canvas
 *  that draws branches — and the Studio's lane is one. A join is never offered alone: it comes
 *  with its parallel. */
function toControl(raw: unknown): ControlDef | null {
  const b = asObj(raw);
  const kind = str(b.kind) as ControlDef["kind"];
  if (!CONTROL_KINDS.includes(kind)) return null;
  return { kind, label: str(b.label) || CONTROL_LABELS[kind], summary: str(b.summary) };
}

/**
 * What a step can be and who can run it: capabilities, the project's own steps, the flow
 * blocks a canvas can place, and the roster with what each agent holds.
 *
 * The project's steps come from `view=custom_steps`, which also carries what a step can bind.
 * That read needs the database; when it fails the palette still opens with the built-ins
 * rather than refusing to open at all.
 */
export async function fetchPalette(projectId: string): Promise<Palette> {
  const [capabilities, custom] = await Promise.all([
    getView("capabilities", { project_id: projectId }),
    getView("custom_steps", { project_id: projectId }).catch(() => null),
  ]);
  const bindable = asObj(custom?.bindable);
  const customSteps = custom
    ? list(custom.steps).filter((s) => asObj(s).enabled !== false)
    : list(capabilities.custom_steps);
  return {
    capabilities: list(capabilities.capabilities).map(toCapability).filter((c) => c.key),
    customSteps: customSteps.map(toCustomStep).filter((c) => c.step_key),
    blocks: list(capabilities.blocks).map(toBlock).filter((b): b is BlockDef => b !== null),
    controls: list(capabilities.blocks).map(toControl).filter((c): c is ControlDef => c !== null),
    agents: list(capabilities.agents).map((a): RosterAgent => {
      const agent = asObj(a);
      return { role_key: str(agent.role_key), name: str(agent.name) || str(agent.role_key),
               skills: strings(agent.holds ?? agent.skills), mcp: strings(agent.mcp) };
    }).filter((a) => a.role_key),
    humanRole: str(capabilities.human_role) || "human",
    roleNames: Object.fromEntries(Object.entries(asObj(capabilities.role_names)).map(([key, v]) => {
      const row = asObj(v);
      return [key, { name: str(row.name), callsign: str(row.callsign) }];
    })),
    graphSteps: Object.fromEntries(Object.entries(asObj(capabilities.graph_steps)).map(([key, v]) => {
      const row = asObj(v);
      return [key, { label: str(row.label), summary: str(row.summary) }];
    })),
    riskFloor: Number(capabilities.manual_approval_risk_floor ?? 2),
    bindable: {
      skills: list(bindable.skills).map((s) => {
        const skill = asObj(s);
        const id = str(skill.skill_id) || str(skill.id);
        return { id, name: str(skill.label) || str(skill.name) || id };
      }).filter((s) => s.id),
      mcp_servers: list(bindable.mcp_servers).map((m) => {
        const server = asObj(m);
        return { id: str(server.id), name: str(server.name) || str(server.id),
                 tools: list(server.tools).map((t) => ({ name: str(asObj(t).name) }))
                   .filter((t) => t.name) };
      }).filter((m) => m.id),
    },
  };
}

/* ── Library ─────────────────────────────────────────────────────────────────────── */

/** A library step's icon key, by the same rule as `stepView().icon`: the capability key of a
 *  built-in step, else the step's type. */
function outlineIcon(step: Obj): string {
  if (step.agent_work) return "agent";
  if (step.mine || step.custom_step) return "custom";
  const kind = str(step.kind);
  const byKind: Record<string, string> = {
    decision: "gate", timer: "wait", subworkflow: "subworkflow", parallel: "parallel",
    condition: "condition", join: "join", bounded_loop: "loop", for_each: "foreach",
  };
  if (byKind[kind]) return byKind[kind];
  // An API that predates `capability` on the outline labels a built-in step with its key.
  return str(step.capability) || (kind === "capability" ? str(step.label) : "") || "other";
}

function toSummary(raw: unknown): WorkflowSummary {
  const w = asObj(raw);
  const steps = list(w.steps).map((s) => {
    const step = asObj(s);
    const capability = outlineIcon(step);
    return {
      id: str(step.id),
      title: str(step.title) || str(step.label) || str(step.id),
      ...(capability ? { capability } : {}),
      ...(str(step.captain) ? { captain: str(step.captain) } : {}),
      ...(str(step.approval) ? { approval: str(step.approval) } : {}),
    };
  });
  const lastRun = asObj(w.last_run);
  const version = w.active_version ?? w.version;
  return {
    definition_id: str(w.definition_id) || str(w.id),
    workflow_key: str(w.workflow_key),
    name: str(w.name) || str(w.workflow_key),
    ...(str(w.description) ? { description: str(w.description) } : {}),
    is_catalogue: Boolean(w.is_catalogue),
    version: typeof version === "number" ? version : null,
    step_count: typeof w.step_count === "number" ? w.step_count : steps.length,
    steps,
    forked_from_workflow_key: str(w.forked_from_workflow_key) || null,
    forked_from_name: str(w.forked_from_name) || null,
    run_count: Number(w.run_count ?? 0),
    active_version_id: str(w.active_version_id) || null,
    last_run: str(lastRun.status)
      ? { ...(str(lastRun.id) ? { id: str(lastRun.id) } : {}), status: str(lastRun.status), at: str(lastRun.at) }
      : null,
    ...(w.schedule !== undefined ? { schedule: w.schedule } : {}),
    ...(Array.isArray(w.used_for) ? { used_for: strings(w.used_for) } : {}),
  };
}

/** Every workflow in the project — its own and the standards — with what a card draws. */
export async function fetchLibrary(projectId: string): Promise<WorkflowSummary[]> {
  const body = await getView("library", { project_id: projectId });
  return list(body.items).map(toSummary).filter((w) => w.workflow_key);
}

/* ── One workflow ────────────────────────────────────────────────────────────────── */

/**
 * A workflow's spec (its running version, or `version` read-only) and what the editor needs to
 * know about it. On a standard, `meta.copy` is the operator's existing copy of it, so Save can
 * default to updating that instead of making another.
 */
export async function fetchSpec(projectId: string, key: string, version?: number): Promise<{
  spec: Spec; meta: EditorMeta;
  versions: { version: number; published_at: string; is_active: boolean }[];
}> {
  const body = await getView("spec", {
    project_id: projectId, key, version: version ? String(version) : undefined,
  });
  const raw = asObj(body.spec);
  const spec = { ...raw, steps: Array.isArray(raw.steps) ? raw.steps : [] } as Spec;
  const versions = list(body.versions).map((v) => {
    const row = asObj(v);
    return { version: Number(row.version), published_at: str(row.published_at),
             is_active: Boolean(row.is_active) };
  }).filter((v) => Number.isFinite(v.version));
  const loadedVersion = typeof body.version === "number" ? body.version : null;
  const active = versions.find((v) => v.is_active)?.version
    ?? (typeof body.active_version === "number" ? body.active_version : null);
  const isCatalogue = Boolean(body.is_catalogue);
  let copy: EditorMeta["copy"] = null;
  if ("copy" in body) {
    const c = asObj(body.copy);
    copy = str(c.workflow_key)
      ? { definition_id: str(c.definition_id) || str(c.id), workflow_key: str(c.workflow_key),
          name: str(c.name) }
      : null;
  } else if (isCatalogue) {
    // Older API: find the copy in the library, newest first (the list is ordered that way).
    const mine = (await fetchLibrary(projectId).catch(() => []))
      .find((w) => !w.is_catalogue && w.forked_from_workflow_key === key);
    copy = mine ? { definition_id: mine.definition_id, workflow_key: mine.workflow_key,
                    name: mine.name } : null;
  }
  const forked = asObj(body.forked_from);
  return {
    spec,
    versions,
    meta: {
      definition_id: str(body.definition_id) || null,
      workflow_key: str(body.workflow_key) || key,
      name: str(body.name) || key,
      description: str(body.description),
      is_catalogue: isCatalogue,
      from_workflow_key: str(forked.workflow_key) || str(body.forked_from_workflow_key) || null,
      version: loadedVersion,
      copy,
      // A version other than the one that runs opens read-only ("Viewing v2 · Restore").
      readOnlyVersion: version !== undefined && version !== active ? version : null,
    },
  };
}

/** One graph kind a step read or wrote on a run: how many nodes, a few of their names. */
export type TracedKind = { kind: string; count: number; examples: string[]; decision_types: string[] };
/** What one step did on a run, from the graph's own record (`view=context_trace`). */
export type StepRunTrace = { status: string; reads: TracedKind[]; writes: TracedKind[];
  calls: { tool: string; server?: string; count: number }[];
  /** The step records its calls (an agent's turns, or a deterministic step's own record), so a
   *  declared call missing from `calls` was not made; without a record nothing can be said. */
  callsRecorded?: boolean };
/** A run, step by step: what each read and wrote in the Knowledge and Context Graphs. */
export type RunContext = { run_id: string; status: string; started_at: string | null;
  completed_at: string | null; steps: Record<string, StepRunTrace> };

const tracedKinds = (value: unknown): TracedKind[] => list(value).map((raw) => {
  const row = asObj(raw);
  return { kind: str(row.kind), count: Number(row.count ?? 0) || 0, examples: strings(row.examples),
           decision_types: strings(row.decision_types) };
}).filter((k) => k.kind);

/** The last run's steps, each with its latest status and the graph kinds it really read and
 *  wrote — Context Graph rows written for the run, the nodes its output names, its recorded
 *  usage — never its input and output keys (§4.7 "Show the last run"). */
export async function fetchRunContext(projectId: string, runId: string): Promise<RunContext> {
  const body = await getView("context_trace", { project_id: projectId, run_id: runId });
  const steps: Record<string, StepRunTrace> = {};
  for (const [stepId, raw] of Object.entries(asObj(body.steps))) {
    const step = asObj(raw);
    steps[stepId] = {
      status: str(step.status), reads: tracedKinds(step.reads), writes: tracedKinds(step.writes),
      calls: list(step.calls).map((c) => ({ tool: str(asObj(c).tool), server: str(asObj(c).server) || undefined,
                                            count: Number(asObj(c).count ?? 0) || 0 }))
        .filter((c) => c.tool),
      callsRecorded: step.calls_recorded === true,
    };
  }
  return { run_id: str(body.run_id) || runId, status: str(body.status),
           started_at: str(body.started_at) || null, completed_at: str(body.completed_at) || null, steps };
}

/** Every check Save runs, without saving. Writes nothing. */
export async function checkSpec(projectId: string, spec: Spec,
                                definitionId?: string | null): Promise<CheckResult> {
  let body: Obj;
  try {
    body = await send("POST", { action: "check", project_id: projectId, spec,
                                ...(definitionId ? { definition_id: definitionId } : {}) });
  } catch (error) {
    // A 422 from check is still an answer: these are the problems.
    if (error instanceof ProblemsError) {
      return { ok: false, problems: error.problems, summary: emptySummary(spec) };
    }
    throw error;
  }
  const summary = asObj(body.summary);
  const problems = list(body.problems).map(toProblem);
  return {
    ok: body.ok === undefined ? !problems.length : Boolean(body.ok),
    problems,
    summary: {
      steps: typeof summary.steps === "number" ? summary.steps : spec.steps.length,
      agents: strings(summary.agents), approvals: strings(summary.approvals),
      typical_minutes: typeof summary.typical_minutes === "number" ? summary.typical_minutes : null,
      needs: strings(summary.needs),
    },
  };
}

const emptySummary = (spec: Spec): CheckResult["summary"] =>
  ({ steps: spec.steps.length, agents: [], approvals: [], typical_minutes: null, needs: [] });

/**
 * Publish: the next version of your own workflow, or — for a new workflow or a standard — a
 * new definition and its v1 in one transaction (`update_copy_id` publishes onto an existing
 * copy instead). Throws ProblemsError on 422 and NameInUseError on 409.
 */
export async function publishSpec(projectId: string, req: PublishRequest): Promise<PublishResult> {
  const body = await send("POST", { action: "publish", project_id: projectId, ...req });
  if (body.unchanged) return { unchanged: true };
  const definition = asObj(body.definition);
  const version = body.version;
  return {
    definition: { id: str(definition.id), workflow_key: str(definition.workflow_key),
                  name: str(definition.name) },
    version: typeof version === "number" ? version : Number(asObj(version).version ?? 0),
    created: Boolean(body.created),
  };
}

/**
 * Run the SAVED workflow as a test (#512): pinned steps return their pinned output instead of
 * running, steps in `allow_live` run for real under every approval and budget rule, and any
 * other step that could spend is refused by the server. Returns the new run's id.
 */
export async function startTestRun(projectId: string, req: {
  workflow_key: string; version?: number | null; input?: Record<string, unknown>;
  pins: Record<string, Record<string, unknown>>; allow_live: string[];
}): Promise<{ id: string; status: string }> {
  const body = await send("POST", {
    action: "test_run", project_id: projectId, workflow_key: req.workflow_key,
    ...(req.version ? { version: req.version } : {}),
    input: req.input ?? {}, pins: req.pins, allow_live: req.allow_live,
  });
  return { id: str(body.id), status: str(body.status) };
}

/** Name and description change in place: no new version. */
export async function renameWorkflow(projectId: string, definitionId: string,
                                     patch: { name?: string; description?: string }): Promise<void> {
  await send("PATCH", { project_id: projectId, definition_id: definitionId, ...patch });
}

/** Deleted, or retired when it has runs (their history keeps pointing at it). */
export async function deleteWorkflow(projectId: string, definitionId: string):
    Promise<{ outcome: "deleted" | "retired" }> {
  const body = await send("DELETE", null, { project_id: projectId, definition_id: definitionId });
  return { outcome: body.outcome === "retired" ? "retired" : "deleted" };
}

/** Steps drafted from a sentence. Writes nothing: they land on the canvas to be reviewed. */
export async function draftFromDescription(projectId: string, description: string):
    Promise<{ name: string; spec: Spec }> {
  const body = await send("POST", { action: "draft", project_id: projectId, description });
  const spec = asObj(body.spec);
  return { name: str(body.name), spec: { ...spec, steps: list(spec.steps) } as Spec };
}
