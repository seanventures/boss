import { apiWrite, type WriteInit } from "./api-write";
import {
  describeActionError,
  failResult,
  isActionResult,
  okResult,
  type ActionResult,
} from "./action-result";

/**
 * The body of every form action runs through here. See lib/action-result.ts for why.
 *
 * Whatever the body does becomes an ActionResult: returning one passes it through, returning
 * nothing is success, and throwing becomes a failure that says what happened and what to do.
 * Next's own control flow (redirect, notFound) is re-thrown untouched, so an action that ends
 * by navigating still navigates.
 */
export async function act(
  body: () => Promise<unknown>,
  success?: string,
): Promise<ActionResult> {
  try {
    const out = await body();
    if (isActionResult(out)) return out;
    return okResult(success);
  } catch (error) {
    if (isNextControlFlow(error)) throw error;
    // Logged on the server as well: the operator sees the sentence, the log keeps the stack.
    console.error("server action failed", error);
    return failResult(describeActionError(error));
  }
}

/**
 * Next's own control flow — redirect(), notFound(), dynamic-usage bailouts — travels as a
 * thrown error with a recognisable digest, and must reach Next untouched. Matched on the
 * digest rather than via `unstable_rethrow` so this module does not depend on an export the
 * test mocks of next/navigation do not provide.
 */
export function isNextControlFlow(error: unknown): boolean {
  const digest = (error as { digest?: unknown } | null)?.digest;
  return typeof digest === "string"
    && /^(NEXT_|DYNAMIC_SERVER_USAGE|BAILOUT_TO_CLIENT_SIDE_RENDERING)/.test(digest);
}

/** An HTTP refusal, carrying the status so the failure can say what to do about it. */
export class ApiRefusal extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiRefusal";
    this.status = status;
  }
}

/** The API's own explanation from a refused response, never just "HTTP 409". */
export async function refusalFrom(response: Response): Promise<ApiRefusal> {
  let said = "";
  try {
    const raw = await response.text();
    try {
      const body = JSON.parse(raw) as { detail?: unknown; error?: unknown; message?: unknown };
      const detail = body.detail ?? body.error ?? body.message;
      if (typeof detail === "string") said = detail;
      else if (detail && typeof detail === "object") {
        const d = detail as Record<string, unknown>;
        said = [d.message, d.next_action, d.reason]
          .filter((v): v is string => typeof v === "string" && !!v.trim()).join(" ")
          || JSON.stringify(detail).slice(0, 300);
      }
    } catch {
      said = raw.slice(0, 300);
    }
  } catch {
    /* an unreadable body still has a status */
  }
  return new ApiRefusal(said || `BOSS's API refused the request (HTTP ${response.status})`,
                        response.status);
}

/**
 * A server action's write, except a refusal throws. A dozen actions awaited fetch and never
 * looked at the response, so a 403 or a 422 was reported to the operator as success.
 *
 * SENT THROUGH apiWrite, so it carries its press (lib/api-write.ts): the console-a stream wrote
 * this over a raw fetch while main moved every write to apiWrite for one press, one effect
 * (45e7693). At integration the two are one door — keyed, and loud on refusal.
 */
export async function apiFetch(input: string, init: WriteInit): Promise<Response> {
  const response = await apiWrite(input, init);
  if (!response.ok) throw await refusalFrom(response);
  return response;
}
