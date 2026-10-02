/**
 * Pressing "Stop and run again" twice started two workflows on boss-1 (2026-10-02).
 *
 * `busy` disabled the buttons only after React re-rendered, so a quick second click still fired,
 * and every start carried a fresh server-side dedupe key, so nothing tied the second request to
 * the first. The console now refuses a second action while one is in flight, and a replacement
 * carries `replace:<run id>` — the server returns the run that key already started.
 *
 * REWRITTEN to the platform rule. The first fix was a ref private to this panel; the next button
 * would have needed its own. The in-flight refusal now comes from lib/api-write.ts, which every
 * console write goes through (tests/every-write-goes-through-api-write.test.ts), and whose
 * behaviour is pinned in tests/a-press-is-sent-once.test.ts. What is checked here is only that
 * this panel uses it, synchronously, before any await.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const UI = readFileSync(join(__dirname, "..", "app", "delivery-workflows-ui.tsx"), "utf8");

describe("a workflow is started once per press", () => {
  it("refuses a second action while one is in flight, checked before any await", () => {
    const act = UI.slice(UI.indexOf("const act = useCallback("));
    const firstAwait = act.indexOf("await ");
    const guard = act.indexOf("if (writeInFlight(RUN_ACTIONS)) return");
    expect(guard).toBeGreaterThan(-1);
    expect(guard).toBeLessThan(firstAwait);
    // ...and every run action is in that one scope, so the guard covers them all.
    expect(UI).toMatch(/apiWrite\("\/api\/delivery-workflows", \{[\s\S]*?\}, \{ action: RUN_ACTIONS \}\)/);
    expect(UI).not.toContain("inFlight.current");
  });

  it("names the run being replaced, so a second press returns the same replacement", () => {
    expect(UI).toContain("dedupe_key: `replace:${run.id}`");
  });

  it("gives every other start its own key for retries", () => {
    expect(UI).toContain("dedupe_key: `again:${run.id}:${crypto.randomUUID()}`");
  });
});
