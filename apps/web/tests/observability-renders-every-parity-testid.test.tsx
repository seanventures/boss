// @vitest-environment happy-dom
/**
 * Every Observability row of the parity table (lib/infra-obs-parity.ts) renders: open the area at
 * the row's URL with the shared wire fixtures, press what the row says, and the testid is there.
 * This is the S4 half of the lead's parity test, so a stream break shows up here first.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PARITY } from "../lib/infra-obs-parity";
import { mockObsFetch } from "./_helpers/obs-fixtures";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { ObsHome } = await import("../app/observability/obs-home");

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const rows = PARITY.filter((r) => r.check.kind === "testid" && r.check.surface === "obs");

describe("Observability renders every parity testid", () => {
  for (const row of rows) {
    const c = row.check as { url: string; press?: string[]; testid: string };
    it(`${row.id} · ${c.testid} at ${c.url}${c.press?.length ? ` after ${c.press.join(" → ")}` : ""}`, async () => {
      mockObsFetch();
      window.history.replaceState(null, "", `/?area=observability&${c.url.replace(/^\?/, "")}`);
      await mount(<ObsHome projectId="p1" actor="ceo" />);
      await settle();
      for (const p of c.press ?? []) await click($(`[data-testid="${p}"]`));
      await settle();
      expect($(`[data-testid="${c.testid}"]`), c.testid).not.toBeNull();
    });
  }
});
