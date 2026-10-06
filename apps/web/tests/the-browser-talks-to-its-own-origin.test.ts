import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

/**
 * Twenty-one client components fetched the control plane cross-origin, and the API has no CORS
 * middleware anywhere in the repository. Measured on the live console:
 *
 *   Access to fetch at 'https://boss-api.../v1/factory/flow?project_id=boss-dogfood'
 *   from origin 'https://boss...' has been blocked by CORS policy
 *   OPTIONS /v1/factory/flow -> 401, no access-control-allow-origin
 *
 * The helpers catch their own errors and return an empty feed, so those areas rendered BLANK
 * rather than broken. That is the worst shape a failure can take: it looks like "no data yet",
 * so nobody goes looking for a cause. `app/api/preview-logs/route.ts` had already diagnosed this
 * exact thing for one endpoint — "the browser cannot reach the API at all in this deployment".
 *
 * These are source assertions, which this repo has been bitten by before (a test asserting
 * `self.agent.chat(` passed for months while the call it named could never run). They are used
 * here only for things a build cannot express: which side of the server/browser split a value
 * is read on. The behaviour of the proxy itself is tested by calling it.
 */

const CLIENT = readFileSync(new URL("../lib/client.ts", import.meta.url), "utf8");
const PROXY = readFileSync(new URL("../app/v1/[...path]/route.ts", import.meta.url), "utf8");
const FORWARD = readFileSync(new URL("../lib/identity/forward.ts", import.meta.url), "utf8");

describe("the base url the browser uses", () => {
  it("is same-origin, so no request is subject to CORS", () => {
    expect(CLIENT).toContain("export const API_URL = IS_SERVER");
    // The browser branch must be the empty string: `${API_URL}/v1/x` -> `/v1/x`.
    expect(CLIENT).toMatch(/:\s*"";/);
  });

  it("still gives the server the absolute address it needs", () => {
    expect(CLIENT).toContain("process.env.BOSS_API_URL");
    expect(CLIENT).toContain("process.env.NEXT_PUBLIC_BOSS_API_URL");
  });
});

describe("the api key", () => {
  it("does not exist in the console any more (identity S4)", () => {
    // NEXT_PUBLIC_* is inlined into the bundle at build time, so reading it client-side shipped
    // a control-plane credential to every visitor. Then the server held one shared key. Now
    // neither: every call carries the signed-in person's own session (lib/identity/forward.ts).
    expect(CLIENT).not.toMatch(/export const API_KEY\b/);
    expect(CLIENT).not.toMatch(/process\.env\.(NEXT_PUBLIC_)?BOSS_API_KEY/);
  });

  it("is not attached by the proxy either: the person's cookie is", () => {
    expect(PROXY).not.toContain('out.set("Authorization"');
    expect(PROXY).toContain("identityCookieHeader(req.headers.get(\"cookie\")");
  });
});

describe("the proxy", () => {
  it("refuses to take an identity from the caller", () => {
    // A browser that could set its own Authorization, or an X-Boss-* principal header, would be
    // choosing who it is. Everything but the identity cookies and the CSRF token is stripped.
    expect(PROXY).toContain("isStrippedHeader(name)");
    expect(FORWARD).toMatch(/authorization/);
    expect(FORWARD).toContain('if (n.startsWith("x-boss-")) return true;');
  });

  it("covers every method a feed uses, not just GET", () => {
    for (const method of ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]) {
      expect(PROXY).toContain(`export const ${method} = handler`);
    }
  });

  it("streams the body through rather than buffering it", () => {
    // A buffering proxy defeats SSE, which is how preview logs arrive.
    expect(PROXY).toContain("new Response(upstream.body");
  });

  it("names the variable when it has nowhere to send the request", () => {
    expect(PROXY).toContain("BOSS_API_URL is not set");
    expect(PROXY).toContain("503");
  });

  it("reports an unreachable control plane as a failure, not an empty feed", () => {
    expect(PROXY).toContain("502");
  });

  it("forwards the query string, which every feed uses for project_id", () => {
    expect(PROXY).toContain("incoming.search");
  });
});
