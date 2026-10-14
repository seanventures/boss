/**
 * A condition as the expression builder edits it, to and from the expression the engine reads.
 *
 * THE LANGUAGE IS THE ENGINE'S, NOT A NEW ONE. `evaluate_expression` (engine/boss_engine/
 * services/workflows.py) reads `{all: […]}`, `{any: […]}`, `{not: …}` and one comparison
 * `{path, op, value}` against the run's payload: `input.<name>` (what it was started with),
 * `context.<field>` (the latest value any step made) and `steps.<id>.output.<field>`. Nothing is
 * evaluated as text. The builder covers the shape people write — one list of comparisons, all or
 * any of them, optionally negated — and anything else (a group inside a group) stays in JSON,
 * where it is shown and edited as written rather than flattened.
 *
 * `fromExpression(toExpression(c))` gives `c` back for every builder condition (one comparison
 * reads back as "all"), and the engine validator (`expression_problems`) accepts every
 * expression `toExpression` makes from a condition whose comparisons are filled in.
 */
import type { Palette, Spec, SpecStep } from "./workflow-editor-types";
import { contractOf, declaredInputs, loopBodies, upstreamOf } from "./workflow-graph";
import { stepTitle } from "./step-view";

export type ExprOp = "eq" | "ne" | "contains" | "in" | "not_in" | "gt" | "gte" | "lt" | "lte"
  | "truthy" | "exists";

/** Every comparison, in the order offered, with what it compares against. */
export const OPS: { op: ExprOp; label: string; takes: "one" | "list" | "none" }[] = [
  { op: "eq", label: "is", takes: "one" },
  { op: "ne", label: "is not", takes: "one" },
  { op: "contains", label: "contains", takes: "one" },
  { op: "in", label: "is one of", takes: "list" },
  { op: "not_in", label: "is none of", takes: "list" },
  { op: "gt", label: "is more than", takes: "one" },
  { op: "gte", label: "is at least", takes: "one" },
  { op: "lt", label: "is less than", takes: "one" },
  { op: "lte", label: "is at most", takes: "one" },
  { op: "truthy", label: "is yes", takes: "none" },
  { op: "exists", label: "is there", takes: "none" },
];
const OP_SET = new Set(OPS.map((o) => o.op));
export const takesOf = (op: ExprOp) => OPS.find((o) => o.op === op)!.takes;

export type Rule = { path: string; op: ExprOp; value?: unknown };
export type Condition = { match: "all" | "any"; rules: Rule[]; negate: boolean };

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);
const LEAF_KEYS = new Set(["path", "op", "value"]);

function ruleOf(raw: unknown): Rule | null {
  if (!isObj(raw) || typeof raw.path !== "string") return null;
  if (Object.keys(raw).some((k) => !LEAF_KEYS.has(k))) return null;
  const op = String(raw.op ?? "truthy").toLowerCase() as ExprOp;
  if (!OP_SET.has(op)) return null;
  const takes = takesOf(op);
  if (takes === "none") return "value" in raw ? null : { path: raw.path, op };
  if (takes === "list" && !Array.isArray(raw.value)) return null;
  if (takes === "one" && (Array.isArray(raw.value) || isObj(raw.value))) return null;
  return { path: raw.path, op, value: raw.value };
}

/** The builder's reading of an expression, or null when only JSON can show it as written. */
export function fromExpression(expression: unknown): Condition | null {
  if (!isObj(expression)) return null;
  const keys = Object.keys(expression);
  if (!keys.length) return { match: "all", rules: [], negate: false };
  if (keys.length === 1 && keys[0] === "not") {
    const inner = fromExpression(expression.not);
    return inner && !inner.negate && inner.rules.length ? { ...inner, negate: true } : null;
  }
  if (keys.length === 1 && (keys[0] === "all" || keys[0] === "any")) {
    const items = expression[keys[0]];
    if (!Array.isArray(items) || !items.length) return null;
    const rules = items.map(ruleOf);
    return rules.every(Boolean) ? { match: keys[0] as "all" | "any", rules: rules as Rule[], negate: false } : null;
  }
  const rule = ruleOf(expression);
  return rule ? { match: "all", rules: [rule], negate: false } : null;
}

function leaf(rule: Rule): Obj {
  const out: Obj = { path: rule.path.trim(), op: rule.op };
  const takes = takesOf(rule.op);
  if (takes === "list") out.value = Array.isArray(rule.value) ? rule.value : [];
  else if (takes === "one") out.value = rule.value ?? "";
  return out;
}

/** The expression the engine reads. No comparisons is `{}`, which the validator refuses: a
 *  Branch nobody described does not publish. */
export function toExpression(condition: Condition): Obj {
  const rules = condition.rules;
  if (!rules.length) return {};
  const body = rules.length === 1 ? leaf(rules[0]) : { [condition.match]: rules.map(leaf) };
  return condition.negate ? { not: body } : body;
}

/* ── Values, as typed ────────────────────────────────────────────────────────────── */

export type ValueKind = "text" | "number" | "yesno";

export function kindOf(value: unknown): ValueKind {
  const first = Array.isArray(value) ? value[0] : value;
  return typeof first === "number" ? "number" : typeof first === "boolean" ? "yesno" : "text";
}

/** One typed value from its text, or null when the text is not that kind ("abc" as a number). */
export function parseValue(text: string, kind: ValueKind): unknown {
  if (kind === "yesno") return text === "true";
  if (kind === "number") {
    const n = Number(text.trim());
    return text.trim() !== "" && Number.isFinite(n) ? n : null;
  }
  return text;
}

/** "a, b, c" as a list of typed values; an item that is not that kind is dropped. */
export function parseList(text: string, kind: ValueKind): unknown[] {
  return text.split(",").map((t) => t.trim()).filter(Boolean)
    .map((t) => parseValue(t, kind)).filter((v) => v !== null);
}

export function valueText(value: unknown): string {
  if (Array.isArray(value)) return value.map((v) => String(v)).join(", ");
  return value === undefined || value === null ? "" : String(value);
}

/* ── What a condition can read ───────────────────────────────────────────────────── */

export type Source = { path: string; label: string; group: "Start form" | "Steps";
  /** Answers a typed decision can give, offered as values. */
  suggestions?: string[] };

/** What a step makes that a condition can compare: its produces, or a typed decision's answer. */
function fieldsOf(step: SpecStep, palette: Palette): { field: string; suggestions?: string[] }[] {
  const binding = isObj(step.binding) ? step.binding : {};
  if (isObj(binding.typed_decision)) {
    const options = isObj(binding.typed_decision.options) ? Object.keys(binding.typed_decision.options) : [];
    return [{ field: "answer", suggestions: options }, { field: "confident" }];
  }
  if (binding.agent_work) {
    return Array.isArray(binding.produces) ? binding.produces.map((f) => ({ field: String(f) })) : [];
  }
  return contractOf(step, palette).produces.map((field) => ({ field }));
}

/**
 * Every value the condition on step `id` can read, earliest first: the start form's fields, then
 * what each step that runs before it makes. A loop also reads what its own repeated step makes.
 *
 * A step a loop repeats has one result PER ROUND, so `steps.<id>` becomes a list after the
 * second round and `steps.<id>.output.<field>` stops matching. Its fields are read as
 * `context.<field>` instead: the latest value, which is what "until it passes" means.
 */
export function expressionSources(spec: Spec, id: string, palette: Palette): Source[] {
  const out: Source[] = declaredInputs(spec).map((name) => ({
    path: `input.${name}`, label: name, group: "Start form" as const,
  }));
  const self = spec.steps.find((s) => s.id === id);
  const ids = upstreamOf(spec, id);
  if (self?.kind === "bounded_loop" && typeof self.body === "string" && !ids.includes(self.body)) {
    ids.push(self.body);
  }
  const repeated = loopBodies(spec);
  const seen = new Set(out.map((s) => s.path));
  for (const stepId of ids) {
    const step = spec.steps.find((s) => s.id === stepId);
    if (!step) continue;
    const title = stepTitle(step, palette);
    for (const { field, suggestions } of fieldsOf(step, palette)) {
      const path = repeated.has(stepId) ? `context.${field}` : `steps.${stepId}.output.${field}`;
      if (seen.has(path)) continue;
      seen.add(path);
      out.push({ path, label: `${title} · ${field}`, group: "Steps",
                 ...(suggestions?.length ? { suggestions } : {}) });
    }
  }
  return out;
}

/** A path no source names, said readably: "steps.review.output.score" → "review · score". */
export function pathLabel(path: string): string {
  const step = /^steps\.([^.]+)\.output\.(.+)$/.exec(path);
  if (step) return `${step[1]} · ${step[2]}`;
  const input = /^input\.(.+)$/.exec(path);
  if (input) return `Start form · ${input[1]}`;
  const latest = /^context\.(.+)$/.exec(path);
  if (latest) return `Latest ${latest[1]}`;
  return path;
}
