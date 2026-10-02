/**
 * WHICH SECTION THE CONSOLE SHOWS, DERIVED FROM THE AREA LIST.
 *
 * v1 renders every area into the DOM and reveals one with CSS: `main.bx-content[data-active=X]`
 * shows the sections tagged `data-area=X`. CSS cannot match one attribute against another, so
 * that rule needs one selector per area — and for months that list was written by hand in
 * globals.css. It drifted three times: Workflow and Activity log shipped invisible, and then AI
 * SRE did (boss-1, ?area=ai-sre: 0 visible sections, 0 characters of text), which the test
 * guarding the list could not see because its own regex had no hyphen in it.
 *
 * So the selectors are no longer written; they are generated from the same `AREAS` array the
 * rail is built from. An area that is navigable is visible, by construction.
 */

/** The ids an area may have. Anything else would need escaping inside a CSS selector, and a
 *  quietly broken selector is exactly the failure this module exists to remove. */
const SAFE_ID = /^[a-z][a-z0-9_-]*$/;

export function areaVisibilityCss(ids: readonly string[]): string {
  const selectors = ids.map((id) => {
    if (!SAFE_ID.test(id)) {
      throw new Error(`area id ${JSON.stringify(id)} cannot be used in a visibility selector`);
    }
    return `.bx-content[data-active="${id}"] .frame > section:is([data-area="${id}"], :has([data-area="${id}"]))`;
  });
  if (selectors.length === 0) return "";
  return `${selectors.join(",\n")} {\n  display: grid;\n  animation: bx-fade 280ms cubic-bezier(0.22, 1, 0.36, 1) both;\n}`;
}
