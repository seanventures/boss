/**
 * Every /api/... path the console calls is served by a route handler.
 *
 * A run's Document step offered "Read the change record" and "Download as PDF", and both went
 * to /api/artifacts/{id} — a path no handler in app/api serves. Every record read "Could not
 * read it — HTTP 404" and every PDF download, the run report's included, failed. The API has
 * always served /v1/artifacts/{id}; the console proxies /v1/* (app/v1/[...path]). This walks
 * every literal /api/ path in the console against the handlers that exist.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      // The handlers themselves are not callers.
      if (path === join("app", "api")) continue;
      walk(path, out);
    } else if (/\.(ts|tsx)$/.test(name)) out.push(path);
  }
  return out;
}

/** Whether `segments` (truncated where a template expression began) reach a handler. */
function served(segments: string[], truncated: boolean): boolean {
  let dir = join("app", "api");
  for (const seg of segments) {
    const literal = join(dir, seg);
    if (existsSync(literal) && statSync(literal).isDirectory()) { dir = literal; continue; }
    const dynamic = readdirSync(dir).find((d) => /^\[.+\]$/.test(d));
    if (!dynamic) return false;
    if (dynamic.includes("...")) return true;  // [...x] and [[...x]] take the rest
    dir = join(dir, dynamic);
  }
  if (existsSync(join(dir, "route.ts"))) return true;
  // A path cut short by `${...}` continues into a dynamic segment.
  return truncated && readdirSync(dir).some((d) => /^\[.+\]$/.test(d)
    || (statSync(join(dir, d)).isDirectory()));
}

const CALLERS = [...walk("app"), ...walk("lib"), ...walk("components")];
const PATH = /["'`](\/api\/[A-Za-z0-9_\-/]*)(\$\{)?/g;

describe("client calls to /api/*", () => {
  it("finds the calls it is checking (the scan works)", () => {
    const n = CALLERS.reduce((sum, f) => sum + [...readFileSync(f, "utf8").matchAll(PATH)].length, 0);
    expect(n).toBeGreaterThan(40);
  });

  it("every one is served by a handler under app/api", () => {
    const missing: string[] = [];
    for (const file of CALLERS) {
      const src = readFileSync(file, "utf8");
      for (const m of src.matchAll(PATH)) {
        const segments = m[1].replace(/^\/api\//, "").split("/").filter(Boolean);
        if (!segments.length) continue;
        if (!served(segments, Boolean(m[2]) || m[1].endsWith("/"))) {
          missing.push(`${relative(".", file)}:${src.slice(0, m.index).split("\n").length} ${m[1]}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });

  it("the change record and its PDF go through the /v1 proxy", () => {
    const dw = readFileSync("app/delivery-workflows-ui.tsx", "utf8");
    expect(dw).toContain("fetch(`/v1/artifacts/${encodeURIComponent(documentId)}`");
    expect(dw).toContain("href={`/v1/artifacts/${encodeURIComponent(documentId)}/pdf`}");
    expect(readFileSync("app/run-report-ui.tsx", "utf8")).toContain("`/v1/artifacts/");
    expect(existsSync(join("app", "v1", "[...path]", "route.ts"))).toBe(true);
  });
});
