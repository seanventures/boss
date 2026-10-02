/**
 * Pressing "Stop and run again" twice started two workflows on boss-1 (2026-10-02).
 *
 * `busy` disabled the buttons only after React re-rendered, so a quick second click still fired,
 * and every start carried a fresh server-side dedupe key, so nothing tied the second request to
 * the first. The console now refuses a second action while one is in flight (a ref, set before
 * the first await), and a replacement carries `replace:<run id>` — the server returns the run
 * that key already started.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const UI = readFileSync(join(__dirname, "..", "app", "delivery-workflows-ui.tsx"), "utf8");

describe("a workflow is started once per press", () => {
  it("refuses a second action while one is in flight, checked before any await", () => {
    expect(UI).toContain("const inFlight = useRef(false);");
    expect(UI).toMatch(/if \(inFlight\.current\) return/);
    expect(UI).toContain("inFlight.current = false;");
  });

  it("names the run being replaced, so a second press returns the same replacement", () => {
    expect(UI).toContain("dedupe_key: `replace:${run.id}`");
  });

  it("gives every other start its own key for retries", () => {
    expect(UI).toContain("dedupe_key: `again:${run.id}:${crypto.randomUUID()}`");
  });
});
