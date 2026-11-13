/**
 * A TYPED QUESTION'S FORM (BOSSAPP-832; engine services/typed_forms.py). A decision may carry
 * `form_schema` — fields of five types — and is answered with a typed `answer` keyed by the
 * fields' keys. This module is the console's half of that contract: read the form defensively,
 * hold a draft answer, say what is still missing in the person's words, and build exactly the
 * object the engine validates (validate_answer). No React here, so it is tested on its own.
 */

export type FieldType = "text" | "select" | "checkbox" | "item_verdicts" | "task_suggestions";
export type FormOption = { value: string; label: string };
export type FormField = {
  key: string; type: FieldType; label: string; required: boolean; help?: string;
  multiline?: boolean;
  options?: FormOption[];
  items?: { id: string; label: string }[];
  verdicts?: string[];
  suggestions?: { title: string; description?: string; assignee?: string }[];
};
export type TypedForm = { fields: FormField[] };

/** What the person has filled in so far. */
export type Draft = Record<string, unknown>;

const TYPES: FieldType[] = ["text", "select", "checkbox", "item_verdicts", "task_suggestions"];
const obj = (v: unknown): Record<string, unknown> =>
  (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const str = (v: unknown) => (typeof v === "string" ? v : "");

/** The form a card carries, or null (no form, or one this console cannot draw). */
export function parseForm(raw: unknown): TypedForm | null {
  const fields = Array.isArray(obj(raw).fields) ? (obj(raw).fields as unknown[]) : [];
  const out: FormField[] = [];
  for (const f of fields.map(obj)) {
    const type = str(f.type) as FieldType;
    if (!str(f.key) || !TYPES.includes(type)) continue;
    out.push({
      key: str(f.key), type, label: str(f.label) || str(f.key), required: f.required !== false,
      ...(str(f.help) ? { help: str(f.help) } : {}),
      ...(type === "text" ? { multiline: Boolean(f.multiline) } : {}),
      ...(type === "select" ? { options: (Array.isArray(f.options) ? f.options : []).map(obj)
        .filter((o) => str(o.value)).map((o) => ({ value: str(o.value), label: str(o.label) || str(o.value) })) } : {}),
      ...(type === "item_verdicts" ? {
        items: (Array.isArray(f.items) ? f.items : []).map(obj).filter((i) => str(i.id))
          .map((i) => ({ id: str(i.id), label: str(i.label) || str(i.id) })),
        verdicts: (Array.isArray(f.verdicts) ? f.verdicts : ["accept", "reject"]).map(String),
      } : {}),
      ...(type === "task_suggestions" ? {
        suggestions: (Array.isArray(f.suggestions) ? f.suggestions : []).map(obj).filter((s) => str(s.title))
          .map((s) => ({ title: str(s.title), ...(str(s.description) ? { description: str(s.description) } : {}),
                         ...(str(s.assignee) ? { assignee: str(s.assignee) } : {}) })),
      } : {}),
    });
  }
  return out.length ? { fields: out } : null;
}

/** A fresh draft: checkboxes off, every suggestion offered as accepted (the person unticks). */
export function emptyDraft(form: TypedForm): Draft {
  const d: Draft = {};
  for (const f of form.fields) {
    if (f.type === "checkbox") d[f.key] = false;
    else if (f.type === "item_verdicts") d[f.key] = {};
    else if (f.type === "task_suggestions") d[f.key] = (f.suggestions ?? []).map(() => true);
    else d[f.key] = "";
  }
  return d;
}

/** The first thing still missing, said as the field's label — or null when it can be sent. */
export function missing(form: TypedForm, draft: Draft): string | null {
  for (const f of form.fields) {
    const v = draft[f.key];
    if (f.type === "checkbox" || f.type === "task_suggestions" || !f.required) continue;
    if (f.type === "item_verdicts") {
      const given = obj(v);
      if ((f.items ?? []).some((i) => !str(given[i.id]))) return `${f.label}: give every item a verdict`;
    } else if (!str(v).trim()) {
      return `${f.label} is needed`;
    }
  }
  return null;
}

/** The answer exactly as the engine validates it (typed_forms.validate_answer). */
export function toAnswer(form: TypedForm, draft: Draft): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of form.fields) {
    const v = draft[f.key];
    if (f.type === "checkbox") out[f.key] = Boolean(v);
    else if (f.type === "item_verdicts") {
      const given = obj(v);
      const kept = Object.fromEntries((f.items ?? []).filter((i) => str(given[i.id])).map((i) => [i.id, str(given[i.id])]));
      if (Object.keys(kept).length) out[f.key] = kept;
    } else if (f.type === "task_suggestions") {
      const picks = Array.isArray(v) ? v : [];
      out[f.key] = (f.suggestions ?? []).map((_, index) => ({ index, accepted: picks[index] === true }));
    } else if (str(v).trim()) {
      out[f.key] = str(v).trim();
    }
  }
  return out;
}

/** An answered form, one line per field ("Database: postgres", "Findings: 1 accept, 1 reject"). */
export function answerLines(form: TypedForm, answer: Record<string, unknown> | null | undefined): string[] {
  if (!answer) return [];
  const lines: string[] = [];
  for (const f of form.fields) {
    const v = answer[f.key];
    if (v === undefined) continue;
    if (f.type === "checkbox") lines.push(`${f.label}: ${v ? "yes" : "no"}`);
    else if (f.type === "select") lines.push(`${f.label}: ${(f.options ?? []).find((o) => o.value === v)?.label ?? str(v)}`);
    else if (f.type === "item_verdicts") {
      const counts: Record<string, number> = {};
      Object.values(obj(v)).forEach((x) => { counts[str(x)] = (counts[str(x)] ?? 0) + 1; });
      lines.push(`${f.label}: ${Object.entries(counts).map(([k, n]) => `${n} ${k}`).join(", ")}`);
    } else if (f.type === "task_suggestions") {
      const taken = (Array.isArray(v) ? v : []).map(obj).filter((p) => p.accepted === true).length;
      lines.push(`${f.label}: ${taken} accepted`);
    } else lines.push(`${f.label}: ${str(v)}`);
  }
  const made = Array.isArray(answer._created_work_items) ? answer._created_work_items.length : 0;
  if (made) lines.push(`${made} work item${made === 1 ? "" : "s"} created`);
  return lines;
}
