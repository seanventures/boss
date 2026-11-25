/**
 * Every template has a drawing (operator, 2026-11-18: "they also look plain, can we have some
 * background just like you did for Ideas list"). Each card in the Command Center template gallery
 * opens with a small drawing of THAT app's main screen. A template the console has not drawn yet
 * falls back to its category's drawing, then a generic one, so no card ships blank; this fails first
 * for a catalogue template without its own.
 */
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DRAWN_TEMPLATES, TemplatePreview, templatePreviewKind } from "../components/v2/templates/template-previews";

const CATALOGUE = join(__dirname, "../../../engine/boss_engine/data/factory_templates");

function catalogue(): Array<{ id: string; category: string }> {
  return readdirSync(CATALOGUE)
    .filter((f) => f.endsWith(".yaml"))
    .map((f) => readFileSync(join(CATALOGUE, f), "utf8"))
    .map((y) => ({
      id: y.match(/^id:\s*(.+)$/m)![1].trim().replace(/^["']|["']$/g, ""),
      category: (y.match(/^category:\s*(.+)$/m)?.[1] ?? "").trim().replace(/^["']|["']$/g, ""),
    }));
}

describe("every template has a drawing", () => {
  it("reads the catalogue", () => {
    expect(existsSync(CATALOGUE)).toBe(true);
    expect(catalogue().length).toBeGreaterThanOrEqual(21);
  });

  it("no template in the catalogue falls back to a category or generic drawing", () => {
    const blank = catalogue().filter((t) => templatePreviewKind(t.id, t.category) !== "template").map((t) => t.id);
    expect(blank, `draw these in components/v2/templates/template-previews.tsx: ${blank.join(", ")}`).toEqual([]);
  });

  it("no drawing is orphaned", () => {
    const ids = new Set(catalogue().map((t) => t.id));
    expect(DRAWN_TEMPLATES.filter((id) => !ids.has(id))).toEqual([]);
  });

  it("every catalogue category has its own fallback drawing", () => {
    for (const { category } of catalogue()) expect(templatePreviewKind("not-drawn-yet", category), category).toBe("category");
    expect(templatePreviewKind("not-drawn-yet", "something-new")).toBe("generic");
  });

  it("each drawing is a decorative, inline 320×180 window", () => {
    for (const { id, category } of [...catalogue(), { id: "not-drawn", category: "web" }, { id: "not-drawn", category: "" }]) {
      const svg = renderToStaticMarkup(React.createElement(TemplatePreview, { id, category }));
      expect(svg, id).toMatch(/^<svg class="ip-svg" aria-hidden="true"/);
      expect(svg, id).toContain('viewBox="0 0 320 180"');
      expect(svg, id).toMatch(/<text x="160" y="16"[^>]*>[^<]+<\/text>/);
      expect(svg, id).not.toMatch(/<image|href=|var\(--|NaN|undefined/);
    }
  });
});
