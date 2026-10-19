/**
 * Observability's writes: one door. Every press goes through `apiWrite` (one press, one effect,
 * an Idempotency-Key per press) to the same-origin `/v1` proxy, and answers `{ok, body, error}`
 * so a tab can say what happened in one toast and then kick the poll that shows it.
 */
import { apiWrite } from "../../lib/api-write";
import { askConsent, consentFromBody } from "../../lib/identity/consent";
import { identityRefusal, identitySentence } from "../../lib/identity/guarded-action";

export type WriteResult<T = Record<string, unknown>> = { ok: boolean; status: number; body: T | null; error: string | null };

/** The API's own sentence, never its raw code. apiWrite already opened "Confirm it's you" and
 *  retried once for a step_up_required (lib/api-write.ts); one that still reaches here was
 *  declined, cancelled, or had no mirror to ask through, and is said in words — not the bare
 *  `step_up_required`/`forbidden` code the API answered with. */
function said(body: unknown, status: number): string {
  const identity = identityRefusal(status, body);
  if (identity) return identitySentence(identity);
  const o = (body && typeof body === "object" ? body : {}) as { detail?: unknown; error?: unknown };
  const d = typeof o.detail === "string" ? o.detail : typeof o.error === "string" ? o.error : "";
  if (d === "consent_required") return "Needs your consent";
  return d || `The API answered ${status}.`;
}

export async function obsWrite<T = Record<string, unknown>>(
  path: string, body: Record<string, unknown>, press: { scope: string; label: string }, method: "POST" | "PATCH" | "PUT" | "DELETE" = "POST",
): Promise<WriteResult<T>> {
  try {
    const send = (withPress: boolean) => apiWrite(path, {
      method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), cache: "no-store",
    }, withPress ? { press } : {});
    let r = await send(true);
    let parsed: unknown = null;
    try { parsed = await r.json(); } catch { /* empty */ }
    // NEEDS YOUR CONSENT (409 consent_required, e.g. a subscription that acts on a person's
    // standing grant): ask, and once granted re-send the same write once, with a fresh key —
    // the refused press's key would replay the refusal.
    const asked = !r.ok ? consentFromBody(parsed) : null;
    if (asked && await askConsent(asked)) {
      r = await send(false);
      parsed = null;
      try { parsed = await r.json(); } catch { /* empty */ }
    }
    return r.ok ? { ok: true, status: r.status, body: parsed as T, error: null }
                : { ok: false, status: r.status, body: null, error: said(parsed, r.status) };
  } catch (e) {
    return { ok: false, status: 0, body: null, error: e instanceof Error ? e.message : String(e) };
  }
}

/** A read the tabs make outside a poll (catalog, recent calls, filtered routing). */
export async function obsRead<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}
