// @vitest-environment happy-dom
/**
 * The app drawer's Logs control opens the Activity log for this deployment.
 * It is not this app's request log. The control stays the word Logs.
 * A sentence over six words is a paragraph, so the sentence is the control's title.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, click, unmount } from "./_helpers/live-render";
import { openObs, params, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the app drawer's Logs control", () => {
  it("says it opens the Activity log, not this app's requests, and still goes there", async () => {
    await openObs("?view=apps&app=t_down");
    const link = $("[data-testid='obs-app-logs']");
    expect(link).not.toBeNull();
    expect(text(link).replace("↗", "").trim()).toBe("Logs");
    const title = link?.getAttribute("title") ?? "";
    expect(title).toBe("Opens the Activity log, not this app's requests.");
    expect(title.trim().split(/\s+/).filter(Boolean).length).toBeLessThanOrEqual(14);
    expect(link?.getAttribute("href")).toBe("?area=activity&src=deployment&subject=t_down");
    await click(link);
    expect(params().get("area")).toBe("activity");
    expect(params().get("src")).toBe("deployment");
    expect(params().get("subject")).toBe("t_down");
    expect(params().get("app")).toBeNull();
  });
});
