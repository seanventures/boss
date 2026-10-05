/**
 * Observability's writes: one door. Every press goes through `apiWrite` (one press, one effect,
 * an Idempotency-Key per press) to the same-origin `/v1` proxy, and answers `{ok, body, error}`
 * so a tab can say what happened in one toast and then kick the poll that shows it.
 */
import { apiWrite } from "../../lib/api-write";

export type WriteResult<T = Record<string, unknown>> = { ok: boolean; status: number; body: T | null; error: string | null };

/** The API's own sentence, never its raw code. */
function said(body: unknown, status: number): string {
  const o = (body && typeof body === "object" ? body : {}) as { detail?: unknown; error?: unknown };
  const d = typeof o.detail === "string" ? o.detail : typeof o.error === "string" ? o.error : "";
  return d || `The API answered ${status}.`;
}

export async function obsWrite<T = Record<string, unknown>>(
  path: string, body: Record<string, unknown>, press: { scope: string; label: string },
): Promise<WriteResult<T>> {
  try {
    const r = await apiWrite(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), cache: "no-store",
    }, { press });
    let parsed: unknown = null;
    try { parsed = await r.json(); } catch { /* empty */ }
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
