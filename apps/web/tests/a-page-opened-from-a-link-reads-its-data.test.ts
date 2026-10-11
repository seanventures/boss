/**
 * A PAGE OPENED FROM A LINK READS ITS DATA (first-run walk, 2026-10-10).
 *
 * Approvals showed "Decision API unavailable · API returned 403" for the Owner, while the same
 * call from the browser answered 200. The page's server render reads the inbox through the
 * writing GET's POST twin (/v1/decisions/inbox/load); the API checks a cookie POST's CSRF
 * (Origin == the public origin, X-Boss-CSRF == the session's token: engine identity/authn.py
 * check_csrf). The render forwarded an Origin only when the page request itself carried
 * same-origin proof, so a page opened from a link (the PR on GitHub, the preview on its own port,
 * an email, a console on plain http where browsers send no fetch metadata) left the twin with
 * none, and the API refused it 403 csrf_failed for every role.
 *
 * Held here end to end on the console side: the REAL resolver (instrumentation.ts, next/headers
 * mocked as the incoming page request) and the REAL typed client + server fetch, against a fake
 * API that applies check_csrf's rule. The Approvals area's own loader (fetchDecisionFeed, what
 * page.tsx calls for `area=decisions`) must get the Owner's data for every way a person opens the
 * page — and the widening must stay a page READ: a route handler, a cross-site sub-resource and
 * an ordinary write still leave with no Origin. apps/api/tests/test_a_page_render_reads_the_inbox_as_owner.py
 * holds the API's half with a real Owner session.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installIdentityResolver } from "../lib/identity/forward";

const API = "http://api.internal:8000";
const CONSOLE = "http://localhost:3000";
const CSRF = "csrf_owner";

type Sent = { url: string; method: string; headers: Headers };

let incoming = new Headers();
const sent: Sent[] = [];

/** check_csrf's rule for a cookie write, and the inbox answer when it passes. */
function fakeApi(url: string, method: string, headers: Headers): Response {
  if (url.endsWith("/v1/identity/me")) return Response.json({ csrf_token: CSRF });
  if (method !== "GET") {
    const site = headers.get("sec-fetch-site");
    const ok = headers.get("origin") === CONSOLE && (site === null || site === "same-origin")
      && headers.get("x-boss-csrf") === CSRF && headers.get("cookie") === "__Host-boss_session=bss_owner";
    if (!ok) return Response.json({ error: "csrf_failed" }, { status: 403 });
  }
  if (url.includes("/v1/decisions/inbox/load")) {
    return Response.json({
      project_id: "p1", limit: null, offset: 0,
      items: [{ id: "dec_1", question: "Merge the first PR?", status: "pending", risk_tier: 2,
                recommendation: {}, options: [], cost_summary: {},
                created_at: "2026-10-10T00:00:00Z", updated_at: "2026-10-10T00:00:00Z" }],
    });
  }
  return Response.json({});
}

beforeEach(() => {
  sent.length = 0;
  vi.resetModules();
  vi.stubEnv("NEXT_RUNTIME", "nodejs");
  vi.stubEnv("BOSS_API_URL", API);
  vi.doMock("next/headers", () => ({ headers: async () => incoming }));
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    sent.push({ url, method, headers });
    return fakeApi(url, method, headers);
  }));
});

afterEach(() => {
  vi.doUnmock("next/headers");
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  delete (globalThis as { __bossIdentityFetch?: true }).__bossIdentityFetch;
  delete (globalThis as { __bossFeedCache?: unknown }).__bossFeedCache;
  installIdentityResolver(async () => null);
});

/** The page request as the browser sent it (path header: what middleware.ts stamps). */
async function asOwnerOpening(path: string, browser: Record<string, string>) {
  incoming = new Headers({ host: "localhost:3000", cookie: "__Host-boss_session=bss_owner; theme=dark",
                           "x-boss-path": path, ...browser });
  await (await import("../instrumentation")).register();
}

const OPENED = {
  "a click inside the console (RSC, same-origin)":
    { "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", accept: "text/x-component" },
  "a typed URL or a reload": { "sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", accept: "text/html" },
  "a link on the PR on GitHub (cross-site)":
    { "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", accept: "text/html" },
  "a link from the preview on its own port (same-site)":
    { "sec-fetch-site": "same-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", accept: "text/html" },
  "a console on plain http (no fetch metadata)": { accept: "text/html,application/xhtml+xml" },
  "a client navigation on plain http (no fetch metadata)": { accept: "text/x-component" },
} as const;

describe("the Approvals page, rendered on the server as the Owner, gets its data", () => {
  it.each(Object.entries(OPENED))("opened by %s", async (_how, browser) => {
    await asOwnerOpening("/", browser);
    const { fetchDecisionFeed } = await import("../lib/decisions");
    const feed = await fetchDecisionFeed("p1");
    expect(feed.blockedReason).toBeUndefined();
    expect(feed.apiReachable).toBe(true);
    expect(feed.items.map((d) => d.id)).toEqual(["dec_1"]);
    const twin = sent.find((s) => s.url.includes("/v1/decisions/inbox/load"))!;
    expect(twin.method).toBe("POST");
    expect(twin.headers.get("origin")).toBe(CONSOLE);
  });
});

describe("the widening is a page READ, nothing more", () => {
  async function twinOrigin(path: string, browser: Record<string, string>, call: string, method = "POST") {
    await asOwnerOpening(path, browser);
    await import("../lib/client");     // installs the server fetch
    await fetch(`${API}${call}`, { method, body: "{}", headers: { "content-type": "application/json" } });
    return sent.find((s) => s.url.endsWith(call))!.headers.get("origin");
  }
  const crossSiteLink = OPENED["a link on the PR on GitHub (cross-site)"];

  it("a route handler reached by a cross-site navigation still gets no Origin", async () => {
    expect(await twinOrigin("/api/kit", crossSiteLink, "/v1/kit/load")).toBeNull();
  });
  it("an ordinary write during a cross-site page render still gets no Origin", async () => {
    expect(await twinOrigin("/", crossSiteLink, "/v1/decisions/dec_1/approve")).toBeNull();
  });
  it("a cross-site image or script pointing at a page gets no Origin", async () => {
    expect(await twinOrigin("/", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors",
                                   "sec-fetch-dest": "image", accept: "image/*" }, "/v1/kit/load")).toBeNull();
  });
  it("on plain http, a request that did not ask for a page gets no Origin", async () => {
    expect(await twinOrigin("/", { accept: "image/avif,image/webp,*/*" }, "/v1/kit/load")).toBeNull();
  });
});
