// @vitest-environment happy-dom
/** A double click on Connect, Test all or Open a PR sends one request. */
import { afterEach, describe, expect, it } from "vitest";
import { act } from "react";

import { fakeApi, flush, mountBoard, q, type, type Mounted } from "./_helpers/setup-board";
import { board, repoDone, tryStep } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

async function doubleClick(el: Element | null) {
  await act(async () => { (el as HTMLElement).click(); (el as HTMLElement).click(); });
}

describe("one press, one effect", () => {
  it("Test all", async () => {
    const api = fakeApi(board());
    api.hold = true;
    mounted = await mountBoard(board());
    await doubleClick(q(document.body, "su-test-all"));
    api.release();
    await flush();
    expect(api.calls.filter((c) => c.op === "checks").length).toBe(1);
  });

  it("Connect a model", async () => {
    const api = fakeApi(repoDone());
    api.hold = true;
    mounted = await mountBoard(repoDone(), "?area=setup&add=openrouter");
    await type(q(document.body, "su-model-key"), "sk-or-v1-0123456789abcdef");
    await doubleClick(q(document.body, "su-model-connect"));
    api.release();
    await flush();
    expect(api.calls.filter((c) => c.op === "models").length).toBe(1);
  });

  it("Open a PR", async () => {
    const api = fakeApi(tryStep());
    api.hold = true;
    mounted = await mountBoard(tryStep(), "?area=setup&focus=try", true, "setup");   // Welcome › Quick PR
    await type(q(document.body, "su-try-input"), "Fix the typo in README");
    await doubleClick(q(document.body, "su-try-open"));
    api.release();
    await flush();
    expect(api.calls.filter((c) => c.op === "try").length).toBe(1);
  });
});
