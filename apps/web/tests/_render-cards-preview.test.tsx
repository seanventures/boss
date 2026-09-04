/** Not a test — a rendering harness, so the card art can be LOOKED AT before it ships.
 *
 *  Three versions of this art were declared finished on the strength of the code reading
 *  correctly, and all three were wrong on the page: a gradient wash, then a wash with a
 *  texture, then random stock photographs. Rendering the real component and opening the output
 *  is the only check that answers the actual question, which is "does this look like anything".
 *
 *  Run: CARD_PREVIEW_OUT=/tmp/cards.html npx vitest run tests/_render-cards-preview.test.tsx
 */
import * as React from "react";
import { describe, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync, writeFileSync } from "node:fs";

describe("card art preview", () => {
  // Opt-in: an ordinary `vitest run` should not drop files on somebody's disk.
  it.runIf(!!process.env.CARD_PREVIEW_OUT)("writes the rendered strip to an html file", async () => {
    // The app is transpiled with the CLASSIC JSX transform here, so the component's compiled
    // output calls React.createElement against a global the component never imports. Set it
    // before the module is evaluated — hence the dynamic import.
    (globalThis as Record<string, unknown>).React = React;
    const { AskBossHero } = await import("../app/ask-boss-hero");

    const html = renderToStaticMarkup(
      <AskBossHero projectId="boss-dogfood" actor="ceo" />,
    );
    // ONLY the card rules. Inlining all ~400KB of globals.css wedged the renderer, and the
    // aura's keyframes spin forever behind a screenshot that never resolves.
    const all = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");
    const css = all
      .split(/(?=^[.#@][^\n]*\{)/m)
      .filter((rule) => /askboss/.test(rule))
      .join("\n");
    const out = process.env.CARD_PREVIEW_OUT as string;
    writeFileSync(out, `<!doctype html><meta charset="utf-8">
<style>
  :root { --panel: #0d1117; }
  body { background: #0b0f19; margin: 0; padding: 24px; font-family: system-ui, sans-serif; }
  *, *::before, *::after { animation: none !important; transition: none !important; }
  .askboss-aura { display: none !important; }
  ${css}
  /* A grid, not a scroller, so every card is in one screenshot. */
  .askboss-idea-strip { display: grid !important; grid-template-columns: repeat(4, 250px);
    gap: 16px; overflow: visible !important; }
</style>
${html}`);
  });
});
