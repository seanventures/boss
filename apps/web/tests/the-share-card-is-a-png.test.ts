/**
 * The share card is a real 1200×630 PNG (Agent World spec §3.12).
 *
 * next/og lays out with a flexbox engine that refuses, at REQUEST time, a div with several
 * children and no `display: flex` — a mistake tsc and the build both let through. So the route
 * is called here, against a fake control plane, and the bytes it returns are checked: a PNG of
 * the right size. And what it must never show is checked on what it was given to draw.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  API_URL: "http://api.test",
  authHeaders: (extra?: Record<string, string>) => ({ Authorization: "Bearer test", ...(extra ?? {}) }),
}));

const PASSPORT = {
  agent_id: "agt_7f3k2q4m9x", role_key: "qa", display_name: "QA Lead",
  persona: { version: 1, archetype: null, callsign: "Vera", tagline: "Show me the test.",
             about_me: "I check what was claimed against what ran.", avatar: { kit: "boss-v1", seed: "agt_7f3k2q4m9x" } },
  born: { at: "2026-10-03T09:12:00Z", by: "ceo", from: { kind: "pack" } }, sponsor: "ceo", lifecycle: "active",
  version: 4, fingerprint: "9f3ac21e", vitals: { trust: 82, runs: 41, clean_rate: 0.96, cost_7d: 3.1, panels: 2, apps: 3 },
  presence: { state: "idle", line: "" },
};

afterEach(() => vi.unstubAllGlobals());

describe("the share card route", () => {
  it("answers a 1200×630 PNG, drawn from the passport and the card", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      const url = String(input);
      seen.push(url);
      if (url.includes("/passport")) return Response.json(PASSPORT);
      if (url.includes("/card")) return Response.json({ skills: [{ id: "qa.run_tests", name: "Run tests" }] });
      return Response.json({}, { status: 404 });
    }));
    const { GET } = await import("../app/api/agent-card/[role]/route");
    const res = await GET(new Request("http://console.test/api/agent-card/qa?project_id=p1&stats=1"),
                          { params: Promise.resolve({ role: "qa" }) });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("image/png");
    const bytes = new Uint8Array(await res.arrayBuffer());
    // To look at it: AG_CARD_OUT=/path/card.png npx vitest run tests/the-share-card-is-a-png.test.ts
    if (process.env.AG_CARD_OUT) (await import("node:fs")).writeFileSync(process.env.AG_CARD_OUT, bytes);
    expect([...bytes.slice(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    const view = new DataView(bytes.buffer);
    expect([view.getUint32(16), view.getUint32(20)]).toEqual([1200, 630]);
    expect(seen).toEqual([
      "http://api.test/v1/agents/qa/passport?project_id=p1",
      "http://api.test/v1/agents/qa/card?project_id=p1",
    ]);
  }, 30_000);

  it("refuses a request with no project, rather than drawing somebody's card", async () => {
    const { GET } = await import("../app/api/agent-card/[role]/route");
    const res = await GET(new Request("http://console.test/api/agent-card/qa"), { params: Promise.resolve({ role: "qa" }) });
    expect(res.status).toBe(400);
  });

  it("says the control plane failed when the passport cannot be read", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "nope" }, { status: 500 })));
    const { GET } = await import("../app/api/agent-card/[role]/route");
    const res = await GET(new Request("http://console.test/api/agent-card/qa?project_id=p1"), { params: Promise.resolve({ role: "qa" }) });
    expect(res.status).toBe(502);
  });
});
