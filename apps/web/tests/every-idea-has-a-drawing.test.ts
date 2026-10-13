/**
 * Every idea has a drawing (operator, 2026-10-13): each card in "Start from an idea" shows a small
 * drawing of the app's main screen. An idea without one falls back to a plain glyph cover, so a new
 * idea could ship looking blank. This fails first.
 *
 * "The catalogue" is both sources the dialog reads (useIdeas): the written briefs (IDEAS) and the
 * starter templates labelled `idea` under templates/apps/. IdeaCover stays only as the runtime
 * fallback for an entry the server adds before the console has drawn it.
 */
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { IDEAS } from "../app/ask-boss-hero";
import { DRAWN_IDEAS, IdeaPreview, hasIdeaPreview, ideaPreviewLabel } from "../components/v2/templates/idea-previews";

const TEMPLATES = join(__dirname, "../../../templates/apps");

/** The starter templates the server offers as ideas: `labels: [idea]` in their boss-template.yaml. */
function starterTitles(): string[] {
  return readdirSync(TEMPLATES)
    .map((d) => join(TEMPLATES, d, "boss-template.yaml"))
    .filter((f) => existsSync(f))
    .map((f) => readFileSync(f, "utf8"))
    .filter((y) => /^labels:\s*\[[^\]]*\bidea\b[^\]]*\]/m.test(y))
    .map((y) => y.match(/^name:\s*(.+)$/m)![1].trim().replace(/^["']|["']$/g, ""));
}

const catalogue = () => [...starterTitles(), ...IDEAS.map((i) => i.title)];

describe("every idea has a drawing", () => {
  it("reads both sources: the starters and the written briefs", () => {
    expect(starterTitles()).toEqual(expect.arrayContaining(["Banking starter", "Product catalog and cart", "Hello endpoint"]));
    expect(IDEAS.length).toBeGreaterThanOrEqual(16);
  });

  it("no idea in the catalogue is left with the plain cover", () => {
    const blank = catalogue().filter((t) => !hasIdeaPreview(t));
    expect(blank, `draw these in components/v2/templates/idea-previews.tsx: ${blank.join(", ")}`).toEqual([]);
  });

  it("no drawing is orphaned: each one belongs to an idea in the catalogue", () => {
    const titles = new Set(catalogue().map((t) => t.toLowerCase()));
    expect(DRAWN_IDEAS.filter((k) => !titles.has(k))).toEqual([]);
  });

  it("each drawing is a labelled 320×180 image that says what the screen shows", () => {
    for (const title of catalogue()) {
      const svg = renderToStaticMarkup(React.createElement(IdeaPreview, { title }));
      expect(svg, title).toMatch(/^<svg class="ip-svg" role="img"/);
      expect(svg, title).toContain('viewBox="0 0 320 180"');
      const label = ideaPreviewLabel(title)!;
      expect(label.startsWith(`${title} preview: `), title).toBe(true);
      expect(label.length - title.length, `${title}: say what the screen shows`).toBeGreaterThan(50);
      // The window chrome, titled with the app's own screen.
      expect(svg, title).toMatch(/<text x="160" y="16"[^>]*>[^<]+ · [^<]+<\/text>|>Terminal<\/text>/);
      // Drawn inline with literal colours; never an image or a theme token.
      expect(svg, title).not.toMatch(/<image|href=|var\(--/);
    }
  });

  it("an unknown idea renders nothing, so the dialog falls back to its cover", () => {
    expect(hasIdeaPreview("Not an idea yet")).toBe(false);
    expect(renderToStaticMarkup(React.createElement(IdeaPreview, { title: "Not an idea yet" }))).toBe("");
  });
});
