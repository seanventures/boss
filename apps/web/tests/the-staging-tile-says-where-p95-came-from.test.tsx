import { readFileSync } from "node:fs";
import { join } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { StagingP95Source } from "../app/projects/tab-overview";

function sourceLine(p95_ms: number | null, p95_source?: string): HTMLElement | null {
  document.body.innerHTML = renderToStaticMarkup(<StagingP95Source p95_ms={p95_ms} p95_source={p95_source} />);
  return document.querySelector("[data-testid='px-tile-staging-p95-source']");
}

describe("the staging tile says where p95 came from", () => {
  it("says the p95 came from the app's requests", () => {
    const el = sourceLine(207, "app");
    expect(el).not.toBeNull();
    expect(el!.textContent).toBe("From the app's requests.");
    expect(el!.textContent).not.toContain("207");
  });

  it("says the p95 came from health probes", () => {
    const el = sourceLine(180, "probe");
    expect(el).not.toBeNull();
    expect(el!.textContent).toBe("From health probes.");
    expect(el!.textContent).not.toContain("180");
  });

  it("says nothing when the source is missing or anything else", () => {
    expect(sourceLine(207)).toBeNull();
    expect(sourceLine(207, "other")).toBeNull();
    expect(sourceLine(null, "app")).toBeNull();
    expect(sourceLine(null, "probe")).toBeNull();
  });

  it("sits beside the p95 number and leaves that number as the millisecond words", () => {
    const src = readFileSync(join(__dirname, "../app/projects/tab-overview.tsx"), "utf8");
    const at = src.indexOf('data-testid="px-tile-staging-p95"');
    expect(at).toBeGreaterThan(-1);
    const span = src.slice(at, src.indexOf("</span>", at));
    expect(span).toContain("{p95Words(t.staging.p95_ms, t.staging.window)}");
    expect(span).not.toContain("From the app's requests.");
    expect(span).not.toContain("From health probes.");
    const beside = src.slice(at, at + 420);
    expect(beside).toContain("<StagingP95Source");
    expect(beside).toContain("p95_ms={t.staging.p95_ms}");
    expect(beside).toContain("p95_source={t.staging.p95_source}");
  });
});
