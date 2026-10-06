/**
 * EVERY IDENTITY CALL THE CONSOLE MAKES EXISTS IN THE REAL API.
 *
 * The console was built against an in-process stub (lib/identity/stub) while S1–S5 built the
 * API; the stub answered paths the API never had (GET /v1/auth/invitations/{token},
 * POST …/access-roles/{id}/clone) and nothing noticed. This pins the console to the API's own
 * routing table, dumped by scripts/dump-identity-api-routes.py into
 * tests/fixtures/identity-api-routes.json (apps/api/tests/test_the_identity_route_fixture_is_fresh.py
 * refuses a stale copy):
 *
 *   1. every `/v1/identity…` or `/v1/auth/…` string literal in the console's source names a real
 *      route path (any method) — constants, hrefs and paths built into variables included;
 *   2. every direct call (idGet/idPost/idPatch/idPut/idDelete/serverGet) uses a method that
 *      route really answers.
 *
 * The stub's own files are excluded: it is a dev-only double that a production build cannot
 * turn on (lib/identity/stub/enabled.ts).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = process.cwd();
const FIXTURE = JSON.parse(readFileSync(join(ROOT, "tests/fixtures/identity-api-routes.json"), "utf8")) as { routes: string[] };

type Route = { method: string; path: string; segs: string[] };
const ROUTES: Route[] = FIXTURE.routes.map((line) => {
  const [method, path] = line.split(" ");
  return { method, path, segs: path.split("/").filter(Boolean) };
});

/** A console segment built from `${…}` (PLACEHOLDER) may be any one route segment; a route
 *  parameter `{x}` takes any one console segment, and `{x:path}` the rest. */
const PLACEHOLDER = "\u0000";
function matches(route: Route, path: string): boolean {
  const segs = path.replace(/\/+$/, "").split("/").filter(Boolean);
  for (let i = 0; i < route.segs.length; i += 1) {
    const r = route.segs[i];
    if (r.startsWith("{") && r.endsWith(":path}")) return true;
    const c = segs[i];
    if (c === undefined) return false;
    if (r.startsWith("{") && r.endsWith("}")) continue;
    if (c !== r && c !== PLACEHOLDER) return false;
  }
  return segs.length === route.segs.length;
}

const SCAN = ["app", "components", "lib"];
const SKIP = [/^lib\/identity\/stub\//, /\.test\.tsx?$/];

function files(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(join(ROOT, dir))) {
    const path = join(dir, name);
    const st = statSync(join(ROOT, path));
    if (st.isDirectory()) out.push(...files(path));
    else if (/\.(ts|tsx)$/.test(name)) out.push(path);
  }
  return out;
}

/** A literal's path, with each `${…}` as a PLACEHOLDER segment and the query cut off. */
function concrete(literal: string): string {
  let out = "";
  for (let i = 0; i < literal.length; i += 1) {
    if (literal[i] === "$" && literal[i + 1] === "{") {
      let depth = 1;
      i += 2;
      while (i < literal.length && depth > 0) {
        if (literal[i] === "{") depth += 1;
        else if (literal[i] === "}") depth -= 1;
        i += 1;
      }
      i -= 1;
      out += PLACEHOLDER;
    } else out += literal[i];
  }
  return out.split("?")[0].split("#")[0];
}

const SOURCES = SCAN.flatMap(files).map((f) => relative(ROOT, join(ROOT, f))).filter((f) => !SKIP.some((re) => re.test(f)));
const LITERAL = /(["'`])(\/v1\/(?:identity(?=[/"'`?])|auth\/)[^"'`\s]*)\1/g;
const CALL = /\b(idGet|idPost|idPatch|idPut|idDelete|serverGet)\b[^(;]*?\(\s*(["'`])(\/v1\/(?:identity|auth)[^"'`]*)\2/g;
const METHOD: Record<string, string> = { idGet: "GET", serverGet: "GET", idPost: "POST", idPatch: "PATCH", idPut: "PUT", idDelete: "DELETE" };

type Use = { file: string; path: string; method?: string };
const literals: Use[] = [];
const calls: Use[] = [];
for (const file of SOURCES) {
  const src = readFileSync(join(ROOT, file), "utf8");
  for (const m of src.matchAll(LITERAL)) literals.push({ file, path: concrete(m[2]) });
  for (const m of src.matchAll(CALL)) calls.push({ file, path: concrete(m[3]), method: METHOD[m[1]] });
}

describe("the identity console calls only real API routes", () => {
  it("finds the console's identity calls (the scan is not silently empty)", () => {
    expect(ROUTES.length).toBeGreaterThan(100);
    expect(calls.length).toBeGreaterThan(80);
    expect(literals.length).toBeGreaterThan(calls.length);
  });

  it("every /v1/identity and /v1/auth path in the console is a real route", () => {
    const missing = literals.filter((u) => !ROUTES.some((r) => matches(r, u.path))).map((u) => `${u.file}: ${u.path}`);
    expect(missing).toEqual([]);
  });

  it("every direct call uses a method the route really answers", () => {
    const wrong = calls.filter((u) => !ROUTES.some((r) => r.method === u.method && matches(r, u.path)))
      .map((u) => `${u.file}: ${u.method} ${u.path}`);
    expect(wrong).toEqual([]);
  });

  it("an invitation token never rides a URL to the API", () => {
    const leaks = literals.filter((u) => /\/v1\/auth\/invitations\/(?!preview$|accept$)/.test(u.path)).map((u) => `${u.file}: ${u.path}`);
    expect(leaks).toEqual([]);
  });
});
