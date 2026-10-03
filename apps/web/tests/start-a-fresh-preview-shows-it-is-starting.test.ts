/**
 * "Start a fresh one" stays "Starting…" until a DIFFERENT preview link arrives. Its starting
 * state used to require that no link existed — exactly never true for a fresh one — so the
 * press looked dead for the minute the new preview took (boss-1, 2026-10-03).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const UI = readFileSync(join(__dirname, "../app/delivery-workflows-ui.tsx"), "utf8");

describe("start a fresh preview", () => {
  it("is starting until the link differs from the one showing at the press", () => {
    expect(UI).toContain("setFreshFrom(previewHref); onRunPreview(buildRunId);");
    expect(UI).toContain("(freshFrom !== undefined ? previewHref === freshFrom : !previewHref)");
    expect(UI).not.toContain("previewStarting === buildRunId && !previewHref;");
  });

  it("says what happens next instead of 'done'", () => {
    expect(UI).toContain('action === "start_preview"');
  });
});
