/**
 * The branch blocks a canvas places whole: Branch (if / else), Do at the same time (parallel
 * and its join) and Repeat until (a bounded loop).
 *
 * THE RUNTIME HAS RUN THESE SINCE WFL-001; THE CANVAS COULD ONLY DRAW THEM. Each block is
 * placed as one edit, already shaped the way the engine's validator and runner read it
 * (engine/boss_engine/services/workflows.py `normalise_workflow_spec`, `_process_control_step`):
 *
 *   - Branch: a `condition` whose yes and no paths each start with a step of their own and meet
 *     again after it. Each path gets an agent task to start with. (The runtime also runs a path
 *     pointed straight at where the two meet: it skips the first step of the path not taken
 *     only when no live path can still reach it — `_skip_unless_reachable`.)
 *   - Do at the same time: a `parallel` naming two branches and its `join`; each branch is an
 *     agent task whose `next` is the join, and the join `wait_for`s them (all, by default).
 *   - Repeat until: a `bounded_loop` around ONE step (its `body`; the runtime returns the body
 *     to its loop and never follows the body's own `next`), at most 3 times by default.
 *   - For each: a `for_each` around ONE step, run once per item of a list (`items`), at most
 *     100 items and one at a time by default. Its step is an agent task handed the item.
 *
 * A condition's and a loop's expression start empty (`{}`), as a For each's list does (`""`):
 * the validator refuses that until the operator says what to check, which is the point — a branch nobody described must not
 * publish. Like workflow-graph.ts, every function returns the SAME spec when it refuses.
 */
import type { Spec, SpecStep } from "./workflow-editor-types";
import { freeStepId } from "./step-view";
import {
  afterGap, afterGaps, edgesOf, insertStep, linearOrder, nextOf, unsetReadsFrom,
} from "./workflow-graph";

export type ControlBlock = "condition" | "parallel" | "bounded_loop" | "for_each";
export const CONTROL_BLOCKS: ControlBlock[] = ["condition", "parallel", "bounded_loop", "for_each"];

/** The block a step heads, if any: the step a canvas removes the whole block by. */
export function blockOfHead(step: SpecStep | undefined): ControlBlock | null {
  return step && (CONTROL_BLOCKS as string[]).includes(step.kind) ? (step.kind as ControlBlock) : null;
}

/** How many times a new Repeat may go round before the run stops and asks. */
export const DEFAULT_MAX_ITERATIONS = 3;

const HEAD_ID: Record<ControlBlock, string> = {
  condition: "branch", parallel: "at_the_same_time", bounded_loop: "repeat", for_each: "for_each",
};

/** How many items a new For each may go through, and how many it runs at once. The engine's
 *  defaults (`FOR_EACH_DEFAULT_MAX_ITEMS`, concurrency 1) and its limits. */
export const DEFAULT_MAX_ITEMS = 100;
export const MAX_ITEMS_LIMIT = 1000;
export const MAX_CONCURRENCY = 20;

const stepsOf = (spec: Spec): SpecStep[] => (Array.isArray(spec?.steps) ? spec.steps : []);

/** An agent task with a name, as the palette's Agent task makes one ("Ask at start"). */
function agentTask(id: string, title: string, next: string[]): SpecStep {
  return { id, kind: "capability", title, next,
           binding: { agent_work: true, inputs: { instructions: { from_input: "instructions" } } } };
}

/** Free ids for `bases`, none taken in `spec` or by each other. */
function freeIds(spec: Spec, bases: string[]): string[] {
  let working: Spec = spec;
  return bases.map((base) => {
    const id = freeStepId(working, base);
    working = { ...working, steps: [...stepsOf(working), { id, kind: "capability" }] };
    return id;
  });
}

/** The steps a block is made of, head first, continuing to `after` (null: the flow ends). */
function buildBlock(spec: Spec, block: ControlBlock, headId: string, after: string | null): SpecStep[] {
  const then = after ? [after] : [];
  if (block === "condition") {
    const [yes, no] = freeIds(spec, ["if_yes", "if_no"]);
    return [
      { id: headId, kind: "condition", expression: {}, on_true: yes, on_false: no, next: [] },
      agentTask(yes, "If yes", then),
      agentTask(no, "If no", then),
    ];
  }
  if (block === "parallel") {
    const [a, b, join] = freeIds(spec, ["branch_1", "branch_2", "wait_for_branches"]);
    return [
      { id: headId, kind: "parallel", branches: [a, b], join, next: [] },
      agentTask(a, "Branch 1", [join]),
      agentTask(b, "Branch 2", [join]),
      { id: join, kind: "join", wait_for: [a, b], success_policy: "all", next: then },
    ];
  }
  if (block === "for_each") {
    const [body] = freeIds(spec, ["each_item"]);
    const task = agentTask(body, "For each item", []);
    const binding = task.binding as Record<string, unknown>;
    // The item it is on, handed to the agent beside its instructions.
    const inputs = { ...(binding.inputs as Record<string, unknown>), item: { from_item: "" } };
    return [
      { id: headId, kind: "for_each", items: "", body, max_items: DEFAULT_MAX_ITEMS, concurrency: 1,
        next: then },
      { ...task, binding: { ...binding, inputs } },
    ];
  }
  const [body] = freeIds(spec, ["repeat_step"]);
  return [
    { id: headId, kind: "bounded_loop", body, expression: {},
      max_iterations: DEFAULT_MAX_ITERATIONS, next: then },
    agentTask(body, "Repeat this", []),
  ];
}

/**
 * `block` placed at gap `at` — any gap `insertStep` accepts (a straight stretch, the head of a
 * path, after a path's last step, the end). Refused where its exits would land in a join's
 * `wait_for`: a block inside a parallel branch ends in that branch, and the join would then
 * wait for a path a condition may skip. Returns the spec and the head's id.
 */
export function placeBlock(spec: Spec, at: number, block: ControlBlock): { spec: Spec; head: string | null } {
  const headId = freeStepId(spec, HEAD_ID[block]);
  const probe: SpecStep = { id: headId, kind: "capability", binding: {}, next: [] };
  const probed = insertStep(spec, at, probe);
  if (probed === spec) return { spec, head: null };
  const joined = probed.steps.some((s) => s.kind === "join" && Array.isArray(s.wait_for)
    && s.wait_for.map(String).includes(headId));
  if (joined) return { spec, head: null };
  const placed = probed.steps.find((s) => s.id === headId)!;
  const after = nextOf(placed)[0] ?? null;
  const made = buildBlock(probed, block, headId, after);
  const steps = probed.steps.flatMap((s) => (s.id === headId ? made : [s]));
  return { spec: { ...probed, steps }, head: headId };
}

/* ── The region a block covers ───────────────────────────────────────────────────── */

function reach(spec: Spec, starts: string[], stopAt: Set<string> = new Set()): Set<string> {
  const out = new Map<string, string[]>();
  for (const e of edgesOf(spec)) out.set(e.from, [...(out.get(e.from) ?? []), e.to]);
  const seen = new Set<string>();
  const stack = [...starts];
  while (stack.length) {
    const id = stack.pop()!;
    if (seen.has(id) || stopAt.has(id)) continue;
    seen.add(id);
    stack.push(...(out.get(id) ?? []));
  }
  return seen;
}

/** Every step a block is made of (head included) and where the flow goes after it. Null when
 *  the block is not one whole: a step inside is also reached from outside it, or it leaves by
 *  more than one way. */
export function blockRegion(spec: Spec, headId: string): { inside: Set<string>; exit: string | null } | null {
  const steps = stepsOf(spec);
  const head = steps.find((s) => s.id === headId);
  const kind = blockOfHead(head);
  if (!head || !kind) return null;
  const byId = new Map(steps.map((s) => [s.id, s]));
  let inside: Set<string>;
  let exits: string[];
  if (kind === "bounded_loop" || kind === "for_each") {
    const body = String(head.body ?? "");
    if (!byId.has(body)) return null;
    inside = new Set([headId, body]);
    exits = nextOf(head);
  } else if (kind === "parallel") {
    const join = byId.get(String(head.join ?? ""));
    if (!join || join.kind !== "join") return null;
    const branches = Array.isArray(head.branches) ? head.branches.map(String) : [];
    inside = new Set([headId, join.id, ...reach(spec, branches, new Set([join.id]))]);
    exits = nextOf(join);
  } else {
    const yes = reach(spec, [String(head.on_true ?? "")].filter((id) => byId.has(id)));
    const no = reach(spec, [String(head.on_false ?? "")].filter((id) => byId.has(id)));
    const meet = linearOrder(spec).find((id) => yes.has(id) && no.has(id)) ?? null;
    const beyond = meet ? reach(spec, [meet]) : new Set<string>();
    inside = new Set([headId, ...[...yes, ...no].filter((id) => !beyond.has(id))]);
    exits = meet ? [meet] : [];
  }
  if (exits.length > 1) return null;
  const edges = edgesOf(spec);
  // Whole: nothing outside reaches past the head, and no outside join waits for a step inside.
  if (edges.some((e) => inside.has(e.to) && e.to !== headId && !inside.has(e.from))) return null;
  if (steps.some((s) => !inside.has(s.id) && s.kind === "join" && Array.isArray(s.wait_for)
      && s.wait_for.map(String).some((w) => inside.has(w)))) return null;
  return { inside, exit: exits[0] ?? null };
}

/**
 * The spec without the block `headId` heads — the head, every step on its paths and its join —
 * with what led into it pointed at where it went next. Refused (same spec) when the block is
 * not one whole, or when that would leave a path with no step of its own (something's yes/no,
 * branch or body pointing at it, with nothing — or a merge point, or a join — after it).
 */
export function removeBlock(spec: Spec, headId: string): Spec {
  const region = blockRegion(spec, headId);
  if (!region) return spec;
  const { inside, exit } = region;
  const steps = stepsOf(spec);
  const edges = edgesOf(spec);
  const into = edges.filter((e) => e.to === headId);
  if (into.some((e) => e.kind === "loop" || e.kind === "join")) return spec;
  const exitStep = exit ? steps.find((s) => s.id === exit) : undefined;
  for (const e of into) {
    if (e.kind !== "yes" && e.kind !== "no" && e.kind !== "branch") continue;
    if (!exitStep || exitStep.kind === "join") return spec;
    if (edges.some((f) => f.to === exit && !inside.has(f.from))) return spec;
  }
  const entry = spec.entry_step ?? steps[0]?.id;
  const kept = steps.filter((s) => !inside.has(s.id));
  if (entry === headId && !exit && kept.length) return spec;
  const rest = kept.map((s) => {
    if (!into.some((e) => e.from === s.id)) return s;
    let out: SpecStep = s;
    const next = nextOf(s);
    if (next.includes(headId)) {
      const targets = exit ? [...new Set(next.map((t) => (t === headId ? exit : t)))]
        : next.filter((t) => t !== headId);
      out = { ...out, next: typeof s.next === "string" && targets.length <= 1 ? (targets[0] ?? "") : targets };
    }
    for (const key of ["on_true", "on_false"]) if (s[key] === headId) out = { ...out, [key]: exit };
    if (Array.isArray(s.branches)) {
      out = { ...out, branches: s.branches.map((b) => (String(b) === headId ? exit : b)) };
    }
    return out;
  });
  const result: Spec = { ...spec, steps: rest };
  if (entry === headId) {
    if (exit) result.entry_step = exit;
    else delete result.entry_step;
  }
  return unsetReadsFrom(result, inside);
}

/**
 * Where a step picked from the palette goes while `selection` is selected: right after it — and
 * after a selected block's head, after the WHOLE block (where its paths have met again), so a
 * step picked with a Repeat or a Do at the same time selected follows it instead of being pushed
 * into its first path (or, for a Repeat, refused: its body stays one step). A Branch whose paths
 * meet again keeps the step at the head of its first path: the model cannot place a step where
 * two paths meet. The last step of a path is followed by its own after-gap, not by the head of
 * the next path in the order. An index of `linearOrder(spec)` (or an after-gap), -1 for the end.
 */
export function gapAfterSelection(spec: Spec, selection: string | null): number {
  const order = linearOrder(spec);
  const at = selection ? order.indexOf(selection) : -1;
  const plain = at >= 0 && at + 1 < order.length ? at + 1 : -1;
  const probe: SpecStep = { id: "__wf_probe__", kind: "capability", binding: {} };
  const fits = (gap: number) => insertStep(spec, gap, probe) !== spec;
  const gaps = afterGaps(spec);
  // Nothing selected: the end (refused when the flow ends in more than one place; the caller
  // says to pick the step to add after).
  if (at < 0) return -1;
  const region = blockRegion(spec, selection!);
  if (region) {
    const exitAt = region.exit ? order.indexOf(region.exit) : -1;
    return fits(exitAt) ? exitAt : plain;
  }
  // The next step in the order is only "after" this one when this one hands to it. The last
  // step of a path hands to where the paths meet (or ends): its own after-gap.
  const step = stepsOf(spec).find((s) => s.id === selection);
  const next = step ? nextOf(step) : [];
  if (plain >= 0 && next.length === 1 && next[0] === order[plain]) return plain;
  return gaps.includes(afterGap(at)) ? afterGap(at) : plain;
}

export function canRemoveBlock(spec: Spec, headId: string): boolean {
  return removeBlock(spec, headId) !== spec;
}

/* ── A parallel's branches ───────────────────────────────────────────────────────── */

/** One more branch on a parallel: an agent task into its join, waited for like the others. */
export function addBranch(spec: Spec, parallelId: string): { spec: Spec; added: string | null } {
  const steps = stepsOf(spec);
  const head = steps.find((s) => s.id === parallelId);
  const join = steps.find((s) => s.id === String(head?.join ?? ""));
  if (!head || head.kind !== "parallel" || !join || join.kind !== "join") return { spec, added: null };
  const branches = Array.isArray(head.branches) ? head.branches.map(String) : [];
  const waits = Array.isArray(join.wait_for) ? join.wait_for.map(String) : [];
  const n = branches.length + 1;
  const id = freeStepId(spec, `branch_${n}`);
  const added = agentTask(id, `Branch ${n}`, [join.id]);
  const at = steps.findIndex((s) => s.id === join.id);
  const rewired = steps.map((s) => {
    if (s.id === head.id) return { ...s, branches: [...branches, id] };
    if (s.id === join.id) return { ...s, wait_for: [...waits, id] };
    return s;
  });
  return { spec: { ...spec, steps: [...rewired.slice(0, at), added, ...rewired.slice(at)] }, added: id };
}

/** The steps of one branch, head to the step before the join, when it is a plain chain. */
export function branchChain(spec: Spec, parallelId: string, headId: string): string[] | null {
  const steps = stepsOf(spec);
  const byId = new Map(steps.map((s) => [s.id, s]));
  const head = byId.get(parallelId);
  const joinId = String(head?.join ?? "");
  if (!head || head.kind !== "parallel" || !byId.has(joinId)) return null;
  const edges = edgesOf(spec);
  const chain: string[] = [];
  let at: string | undefined = headId;
  while (at && at !== joinId) {
    const step = byId.get(at);
    if (!step || chain.includes(at) || blockOfHead(step) || step.kind === "join") return null;
    if (edges.filter((e) => e.to === at).length !== 1) return null;
    const next = nextOf(step);
    if (next.length !== 1) return null;
    chain.push(at);
    at = next[0];
  }
  return at === joinId ? chain : null;
}

/** A parallel without one of its branches — refused when two would be left with fewer, or
 *  when the branch is more than a plain run of steps into the join. */
export function removeBranch(spec: Spec, parallelId: string, headId: string): Spec {
  const steps = stepsOf(spec);
  const head = steps.find((s) => s.id === parallelId);
  const branches = Array.isArray(head?.branches) ? head!.branches.map(String) : [];
  if (!head || branches.length <= 2 || !branches.includes(headId)) return spec;
  const chain = branchChain(spec, parallelId, headId);
  if (!chain) return spec;
  const gone = new Set(chain);
  const joinId = String(head.join);
  const rest = steps.filter((s) => !gone.has(s.id)).map((s) => {
    if (s.id === parallelId) return { ...s, branches: branches.filter((b) => b !== headId) };
    if (s.id === joinId && Array.isArray(s.wait_for)) {
      return { ...s, wait_for: s.wait_for.filter((w) => !gone.has(String(w))) };
    }
    return s;
  });
  return unsetReadsFrom({ ...spec, steps: rest }, gone);
}

/** Why Remove does nothing to this step: said where the operator pressed it. */
export function removeRefusal(spec: Spec, id: string): string {
  const steps = stepsOf(spec);
  const step = steps.find((s) => s.id === id);
  if (!step) return "";
  if (step.kind === "join") return "Remove its Do at the same time step to remove this";
  if (blockOfHead(step)) return "Its paths reach steps outside it: edit in JSON";
  if (steps.some((s) => s.kind === "bounded_loop" && s.body === id)) {
    return "A Repeat needs its one step: remove the Repeat instead";
  }
  if (steps.some((s) => s.kind === "for_each" && s.body === id)) {
    return "A For each needs its one step: remove the For each instead";
  }
  const into = edgesOf(spec).filter((e) => e.to === id);
  if (into.some((e) => e.kind === "yes" || e.kind === "no" || e.kind === "branch")) {
    return "Each path needs a step of its own: add another first";
  }
  return "Edit branches in JSON";
}
