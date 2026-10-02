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
 */

import { detailText } from "./api-error";

export type ApiRefusal = {
  /** Always a sentence — safe to render as text. */
  error: string;
  /** Present when the refusal is "this project cannot run it yet": the checklist. */
  readiness?: unknown;
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

/** The refusal in the API's response body `parsed`, as `{error: string, readiness?}`. */
export function refusalFrom(status: number, parsed: unknown): ApiRefusal {
  const body = (parsed && typeof parsed === "object") ? parsed as Record<string, unknown> : {};
  const detail = body.detail ?? body.error;
  const flat = sentence(detail);
  if (flat) return { error: flat };
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    const message = sentence(d.message) ?? sentence(d.error) ?? sentence(d.detail)
      ?? (detailText(d) || undefined);
    return {
      error: message ?? `API returned ${status}`,
      ...(d.readiness && typeof d.readiness === "object" ? { readiness: d.readiness } : {}),
    };
  }
  return { error: `API returned ${status}` };
}
