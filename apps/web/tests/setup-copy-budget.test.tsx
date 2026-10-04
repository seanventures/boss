// @vitest-environment happy-dom
/**
 * Tooltips, not paragraphs — Repo and models held to the Studio's copy budget, by rendering.
 * No visible run over six words outside a tooltip, placeholder or the operator's own text; no
 * <p> inside the board; none of the words that narrate BOSS's plumbing instead of the
 * operator's state.
 */
import { afterEach, describe, expect, it } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { click, fakeApi, mountBoard, q, type Mounted } from "./_helpers/setup-board";
import { board, everyRed, firstRun, repoDone, tryStep } from "./_helpers/setup-fixtures";

const BANNED = ["environment variable", " env ", "harness", "entitlement", "binding", "governed",
                "verification state", " lane", "inherited", "deployment default", "BOSS_",
                "--connect", "container"];

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

function audit(root: HTMLElement) {
  expect(overBudget(root)).toEqual([]);
  expect(root.querySelectorAll(".su-board p, .su-drawer p").length).toBe(0);
  const visible = ` ${root.textContent ?? ""} `;
  for (const word of BANNED) expect(visible.toLowerCase()).not.toContain(word.toLowerCase());
}

describe("the board stays within the copy budget", () => {
  for (const [name, make] of [["first run", firstRun], ["model step", repoDone],
                              ["try step", tryStep], ["board", board],
                              ["every row red", everyRed]] as const) {
    it(name, async () => {
      fakeApi(make());
      mounted = await mountBoard(make());
      audit(document.body);
    });
  }

  it("with More jobs open and the budget being edited", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    await click(q(document.body, "su-more-jobs"));
    await click(q(document.body, "su-budget-edit"));
    audit(document.body);
  });

  it("with Add a model open on every tile and provider", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    await click(q(document.body, "su-add-model-open"));
    for (const tile of ["subscription", "api_key", "local"]) {
      await click(q(document.body, `su-tile-${tile}`));
      const providers = [...document.body.querySelectorAll('[data-testid^="su-provider-"]')];
      for (const chip of providers) {
        await click(chip);
        audit(document.body);
      }
    }
  });

  it("with Connect GitHub open, and when consent is needed", async () => {
    const view = { ...board(), repository: { connection: null, repos: [] },
                   secrets: { backend: "database", needs_consent: true } };
    fakeApi(view);
    mounted = await mountBoard(view);
    await click(q(document.body, "su-connect-github"));
    expect(q(document.body, "su-consent")).not.toBeNull();
    audit(document.body);
  });
});
