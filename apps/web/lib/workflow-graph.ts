/**
 * A workflow spec as a graph: its edges, its layout, and the edits a canvas may make to it.
 *
 * THE SPEC IS THE STATE. Every function here takes a spec and returns a spec (or a fact about
 * one). Nothing is projected into a canvas model and rebuilt on save, which is how the old
 * builder flattened a parallel review into a straight line: it rewrote every step's `next` from
 * the order the steps were drawn in, whatever the spec said.
 *
 * THE EDGES ARE THE RUNTIME'S (engine/boss_engine/services/workflows.py `_targets` and
 * `_process_control_step`), each tagged with what it means on screen:
 *   - `next`    a step's own successors;
 *   - `yes`/`no` a condition's `on_true` / `on_false`;
 *   - `branch`  a parallel step's `branches` (the runtime starts them all at once);
 *   - `join`    a step a join `wait_for`s, into that join (its `next` to the join is the same
 *               edge, drawn once);
 *   - `loop`    a bounded loop into its `body` (the body returns to the loop by itself).
 * A parallel step's `join` field is a reference, not an edge: the runtime never follows it.
 *
 * EDITS KEEP EVERY BRANCH WHOLE. Moving and duplicating rewrite `next` only where one step hands
 * to exactly one other through a plain `next`. Inserting and removing may also work INSIDE a
 * branch: a step placed at the head of a path (a condition's yes/no, a parallel's branch) takes
 * that reference over; one placed after a path's last step goes between it and where the path
 * meets the others (a join's `wait_for` follows). Every path keeps at least one step of its own,
 * and a loop's body stays one step. Anything else is refused by returning the SAME spec object,
 * so callers can test `result === spec`. Whole branch blocks are placed and removed by
 * lib/workflow-blocks.ts. One arrow at a time can be pointed at another step (`rewireLink`):
 * the one field that draws it is rewritten, and the validator judges the flow that results.
 *
 * INDEXES are positions in `linearOrder(spec)`. `insertStep(spec, at, …)` puts the new step at
 * index `at` (so `at` is the gap before the step now at `at`; -1 or `length` is the end).
 * `moveStep(spec, id, to)` leaves the step at index `to` — array-move semantics, so Alt+→ is
 * `to = index + 1`, and a drop on gap `g` of the unmoved order is `to = g > index ? g - 1 : g`.
 * In a branched flow a path's last step has a gap AFTER it too (`afterGap(index)`, -2, -3, …),
 * because the gap before the next step in the order is the head of another path.
 */
import type {
  FlowLayout, FlowLink, GraphEdge, InputSource, LaidOutNode, Palette, Spec, SpecStep,
} from "./workflow-editor-types";

type Obj = Record<string, unknown>;

const asObj = (value: unknown): Obj =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Obj) : {};

/** A step's `next`, whichever of the two shapes it was written in. */
export function nextOf(step: Obj): string[] {
  const next = step.next;
  if (typeof next === "string") return next ? [next] : [];
  return Array.isArray(next) ? next.map(String).filter(Boolean) : [];
}

/** `next` written back in the shape it was loaded in: a string stays a string. */
function withNext(step: SpecStep, targets: string[]): SpecStep {
  if (typeof step.next === "string" && targets.length <= 1) {
    return { ...step, next: targets[0] ?? "" };
  }
  return { ...step, next: targets };
}

/** `step` with every reference to `from` (its `next`, a condition's paths, a parallel's
 *  branches, a loop's body) pointed at `to` — or, for `next`, dropped when `to` is null. */
function retarget(step: SpecStep, from: string, to: string | null): SpecStep {
  let out: SpecStep = step;
  const next = nextOf(step);
  if (next.includes(from)) {
    const targets = to === null ? next.filter((t) => t !== from)
      : [...new Set(next.map((t) => (t === from ? to : t)))];
    out = withNext(out, targets);
  }
  for (const key of ["on_true", "on_false", "body"]) {
    if (step[key] === from && to !== null) out = { ...out, [key]: to };
  }
  if (Array.isArray(step.branches) && step.branches.map(String).includes(from) && to !== null) {
    out = { ...out, branches: step.branches.map((b) => (String(b) === from ? to : b)) };
  }
  return out;
}

/** The kinds that repeat ONE step (their `body`) and take it back when it is done: a Repeat
 *  (`bounded_loop`) and a For each (`for_each`, once per item). Drawn, laid out and edited alike. */
export const REPEATS = new Set(["bounded_loop", "for_each"]);

/** The steps a Repeat or For each runs, in order. One step (`body: "x"`) as the canvas makes
 *  it, or a list (`body: ["a", "b", "c"]`, a loop over three stages, as the DQ RCA workflow's
 *  Evidence loop): the runtime runs a list in order and returns to the loop after the last. */
export function bodiesOf(step: SpecStep): string[] {
  if (!REPEATS.has(step.kind)) return [];
  const body = step.body;
  return (Array.isArray(body) ? body : body ? [body] : []).map(String).filter(Boolean);
}

/** Every step some Repeat or For each repeats. Its `next` is never followed: it returns to the
 *  step that repeats it. */
export function loopBodies(spec: Spec): Set<string> {
  return new Set(stepsOf(spec).flatMap(bodiesOf));
}

const BRANCH_KINDS = new Set(["parallel", "condition", "join", "bounded_loop", "for_each"]);
const BRANCH_KEYS = ["branches", "wait_for", "on_true", "on_false", "body"];

/** A step that is (or carries) branch structure: only JSON may add or copy one. */
function isBranchStructure(step: SpecStep): boolean {
  return BRANCH_KINDS.has(step.kind) || BRANCH_KEYS.some((key) => key in step);
}

const stepsOf = (spec: Spec): SpecStep[] => (Array.isArray(spec?.steps) ? spec.steps : []);

/* ── Edges ───────────────────────────────────────────────────────────────────────── */

export function edgesOf(spec: Spec): GraphEdge[] {
  const steps = stepsOf(spec);
  const ids = new Set(steps.map((s) => s.id));
  // Every step a join waits for, so the `next` into that join is drawn once, as `join`.
  const waitedBy = new Map<string, Set<string>>();
  for (const step of steps) {
    if (step.kind !== "join" || !Array.isArray(step.wait_for)) continue;
    for (const member of step.wait_for.map(String)) {
      if (!waitedBy.has(member)) waitedBy.set(member, new Set());
      waitedBy.get(member)!.add(step.id);
    }
  }
  const edges: GraphEdge[] = [];
  const seen = new Set<string>();
  const add = (from: string, to: string, kind: GraphEdge["kind"]) => {
    if (!to || !ids.has(to)) return;
    const key = `${from}>${to}`;
    if (seen.has(key)) return;
    seen.add(key);
    edges.push({ from, to, kind });
  };
  for (const step of steps) {
    const id = step.id;
    if (step.kind === "condition") {
      add(id, String(step.on_true ?? ""), "yes");
      add(id, String(step.on_false ?? ""), "no");
    }
    if (step.kind === "parallel" && Array.isArray(step.branches)) {
      for (const branch of step.branches) add(id, String(branch), "branch");
    }
    // A loop over several steps goes into the first and through them in order: drawn as the
    // chain the runtime runs, not as steps nothing leads to (which all stacked in the first
    // column — the "narrow vertical strip" of a 21-step run, 2026-11-01).
    const members = bodiesOf(step);
    if (members.length) add(id, members[0], "loop");
    for (let i = 1; i < members.length; i += 1) add(members[i - 1], members[i], "next");
    for (const target of nextOf(step)) {
      add(id, target, waitedBy.get(id)?.has(target) ? "join" : "next");
    }
    for (const join of waitedBy.get(id) ?? []) add(id, join, "join");
  }
  return edges;
}

/** The step a step's `on_error` names, when it names another step of the spec (not itself). */
export function errorTargetOf(spec: Spec, step: SpecStep): string | null {
  const to = typeof step.on_error === "string" ? step.on_error : "";
  return to && to !== step.id && stepsOf(spec).some((s) => s.id === to) ? to : null;
}

/**
 * Every step's error path (`on_error`): where the run goes when that step fails for good. Not
 * part of `edgesOf`, because nothing the canvas edits by order (insert, move, remove, the `+`
 * gaps) may follow it: a step with an error path still sits in its straight stretch. It is laid
 * out and drawn (`layoutFlow`), and pointed elsewhere like any arrow (`rewireLink`).
 */
export function errorEdgesOf(spec: Spec): GraphEdge[] {
  const out: GraphEdge[] = [];
  for (const step of stepsOf(spec)) {
    const to = errorTargetOf(spec, step);
    if (to) out.push({ from: step.id, to, kind: "error" });
  }
  return out;
}

/** The spec with every `on_error` that names a step it no longer has unset ("Stop the run"). */
export function pruneErrorPaths(spec: Spec): Spec {
  const ids = new Set(stepsOf(spec).map((s) => s.id));
  if (!stepsOf(spec).some((s) => typeof s.on_error === "string" && s.on_error && !ids.has(s.on_error))) {
    return spec;
  }
  return { ...spec, steps: stepsOf(spec).map((s) => {
    if (!(typeof s.on_error === "string" && s.on_error && !ids.has(s.on_error))) return s;
    const { on_error: _gone, ...rest } = s;
    return rest as SpecStep;
  }) };
}

/** Every step has at most one `next`, nothing branches, and following `next` from the entry
 *  visits every step exactly once. Only then may `next` be rewritten from order alone. */
export function isLinear(spec: Spec): boolean {
  const steps = stepsOf(spec);
  if (!steps.length) return true;
  for (const step of steps) {
    if (BRANCH_KINDS.has(step.kind)) return false;
    if (BRANCH_KEYS.some((key) => key in step)) return false;
    if (nextOf(step).length > 1) return false;
  }
  const byId = new Map(steps.map((s) => [s.id, s]));
  if (byId.size !== steps.length) return false;
  const visited = new Set<string>();
  let cursor: string | undefined = spec.entry_step ?? steps[0].id;
  while (cursor && byId.has(cursor) && !visited.has(cursor)) {
    visited.add(cursor);
    cursor = nextOf(byId.get(cursor)!)[0];
  }
  return visited.size === steps.length && !cursor;
}

/* ── Order and layout ────────────────────────────────────────────────────────────── */

function adjacency(spec: Spec, edges = edgesOf(spec)) {
  const out = new Map<string, GraphEdge[]>();
  const into = new Map<string, GraphEdge[]>();
  for (const step of stepsOf(spec)) { out.set(step.id, []); into.set(step.id, []); }
  for (const edge of edges) {
    out.get(edge.from)?.push(edge);
    into.get(edge.to)?.push(edge);
  }
  return { out, into };
}

/**
 * The order index-based edits use: every linear stretch is contiguous, and each branch is
 * finished before the next one starts (P, A1, A2, B1, J — never P, A1, B1, A2, J).
 *
 * Reverse postorder of a depth-first walk from the entry, visiting children last-first. On an
 * acyclic graph that is a topological order, and a step with one successor that has no other
 * predecessor is immediately followed by it. Steps the entry cannot reach follow, in spec
 * order; a cycle (only possible in hand-written JSON) cannot hang it.
 */
export function linearOrder(spec: Spec): string[] {
  const steps = stepsOf(spec);
  if (!steps.length) return [];
  const { out, into } = adjacency(spec);
  const visited = new Set<string>();
  const order: string[] = [];
  /** One walk's reverse postorder, appended — so separate walks keep the order they ran in. */
  const walk = (start: string) => {
    const post: string[] = [];
    // Iterative, so a long hand-written chain cannot overflow the stack.
    const stack: { id: string; children: string[] }[] = [];
    const enter = (id: string) => {
      visited.add(id);
      stack.push({ id, children: (out.get(id) ?? []).map((e) => e.to) });
    };
    enter(start);
    while (stack.length) {
      const top = stack[stack.length - 1];
      const child = top.children.pop();
      if (child === undefined) { post.push(top.id); stack.pop(); continue; }
      if (!visited.has(child)) enter(child);
    }
    order.push(...post.reverse());
  };
  const entry = spec.entry_step && out.has(spec.entry_step) ? spec.entry_step : steps[0].id;
  walk(entry);
  // Unreached steps: sources first, so a stray chain still reads front to back.
  for (const step of steps) {
    if (!visited.has(step.id) && !(into.get(step.id) ?? []).length) walk(step.id);
  }
  for (const step of steps) if (!visited.has(step.id)) walk(step.id);
  return order;
}

/** Columns by longest path from the entry over every edge; siblings stacked in flow order. */
export function layoutFlow(spec: Spec): FlowLayout {
  const steps = stepsOf(spec);
  const structural = edgesOf(spec);
  const order = linearOrder(spec);
  const position = new Map(order.map((id, i) => [id, i]));
  // An error path places the step it leads to after the step that fails, as a branch's path is
  // placed after its Branch — unless it leads back (a retry from an earlier step), when it is
  // drawn as a way back and does not move anything.
  const errors = errorEdgesOf(spec);
  const forward = forwardErrorEdges(spec, structural, errors);
  const edges = [...structural, ...errors];
  const { out, into } = adjacency(spec, [...structural, ...forward]);
  const ends = adjacency(spec, structural).out;

  // Kahn over the edges; whatever a cycle leaves behind goes after everything else.
  const column = new Map<string, number>();
  const pending = new Map(steps.map((s) => [s.id, (into.get(s.id) ?? []).length]));
  const ready = order.filter((id) => pending.get(id) === 0);
  for (const id of ready) column.set(id, 0);
  while (ready.length) {
    const id = ready.shift()!;
    for (const edge of out.get(id) ?? []) {
      column.set(edge.to, Math.max(column.get(edge.to) ?? 0, (column.get(id) ?? 0) + 1));
      const left = (pending.get(edge.to) ?? 0) - 1;
      pending.set(edge.to, left);
      if (left === 0) ready.push(edge.to);
    }
  }
  let last = Math.max(-1, ...column.values());
  for (const id of order) if (!column.has(id) || (pending.get(id) ?? 0) > 0) column.set(id, ++last);

  // A STEP NOTHING LEADS TO AND THAT LEADS NOWHERE (a shared service, a side workflow's stage)
  // is not the start of the flow. Kahn put every one of them in the first column, under the
  // entry, so a workflow with three of them began with a tower. They go along the bottom
  // instead, one per column from the second, after the flow's own steps there.
  const entry = String(spec.entry_step ?? order[0] ?? "");
  const linked = new Set(edges.flatMap((e) => [e.from, e.to]));
  const isolated = linked.size ? order.filter((id) => id !== entry && !linked.has(id)) : [];
  isolated.forEach((id, i) => column.set(id, 1 + i));

  // A loop's body goes to the BOTTOM of its column: its way back to the loop runs along the
  // bottom of the lane, and from any higher row it would cross the step under it.
  const bodies = loopBodies(spec);
  for (const e of edges) if (e.kind === "loop") bodies.add(e.to);
  const alone = new Set(isolated);
  const byColumn = new Map<number, string[]>();
  for (const id of [...order.filter((x) => !bodies.has(x) && !alone.has(x)),
                    ...order.filter((x) => bodies.has(x)), ...isolated]) {
    const c = column.get(id)!;
    if (!byColumn.has(c)) byColumn.set(c, []);
    byColumn.get(c)!.push(id);
  }
  const nodes: LaidOutNode[] = [];
  for (const id of order) {
    const siblings = byColumn.get(column.get(id)!)!;
    nodes.push({ id, column: column.get(id)!, row: siblings.indexOf(id),
                 rowsInColumn: siblings.length });
  }
  nodes.sort((a, b) => a.column - b.column || a.row - b.row
    || (position.get(a.id)! - position.get(b.id)!));
  return {
    nodes,
    edges,
    columns: byColumn.size ? Math.max(...byColumn.keys()) + 1 : 0,
    maxRows: Math.max(0, ...[...byColumn.values()].map((ids) => ids.length)),
    linear: isLinear(spec),
    // A loop body has no edge out, but it returns to its loop: it is not where the flow ends.
    // A step whose only way out is its error path still ends the flow when it succeeds.
    // Nor is a step off the flow altogether (a shared service): no arrow to End from it.
    terminal: order.filter((id) => !(ends.get(id) ?? []).length && !bodies.has(id) && !alone.has(id)),
  };
}

/** The error edges that lead on, never back: each one kept only when its target cannot already
 *  reach the step it leaves (over the structure and the error edges kept before it). */
function forwardErrorEdges(spec: Spec, structural: GraphEdge[], errors: GraphEdge[]): GraphEdge[] {
  if (!errors.length) return [];
  const next = new Map<string, string[]>();
  const link = (e: GraphEdge) => { if (!next.has(e.from)) next.set(e.from, []); next.get(e.from)!.push(e.to); };
  structural.forEach(link);
  // A loop body returns to its loop: a way back the edges do not name.
  for (const step of stepsOf(spec)) {
    const members = bodiesOf(step);
    if (members.length) link({ from: members[members.length - 1], to: step.id, kind: "next" });
  }
  const reaches = (from: string, to: string) => {
    const seen = new Set([from]);
    const stack = [from];
    while (stack.length) {
      const id = stack.pop()!;
      if (id === to) return true;
      for (const t of next.get(id) ?? []) if (!seen.has(t)) { seen.add(t); stack.push(t); }
    }
    return false;
  };
  const kept: GraphEdge[] = [];
  for (const e of errors) {
    if (reaches(e.to, e.from)) continue;
    kept.push(e);
    link(e);
  }
  return kept;
}

/** Every step that runs before `id` on some path into it, in flow order. */
export function upstreamOf(spec: Spec, id: string): string[] {
  const { into } = adjacency(spec);
  const found = new Set<string>();
  const stack = [id];
  while (stack.length) {
    for (const edge of into.get(stack.pop()!) ?? []) {
      if (!found.has(edge.from) && edge.from !== id) {
        found.add(edge.from);
        stack.push(edge.from);
      }
    }
  }
  return linearOrder(spec).filter((s) => found.has(s));
}

/* ── Rename ──────────────────────────────────────────────────────────────────────── */

/**
 * An expression (a condition's or a bounded loop's) with every `path` that reads `steps.<from>`
 * pointed at `steps.<to>`, through `all`/`any`/`not`. The runtime looks the path up in the run's
 * payload (workflows.py `_lookup_path`, which drops a leading `$`/`.`), so a typed decision or a
 * loop's exit test that still named the old id would silently never match. The same object when
 * nothing reads `from`.
 */
function renameInExpression(expression: unknown, from: string, to: string): unknown {
  if (Array.isArray(expression)) {
    const items = expression.map((item) => renameInExpression(item, from, to));
    return items.some((item, i) => item !== expression[i]) ? items : expression;
  }
  if (!expression || typeof expression !== "object") return expression;
  const node = expression as Obj;
  let out: Obj | null = null;
  for (const key of ["all", "any", "not"]) {
    if (!(key in node)) continue;
    const renamed = renameInExpression(node[key], from, to);
    if (renamed !== node[key]) out = { ...(out ?? node), [key]: renamed };
  }
  if (typeof node.path === "string") {
    const match = /^([$.]*)steps\.([^.]+)(\..*)?$/.exec(node.path);
    if (match && match[2] === from) {
      out = { ...(out ?? node), path: `${match[1]}steps.${to}${match[3] ?? ""}` };
    }
  }
  return out ?? expression;
}

/** A step id renamed everywhere a spec can name it. Refused (same spec) when `to` is taken. */
export function renameStepId(spec: Spec, from: string, to: string): Spec {
  const steps = stepsOf(spec);
  if (from === to || !to || !steps.some((s) => s.id === from) || steps.some((s) => s.id === to)) {
    return spec;
  }
  const swap = (value: unknown) => (value === from ? to : value);
  const swapList = (value: unknown) => (Array.isArray(value) ? value.map(swap) : value);
  const renamed = steps.map((raw) => {
    const step: SpecStep = { ...raw, id: swap(raw.id) as string };
    if (typeof step.next === "string") step.next = swap(step.next) as string;
    else if (Array.isArray(step.next)) step.next = swapList(step.next) as string[];
    for (const key of ["on_true", "on_false", "join", "body", "on_error"]) {
      if (key in step) step[key] = swap(step[key]);
    }
    for (const key of ["branches", "wait_for"]) {
      if (Array.isArray(step[key])) step[key] = swapList(step[key]);
    }
    if ("expression" in step) step.expression = renameInExpression(step.expression, from, to);
    const binding = asObj(step.binding);
    const inputs = asObj(binding.inputs);
    if (Object.values(inputs).some((v) => asObj(v).from_step === from)) {
      step.binding = {
        ...binding,
        inputs: Object.fromEntries(Object.entries(inputs).map(([name, value]) => [
          name, asObj(value).from_step === from ? { ...asObj(value), from_step: to } : value,
        ])),
      };
    }
    return step;
  });
  return { ...spec, steps: renamed, ...(spec.entry_step === from ? { entry_step: to } : {}) };
}

/* ── Wiring ──────────────────────────────────────────────────────────────────────── */

const isSet = (value: unknown) => value !== undefined && value !== null && value !== "";

/** What a step reads and makes, by what it binds. */
export function contractOf(step: SpecStep, palette: Palette): { required: string[];
  optional: string[]; produces: string[] } {
  const binding = asObj(step.binding);
  if (step.kind !== "capability") return { required: [], optional: [], produces: [] };
  if (binding.agent_work) {
    return { required: ["instructions"], optional: [],
             produces: Array.isArray(binding.produces) ? binding.produces.map(String) : [] };
  }
  if (binding.custom_step) {
    const def = palette.customSteps.find((c) => c.step_key === binding.custom_step);
    return {
      required: (def?.inputs ?? []).filter((i) => i.required).map((i) => i.name),
      optional: (def?.inputs ?? []).filter((i) => !i.required).map((i) => i.name),
      produces: def?.produces ?? [],
    };
  }
  const cap = palette.capabilities.find((c) => c.key === binding.capability);
  return { required: cap?.required_inputs ?? [], optional: cap?.optional_inputs ?? [],
           produces: cap?.produces ?? [] };
}

/** The names a start form declares, in either of the two shapes input_schema is written in. */
export function declaredInputs(spec: Spec): string[] {
  const schema = asObj(spec.input_schema);
  if ("properties" in schema || "required" in schema) {
    const names = Object.keys(asObj(schema.properties));
    for (const name of Array.isArray(schema.required) ? schema.required.map(String) : []) {
      if (!names.includes(name)) names.push(name);
    }
    return names;
  }
  return Object.keys(schema);
}

/**
 * Point each unset required input at the nearest earlier step that makes it, else at the start
 * form when the start form declares it, else leave it unset — the rule of builtin.py
 * `_wire_inputs`. Leaving it unset is deliberate: wiring every unmatched name `from_input`
 * would publish a workflow that dies at run time on a value nobody supplies.
 *
 * "Earlier" is the real graph (`upstreamOf`), not list position, so a branch never reads from
 * its sibling. Agent tasks and custom steps count as producers too.
 */
export function wireInputs(spec: Spec, palette: Palette, ids?: string[]):
    { spec: Spec; wired: { step_id: string; input: string; source: InputSource }[] } {
  const steps = stepsOf(spec);
  const declared = new Set(declaredInputs(spec));
  const byId = new Map(steps.map((s) => [s.id, s]));
  const wanted = ids ? new Set(ids) : null;
  const wired: { step_id: string; input: string; source: InputSource }[] = [];
  const replaced = new Map<string, SpecStep>();
  for (const id of linearOrder(spec)) {
    if (wanted && !wanted.has(id)) continue;
    const step = byId.get(id)!;
    const { required } = contractOf(step, palette);
    const binding = asObj(step.binding);
    const inputs = asObj(binding.inputs);
    const additions: Obj = {};
    const upstream = upstreamOf(spec, id).reverse(); // nearest first
    for (const name of required) {
      if (isSet(inputs[name])) continue;
      const producer = upstream.find((u) => contractOf(byId.get(u)!, palette).produces.includes(name));
      const source: InputSource | null = producer ? { from_step: producer, field: name }
        : declared.has(name) ? { from_input: name } : null;
      if (!source) continue;
      additions[name] = source;
      wired.push({ step_id: id, input: name, source });
    }
    if (Object.keys(additions).length) {
      replaced.set(id, { ...step, binding: { ...binding, inputs: { ...inputs, ...additions } } });
    }
  }
  if (!replaced.size) return { spec, wired };
  return { spec: { ...spec, steps: steps.map((s) => replaced.get(s.id) ?? s) }, wired };
}

/* ── Structural edits (linear stretches only) ────────────────────────────────────── */

/** Whether `id` sits in a linear stretch: reached only through plain `next`s that name nothing
 *  else, leaving through at most one plain `next`, and not itself a branch construct. */
function detachable(spec: Spec, id: string, edges = edgesOf(spec)): boolean {
  const steps = stepsOf(spec);
  const step = steps.find((s) => s.id === id);
  if (!step || BRANCH_KINDS.has(step.kind)) return false;
  const byId = new Map(steps.map((s) => [s.id, s]));
  const incoming = edges.filter((e) => e.to === id);
  const outgoing = edges.filter((e) => e.from === id);
  if (incoming.some((e) => e.kind !== "next")) return false;
  if (incoming.some((e) => nextOf(byId.get(e.from)!).length !== 1)) return false;
  if (outgoing.length > 1 || outgoing.some((e) => e.kind !== "next")) return false;
  // The successor must not be a merge point other steps also reach, or reconnecting would
  // give it a second way in that the author did not draw. Several predecessors of THIS step
  // merging into it is fine: each is pointed at the successor instead.
  const successor = outgoing[0]?.to;
  if (successor && edges.some((e) => e.to === successor && e.from !== id)) return false;
  return true;
}

/** The spec without `id`, its predecessors pointed at its successor. Null when refused. */
function detach(spec: Spec, id: string): { spec: Spec; step: SpecStep } | null {
  if (!detachable(spec, id)) return null;
  const steps = stepsOf(spec);
  const step = steps.find((s) => s.id === id)!;
  const successor = nextOf(step);
  const rest = steps.filter((s) => s.id !== id).map((s) =>
    nextOf(s).includes(id) ? withNext(s, successor) : s);
  const out: Spec = { ...spec, steps: rest };
  if (spec.entry_step === id || (!spec.entry_step && steps[0]?.id === id)) {
    const entry = successor[0] ?? rest[0]?.id;
    if (entry) out.entry_step = entry;
    else delete out.entry_step;
  }
  return { spec: out, step };
}

/**
 * `step` placed at index `at` of `linearOrder(spec)` (-1 = the end), spliced into the `next`
 * chain. Refused (same spec) unless the gap is in a linear stretch: before the entry, after the
 * single last step, or between a step whose only `next` is its neighbour and a neighbour with no
 * other way in. Refused too for a step that is itself branch structure (a parallel, condition,
 * join or loop, or anything carrying their keys): placing one would draw a branch, and branch
 * structure is edited in JSON.
 */
export function insertStep(spec: Spec, at: number, step: SpecStep): Spec {
  const steps = stepsOf(spec);
  if (!step?.id || steps.some((s) => s.id === step.id) || isBranchStructure(step)) return spec;
  const order = linearOrder(spec);
  const after = afterIndex(at);
  if (after !== null) return after < order.length ? insertAfter(spec, order[after], step) : spec;
  const linear = insertLinear(spec, at, step);
  if (linear !== spec) return linear;
  // Inside a branch: at the head of a path, or after the flow's one end.
  if (at >= 0 && at < order.length) return insertBefore(spec, order[at], step);
  const end = soleEnd(spec);
  return end ? insertAfter(spec, end, step) : spec;
}

/** The gap code for "after the step at `index` of linearOrder": -2, -3, … (-1 is the end). */
export const afterGap = (index: number): number => -(index + 2);
/** The index an after-gap follows, or null when `at` is any other gap. */
export const afterIndex = (at: number): number | null => (at <= -2 ? -at - 2 : null);

/** The one step the flow ends at (no way out but a loop's own body), when there is exactly one. */
function soleEnd(spec: Spec): string | null {
  const edges = edgesOf(spec);
  const bodies = loopBodies(spec);
  const ends = stepsOf(spec).filter((s) => !bodies.has(s.id)
    && !edges.some((e) => e.from === s.id && e.kind !== "loop"));
  return ends.length === 1 ? ends[0].id : null;
}

/** `placed` put into the step list just before `beforeId` (or at the end). */
function spliceBefore(steps: SpecStep[], beforeId: string | null, placed: SpecStep): SpecStep[] {
  const at = beforeId ? steps.findIndex((s) => s.id === beforeId) : -1;
  return at < 0 ? [...steps, placed] : [...steps.slice(0, at), placed, ...steps.slice(at)];
}

/** `step` on the one edge into `succId` — the head of a path, or any step with one way in. */
function insertBefore(spec: Spec, succId: string, step: SpecStep): Spec {
  const into = edgesOf(spec).filter((e) => e.to === succId);
  if (into.length !== 1 || into[0].kind === "loop" || into[0].kind === "join") return spec;
  const from = stepsOf(spec).find((s) => s.id === into[0].from)!;
  // A condition, a parallel and a loop body never follow their own `next`.
  if (into[0].kind === "next"
      && (from.kind === "condition" || from.kind === "parallel" || loopBodies(spec).has(from.id))) {
    return spec;
  }
  const placed = withNext({ ...step }, [succId]);
  const steps = stepsOf(spec).map((s) => (s.id === from.id ? retarget(s, succId, step.id) : s));
  return { ...spec, steps: spliceBefore(steps, succId, placed) };
}

/** `step` after `id`: between it and where its path goes next (a join's `wait_for` follows),
 *  or after it when it is an end. Never after a condition, a parallel or a loop body. */
function insertAfter(spec: Spec, id: string, step: SpecStep): Spec {
  const steps = stepsOf(spec);
  const x = steps.find((s) => s.id === id);
  if (!x || x.kind === "condition" || x.kind === "parallel" || loopBodies(spec).has(id)) return spec;
  const outs = edgesOf(spec).filter((e) => e.from === id && e.kind !== "loop");
  if (outs.length > 1 || nextOf(x).length > outs.length) return spec;
  const out = outs[0];
  if (out && !nextOf(x).includes(out.to)) return spec;
  const placed = withNext({ ...step }, out ? [out.to] : []);
  const rewired = steps.map((s) => {
    if (s.id === id) return out ? retarget(s, out.to, step.id) : withNext(s, [step.id]);
    if (out?.kind === "join" && s.id === out.to && Array.isArray(s.wait_for)) {
      return { ...s, wait_for: s.wait_for.map((w) => (String(w) === id ? step.id : w)) };
    }
    return s;
  });
  const index = rewired.findIndex((s) => s.id === id);
  return { ...spec, steps: [...rewired.slice(0, index + 1), placed, ...rewired.slice(index + 1)] };
}

/**
 * The after-gaps a branched flow draws: after each step whose next place is not already a gap of
 * its own — a path's last step before the paths meet or join, and each end of a flow with
 * several. The flow's one end is the end gap (-1), never an after-gap.
 */
export function afterGaps(spec: Spec): number[] {
  if (isLinear(spec)) return [];
  const order = linearOrder(spec);
  const edges = edgesOf(spec);
  const end = insertStep(spec, -1, PROBE_STEP) !== spec ? soleEnd(spec) : null;
  const out: number[] = [];
  order.forEach((id, index) => {
    if (id === end) return;
    const outs = edges.filter((e) => e.from === id && e.kind !== "loop");
    if (outs.length > 1) return;
    if (outs.length === 1 && outs[0].kind === "next"
        && edges.filter((e) => e.to === outs[0].to).length === 1) return;
    if (insertAfter(spec, id, PROBE_STEP) !== spec) out.push(afterGap(index));
  });
  return out;
}

const PROBE_STEP: SpecStep = { id: "__wf_gap_probe__", kind: "capability", binding: {} };

/** The old rule, kept for moves and copies: a gap in a straight stretch only. */
function insertLinear(spec: Spec, at: number, step: SpecStep): Spec {
  const steps = stepsOf(spec);
  const order = linearOrder(spec);
  const index = at < 0 || at > order.length ? order.length : at;
  const byId = new Map(steps.map((s) => [s.id, s]));
  const edges = edgesOf(spec);
  const prev = index > 0 ? byId.get(order[index - 1]) : undefined;
  const succ = index < order.length ? byId.get(order[index]) : undefined;

  if (!steps.length) {
    return { ...spec, entry_step: step.id, steps: [withNext({ ...step }, [])] };
  }
  let placed: SpecStep;
  let rewired: SpecStep | undefined;
  if (!prev && succ) {
    // Before the entry: the new step becomes the entry. Only when the gap really is the front.
    const entry = spec.entry_step ?? steps[0].id;
    if (succ.id !== entry) return spec;
    placed = withNext({ ...step }, [succ.id]);
  } else if (prev && !succ) {
    // After the end: only when the flow has exactly one end, and this is it.
    // A loop body has no edge out but returns to its loop: it is not an end.
    const loopBodies = new Set(edges.filter((e) => e.kind === "loop").map((e) => e.to));
    const ends = steps.filter((s) => !loopBodies.has(s.id) && !edges.some((e) => e.from === s.id));
    if (ends.length !== 1 || ends[0].id !== prev.id || BRANCH_KINDS.has(prev.kind)) return spec;
    placed = withNext({ ...step }, []);
    rewired = withNext(prev, [step.id]);
  } else if (prev && succ) {
    const between = edges.filter((e) => e.to === succ.id);
    if (nextOf(prev).length !== 1 || nextOf(prev)[0] !== succ.id) return spec;
    if (between.length !== 1 || between[0].from !== prev.id || between[0].kind !== "next") {
      return spec;
    }
    placed = withNext({ ...step }, [succ.id]);
    rewired = withNext(prev, [step.id]);
  } else {
    return spec;
  }
  // In the step list, beside the step it follows, so the JSON reads in flow order too.
  const out: SpecStep[] = [];
  if (!prev) out.push(placed);
  for (const s of steps) {
    out.push(rewired && s.id === rewired.id ? rewired : s);
    if (prev && s.id === prev.id) out.push(placed);
  }
  return { ...spec, steps: out, ...(!prev ? { entry_step: placed.id } : {}) };
}

/** A copy of `id` under `copyId`, right after it in the flow. Refused (same spec) where
 *  `insertStep` would refuse it: a branch construct, or a step outside a linear stretch. */
export function duplicateStep(spec: Spec, id: string, copyId: string): Spec {
  const original = stepsOf(spec).find((s) => s.id === id);
  if (!original) return spec;
  const copy = { ...structuredClone(original), id: copyId } as SpecStep;
  if (stepsOf(spec).some((s) => s.id === copyId) || isBranchStructure(copy)) return spec;
  return insertLinear(spec, linearOrder(spec).indexOf(id) + 1, copy);
}

/** Whether `duplicateStep` would copy it, so Duplicate can be disabled where it would not. */
export function canDuplicate(spec: Spec, id: string): boolean {
  const ids = new Set(stepsOf(spec).map((s) => s.id));
  let probe = `${id}_copy`;
  while (ids.has(probe)) probe += "_";
  return duplicateStep(spec, id, probe) !== spec;
}

/** The step left at index `to` of the flow. Refused (same spec) across a branch boundary. */
export function moveStep(spec: Spec, id: string, to: number): Spec {
  const order = linearOrder(spec);
  const from = order.indexOf(id);
  if (from < 0 || to < 0 || to >= order.length || to === from) return spec;
  const detached = detach(spec, id);
  if (!detached) return spec;
  const moved = insertLinear(detached.spec, to, detached.step);
  return moved === detached.spec ? spec : moved;
}

export function canMove(spec: Spec, id: string, to: number): boolean {
  return moveStep(spec, id, to) !== spec;
}

/** Whether `removeStep` would remove it: a step in a linear stretch, or one a path can spare. */
export function canRemove(spec: Spec, id: string): boolean {
  return detachable(spec, id) || detachFromPath(spec, id) !== null;
}

/**
 * A step inside a path taken out, the path closed up behind it: what pointed at it points at
 * its successor, and when it was a path's last step before a join, the join waits for the step
 * before it instead. Null — refused — when that would leave a path with no step of its own
 * (a condition's yes/no or a parallel's branch pointed straight at where the paths meet), for a
 * loop's body, for the entry, and for the branch constructs themselves.
 */
function detachFromPath(spec: Spec, id: string): Spec | null {
  const steps = stepsOf(spec);
  const x = steps.find((s) => s.id === id);
  if (!x || BRANCH_KINDS.has(x.kind) || id === (spec.entry_step ?? steps[0]?.id)) return null;
  const edges = edgesOf(spec);
  const into = edges.filter((e) => e.to === id);
  const outs = edges.filter((e) => e.from === id);
  if (!into.length || into.some((e) => e.kind === "loop" || e.kind === "join")) return null;
  if (outs.length > 1 || nextOf(x).length > outs.length) return null;
  const out = outs[0];
  if (out && (!["next", "join"].includes(out.kind) || !nextOf(x).includes(out.to))) return null;
  const successor = out?.to ?? null;
  for (const e of into) {
    if (e.kind !== "yes" && e.kind !== "no" && e.kind !== "branch") continue;
    // The path's head moves on to the successor: only when that is still the path's own step.
    if (!successor || out.kind === "join") return null;
    if (edges.some((f) => f.to === successor && f.from !== id)) return null;
  }
  // A path's last step before a join: the step before it becomes the last, and is waited for.
  if (out?.kind === "join" && (into.length !== 1 || into[0].kind !== "next")) return null;
  const preds = new Set(into.map((e) => e.from));
  const rest = steps.filter((s) => s.id !== id).map((s) => {
    let next = preds.has(s.id) ? retarget(s, id, successor) : s;
    if (out?.kind === "join" && s.id === successor && Array.isArray(s.wait_for)) {
      next = { ...next, wait_for: s.wait_for.map((w) => (String(w) === id ? into[0].from : w)) };
    }
    return next;
  });
  return { ...spec, steps: rest };
}

/** Inputs that read from steps no longer in `spec` are unset, so they show as unwired. */
export function unsetReadsFrom(spec: Spec, gone: Set<string>): Spec {
  const steps = stepsOf(spec).map((s) => {
    const binding = asObj(s.binding);
    const inputs = asObj(binding.inputs);
    const dangling = Object.keys(inputs).filter((k) => gone.has(String(asObj(inputs[k]).from_step)));
    if (!dangling.length) return s;
    const kept = { ...inputs };
    for (const name of dangling) delete kept[name];
    return { ...s, binding: { ...binding, inputs: kept } };
  });
  return { ...spec, steps };
}

/**
 * The spec without `id`, its predecessor pointed at its successor. An input another step read
 * from it is unset rather than left dangling, so it shows as unwired instead of failing a run.
 * Refused (same spec) for a branch construct, and for a step its path cannot spare.
 */
export function removeStep(spec: Spec, id: string): Spec {
  const detached = detach(spec, id)?.spec ?? detachFromPath(spec, id);
  if (!detached) return spec;
  // A step whose error path led to it stops the run instead when it fails.
  return pruneErrorPaths(unsetReadsFrom(detached, new Set([id])));
}

/* ── Re-linking an arrow ─────────────────────────────────────────────────────────── */

/** What the lane passes for a drop on the Start flag. Nothing can lead there. */
export const START_TARGET = "__start__";

/** Every arrow out of `id`: its edges, or its arrow to End when it has none (a loop's body
 *  returns to its loop and draws none). */
export function linksFrom(spec: Spec, id: string): FlowLink[] {
  const edges = edgesOf(spec).filter((e) => e.from === id);
  const errors: FlowLink[] = errorEdgesOf(spec).filter((e) => e.from === id)
    .map((e) => ({ from: e.from, to: e.to, kind: e.kind }));
  if (edges.length) return [...edges.map((e) => ({ from: e.from, to: e.to, kind: e.kind })), ...errors];
  if (!stepsOf(spec).some((s) => s.id === id) || loopBodies(spec).has(id)) return errors;
  return [{ from: id, to: null, kind: "end" }, ...errors];
}

/** A step's error path as a link: `to` null when it has none (it stops the run when it fails),
 *  so the step settings' "If it still fails, go to…" can set one through `rewireLink` too. */
export function errorLinkOf(spec: Spec, id: string): FlowLink {
  const step = stepsOf(spec).find((s) => s.id === id);
  return { from: id, to: step ? errorTargetOf(spec, step) : null, kind: "error" };
}

/** Refusals of an error path, in the canvas's words. */
export const ERROR_PATH_SELF = "A step cannot go to itself when it fails: pick another step, or Stop the run";
export const ERROR_PATH_START = "Nothing can lead back to Start: it is where the flow begins";

/** `on_error` pointed at `target` (null unsets it: the run stops when the step fails). */
function rewireError(spec: Spec, link: FlowLink, target: string | null): { spec: Spec } | { refused: string } {
  const steps = stepsOf(spec);
  const from = steps.find((s) => s.id === link.from);
  if (!from || errorLinkOf(spec, link.from).to !== link.to) return { refused: "That arrow is not there any more" };
  if (target === link.to) return { spec };
  if (target === START_TARGET) return { refused: ERROR_PATH_START };
  if (target === link.from) return { refused: ERROR_PATH_SELF };
  if (target !== null && !steps.some((s) => s.id === target)) return { refused: "Link it to a step, or Stop the run" };
  if (target !== null && steps.find((s) => s.id === target)!.kind === "join") {
    return { refused: "Only the branches of its Do at the same time lead into a join" };
  }
  const next: SpecStep = { ...from };
  if (target === null) delete next.on_error;
  else next.on_error = target;
  return { spec: { ...spec, steps: steps.map((s) => (s.id === from.id ? next : s)) } };
}

/**
 * The arrow `link` pointed at `target` (a step id; null for End) instead: one rewrite of the field
 * that drew it — a step's `next`, a condition's `on_true`/`on_false`, a parallel's `branches`.
 * The SAME spec when nothing changes (`target` is where it already goes).
 *
 * Refused, with the words the canvas says, when the spec cannot say it the way the canvas would
 * draw it: an arrow that is not there; Start; a target that is not a step; a path of a Branch or
 * a branch of a Do at the same time with no step of its own (End, or straight into its join,
 * as `detachFromPath` keeps them); Yes and No to one step, or two branches to one step (drawn as
 * one arrow); a step that already leads there; an arrow into a join from outside what it waits
 * for, and a waited-for path's arrow into its join (the join's `wait_for` names it); a Repeat's
 * one step (the runtime returns the body to its loop, so it is the block's own). Whether the
 * flow that results is a valid workflow — a cycle outside a Repeat, a step nothing reaches any
 * more — is the engine validator's to say (lib/workflow-rewire.ts asks it), never decided here.
 *
 * An error path (`kind` "error") is the step's `on_error`: pointed at another step, or unset with
 * `target` null (the run stops when the step fails). Its link may have `to` null — a step with no
 * error path yet (`errorLinkOf`). Start and the step itself are refused; whether it makes a cycle
 * is the validator's, like every other arrow.
 */
export function rewireLink(spec: Spec, link: FlowLink, target: string | null):
    { spec: Spec } | { refused: string } {
  if (link.kind === "error") return rewireError(spec, link, target);
  const steps = stepsOf(spec);
  const byId = new Map(steps.map((s) => [s.id, s]));
  const from = byId.get(link.from);
  const current = linksFrom(spec, link.from)
    .some((l) => l.kind === link.kind && l.to === link.to);
  if (!from || !current) return { refused: "That arrow is not there any more" };
  if (target === link.to) return { spec };
  if (target === START_TARGET) return { refused: "Nothing can lead back to Start: it is where the flow begins" };
  if (target !== null && !byId.has(target)) return { refused: "Link it to a step, or to End" };
  const into = target === null ? undefined : byId.get(target)!;
  if (link.kind === "loop" && from.kind === "for_each") {
    // A FOR EACH'S ARROW CAN BE POINTED AT ANOTHER STEP: the one it runs for each item. Only its
    // `body` is rewritten; whether that step can be one (no next of its own, nothing else leading
    // to it) is the validator's to say, like every other arrow.
    if (target === null) return { refused: "A For each needs a step to run for each item" };
    if (target === link.from) return { refused: "A For each cannot run itself for each item" };
    return { spec: { ...spec, steps: steps.map((s) => (s.id === from.id ? { ...s, body: target } : s)) } };
  }
  if (link.kind === "loop") {
    return { refused: "A Repeat keeps its one step: put a step in it instead" };
  }
  if (link.kind === "join") {
    return { refused: "This path ends where its Do at the same time waits: remove the branch to change it" };
  }
  if (into?.kind === "join") {
    return { refused: "Only the branches of its Do at the same time lead into a join" };
  }
  const swap = (step: SpecStep) => ({ ...spec, steps: steps.map((s) => (s.id === step.id ? step : s)) });

  if (link.kind === "yes" || link.kind === "no") {
    if (target === null) return { refused: "Each path of a Branch needs a step to go to" };
    const key = link.kind === "yes" ? "on_true" : "on_false";
    const other = link.kind === "yes" ? "on_false" : "on_true";
    if (String(from[other] ?? "") === target) {
      return { refused: `Yes and No would both go to the same step: link ${link.kind === "yes" ? "No" : "Yes"} elsewhere first` };
    }
    return { spec: swap({ ...from, [key]: target }) };
  }
  if (link.kind === "branch") {
    if (target === null) return { refused: "Each branch needs a step of its own" };
    const branches = Array.isArray(from.branches) ? from.branches.map(String) : [];
    if (branches.includes(target)) return { refused: "That step is already one of its branches" };
    return { spec: swap({ ...from, branches: branches.map((b) => (b === link.to ? target : b)) }) };
  }
  // A plain `next`, or a flow's end.
  const next = nextOf(from);
  if (target !== null && next.includes(target)) return { refused: "It already leads there" };
  if (from.kind === "condition" || from.kind === "parallel") {
    return { refused: "A Branch or a Do at the same time goes on through its paths" };
  }
  const targets = link.kind === "end" ? (target === null ? next : [...next, target])
    : target === null ? next.filter((t) => t !== link.to)
      : next.map((t) => (t === link.to ? target : t));
  return { spec: swap(withNext(from, targets)) };
}
