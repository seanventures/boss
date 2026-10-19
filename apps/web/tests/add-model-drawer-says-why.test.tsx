// @vitest-environment happy-dom
/**
 * ADD A MODEL DRAWER (boss-1 rc10.9): a stale pill from an older check kept showing while the
 * operator tried a fresh key, and a disabled Connect never said why.
 *
 * - A model being replaced loses its old pill at once: the old check no longer describes
 *   anything once the drawer is open to test a new key.
 * - Connect is disabled until the form is filled, and now says exactly what is missing.
 */
import { afterEach, describe, expect, it } from "vitest";

import { click, fakeApi, flush, mountBoard, q, type, type Mounted } from "./_helpers/setup-board";
import { board, everyRed } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

function replaceButtonIn(row: Element): HTMLButtonElement {
  const found = Array.from(row.querySelectorAll("button"))
    .find((b) => b.textContent?.trim() === "Replace");
  if (!found) throw new Error("no Replace button in this row");
  return found as HTMLButtonElement;
}

describe("replacing a model", () => {
  it("drops the old pill at once — never a stale check while a new key is tried", async () => {
    fakeApi(everyRed());
    mounted = await mountBoard(everyRed());
    const row = q(document.body, "su-model-setup-openrouter")!;
    const pillBefore = q(row, "su-model-pill");
    expect(pillBefore?.textContent).toBe("Key rejected");

    await click(replaceButtonIn(row));

    const rowAfter = q(document.body, "su-model-setup-openrouter")!;
    const pillAfter = q(rowAfter, "su-model-pill");
    expect(pillAfter?.textContent).toBe("Editing…");
    expect(pillAfter?.textContent).not.toBe("Key rejected");
  });
});

describe("the Connect button, disabled", () => {
  it("says why: paste a key first", async () => {
    fakeApi(board());
    mounted = await mountBoard(board(), "?area=setup&add=openrouter");
    const connectButton = q(document.body, "su-model-connect") as HTMLButtonElement;
    expect(connectButton.disabled).toBe(true);
    expect(connectButton.title).toBe("Paste a key first");

    await type(q(document.body, "su-model-key"), "sk-or-v1-abcdefghijklmnop");
    await type(q(document.body, "su-model-name"), "anthropic/claude-sonnet-4.5");
    await flush();
    const connectAfter = q(document.body, "su-model-connect") as HTMLButtonElement;
    expect(connectAfter.disabled).toBe(false);
    expect(connectAfter.title).toBe("Saves it, then asks one tiny question. Budgeted and receipted.");
  });
});
