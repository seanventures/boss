/**
 * HOTFIX (boss-1, 2026-10-07): `boss identity recover-owner` printed `/auth/recover#token=…`, a page
 * that did not exist; the visitor was sent to /login with the token, while /reset (which consumes
 * it) reads `#t=`. Every link the engine mints is now in ONE table (engine/boss_engine/identity/
 * links.py → lib/identity/auth-links.gen.ts). This walks it on the console side:
 *   - the page each link lands on exists and reads its token with that kind's fragment key;
 *   - every such page (and every alias an older build printed) is public: middleware never sends
 *     it to /login;
 *   - a redirect to /login carries an EMPTY fragment, so no link's token ever rides along.
 * The engine half (the generated copy is current, no link spelled by hand, recover-owner → reset →
 * enrolment → signed-in Owner) is apps/api/tests/test_every_minted_link_lands_on_a_real_page.py.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { AUTH_LINKS, LEGACY_LINK_ALIASES, type AuthLinkKind } from "../lib/identity/auth-links.gen";
import { isPublicConsolePath } from "../lib/identity/pages";
import { middleware } from "../middleware";

const APP = join(__dirname, "..", "app");

function pages(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? pages(full) : name === "page.tsx" ? [full] : [];
  });
}
const routeOf = (page: string) => "/" + relative(APP, page).split("/").slice(0, -1).filter((p) => !/^\(.*\)$/.test(p)).join("/");
const PAGES = new Map(pages(APP).map((p) => [routeOf(p).replace(/\/$/, "") || "/", p]));

/** The page's own source and every sibling component it renders. */
function sourceOf(route: string): string {
  const page = PAGES.get(route)!;
  const dir = page.slice(0, page.lastIndexOf("/"));
  const own = readdirSync(dir).filter((f) => f.endsWith(".tsx") || f.endsWith(".ts")).map((f) => readFileSync(join(dir, f), "utf8"));
  // …and the components the page imports from a parent directory (e.g. /signup/verify → ../signup).
  const imported = [...readFileSync(page, "utf8").matchAll(/from "(\.\.\/[^"]+)"/g)]
    .map((m) => join(dir, m[1])).flatMap((base) => [`${base}.tsx`, `${base}.ts`])
    .filter((f) => { try { return statSync(f).isFile(); } catch { return false; } })
    .filter((f) => f.startsWith(APP)).map((f) => readFileSync(f, "utf8"));
  return [...own, ...imported].join("\n");
}

afterEach(() => vi.unstubAllEnvs());

describe("every link the engine mints lands on a real page that reads its token", () => {
  it.each(Object.entries(AUTH_LINKS))("%s → %o", (kind, link) => {
    expect(PAGES.has(link.path), `${kind}: no page at ${link.path}`).toBe(true);
    expect(isPublicConsolePath(link.path), `${kind}: ${link.path} would be sent to /login`).toBe(true);
    // The page reads the token with a kind whose table entry is this path and this fragment key.
    const src = sourceOf(link.path);
    const readers = [...src.matchAll(/linkToken\("([a-z_]+)"\)/g)].map((m) => m[1] as AuthLinkKind);
    const fragmentReader = /fragmentToken\(\)/.test(src) ? (["magic_link"] as AuthLinkKind[]) : [];
    const ok = [...readers, ...fragmentReader].some((k) => AUTH_LINKS[k]?.path === link.path && AUTH_LINKS[k]?.fragment === link.fragment);
    expect(ok, `${link.path} does not read #${link.fragment}= (linkToken("${kind}"))`).toBe(true);
  });

  it.each(Object.entries(LEGACY_LINK_ALIASES))("an older build's %s forwards to its kind's page", (path, alias) => {
    expect(PAGES.has(path)).toBe(true);
    expect(isPublicConsolePath(path)).toBe(true);
    expect(sourceOf(path)).toContain("LEGACY_LINK_ALIASES");
    expect(AUTH_LINKS[alias.kind]).toBeTruthy();
  });

  it.each([...new Set([...Object.values(AUTH_LINKS).map((l) => l.path), ...Object.keys(LEGACY_LINK_ALIASES)])])(
    "middleware lets %s through with no session (never to /login)", async (path) => {
      vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://100.64.0.20");
      const r = await middleware(new NextRequest(`https://100.64.0.20${path}`, { headers: { host: "100.64.0.20" } }));
      expect(r.status).not.toBe(307);
      expect(r.headers.get("location")).toBeNull();
    });

  it("a redirect to /login carries an empty fragment, so a link's token never rides into it", async () => {
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://100.64.0.20");
    const r = await middleware(new NextRequest("https://100.64.0.20/identity", { headers: { host: "100.64.0.20" } }));
    expect(r.status).toBe(307);
    expect(r.headers.get("location")!.endsWith("#")).toBe(true);
  });
});
