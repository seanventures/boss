/**
 * 13-12 (and every project page): an "are you sure?" is the console's own sheet
 * (app/projects/confirm-sheet.tsx), never the browser's window.confirm() / alert() / prompt(),
 * which the console cannot style, test or theme and which blocks the page.
 */
import { readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const DIR = "app/projects";

describe("project pages ask in the console's own sheet", () => {
  it("no window.confirm / alert / prompt in app/projects", () => {
    const files = readdirSync(DIR).filter((f) => f.endsWith(".tsx") || f.endsWith(".ts"));
    expect(files.length).toBeGreaterThan(10);
    const bad = files.filter((f) => /\b(?:window\.)?(?:confirm|alert|prompt)\s*\(/.test(codeOnly(`${DIR}/${f}`).replace(/\bonConfirm\b|ConfirmSheet/g, "")));
    expect(bad).toEqual([]);
  });

  it("the shared sheet is a real modal dialog with Esc, Cancel and a red refusal", () => {
    const src = codeOnly(`${DIR}/confirm-sheet.tsx`);
    expect(src).toContain('role="dialog"');
    expect(src).toContain('aria-modal="true"');
    expect(src).toContain('"Escape"');
    expect(src).toContain('role="alert"');
  });
});
