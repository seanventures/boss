/**
 * A dead cookie, or a network-mode console whose bridge is off, gets past middleware (the cookie
 * or the mode is enough). The v2 page then sends the visitor to sign in. That redirect used to
 * name the path only, so /v2/timeline?org=org_boss came back as next=/v2/timeline and the org
 * the bookmark asked for was gone. The v1 console keeps the query.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const boot = { me: null as unknown, org: null as unknown, project: null as unknown, problem: null as string | null };
const requestHeaders = new Headers();

vi.mock("../lib/identity/server-cache", () => ({ loadIdentityCached: async () => boot }));
vi.mock("next/navigation", () => ({ redirect: (to: string) => { throw new Error(`redirect ${to}`); } }));
vi.mock("next/headers", () => ({
  headers: async () => requestHeaders,
  cookies: async () => ({ get: () => undefined }),
}));

import { consolePageAllowed } from "../components/identity/route-shell";

beforeEach(() => {
  boot.me = null;
  boot.org = null;
  boot.project = null;
  boot.problem = null;
  [...requestHeaders.keys()].forEach((key) => requestHeaders.delete(key));
});

describe("a dead v2 session", () => {
  it("signs in again without forgetting the org", async () => {
    boot.problem = "unauthenticated";
    requestHeaders.set("x-boss-search", "?org=org_boss");
    await expect(consolePageAllowed("/v2/timeline")).rejects.toEqual(
      new Error("redirect /login?next=%2Fv2%2Ftimeline%3Forg%3Dorg_boss"),
    );
  });

  it("with no query, signs in to the path alone", async () => {
    boot.problem = "unauthenticated";
    await expect(consolePageAllowed("/v2")).rejects.toEqual(
      new Error("redirect /login?next=%2Fv2"),
    );
  });

  it("drops a search that could break out of the sign-in link", async () => {
    boot.problem = "unauthenticated";
    requestHeaders.set("x-boss-search", "?org=org_boss#https://evil.example");
    await expect(consolePageAllowed("/v2/boss")).rejects.toEqual(
      new Error("redirect /login?next=%2Fv2%2Fboss"),
    );
  });
});
