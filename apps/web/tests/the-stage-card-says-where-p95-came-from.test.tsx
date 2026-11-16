import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { stageP95SourceSentence } from "../app/projects/drawers-more";

/** What production change this catches: the stage card hides an app or probe source, or invents one. */
describe("the stage card says where p95 came from", () => {
  it("says the app's requests when the source is the app", () => {
    expect(stageP95SourceSentence("app")).toBe("From the app's requests.");
  });

  it("says health probes when the source is a probe", () => {
    expect(stageP95SourceSentence("probe")).toBe("From health probes.");
  });

  it("says nothing when the source is missing or anything else", () => {
    expect(stageP95SourceSentence(undefined)).toBeNull();
    expect(stageP95SourceSentence(null)).toBeNull();
    expect(stageP95SourceSentence("")).toBeNull();
    expect(stageP95SourceSentence("gateway")).toBeNull();
  });

  it("calls stageP95SourceSentence beside the p95 span", () => {
    const src = readFileSync(join(__dirname, "../app/projects/drawers-more.tsx"), "utf8");
    const at = src.indexOf("e.p95_ms !== null ? `p95 ${e.p95_ms}ms`");
    expect(at).toBeGreaterThan(-1);
    const spanClose = src.indexOf("</span>", at);
    const span = src.slice(at, spanClose);
    expect(span).toContain("p95 ${e.p95_ms}ms");
    expect(span).not.toContain("stageP95SourceSentence");
    expect(span).not.toContain("From the app's requests.");
    expect(span).not.toContain("From health probes.");
    const beside = src.slice(spanClose, spanClose + 600);
    expect(beside).toContain("stageP95SourceSentence(e.p95_source)");
    expect(beside).toContain("px-stage-env-p95-source-${e.key}");
    const work = src.indexOf('data-testid="px-stage-work"');
    expect(work).toBeGreaterThan(-1);
    expect(work).toBeLessThan(at);
    expect(src.slice(work, at)).not.toContain("stageP95SourceSentence");
  });
});
