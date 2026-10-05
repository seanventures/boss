/**
 * page.tsx is a SERVER component: nothing it imports (outside a "use client" boundary) may
 * import a React hook. Next refuses the whole console at runtime — every route a 500 — while
 * tsc and vitest both pass, because neither knows which graph a module lands in.
 *
 * Seen on the Command Center bridge (2026-10-04): page.tsx imported the view's server fetch from
 * lib/command-center-view.ts, which also held the poll (usePolled) and Live's parsers (whose
 * module holds useLiveBoard). The first browser load answered 500. The fetch now lives in
 * lib/command-center-view-server.ts and hands the client component the wire.
 *
 * This walks the real import graph from page.tsx, stopping at "use client" files (a client
 * boundary may use hooks), and fails on the first server-graph module that imports one.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = resolve(__dirname, "..");
const HOOK = /import\s*(?:type\s+)?\{[^}]*\b(useState|useEffect|useRef|useCallback|useMemo|useReducer|useContext|useLayoutEffect|useSyncExternalStore|useTransition)\b[^}]*\}\s*from\s*["']react["']/;
const IMPORT = /(?:^|\n)\s*(?:import|export)\s+(?!type\b)[^"';]*?from\s*["'](\.{1,2}\/[^"']+)["']/g;
const DYNAMIC = /import\(\s*["'](\.{1,2}\/[^"']+)["']\s*\)/g;

function resolveModule(from: string, spec: string): string | null {
  const base = resolve(dirname(from), spec);
  for (const ext of ["", ".ts", ".tsx", "/index.ts", "/index.tsx"]) {
    const p = base + ext;
    if (existsSync(p) && !p.endsWith("/")) {
      try { readFileSync(p, "utf8"); return p; } catch { /* a directory */ }
    }
  }
  return null;
}

function isClient(src: string): boolean {
  return /^\s*(?:\/\/[^\n]*\n|\/\*[\s\S]*?\*\/\s*)*["']use client["']/.test(src);
}

describe("the console page's server graph", () => {
  it("imports no React hook outside a \"use client\" boundary", () => {
    const start = resolve(ROOT, "app/page.tsx");
    const seen = new Set<string>();
    const offenders: string[] = [];
    const queue: [string, string[]][] = [[start, ["app/page.tsx"]]];
    while (queue.length) {
      const [file, trail] = queue.shift()!;
      if (seen.has(file)) continue;
      seen.add(file);
      const src = readFileSync(file, "utf8");
      if (file !== start && isClient(src)) continue;
      const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/[^\n]*/g, "$1");
      const hook = code.match(HOOK);
      if (hook && file !== start) offenders.push(`${trail.join(" → ")} imports ${hook[1]}`);
      for (const re of [IMPORT, DYNAMIC]) {
        for (const m of code.matchAll(re)) {
          const next = resolveModule(file, m[1]);
          if (next && !seen.has(next)) queue.push([next, [...trail, next.slice(ROOT.length + 1)]]);
        }
      }
    }
    expect(seen.size).toBeGreaterThan(20);
    expect(offenders).toEqual([]);
  });
});
