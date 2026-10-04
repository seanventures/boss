/**
 * ONE WAY TO TURN AN API REFUSAL INTO SOMETHING THE CONSOLE CAN DRAW.
 *
 * Start answers 409 with a structured detail — `{message, readiness}` — when the project lacks
 * what the workflow needs. Every proxy forwarded `detail` as `error`, so the console was handed
 * an OBJECT where it expected a sentence: the Ask-BOSS hero rendered `⚠ {error}` and React
 * refused to draw it, so the headline Build button crashed on any project with no deployment
 * host (review of the readiness change). Only one of four start doors had been taught to
 * flatten it, by hand.
 *
 * So there is one function, and `start-doors-forward-the-checklist.test.ts` lists every route
 * and action that posts a start and fails if one of them does not use it. `error` is ALWAYS a
 * string; the checklist travels beside it as `readiness`, for the panel that offers
 * "Start anyway".
 *
 * Two more structured parts travel beside `error` the same way, for the Workflow Studio editor:
 * `problems` (a 422 from check or publish, each pinned to a step and a field, so the editor can
 * point at them) and `suggested_name` (a 409 name clash). Flattened into the sentence they
 * would lose which step and which field.
 */

import { detailText } from "./api-error";

export type ApiRefusal = {
  /** Always a sentence — safe to render as text. */
  error: string;
  /** Present when the refusal is "this project cannot run it yet": the checklist. */
  readiness?: unknown;
  /** Present on a workflow check/publish refusal: `{step_id, field, message}` each. */
  problems?: unknown[];
  /** Present on a name clash: a free name to offer instead. */
  suggested_name?: string;
};

/** A detail that is words on its own: a string, or FastAPI's validation array. An object is
 *  NOT — it may be the readiness checklist, read below. The words come from lib/api-error.ts's
 *  reader (integration of the templates and console-b streams, which each wrote a parser for
 *  the same "[object Object]" / crashed-hero incident; there is one reader now). */
function sentence(value: unknown): string | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value === "object" && !Array.isArray(value)) return undefined;
  return detailText(value) || undefined;
}

/** The editor's structured parts of a refusal, from the top level or from inside `detail`. */
function structured(body: Record<string, unknown>, d: Record<string, unknown>):
    Pick<ApiRefusal, "problems" | "suggested_name"> {
  const problems = Array.isArray(body.problems) ? body.problems
    : Array.isArray(d.problems) ? d.problems : undefined;
  const suggested = body.suggested_name ?? d.suggested_name;
  return {
    ...(problems ? { problems } : {}),
    ...(typeof suggested === "string" ? { suggested_name: suggested } : {}),
  };
}

/** The refusal in the API's response body `parsed`, as `{error: string, readiness?}` plus the
 *  editor's `problems` / `suggested_name` when the API sent them. */
export function refusalFrom(status: number, parsed: unknown): ApiRefusal {
  const body = (parsed && typeof parsed === "object") ? parsed as Record<string, unknown> : {};
  const detail = body.detail ?? body.error;
  const d = (detail && typeof detail === "object" && !Array.isArray(detail))
    ? detail as Record<string, unknown> : {};
  const flat = sentence(detail);
  if (flat) return { error: flat, ...structured(body, d) };
  if (detail && typeof detail === "object") {
    const message = sentence(d.message) ?? sentence(d.error) ?? sentence(d.detail)
      ?? (Array.isArray(d.problems) ? undefined : detailText(d) || undefined);
    const problems = structured(body, d);
    return {
      error: message ?? firstProblem(problems.problems) ?? `API returned ${status}`,
      ...(d.readiness && typeof d.readiness === "object" ? { readiness: d.readiness } : {}),
      ...problems,
    };
  }
  return { error: firstProblem(body.problems) ?? `API returned ${status}`, ...structured(body, d) };
}

/** The machine-readable reason a refusal carries (`detail.code`: needs_model, budget, busy …),
 *  for a console that acts on it; undefined when it has none. */
export function refusalCode(parsed: unknown): string | undefined {
  const body = (parsed && typeof parsed === "object") ? parsed as Record<string, unknown> : {};
  const detail = body.detail;
  const code = (detail && typeof detail === "object" && !Array.isArray(detail))
    ? (detail as Record<string, unknown>).code : undefined;
  return typeof code === "string" && code ? code : undefined;
}

/** A refusal made only of problems still reads as a sentence: the first problem's. */
function firstProblem(problems: unknown): string | undefined {
  if (!Array.isArray(problems) || !problems.length) return undefined;
  const first = problems[0] as Record<string, unknown> | null;
  return sentence(first?.message) ?? undefined;
}
