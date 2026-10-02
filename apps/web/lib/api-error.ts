/** WHAT WENT WRONG, AS A SENTENCE.
 *
 *  FastAPI's `detail` is a string for the errors BOSS writes and an ARRAY of
 *  `{loc, msg}` for a request that fails validation. Forwarding it as-is made the page print
 *  "[object Object]" — the one error an operator could most easily fix, shown as nothing.
 */
export function describeApiError(data: unknown, status: number): string {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    return detail.map((item) => {
      const entry = item as { loc?: unknown[]; msg?: string };
      const field = Array.isArray(entry.loc)
        ? entry.loc.filter((p) => p !== "body" && p !== "query").join(".")
        : "";
      return field ? `${field}: ${entry.msg ?? "invalid"}` : String(entry.msg ?? "invalid");
    }).join("; ");
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  if (status === 409) return "That conflicts with something that already exists.";
  if (status === 403) return "You do not have permission to change this in this project.";
  return `The BOSS API answered HTTP ${status}.`;
}
