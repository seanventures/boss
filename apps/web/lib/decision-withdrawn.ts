/**
 * A PRESS THAT WITHDREW ITS CARD (PR #170). Pressing a stale re-check "Fix these" card withdraws
 * it instead of approving it: the API answers 409 with `{status: "withdrawn", detail: <note>}`.
 * That is not a refusal to retry and not an approval, so every surface says what happened in the
 * API's own words: "Withdrawn: <note>".
 */
export function withdrawnNote(error: unknown): string | null {
  const e = error as { status?: unknown; body?: unknown } | null;
  if (!e || e.status !== 409 || typeof e.body !== "object" || e.body === null) return null;
  const body = e.body as { status?: unknown; detail?: unknown };
  if (body.status !== "withdrawn") return null;
  return typeof body.detail === "string" && body.detail.trim()
    ? body.detail.trim() : "This card was withdrawn.";
}

export function withdrawnSentence(note: string): string {
  return `Withdrawn: ${note}`;
}
