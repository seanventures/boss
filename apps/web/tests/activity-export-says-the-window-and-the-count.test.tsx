// @vitest-environment happy-dom
/**
 * An activity download says two extra facts, and only those.
 * A cut retention window is not a second row cap. No extra header keeps today's toast.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { fakeLogApi, newLog, rows, type FakeLog } from "./_helpers/log-fixtures";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));

const { LogExplorer } = await import("../app/activity/log-explorer");

let log: FakeLog;
const toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent).detail.message);

beforeEach(() => {
  log = newLog();
  log.page.items = rows(1);
  toasts.length = 0;
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?org=o1&area=activity");
  window.addEventListener("boss:toast", onToast);
  (URL as unknown as { createObjectURL: () => string }).createObjectURL = () => "blob:x";
  (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = () => {};
});
afterEach(() => {
  unmount();
  window.removeEventListener("boss:toast", onToast);
  vi.unstubAllGlobals();
  setConsoleLive(true);
});

async function exportCsv(headers: Record<string, string>) {
  const base = fakeLogApi(log);
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const u = String(input);
    if (u.startsWith("/api/activity/export")) {
      return new Response("created_at,level\n", { headers });
    }
    return base(input, init);
  }));
  const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  await mount(<LogExplorer projectId="p1" actor="ceo" />);
  await click($("[data-testid=lx-export]"));
  await click($("[data-testid=lx-export-csv]"));
  await settle();
  anchorClick.mockRestore();
}

describe("an activity export says when retention cut the window", () => {
  it("says the retention window cut this export and how many lines left", async () => {
    await exportCsv({ "x-truncated": "0", "x-window-capped": "1", "x-row-count": "1204" });
    expect(toasts).toContain("The retention window cut this export");
    expect(toasts).toContain("1,204 lines exported");
    expect(toasts).not.toContain("First 50,000 lines exported");
  });

  it("says one line when the file has one", async () => {
    await exportCsv({ "x-truncated": "0", "x-window-capped": "0", "x-row-count": "1" });
    expect(toasts).toContain("1 line exported");
    expect(toasts).not.toContain("The retention window cut this export");
    expect(toasts).not.toContain("First 50,000 lines exported");
  });

  it("keeps the 50,000-line toast when neither extra header is present", async () => {
    await exportCsv({ "x-truncated": "1" });
    expect(toasts).toEqual(["First 50,000 lines exported"]);
  });

  it("says both facts and the row cap when the file hit all three", async () => {
    await exportCsv({ "x-truncated": "1", "x-window-capped": "1", "x-row-count": "50000" });
    expect(toasts).toContain("First 50,000 lines exported");
    expect(toasts).toContain("The retention window cut this export");
    expect(toasts).toContain("50,000 lines exported");
  });
});
