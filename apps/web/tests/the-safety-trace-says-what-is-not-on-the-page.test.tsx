/**
 * The trace view draws the first forty events. When the timeline is longer, it
 * says how many are on this page and how many are not. A timeline that fits
 * stays quiet. The sentence is traceRestSentence(trace.timeline.length), not
 * the run's eventCount.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, refresh: () => {} }),
  useSearchParams: () => new URLSearchParams(),
}));

import { RunTraceViewer, traceRestSentence } from "../app/safety-ui";
import type { RunTrace } from "../lib/safety";

function traceOf(count: number, eventCount = count): NonNullable<RunTrace> {
  return {
    run: {
      id: "ctr_trace",
      status: "running",
      phase: "build",
      title: "Ship the trace",
      pullRequestUrl: null,
    },
    qualityGate: null,
    timeline: Array.from({ length: count }, (_, i) => ({
      eventType: `trace_event_${i + 1}`,
      actor: "ceo",
      createdAt: "2026-08-12T01:25:15.000Z",
    })),
    eventCount,
  };
}

function markup(count: number, eventCount?: number): string {
  return renderToStaticMarkup(
    <RunTraceViewer trace={traceOf(count, eventCount)} initialRunId="ctr_trace" />,
  );
}

function restParagraph(html: string): string | undefined {
  return html.match(/<p[^>]*>[^<]*<\/p>/g)?.find((p) => p.includes("are on this page"));
}

describe("the safety trace says what is not on the page", () => {
  it("says forty events are on the page and how many more are not", () => {
    expect(traceRestSentence(41)).toBe("40 events are on this page. 1 more are not.");
    expect(traceRestSentence(55)).toBe("40 events are on this page. 15 more are not.");

    const html = markup(41, 90);
    const note = restParagraph(html);
    expect(note).toBeTruthy();
    expect(note).toContain("40 events are on this page. 1 more are not.");
    expect(html).toContain("trace_event_40");
    expect(html).not.toContain("trace_event_41");
    expect(html.indexOf("40 events are on this page. 1 more are not.")).toBeGreaterThan(
      html.lastIndexOf("trace_event_40"),
    );
  });

  it("says nothing when every event is on the page", () => {
    expect(traceRestSentence(40)).toBeNull();
    expect(traceRestSentence(0)).toBeNull();
    expect(traceRestSentence(1)).toBeNull();
    const html = markup(40, 200);
    expect(html).not.toContain("more are not");
    expect(html).not.toContain("are on this page");
    expect(html).toContain("trace_event_40");
  });

  it("calls the sentence with the timeline length", () => {
    const src = readFileSync(join(__dirname, "../app/safety-ui.tsx"), "utf8");
    expect(src).toContain("traceRestSentence(trace.timeline.length)");
  });
});
