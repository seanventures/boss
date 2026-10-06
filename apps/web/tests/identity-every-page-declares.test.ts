/**
 * EVERY IDENTITY AND MY-ACCOUNT PAGE DECLARES WHAT OPENING IT NEEDS (IDENTITY.md §6.5, R4), and
 * loads nothing before the guard has said yes. The sign-in pages are the only public ones.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { IDENTITY_PAGES, isPublicConsolePath, pageFor } from "../lib/identity/pages";
import { isPermission } from "../lib/identity/permissions.gen";

const ROOT = process.cwd();
function pages(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(join(ROOT, dir))) {
    const path = join(dir, name);
    if (statSync(join(ROOT, path)).isDirectory()) out.push(...pages(path));
    else if (name === "page.tsx") out.push(path);
  }
  return out;
}
const routeOf = (file: string) => "/" + file.replace(/^app\//, "").replace(/\/page\.tsx$/, "").replace(/\(auth\)\//, "").replace(/\[[^\]]+\]/g, "x");

describe("identity and my-account pages", () => {
  const files = [...pages("app/identity"), ...pages("app/account")];

  it("finds them", () => {
    expect(files.length).toBeGreaterThanOrEqual(10);
  });

  it.each(files)("%s is declared and guarded before it loads", (file) => {
    const route = routeOf(file);
    const page = pageFor(route);
    expect(page, `${route} has no IDENTITY_PAGES entry`).not.toBeNull();
    if (page && "permission" in page.access) expect(isPermission(page.access.permission)).toBe(true);
    const src = readFileSync(join(ROOT, file), "utf8");
    const body = src.slice(src.indexOf("export default"));
    expect(body, "the page checks pageAllowed before anything else").toMatch(/^export default async function \w+\([^)]*\)[^{]*\{\s*(const \{[^}]*\} = await params;\s*)?if \(!\(await pageAllowed\(/);
  });

  it("every declared page exists", () => {
    const routes = new Set(files.map(routeOf));
    for (const p of IDENTITY_PAGES) expect(routes.has(p.path.replace(/\[id\]/g, "x")), p.path).toBe(true);
  });
});

describe("the sign-in pages are public, and only they", () => {
  const auth = pages("app/(auth)").map(routeOf);

  it.each(auth.filter((r) => r !== "/enrol"))("%s is public", (route) => {
    expect(isPublicConsolePath(route)).toBe(true);
  });

  it("enrolment, identity and account pages need a session", () => {
    for (const r of ["/enrol", "/identity", "/identity/people", "/account", "/account/keys", "/"]) expect(isPublicConsolePath(r), r).toBe(false);
  });
});
