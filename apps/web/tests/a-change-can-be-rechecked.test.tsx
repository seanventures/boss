// @vitest-environment happy-dom
/**
 * "RE-CHECK THIS CHANGE" ON THE BUILD PAGE (BOSSAPP-1326).
 *
 * A finished build's change had no way to run its checks again after a platform fix (Northgate
 * change #2 kept a false 0/30 acceptance table). The build page now offers "Re-check this
 * change", with its estimated cost on the button; it posts to the engine's re-check (every
 * gate on the branch head, no code written) and is gated on the steer permission.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

import { RecheckLine } from "../app/factory/outputs";
import { click, mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const c = { run: { id: "crun_95aca9c7" } } as unknown as Parameters<typeof RecheckLine>[0]["c"];

afterEach(() => unmount());

describe("Re-check this change", () => {
  it("shows its cost, and posts the re-check", async () => {
    const calls = stubFetch((call) => {
      if (call.method === "GET" && call.path.endsWith("/recheck")) {
        return { body: { can: true, why: "", gates: ["tests", "security", "review", "consistency", "acceptance"],
                         estimate: { usd: 3.41, basis: "what the checks cost on crun_95ac" } } };
      }
      if (call.method === "POST" && call.path.endsWith("/recheck")) {
        return { body: { run_id: "crun_next", estimate: { usd: 3.41 } } };
      }
      return undefined;
    });
    await mountAs(<RecheckLine c={c} actor="ceo" />);
    const button = tid("fx-recheck") as HTMLButtonElement;
    expect(button.textContent).toBe("Re-check this change · about $3.41");
    expect(button.title).toContain("writes no code");
    await click(button);
    const sent = calls.find((x) => x.method === "POST" && x.path === "/v1/code-tasks/crun_95aca9c7/recheck");
    expect(sent?.body).toEqual({ actor: "ceo" });
    expect((tid("fx-recheck") as HTMLButtonElement).textContent).toBe("Re-check started");
  });

  it("says why it cannot run", async () => {
    stubFetch((call) => (call.method === "GET"
      ? { body: { can: false, why: "this build is still running; its checks are still to come", gates: [],
                  estimate: { usd: 3, basis: "" } } }
      : undefined));
    await mountAs(<RecheckLine c={c} actor="ceo" />);
    expect((tid("fx-recheck") as HTMLButtonElement).disabled).toBe(true);
    expect(tid("fx-recheck-why")?.textContent).toContain("still running");
  });
});
