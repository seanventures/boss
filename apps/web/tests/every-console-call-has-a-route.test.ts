/**
 * EVERY CONSOLE CALL HAS A ROUTE (operator, 2026-11-18: "Why in command center it says Templates
 * did not load"). The build home fetched /api/factory/templates, /attachments and /launch from
 * the day it shipped, but no Next route existed: the gallery always failed and Easy Mode could
 * not upload or launch. Every literal `fetch("/api/…")` in the console must name a route file.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { describe, expect, it } from "vitest";

const WEB = join(__dirname, "..");

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

const routes = walk(join(WEB, "app", "api"))
  .filter((p) => p.endsWith(`${sep}route.ts`))
  .map((p) => relative(join(WEB, "app"), p).split(sep).slice(0, -1));

function served(path: string): boolean {
  const segs = path.replace(/^\/+|\/+$/g, "").split("/");
  return routes.some((r) => {
    for (let i = 0; i < r.length; i++) {
      if (r[i].startsWith("[...") || r[i].startsWith("[[...")) return true;
      if (i >= segs.length) return true;           // a template literal stops before an [id]
      if (!r[i].startsWith("[") && r[i] !== segs[i]) return false;
    }
    return r.length === segs.length;
  });
}

describe("console fetches", () => {
  it("every literal fetch('/api/…') names a route that exists", () => {
    const missing: string[] = [];
    for (const dir of ["app", "lib", "components"]) {
      let files: string[] = [];
      try { files = walk(join(WEB, dir)); } catch { continue; }
      for (const f of files) {
        if (f.includes(`${sep}app${sep}api${sep}`)) continue;
        const src = readFileSync(f, "utf8");
        for (const m of src.matchAll(/fetch\(\s*["'`](\/api\/[A-Za-z0-9_\-/]+)/g)) {
          if (!served(m[1])) missing.push(`${relative(WEB, f)} → ${m[1]}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });
});
