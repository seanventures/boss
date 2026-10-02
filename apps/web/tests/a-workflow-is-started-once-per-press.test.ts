/**
 * Pressing "Stop and run again" twice started two workflows on boss-1 (2026-10-02).
 *
 * `busy` disabled the buttons only after React re-rendered, so a quick second click still fired,
 * and every start carried a fresh server-side dedupe key, so nothing tied the second request to
 * the first. The console now refuses a second action while one is in flight, and a replacement
 * carries `replace:<run id>` — the server returns the run that key already started.
 *
 * REWRITTEN to the platform rule, twice. The first fix was a ref private to this panel; the
 * second (45e7693) put every write of the panel in one scope — the automatic recheck included,
 * which then refused every recheck but the first and silently dropped a Retry pressed meanwhile.
 * Now every PRESS on the panel goes through `pressOn`, which holds the press scope in
 * lib/api-write's one registry before any await and is the only thing that sets `busy`; the
 * background recheck takes no scope at all. The behaviour is proven by mounting the panel in
 * tests/a-background-write-never-takes-a-press.test.tsx; what is pinned here is the wiring.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const UI = readFileSync(join(__dirname, "..", "app", "delivery-workflows-ui.tsx"), "utf8");

describe("a workflow is started once per press", () => {
  it("every press on the panel holds the press scope, and only presses set busy", () => {
    const panel = UI.slice(UI.indexOf("export function DeliveryWorkflows("),
                           UI.indexOf("\nexport function", UI.indexOf("export function DeliveryWorkflows(") + 1));
    const pressOn = panel.slice(panel.indexOf("const pressOn = useCallback("),
                                panel.indexOf("const act = useCallback("));
    expect(pressOn).toMatch(/runPress<T>\(\{ press: \{ scope: PANEL_PRESSES, label \}/);
    // `busy` is set in exactly one place: inside the press.
    expect(panel.match(/setBusy\((?!null)/g)?.length).toBe(1);
    expect(pressOn).toContain("setBusy(label)");
    expect(panel).toMatch(/const act = useCallback\([\s\S]*?pressOn\(label, \[action, payload\]/);
    expect(UI).not.toContain("inFlight.current");
    expect(UI).not.toContain("writeInFlight");
  });

  it("the automatic recheck is not a press: it goes out without a press scope or key", () => {
    expect(UI).toMatch(/post\("recheck", \{ project_id: projectId, actor, run_id: r\.id \}\)\)/);
  });

  it("names the run being replaced, so a second press returns the same replacement", () => {
    expect(UI).toContain("dedupe_key: `replace:${run.id}`");
  });

  it("gives every other start its own key for retries — minted from the press, not the payload", () => {
    // REWRITTEN (review of 6ca9119): this pinned `again:${run.id}:${crypto.randomUUID()}` in the
    // payload. The payload is what pressOn recognises a press by, so the random part made a
    // double click a DIFFERENT press: refused and toasted instead of joining the first. The key
    // is now minted from the press's own key inside the send; behaviour in
    // tests/a-double-rerun-sends-once.test.ts.
    expect(UI).toContain('}, `again:${run.id}`)}');
    expect(UI).toContain("dedupe_key: `${dedupeFromPress}:${key}`");
    const panel = UI.slice(UI.indexOf("export function DeliveryWorkflows("));
    expect(panel.slice(0, panel.indexOf("\nexport function", 1))).not.toContain("crypto.randomUUID()");
  });
});
