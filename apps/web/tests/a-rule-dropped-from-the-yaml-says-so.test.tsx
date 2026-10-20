// @vitest-environment happy-dom
/**
 * P0-A (PR #234): a deploy whose boss.deploy.yaml lost a rule keeps it running, and the Rules list
 * says so on that rule — never a rule that silently drifts from the repository.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ruleWire, rulesWire } from "./_helpers/alerts-fixtures";
import { $, unmount } from "./_helpers/live-render";
import { openObs } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const DROPPED = "Not in boss.deploy.yaml since the latest deploy — still running";

describe("a rule the latest deploy's manifest dropped", () => {
  it("is marked on the Rules list, with why in the tooltip; a declared rule is not", async () => {
    await openObs("?view=apps&app=t_down&atab=alerts", {
      rules: (id: string) => rulesWire(id, { rules: [
        ruleWire("error_rate", { managed_by: "code" }),
        ruleWire("latency_p95", { managed_by: "code", dropped_word: DROPPED,
                                  dropped_from_manifest_at: "2026-10-19T21:30:00Z" }),
      ] }),
    });
    const chip = $("[data-testid='obs-rule-latency_p95'] [data-testid='obs-rule-dropped']");
    expect(chip?.textContent).toBe("Dropped from yaml");
    expect(chip?.getAttribute("title")).toContain("still running");
    expect($("[data-testid='obs-rule-error_rate'] [data-testid='obs-rule-dropped']")).toBeNull();
  });
});
