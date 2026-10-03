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
 * EDITING IS LIMITED TO LINEAR STRETCHES. Inserting, moving and removing rewrite `next` only
 * where one step hands to exactly one other through a plain `next`. Anything that would touch a
 * branch, a join's `wait_for`, a loop body or a condition is refused by returning the SAME spec
 * object, so callers can test `result === spec`. Branch structure is edited in JSON.
 *
 * INDEXES are positions in `linearOrder(spec)`. `insertStep(spec, at, …)` puts the new step at
 * index `at` (so `at` is the gap before the step now at `at`; -1 or `length` is the end).
 * `moveStep(spec, id, to)` leaves the step at index `to` — array-move semantics, so Alt+→ is
 * `to = index + 1`, and a drop on gap `g` of the unmoved order is `to = g > index ? g - 1 : g`.
 */
import type {
  FlowLayout, GraphEdge, InputSource, LaidOutNode, Palette, Spec, SpecStep,
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

const BRANCH_KINDS = new Set(["parallel", "condition", "join", "bounded_loop"]);

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
    if (step.kind === "bounded_loop" && step.body) add(id, String(step.body), "loop");
    for (const target of nextOf(step)) {
      add(id, target, waitedBy.get(id)?.has(target) ? "join" : "next");
    }
    for (const join of waitedBy.get(id) ?? []) add(id, join, "join");
  }
  return edges;
}

/** Every step has at most one `next`, nothing branches, and following `next` from the entry
 *  visits every step exactly once. Only then may `next` be rewritten from order alone. */
export function isLinear(spec: Spec): boolean {
  const steps = stepsOf(spec);
  if (!steps.length) return true;
  for (const step of steps) {
    if (BRANCH_KINDS.has(step.kind)) return false;
    if ("wait_for" in step || "branches" in step || "on_true" in step || "on_false" in step
        || "body" in step) return false;
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
  const edges = edgesOf(spec);
  const order = linearOrder(spec);
  const position = new Map(order.map((id, i) => [id, i]));
  const { out, into } = adjacency(spec, edges);

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

  const byColumn = new Map<number, string[]>();
  for (const id of order) {
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
  const loopBodies = new Set(edges.filter((e) => e.kind === "loop").map((e) => e.to));
  return {
    nodes,
    edges,
    columns: byColumn.size ? Math.max(...byColumn.keys()) + 1 : 0,
    maxRows: Math.max(0, ...[...byColumn.values()].map((ids) => ids.length)),
    linear: isLinear(spec),
    // A loop body has no edge out, but it returns to its loop: it is not where the flow ends.
    terminal: order.filter((id) => !(out.get(id) ?? []).length && !loopBodies.has(id)),
  };
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
    for (const key of ["on_true", "on_false", "join", "body"]) {
      if (key in step) step[key] = swap(step[key]);
    }
    for (const key of ["branches", "wait_for"]) {
      if (Array.isArray(step[key])) step[key] = swapList(step[key]);
    }
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
 * other way in.
 */
export function insertStep(spec: Spec, at: number, step: SpecStep): Spec {
  const steps = stepsOf(spec);
  if (!step?.id || steps.some((s) => s.id === step.id)) return spec;
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

/** The step left at index `to` of the flow. Refused (same spec) across a branch boundary. */
export function moveStep(spec: Spec, id: string, to: number): Spec {
  const order = linearOrder(spec);
  const from = order.indexOf(id);
  if (from < 0 || to < 0 || to >= order.length || to === from) return spec;
  const detached = detach(spec, id);
  if (!detached) return spec;
  const moved = insertStep(detached.spec, to, detached.step);
  return moved === detached.spec ? spec : moved;
}

export function canMove(spec: Spec, id: string, to: number): boolean {
  return moveStep(spec, id, to) !== spec;
}

/** Whether `removeStep` would remove it (a step in a linear stretch). */
export function canRemove(spec: Spec, id: string): boolean {
  return detachable(spec, id);
}

/**
 * The spec without `id`, its predecessor pointed at its successor. An input another step read
 * from it is unset rather than left dangling, so it shows as unwired instead of failing a run.
 * Refused (same spec) for a step that is part of a branch structure.
 */
export function removeStep(spec: Spec, id: string): Spec {
  const detached = detach(spec, id);
  if (!detached) return spec;
  const steps = detached.spec.steps.map((s) => {
    const binding = asObj(s.binding);
    const inputs = asObj(binding.inputs);
    const dangling = Object.keys(inputs).filter((k) => asObj(inputs[k]).from_step === id);
    if (!dangling.length) return s;
    const kept = { ...inputs };
    for (const name of dangling) delete kept[name];
    return { ...s, binding: { ...binding, inputs: kept } };
  });
  return { ...detached.spec, steps };
}
