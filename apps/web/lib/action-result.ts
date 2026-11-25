/**
 * ONE SHAPE FOR WHAT A BUTTON DID.
 *
 * Ninety-four of the console's server actions returned `Promise<void>` and reported failure by
 * throwing. In a production build Next replaces a thrown server-action error with "An error
 * occurred in the Server Components render", and the console has no error boundary, so a
 * refused Approve, a 403 on Create agent or a 409 on Retry reached the operator as a blank
 * crash or as nothing at all. A dozen more never looked at the response and reported success
 * for a request the API had refused. Approvals went further and swallowed the error, then
 * showed "Decision recorded".
 *
 * So every form action returns an ActionResult, and every form renders it through ActionForm
 * (client-forms.tsx): pending while it runs, a toast and an inline line either way, and on
 * failure a sentence saying what happened and what to do. tests/server-actions-return-results
 * enumerates every action and every form so a new one cannot opt out.
 *
 * This module is isomorphic on purpose: the server actions build results with it and the
 * client reads them with the same types.
 */

export type ActionResult = {
  ok: boolean;
  /** What happened, for a toast. Optional on success: the form may supply its own. */
  message?: string;
  /** Why it failed and what to do about it. Set whenever ok is false. */
  error?: string;
  data?: Record<string, unknown>;
  /** Where the work that just started can be watched, when there is such a place. */
  href?: string;
  hrefLabel?: string;
};

export function okResult(message?: string, extra: Partial<ActionResult> = {}): ActionResult {
  return { ok: true, ...(message ? { message } : {}), ...extra };
}

export function failResult(error: string, extra: Partial<ActionResult> = {}): ActionResult {
  return { ok: false, error, ...extra };
}

/** The next step for a failure, by HTTP status. The server says WHAT; this says what to DO. */
export function whatToDo(status: number): string {
  if (status === 401) return "Your session has expired — sign in again and retry.";
  if (status === 403) return "You're not allowed: you don't hold the permission for this · ask an Admin.";
  if (status === 404) return "It may have been removed or renamed — reload the page and look again.";
  if (status === 409) return "Reload to see its current state before trying again.";
  if (status === 400 || status === 422) return "Check the fields you entered and try again.";
  if (status === 429) return "BOSS is rate-limiting this — wait a minute and try again.";
  if (status >= 500) {
    return "BOSS's API failed while doing it. Try again in a moment; if it keeps failing, "
      + "the Activity log has the error.";
  }
  return "Try again; if it keeps failing, the Activity log has the details.";
}

/** "API said X" plus what to do, without repeating a sentence the server already ended with. */
/** AN API'S BARE CODE IS NEVER SHOWN AS IS ("consent_required" under a Retry button, the
 *  laptop 2026-11-18): the known codes in words, any other bare snake_case code said as a
 *  refusal with its words. A sentence passes through unchanged. */
const CODE_WORDS: Record<string, string> = {
  consent_required: "Needs your consent: the agents need your permission to do this. Press it again to give it.",
  step_up_required: "Confirm it's you, then press it again.",
  forbidden: "You don't have permission to do this. Ask an Admin.",
  not_found: "It isn't there any more. Reload to see its current state.",
  conflict: "It changed meanwhile. Reload to see its current state.",
  rate_limited: "Too many requests just now. Try again in a moment.",
  budget_refused: "The budget refused it. Raise the cap in Govern, then try again.",
};
export function codeWords(text: string): string {
  const t = String(text ?? "").trim();
  if (!/^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$|^[a-z]+$/.test(t)) return t;
  return CODE_WORDS[t] ?? (t.includes("_") ? `The API refused it (${t.replace(/_/g, " ")}).` : t);
}

/** What to do when BOSS refused or failed and said nothing: a step and the place to look. The
 *  Activity log is the console area "Activity log" (area id `activity`). */
export const NO_REASON_NEXT =
  "Reload the page and try once more; if it repeats, the Activity log has the error.";

/** The no-reason sentence when we don't know which action it was. */
export const NO_REASON = `It didn't go through and BOSS gave no reason. ${NO_REASON_NEXT}`;

/**
 * EVERY FAILURE SAYS WHAT HAPPENED AND WHAT TO DO (BOSSAPP-1428).
 *
 * A customer kept reading "Couldn't do that", "Could not change it", "Save failed." — a symptom
 * with the cause in a tooltip or nowhere. Every console failure line is built here:
 *
 *   couldNot("retry step 2", "the step is still running")
 *     → "Couldn't retry step 2: the step is still running."
 *   couldNot("save Build speed", "", "Nothing changed.")
 *     → "Couldn't save Build speed: BOSS gave no reason. Nothing changed."
 *   couldNot("save Build speed")
 *     → "Couldn't save Build speed: BOSS gave no reason. Reload the page and try once more; …"
 *
 * `what` is what the button does, in its own words. The reason is the server's sentence (a bare
 * API code is said in words). `next` is the next step; with no reason and no `next`, the
 * generic one names where the error is (the Activity log). tests/every-failure-says-what-to-do
 * fails on a vague literal anywhere in the console.
 */
export function couldNot(what: string, reason?: string | null, next?: string): string {
  const doing = String(what ?? "").trim().replace(/[.\s]+$/, "") || "do that";
  const said = codeWords(String(reason ?? "").trim()).replace(/[.\s]+$/, "");
  const after = (next ?? "").trim();
  if (!said) return `Couldn't ${doing}: BOSS gave no reason. ${after || NO_REASON_NEXT}`;
  // The server already began with "Couldn't …"/"Could not …": don't say it twice.
  const head = /^(couldn'?t|could not|can'?t|cannot)\b/i.test(said) ? said : `Couldn't ${doing}: ${said}`;
  const ended = /[!?]$/.test(head) ? head : `${head}.`;
  return after ? `${ended} ${after}` : ended;
}

/** A caught value's words ("" when it has none), for couldNot's reason. */
export function reasonOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  if (error && typeof error === "object" && typeof (error as { message?: unknown }).message === "string") {
    return (error as { message: string }).message;
  }
  return "";
}

export function failureSentence(what: string, status?: number): string {
  const said = codeWords(String(what ?? "").trim());
  const next = typeof status === "number" && status > 0 ? whatToDo(status) : "";
  if (!said) return next || NO_REASON;
  if (!next) return said;
  return `${said.replace(/[.\s]+$/, "")}. ${next}`;
}

type StatusError = { status?: unknown; message?: unknown };

/** Any thrown value as a sentence an operator can act on. */
export function describeActionError(error: unknown): string {
  if (error && typeof error === "object") {
    const e = error as StatusError;
    const status = typeof e.status === "number" ? e.status : undefined;
    const message = typeof e.message === "string" ? e.message : "";
    if (status !== undefined) return failureSentence(message, status);
    if (message) {
      if (/fetch failed|ECONNREFUSED|ENOTFOUND|timed? ?out|TimeoutError/i.test(message)) {
        return `BOSS's API could not be reached (${message}). It may be restarting — try `
          + "again in a moment.";
      }
      return codeWords(message);
    }
  }
  if (typeof error === "string" && error.trim()) return codeWords(error.trim());
  return NO_REASON;
}

/** True for the result shape, so a caller can tell a result from a legacy void return. */
export function isActionResult(value: unknown): value is ActionResult {
  return !!value && typeof value === "object" && typeof (value as ActionResult).ok === "boolean";
}

/**
 * The same result for a button that calls a route handler instead of a server action, so a
 * client fetch says what happened in the same words a form does.
 */
export async function resultFromResponse(response: Response, success?: string):
    Promise<ActionResult> {
  let body: Record<string, unknown> = {};
  try {
    body = (await response.json()) as Record<string, unknown>;
  } catch {
    /* an empty or non-JSON body still has a status */
  }
  if (response.ok) return okResult(success, { data: body });
  const said = [body.error, body.detail, body.message]
    .find((v): v is string => typeof v === "string" && v.trim().length > 0) ?? "";
  return failResult(failureSentence(said || `The request was refused (HTTP ${response.status})`,
                                    response.status));
}

/** A required field was empty. These actions used to `return` silently, which read as success. */
export function missingInput(...fields: string[]): ActionResult {
  const named = fields.map((f) => f.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ")
    .toLowerCase().replace(/ id$/, ""));
  const list = named.length > 1
    ? `${named.slice(0, -1).join(", ")} and ${named[named.length - 1]}`
    : named[0] ?? "a required field";
  const plural = named.length > 1;
  return failResult(`${list.charAt(0).toUpperCase()}${list.slice(1)} ${plural ? "are" : "is"} `
    + `required — fill ${plural ? "them" : "it"} in and try again.`);
}
