/**
 * WHAT ONE ROW OF A CONSOLE LIST MAY WEIGH.
 *
 * A list row is what fits on one line of a screen — a title, a status, a verdict, a couple of
 * counts, why it is stuck. 2 KB holds that several times over. Anything heavier is a DETAIL,
 * fetched when the row is opened, never shipped with the list.
 *
 * Why it matters more here than on an ordinary page: whatever a server component passes to a
 * client component is serialised into the RSC payload of EVERY refresh that renders the area.
 * The Discovery list passed each run's full research dossier — 447 KB for 22 runs on boss-1,
 * 425 KB of it state the list never read — into every console refresh, for four numbers.
 *
 * Same bound as the API's (engine discovery_service.LIST_ITEM_BUDGET_BYTES).
 */
export const LIST_ITEM_BUDGET_BYTES = 2048;

/** Free text a list row shows, clipped to what a row can show. */
export const LIST_TEXT_CHARS = 280;

export function clipText(value: string | null | undefined, limit = LIST_TEXT_CHARS): string | null | undefined {
  if (typeof value !== "string" || value.length <= limit) return value;
  return `${value.slice(0, limit - 1)}…`;
}

/** The serialised weight of a value, in bytes — what it costs in a payload. */
export function payloadBytes(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value) ?? "").length;
}
