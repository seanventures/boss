/**
 * EVERY ALIAS AND UNKNOWN AREA LANDS — the class behind `?area=govern` and `?area=workflows`
 * answering 500 on shared-server. Two doors had it:
 *
 *   1. the middleware's 307 carried a RELATIVE Location; Next's adapter parses it with
 *      `new NextURL(location)` and no base, which throws → 500 on every document load;
 *   2. page.tsx called redirect() after the shell had streamed → 500 on every RSC request.
 *
 * Now: the middleware answers an absolute Location on the browser's own origin (proxied or not),
 * the page renders what the URL MEANS (consoleParams) and never redirects, and an unknown area
 * falls back to the Command Center.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { middleware } from "../middleware";
import { consoleParams } from "../app/legacy-areas";
import { AREA_IDS } from "../lib/area-ids";

afterEach(() => vi.unstubAllEnvs());

const ALIASES: [string, string][] = [
  ["govern", "budget"], ["workflows", "delivery_workflows"], ["studio", "workforce"],
  ["org_chart", "workforce"], ["capabilities", "skills"], ["delivery", "infra"],
  ["monitoring", "observability"], ["not_an_area_xyz", "overview"],
];

function proxied(path: string, headers: Record<string, string> = {}) {
  return new NextRequest(`http://10.0.0.5:3000${path}`, {
    headers: { host: "boss.example.test", "x-forwarded-host": "boss.example.test",
               "x-forwarded-proto": "https", ...headers },
  });
}

describe("every alias and unknown area", () => {
  for (const [alias, area] of ALIASES) {
    it(`?area=${alias} → ${area}, as an absolute 307 on the public origin`, async () => {
      vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
      vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
      const res = await middleware(proxied(`/?org=org_boss&area=${alias}&tab=sharing`));
      expect(res.status).toBe(307);
      const location = res.headers.get("location") ?? "";
      const url = new URL(location); // what Next's adapter does: throws on a relative one
      expect(url.origin).toBe("https://boss.example.test");
      expect(url.searchParams.get("area")).toBe(area);
      expect(url.searchParams.get("org")).toBe("org_boss");
      const meant = consoleParams(new URLSearchParams(`area=${alias}`));
      expect(meant.changed).toBe(true);
      expect(meant.params.get("area")).toBe(area);
    });
  }

  it("leaves every area that renders alone", () => {
    const aliases = new Set(ALIASES.map(([a]) => a));
    for (const id of AREA_IDS.filter((a) => !aliases.has(a))) {
      const meant = consoleParams(new URLSearchParams(`area=${id}`));
      expect(meant.changed, id).toBe(false);
    }
  });

  it("uses the configured public origin when there is one", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example.com/");
    const res = await middleware(proxied("/?area=govern"));
    expect(res.headers.get("location")).toBe("https://boss.example.com/?area=budget");
  });

  it("the page renders what the URL means and never redirects", () => {
    const page = readFileSync(join(process.cwd(), "app", "page.tsx"), "utf8")
      .replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
    expect(page).not.toMatch(/\bredirect\(/);
    expect(page).toContain("consoleParams(searchParamsFrom(rawSearchParams)).params");
  });

  it("keeps a browser on this machine on the host it typed (a rewrite, never a Location)", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
    // What Next hands middleware for a browser on 127.0.0.1: an internal localhost URL and host.
    const res = await middleware(new NextRequest("http://localhost:3000/?area=govern", {
      headers: { host: "localhost:3000", "x-forwarded-host": "localhost:3000" } }));
    expect(res.status).toBe(200);
    expect(res.headers.get("location")).toBeNull();
    expect(new URL(res.headers.get("x-middleware-rewrite")!).searchParams.get("area")).toBe("budget");
  });

  it("the collector's three POST paths never pass middleware (Next would tee their bodies)", async () => {
    const { config } = await import("../middleware");
    const re = new RegExp(`^${config.matcher[0].replace(/^\//, "\\/")}$`);
    for (const p of ["/v1/telemetry/ingest", "/v1/telemetry/delete", "/v1/telemetry/share"]) {
      expect(re.test(p), p).toBe(false);
    }
    for (const p of ["/", "/v1/telemetry/sharing", "/v1/telemetry/ingest/x", "/api/improve"]) {
      expect(re.test(p), p).toBe(true);
    }
  });

  it("a client navigation (router.push) is not redirected: the page renders the target with 200", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "");
    for (const extra of [{ path: "/?area=govern", headers: { "sec-fetch-dest": "empty" } },
                         { path: "/?area=govern", headers: { accept: "text/x-component" } },
                         { path: "/?area=govern&_rsc=abc", headers: {} }]) {
      const res = await middleware(proxied(extra.path, extra.headers as Record<string, string>));
      expect(res.status, extra.path).toBe(200);
      expect(res.headers.get("x-middleware-next")).toBe("1");
    }
  });
});
