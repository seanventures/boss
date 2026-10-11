import { apiWrite, type WriteInit } from "./api-write";
import { identityRefusal, identitySentence, type IdentityRefusalInfo } from "./identity/guarded-action";
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
    // AN IDENTITY REFUSAL IS SOMETHING THE PAGE CAN ACT ON (lib/identity/guarded-action.ts):
    // step-up opens the sheet and retries; a missing permission is named. Short, not logged as
    // a failure of the action.
    const identity = error instanceof ApiRefusal ? error.identity : identityOfThrown(error);
    if (identity) return failResult(identitySentence(identity), { data: { identity } });
    // A REFUSAL THAT SAYS WHO TO ASK AND WHERE (e.g. seat_required: the seat's holders and
    // Identity › Approval seats) is shown as written, with its link — never followed by the
    // generic "ask an Admin", which sent people to the wrong person.
    const pointed = pointedRefusal(error);
    if (pointed) return failResult(pointed.message, { href: pointed.href, hrefLabel: pointed.hrefLabel });
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

/** A refusal whose body names where to go (`href`, a console path) and says what to do there. */
export function pointedRefusal(error: unknown): { message: string; href: string; hrefLabel: string } | null {
  if (!error || typeof error !== "object") return null;
  const body = (error as { body?: unknown }).body;
  if (!body || typeof body !== "object") return null;
  const b = body as { message?: unknown; href?: unknown; href_label?: unknown };
  if (typeof b.message !== "string" || !b.message.trim()) return null;
  if (typeof b.href !== "string" || !b.href.startsWith("/") || b.href.startsWith("//")) return null;
  return { message: b.message.trim(), href: b.href,
    hrefLabel: typeof b.href_label === "string" && b.href_label ? b.href_label : "Open" };
}

/** The typed client's BossApiError (status + parsed body) as an identity refusal, if it is one. */
function identityOfThrown(error: unknown): IdentityRefusalInfo | null {
  if (!error || typeof error !== "object") return null;
  const e = error as { status?: unknown; body?: unknown };
  return typeof e.status === "number" ? identityRefusal(e.status, e.body) : null;
}

/** An HTTP refusal, carrying the status so the failure can say what to do about it. */
export class ApiRefusal extends Error {
  status: number;
  /** Set when the API refused for identity (401 step_up_required, 403 forbidden + permission). */
  identity: IdentityRefusalInfo | null;
  constructor(message: string, status: number, identity: IdentityRefusalInfo | null = null) {
    super(message);
    this.name = "ApiRefusal";
    this.status = status;
    this.identity = identity;
  }
}

/** The API's own explanation from a refused response, never just "HTTP 409". */
export async function refusalFrom(response: Response): Promise<ApiRefusal> {
  let said = "";
  let identity: IdentityRefusalInfo | null = null;
  try {
    const raw = await response.text();
    try {
      const body = JSON.parse(raw) as { detail?: unknown; error?: unknown; message?: unknown };
      identity = identityRefusal(response.status, body);
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
  if (identity) return new ApiRefusal(identitySentence(identity), response.status, identity);
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
