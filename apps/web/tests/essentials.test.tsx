// @vitest-environment happy-dom
/**
 * The first run (skills-mcp spec §5.2 Essentials, test 3): four tiles on a fresh install, each
 * advancing as it is set up; all four ready → one line with Test crew; dismissal is remembered,
 * and a throwing localStorage only means the strip shows.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, cleanup, click, render } from "./_helpers/kit-render";
import { clone, installKitApi, KITS } from "./_helpers/kit-api";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { KitHome } = await import("../app/skills/kit-home");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); try { localStorage.clear(); } catch { /* none */ } });

describe("the Essentials strip", () => {
  it("a fresh install shows the four, in order, each with its state and one button", async () => {
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills");
    await render(<KitHome projectId="p1" actor="ceo" />);
    expect($$('li[data-testid^="kit-ess-"][data-status]').map((t) => t.dataset.testid))
      .toEqual(["kit-ess-open_prs", "kit-ess-web_research", "kit-ess-file_tickets", "kit-ess-email"]);
    expect($('[data-testid="kit-ess-action-open_prs"]')!.textContent).toBe("Connect repo ↗");
    expect($('[data-testid="kit-ess-action-web_research"]')!.textContent).toBe("Done ✓");
    expect($('[data-testid="kit-ess-action-email"]')!.textContent).toBe("Set up");
  });

  it("setting one up advances its dot; all four ready is one line with Test crew", async () => {
    const kit = clone(KITS.fresh);
    installKitApi(kit, (url, body) => {
      if (body?.action === "connect") {
        const email = kit.abilities.find((a) => a.key === "email")!;
        email.status = "ready";
        return email;
      }
      return undefined;
    });
    await render(<KitHome projectId="p1" actor="ceo" />);
    expect($('[data-testid="kit-ess-email"]')!.dataset.status).toBe("setup");
    cleanup();
    installKitApi(clone(KITS.ready));
    await render(<KitHome projectId="p1" actor="ceo" />);
    expect($('[data-testid="kit-essentials"]')).toBeNull();
    expect($('[data-testid="kit-essentials-done"]')!.textContent).toContain("Essentials ready");
    expect($('[data-testid="kit-test-crew"]')!.textContent).toBe("Test crew");
  });

  it("dismissing it is remembered, and &setup=1 brings it back", async () => {
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills");
    await render(<KitHome projectId="p1" actor="ceo" />);
    await click('[data-testid="kit-essentials-dismiss"]');
    expect($('[data-testid="kit-essentials"]')).toBeNull();
    cleanup();
    await render(<KitHome projectId="p1" actor="ceo" />);
    expect($('[data-testid="kit-essentials"]')).toBeNull();
    cleanup();
    window.history.replaceState(null, "", "/?area=skills&setup=1");
    await render(<KitHome projectId="p1" actor="ceo" />);
    expect($('[data-testid="kit-essentials"]')).not.toBeNull();
  });

  it("a storage that throws only means the strip shows", async () => {
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills");
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    await render(<KitHome projectId="p1" actor="ceo" />);
    expect($('[data-testid="kit-essentials"]')).not.toBeNull();
    await click('[data-testid="kit-essentials-dismiss"]');
    expect($('[data-testid="kit-essentials"]')).toBeNull();
    get.mockRestore(); set.mockRestore();
  });
});
