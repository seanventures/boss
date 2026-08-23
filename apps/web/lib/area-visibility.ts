/**
 * Which console sections are worth rendering for the requested area.
 *
 * `page.tsx` builds all 64 top-level `<section>`s on every request and `globals.css` reveals
 * one area's worth with
 *
 *     .bx-content[data-active="X"] .frame > section:is([data-area="X"], :has([data-area="X"]))
 *
 * So the server renders 64 React trees, serialises every one of them across the
 * server→client boundary (ConsoleShell is a client component, so its children are PROPS), and
 * CSS then hides ~63. Measured: 6.38 MB of payload, of which **2.04 MB (32%) is `className`** —
 * class names, not data. Gating the data fetches could not touch this, and did not: that change
 * moved 8.38 MB to 8.23 MB.
 *
 * This mirrors the CSS rule rather than inventing a second one, INCLUDING the `:has()` clause:
 * sixteen sections carry no `data-area` of their own and are identified by a descendant
 * `<span className="eyebrow" data-area="…">`. Three of those span 1027, 451 and 343 lines — they
 * are most of the weight, so a rule that only understood the attribute form would have skipped
 * exactly the sections worth skipping.
 *
 * The trade this accepts: switching tabs becomes a server round-trip instead of a CSS toggle.
 * With one area rendering that should be seconds rather than the twenty this replaces.
 */

/** Areas whose data every screen depends on, so a bare `?org=` still renders something. */
export const ALWAYS_RENDER = ["overview"] as const;

export function activeAreaId(requestedArea: string | undefined): string {
  const area = (requestedArea ?? "").trim();
  return area || "overview";
}

/**
 * Render this section?
 *
 * @param requestedArea the `?area=` param, or undefined
 * @param sectionAreas  every `data-area` value appearing in the section, its own or a
 *                      descendant's — the `:is(..., :has(...))` pair, in code
 *
 * A section naming NO area is always rendered: it is chrome, not an area, and hiding it would
 * be a guess. Failing toward rendering keeps the failure mode "still too big" rather than
 * "a panel vanished".
 */
export function shouldRenderSection(
  requestedArea: string | undefined,
  sectionAreas: readonly string[],
): boolean {
  if (sectionAreas.length === 0) return true;
  const active = activeAreaId(requestedArea);
  return sectionAreas.includes(active);
}
