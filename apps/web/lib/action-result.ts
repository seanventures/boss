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
  if (status === 403) {
    return "Your sign-in is not allowed to do this. Ask an owner to grant the permission "
      + "in Access and packs, or sign in as someone who has it.";
  }
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
export function failureSentence(what: string, status?: number): string {
  const said = String(what ?? "").trim();
  const next = typeof status === "number" && status > 0 ? whatToDo(status) : "";
  if (!said) return next || "The action failed and the server gave no reason. Try again.";
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
      return message;
    }
  }
  if (typeof error === "string" && error.trim()) return error.trim();
  return "The action failed and the server gave no reason. Try again.";
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
