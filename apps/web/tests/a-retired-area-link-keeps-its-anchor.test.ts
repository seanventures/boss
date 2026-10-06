/**
 * A link to a retired area is an HTTP redirect, so its #anchor survives (P-I-07).
 *
 * `?area=delivery#hardware-manager` (the old "Where apps install" anchor) must land on Infra and
 * open This computer. page.tsx's redirect() ran after the shell had streamed, the browser followed
 * it as a client navigation and the fragment was gone, so nothing opened. The middleware answers a
 * document load with a 307 to the rewritten URL — a browser carries the fragment across a 3xx —
 * and Infra's home opens This computer from it.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { middleware } from "../middleware";

afterEach(() => vi.unstubAllEnvs());

const req = (path: string, headers: Record<string, string> = {}) =>
  new NextRequest(`http://127.0.0.1:3000${path}`, { headers: { host: "127.0.0.1:3000", ...headers } });

describe("a retired area link", () => {
  // DIRECT TO THIS SERVER, it is a REWRITE: the target renders on the browser's own address, so
  // the fragment is never at risk (no navigation happens) and the host is the one the browser
  // typed. Behind a proxy it is an absolute 307 (tests/every-alias-area-lands.test.ts).
  it("is rendered where it lives now, on the browser's own address", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    const res = await middleware(req("/?org=org_boss&area=delivery"));
    expect(res.status).toBe(200);
    expect(res.headers.get("location")).toBeNull();  // the browser stays where it is
    expect(new URL(res.headers.get("x-middleware-rewrite")!).search).toBe("?org=org_boss&area=infra&view=fleet");
  });

  it("leaves current areas, RSC fetches and server actions to the console", async () => {
    vi.stubEnv("BOSS_CONSOLE_AUTH", "network");
    expect((await middleware(req("/?area=infra&view=fleet"))).status).toBe(200);
    const rsc = await middleware(req("/?area=delivery", { rsc: "1" }));
    expect(rsc.status).toBe(200);
    expect(rsc.headers.get("x-middleware-rewrite")).toBeNull();
  });

  it("Infra's home opens This computer from the kept #hardware-manager anchor", () => {
    const home = readFileSync(join(process.cwd(), "app", "infra", "infra-home.tsx"), "utf8");
    expect(home).toMatch(/hash === "#hardware-manager"[\s\S]{0,200}host: "this"/);
  });
});
