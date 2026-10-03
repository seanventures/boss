/**
 * What starting a workflow will ask the operator for.
 *
 * A workflow whose steps read `from_input` — fetch this video, trim to this clip, publish to
 * this channel — was startable only by a caller that already knew the field names. The form
 * renders FROM the declaration, so this turns whichever shape a version wrote into one list
 * the form can draw.
 *
 * TWO SHAPES EXIST IN ONE DATABASE and neither is going away:
 *
 *   {"video_url": "string"}                     — what draft_workflow emits from the wiring
 *   {"required": [...], "properties": {...}}    — JSON Schema, the majority of real versions
 *
 * Reading `.keys()` on the second yields ["required"], which once told operators to supply an
 * input called "required" — confidently wrong, and the mistake this function exists to avoid.
 */

export type WorkflowInputField = {
  name: string;
  label: string;
  help: string;
  required: boolean;
  example: string;
  /** The schema's `default`, as the form's starting value ("" when none). */
  default: string;
  /** The control to draw and the check to make. `number` and `boolean` are sent typed
   *  (`typedInputs`); everything else as text. */
  kind: "text" | "url" | "number" | "multiline" | "email" | "boolean";
};

const KIND_BY_FORMAT: Record<string, WorkflowInputField["kind"]> = {
  uri: "url", url: "url", "uri-reference": "url", textarea: "multiline", multiline: "multiline",
  email: "email", "idn-email": "email",
};

const KIND_BY_TYPE: Record<string, WorkflowInputField["kind"]> = {
  number: "number", integer: "number", boolean: "boolean",
};

//: An example longer than this is prose, and prose does not belong in a one-line box.
//:
//: INFERRED AS WELL AS DECLARED, because the workflows that most need a paragraph are the ones
//: whose author never thought about the form. `"format": "textarea"` is the explicit way to say
//: it; a long example is the tell for everyone who did not.
const PROSE_EXAMPLE_LENGTH = 60;

function titleise(name: string): string {
  const words = name.replace(/[_.-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : name;
}

export function workflowInputFields(schema: unknown): WorkflowInputField[] {
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) return [];
  const raw = schema as Record<string, unknown>;

  // JSON Schema.
  if ("properties" in raw || "required" in raw) {
    const props = (raw.properties ?? {}) as Record<string, Record<string, unknown>>;
    const required = new Set(
      (Array.isArray(raw.required) ? raw.required : []).map((x) => String(x)),
    );
    // A name listed as required with no property still has to be asked for, or the run is
    // refused for a field the form never showed.
    const names = [...new Set([...Object.keys(props), ...required])];
    return names.map((name) => {
      const p = props[name] ?? {};
      const format = String(p.format ?? "").toLowerCase();
      const examples = Array.isArray(p.examples) ? p.examples : [];
      return {
        name,
        label: String(p.title ?? titleise(name)),
        help: String(p.description ?? ""),
        required: required.has(name),
        example: examples.length ? String(examples[0]) : "",
        default: p.default === undefined || p.default === null ? "" : String(p.default),
        kind: KIND_BY_FORMAT[format]
          ?? KIND_BY_TYPE[String(p.type ?? "")]
          ?? (examples.length && String(examples[0]).length > PROSE_EXAMPLE_LENGTH
                ? "multiline"
                : "text"),
      };
    });
  }

  // Name-to-type map. It says nothing about required, and treating its fields as mandatory
  // would refuse runs that have always been legal.
  return Object.keys(raw).map((name) => ({
    name,
    label: titleise(name),
    help: "",
    required: false,
    example: "",
    default: "",
    kind: KIND_BY_TYPE[String(raw[name])] ?? "text",
  }));
}

/**
 * Whether this start form IS the product brief: it declares `directive`, as every catalogue
 * template does (workflow_catalogue.input_schema_for) and every fork of one keeps. Only then
 * does the composer ask the product questions; any other workflow is started from its own
 * fields. No workflow chosen means "this project's usual path", which is a product brief too.
 */
export function startsFromABrief(schema: unknown, workflowChosen = true): boolean {
  if (!workflowChosen) return true;
  return workflowInputFields(schema).some((f) => f.name === "directive");
}

/** The starting values: each field's declared default, and "no" for a yes/no without one —
 *  an unticked box is an answer. */
export function defaultInputs(fields: WorkflowInputField[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const field of fields) {
    if (field.default) out[field.name] = field.default;
    else if (field.kind === "boolean") out[field.name] = "false";
  }
  return out;
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Filled-in fields whose value is not what their kind needs, by label. A blank one is
 *  `missingRequired`'s business, not this. */
export function invalidInputs(
  fields: WorkflowInputField[], values: Record<string, string>,
): string[] {
  return fields.filter((f) => {
    const v = (values[f.name] ?? "").trim();
    if (!v) return false;
    if (f.kind === "email") return !EMAIL.test(v);
    if (f.kind === "url") return !/^https?:\/\/\S+$/i.test(v);
    if (f.kind === "number") return !Number.isFinite(Number(v));
    return false;
  }).map((f) => f.label);
}

/** The supplied values as the start API takes them: a number as a number, a yes/no as a
 *  boolean, everything else as the text typed. Keys are the declared names, unchanged. */
export function typedInputs(
  fields: WorkflowInputField[], supplied: Record<string, string>,
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...supplied };
  for (const f of fields) {
    if (!(f.name in supplied)) continue;
    if (f.kind === "number") out[f.name] = Number(supplied[f.name]);
    if (f.kind === "boolean") out[f.name] = supplied[f.name] === "true";
  }
  return out;
}

/** Only the values actually filled in — a blank box is not an answer, and sending "" would
 *  read further down as a deliberate choice of empty. */
export function suppliedInputs(
  fields: WorkflowInputField[], values: Record<string, string>,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const field of fields) {
    const value = (values[field.name] ?? "").trim();
    if (value) out[field.name] = value;
  }
  return out;
}

/** Required fields still blank. The start button stays disabled while this is non-empty. */
export function missingRequired(
  fields: WorkflowInputField[], values: Record<string, string>,
): string[] {
  return fields
    .filter((f) => f.required && !(values[f.name] ?? "").trim())
    .map((f) => f.label);
}
