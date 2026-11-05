// @vitest-environment happy-dom
/**
 * The console never computes a tone. Every pill the server sends is rendered with the server's
 * label, the server's tone and the server's tooltip — one verdict, decided once.
 */
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";
import { fakeApi, mountBoard, type Mounted } from "./_helpers/setup-board";
import { board, everyRed } from "./_helpers/setup-fixtures";
import type { Pill, SetupView } from "../lib/setup-types";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

function pillsOf(view: SetupView): Pill[] {
  return [...view.repository.repos.map((r) => r.pill), ...view.models.map((m) => m.pill)];
}

describe("pills are the server's", () => {
  for (const [name, make] of [["board", board], ["every row red", everyRed]] as const) {
    it(name, async () => {
      const view = make();
      fakeApi(view);
      mounted = await mountBoard(view);
      for (const pill of pillsOf(view)) {
        const shown = [...document.body.querySelectorAll(`.su-pill[data-state="${pill.state}"]`)]
          .find((el) => el.textContent === pill.label && el.getAttribute("data-tip") === pill.tip);
        expect(shown, `${pill.state} ${pill.label}`).toBeTruthy();
        expect(shown!.className).toContain(`su-tone-${pill.tone}`);
        expect(shown!.getAttribute("data-tip")).toBe(pill.tip);
      }
      const verdict = document.body.querySelector('[data-testid="su-verdict"]')!;
      expect(verdict.textContent).toBe(view.verdict.label);
      expect(verdict.getAttribute("title")).toBe(view.verdict.tip);
    });
  }

  it("no component maps a state to a colour", () => {
    for (const file of ["pill", "repository-row", "models-row", "jobs-row", "try-row",
                        "setup-board"]) {
      const source = codeOnly(`app/setup/${file}.tsx`);
      expect(source).not.toMatch(/state === "(working|cant_write|key_rejected|rate_limited)"/);
      expect(source).not.toMatch(/tone:\s*"(green|red|amber)"/);
    }
  });

  it("a failing row offers the pill's own fix and Ask BOSS", async () => {
    fakeApi(everyRed());
    mounted = await mountBoard(everyRed());
    const fix = document.body.querySelector('[data-testid="su-fix-github"]');
    expect(fix?.getAttribute("href")).toBe("https://github.com/settings/personal-access-tokens");
    expect(document.body.querySelector('[data-testid="su-ask-Repository"]')).toBeNull(); // no model is green
  });
});
