// @vitest-environment node
/**
 * The Credits image is a real PNG, drawn from the run's chain through the API (spec §3.6).
 * The route is called as Next calls it, with only the API stood in.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { shipItChain } from "./_helpers/run-acts-fixtures";

// The test runner compiles JSX classically; Next compiles the route with the automatic runtime.
(globalThis as Record<string, unknown>).React = React;
const { GET } = await import("../app/api/run-credits/[id]/route");

afterEach(() => vi.unstubAllGlobals());

function call(id: string, query = "project_id=p1&title=Ship%20it") {
  return GET(new Request(`http://console.test/api/run-credits/${id}?${query}`), { params: Promise.resolve({ id }) });
}

describe("the Credits image", () => {
  it("is a 1200×630 PNG of the run's credits", async () => {
    const asked: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      asked.push(String(input));
      return Response.json(shipItChain());
    }));
    const r = await call("wfr_1");
    expect(r.status).toBe(200);
    expect(r.headers.get("content-type")).toBe("image/png");
    const bytes = new Uint8Array(await r.arrayBuffer());
    expect([...bytes.slice(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    // IHDR: width and height, big-endian, at bytes 16-23.
    const view = new DataView(bytes.buffer);
    expect([view.getUint32(16), view.getUint32(20)]).toEqual([1200, 630]);
    expect(asked[0]).toMatch(/\/v1\/workflow-runs\/wfr_1\/chain\?project_id=p1$/);
  }, 60_000);

  it("credits a build from the build's own chain, not the run around it", async () => {
    const asked: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      asked.push(String(input));
      return Response.json(shipItChain());
    }));
    const r = await call("crun_1");
    expect(r.status).toBe(200);
    expect(asked[0]).toMatch(/\/v1\/code-tasks\/crun_1\/chain\?project_id=p1$/);
  }, 60_000);

  it("refuses a run with nobody to credit, and a malformed id", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ acts: [], totals: {}, credits: [] })));
    expect((await call("wfr_2")).status).toBe(404);
    expect((await call("../etc")).status).toBe(400);
  });
});
