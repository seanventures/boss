/**
 * A SERVER MODULE CALLS NO FUNCTION FROM A "use client" FILE (round 2, R2-WEB).
 *
 * On the server, every export of a "use client" module is a client REFERENCE: a component can be
 * rendered from it, but a function or a constant cannot be used. /reset imported
 * emailSignInOffered() from the sign-in page's client module and crashed for every visitor
 * ("Attempted to call emailSignInOffered() from the server"). The fix moved it to
 * lib/identity/email-sign-in.ts; this test holds the class.
 *
 * Server code is every page, layout, template, not-found, route handler, "use server" module,
 * middleware and instrumentation, plus every non-client module they import, transitively. From a
 * client module such code may import only components (PascalCase, not ALL_CAPS) and types.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, normalize, relative } from "node:path";
import { describe, expect, it } from "vitest";

const WEB = process.cwd();
const ROOTS = ["app", "components", "lib"];

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(join(WEB, dir))) {
    const p = join(dir, name);
    if (name === "node_modules") continue;
    if (statSync(join(WEB, p)).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(p);
  }
  return out;
}

const files = [...ROOTS.flatMap(walk), "middleware.ts", "instrumentation.ts"];
const src = new Map(files.map((f) => [f, readFileSync(join(WEB, f), "utf8")]));
const DIRECTIVE = (word: string) => new RegExp(`^(?:\\s*(?:/\\*[\\s\\S]*?\\*/|//[^\\n]*))*\\s*["']use ${word}["']`);
const isClient = (f: string) => DIRECTIVE("client").test(src.get(f) ?? "");
const isServerEntry = (f: string) =>
  /(^|\/)(page|layout|template|not-found|route|default)\.tsx?$/.test(f)
  || f === "middleware.ts" || f === "instrumentation.ts" || DIRECTIVE("server").test(src.get(f) ?? "");

function resolve(from: string, spec: string): string | null {
  if (!spec.startsWith(".")) return null;
  const base = normalize(join(dirname(from), spec));
  for (const c of [`${base}.tsx`, `${base}.ts`, `${base}/index.tsx`, `${base}/index.ts`, base]) {
    if (src.has(c) || (existsSync(join(WEB, c)) && statSync(join(WEB, c)).isFile())) return src.has(c) ? c : null;
  }
  return null;
}

const IMPORT = /import\s+(type\s+)?(?:[A-Za-z_$][\w$]*\s*,\s*)?\{([^}]*)\}\s*from\s*["']([^"']+)["']/g;
const DYNAMIC = /(?:import|from)\s*\(?\s*["'](\.[^"']+)["']/g;
const COMPONENT = /^[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*$/;

// Every non-client module the server runs: the entries and what they import, transitively.
const server = new Set<string>();
const queue = files.filter((f) => isServerEntry(f) && !isClient(f));
while (queue.length) {
  const f = queue.pop()!;
  if (server.has(f)) continue;
  server.add(f);
  for (const m of (src.get(f) ?? "").matchAll(DYNAMIC)) {
    const t = resolve(f, m[1]);
    if (t && !isClient(t) && !server.has(t)) queue.push(t);
  }
}

type Hit = { file: string; from: string; names: string[] };
const hits: Hit[] = [];
for (const f of server) {
  for (const m of (src.get(f) ?? "").matchAll(IMPORT)) {
    if (m[1]) continue; // import type { … }
    const target = resolve(f, m[3]);
    if (!target || !isClient(target)) continue;
    const bad = m[2].split(",").map((n) => n.trim()).filter(Boolean)
      .filter((n) => !n.startsWith("type "))
      .filter((n) => !COMPONENT.test(n.split(/\s+as\s+/)[0]));
    if (bad.length) hits.push({ file: f, from: relative(WEB, join(WEB, target)), names: bad });
  }
}

describe("server code calls no function from a client module", () => {
  it("finds the server modules (pages, routes, actions and what they import)", () => {
    expect(server.size).toBeGreaterThan(100);
    expect(server.has("app/(auth)/reset/page.tsx")).toBe(true);
  });

  it("imports only components and types from a 'use client' file", () => {
    expect(hits, "move the function to a plain module both sides can import").toEqual([]);
  });
});
