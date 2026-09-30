import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * "Startup logs" streams, or says why it cannot.
 *
 * Clicking it showed "Waiting for preview output…" forever. Two faults, and the second is the
 * one that made it look like nothing happened:
 *
 *  1. It opened an EventSource straight at the API. The browser cannot reach the API in this
 *     deployment — every other call on the page is server-side — and the API requires
 *     `Authorization: Bearer …`, which EventSource cannot send. The request was refused before
 *     it began.
 *  2. `onerror` closed the connection silently, and the panel's only empty-state text was
 *     "Waiting…". So a stream that had already failed was indistinguishable from one about to
 *     deliver, and it claimed to be waiting indefinitely.
 */
const SRC = codeOnly("app/code-tasks-ui.tsx");
const ROUTE = codeOnly("app/api/preview-logs/route.ts");

/** Bound the slice FORWARD from the declaration: `return (` also appears earlier in this
 *  6,000-line file, and searching from zero sliced backwards to an empty string — which
 *  "contains" nothing and passes any negative assertion while proving nothing. */
function placeholderBlock(): string {
  const from = SRC.indexOf("const placeholder");
  const block = SRC.slice(from, SRC.indexOf("return (", from));
  expect(block.length).toBeGreaterThan(80);
  return block;
}

describe("the stream goes through the server", () => {
  it("connects to the same-origin route, not the API", () => {
    const effect = SRC.slice(SRC.indexOf("function PreviewLogs"), SRC.indexOf("const placeholder"));
    expect(effect).toContain("/api/preview-logs?preview_id=");
    // The direct call is what could never work.
    expect(effect).not.toContain("${PREVIEW_API}/v1/previews/");
  });

  it("the route carries the API key the browser cannot send", () => {
    expect(ROUTE).toContain("authHeaders(");
  });

  it("passes the body through as a stream rather than buffering it", () => {
    // A log you only see once the process exits is not a startup log.
    expect(ROUTE).toContain("new Response(upstream.body");
    expect(ROUTE).not.toContain("await upstream.text()");
  });

  it("tells proxies not to buffer it either", () => {
    expect(ROUTE).toContain('"X-Accel-Buffering": "no"');
    expect(ROUTE).toContain("text/event-stream");
  });
});

describe("failure is visible", () => {
  it("distinguishes connecting, streaming, ended and failed", () => {
    expect(SRC).toContain('useState<"connecting" | "streaming" | "ended" | "failed">');
  });

  it("only says failed when EventSource has actually given up", () => {
    // It reports the same error event for a transient drop it will retry; calling that failure
    // would flap the panel on every hiccup.
    expect(SRC).toContain("source.readyState === EventSource.CLOSED");
  });

  it("stops claiming to be waiting once it has failed", () => {
    const placeholder = placeholderBlock();
    expect(placeholder).toContain('state === "failed"');
    expect(placeholder).toContain("Lost the log stream");
  });

  it("says so when the stream ended with no output at all", () => {
    // Otherwise a preview that simply printed nothing reads as a broken panel.
    const placeholder = placeholderBlock();
    expect(placeholder).toContain("produced none");
  });

  it("reports an upstream failure INTO the stream, where the panel can show it", () => {
    // A 502 status is invisible to an EventSource reader; a log line is not.
    expect(ROUTE).toContain("function errorStream");
    expect(ROUTE).toContain("event: log");
    expect(ROUTE).toContain("Could not reach the preview service");
    expect(ROUTE).toContain("may have expired");
  });
});
