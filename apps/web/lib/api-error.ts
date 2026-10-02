/**
 * An API refusal, as a sentence an operator can read.
 *
 * The console's route handlers forwarded FastAPI's `detail` as-is. For a refusal that is a
 * string, fine; for a validation error it is an ARRAY of {loc, msg} objects, and the screens
 * render errors with String(...) — so the operator read "[object Object]" (console walk on
 * boss-1, 2026-10-02, Skills and MCP; the same forwarding was in every route under app/api).
 * Every route handler builds its error text here; tests/every-api-error-reads-as-words.test.ts
 * fails on a route that forwards `detail` raw.
 */
export function apiErrorText(body: unknown, status: number): string {
  const fallback = `The BOSS API refused this (HTTP ${status}).`;
  if (body === null || body === undefined) return fallback;
  if (typeof body === "string") return body.trim() || fallback;
  if (typeof body !== "object") return fallback;
  const record = body as Record<string, unknown>;
  const detail = "detail" in record ? record.detail : record.error;
  return describe(detail) || fallback;
}

function describe(detail: unknown): string {
  if (detail === null || detail === undefined) return "";
  if (typeof detail === "string") return detail.trim();
  if (typeof detail === "number" || typeof detail === "boolean") return String(detail);
  if (Array.isArray(detail)) {
    // FastAPI validation: [{loc: ["body", "name"], msg: "Field required", ...}, ...]
    return detail
      .map((item) => {
        if (item && typeof item === "object") {
          const entry = item as { loc?: unknown; msg?: unknown; message?: unknown };
          const where = Array.isArray(entry.loc)
            ? entry.loc.filter((part) => part !== "body" && part !== "query").join(".")
            : "";
          const what = typeof entry.msg === "string" ? entry.msg
            : typeof entry.message === "string" ? entry.message : "";
          if (what) return where ? `${where}: ${what}` : what;
        }
        return describe(item);
      })
      .filter(Boolean)
      .join("; ");
  }
  if (typeof detail === "object") {
    const entry = detail as Record<string, unknown>;
    for (const key of ["message", "reason", "error", "detail"]) {
      const text = describe(entry[key]);
      if (text) return text;
    }
    try {
      return JSON.stringify(detail).slice(0, 300);
    } catch {
      return "";
    }
  }
  return "";
}
