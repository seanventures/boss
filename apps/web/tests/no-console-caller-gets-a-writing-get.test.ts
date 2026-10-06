/**
 * NO CONSOLE CALLER GETs A ROUTE THAT WRITES ON GET.
 *
 * The API declares 27 GETs `writes_on_get` (they seed, reap, freeze or probe as they answer) and
 * gives each a POST twin at `<path>/load` (apps/api/app/identity_guard.py `writing_get`). A
 * cookie-authenticated GET to one is a write for CSRF: it needs `Sec-Fetch-Site: same-origin` or
 * `X-Boss-CSRF`, and a Next server-side GET sends neither, so it is refused 403. Every caller in
 * the console and the api-client must use the twin (lib/writing-get.ts `loadPath`).
 *
 * The list is the API's own (scripts/dump-identity-api-routes.py → tests/fixtures/
 * identity-api-routes.json `writes_on_get`; a python test refuses a stale copy). A source literal
 * naming one of those paths exactly is a GET unless it is wrapped in loadPath() or the call it
 * sits in names a write method (a different route on the same path, e.g. POST /v1/mcp/servers).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

import { loadPath } from "../lib/writing-get";

const WEB = process.cwd();
const REPO = join(WEB, "..", "..");
const FIXTURE = JSON.parse(readFileSync(join(WEB, "tests/fixtures/identity-api-routes.json"), "utf8")) as { writes_on_get: string[] };
const WRITING = FIXTURE.writes_on_get.map((p) => p.split("/").filter(Boolean));

const ROOTS = ["apps/web/app", "apps/web/lib", "apps/web/components", "packages/api-client/src"];
function files(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(join(REPO, dir))) {
    const path = join(dir, name);
    if (name === "node_modules") continue;
    if (statSync(join(REPO, path)).isDirectory()) out.push(...files(path));
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(path);
  }
  return out;
}

const PLACEHOLDER = "\u0000";
function concrete(literal: string): string {
  const flat = literal.replace(/\$\{[^}]*\}/g, PLACEHOLDER);
  return flat.replace(new RegExp(`^${PLACEHOLDER}(?=/v1/)`), "").split("?")[0].split("#")[0];
}
function isWriting(path: string): boolean {
  const segs = path.replace(/\/+$/, "").split("/").filter(Boolean);
  return WRITING.some((r) => r.length === segs.length
    // A `${…}` segment stands for a route parameter, never a fixed word (`/v1/decisions/${id}` is
    // GET /v1/decisions/{id}, not the writing /v1/decisions/inbox).
    && r.every((seg, i) => (seg.startsWith("{") && seg.endsWith("}")) || seg === segs[i]));
}

const LITERAL = /(["'`])((?:\$\{[^}]*\})?\/v1\/[^"'`\s]*)\1/g;
const WRITE_CALL = /"(POST|PUT|PATCH|DELETE)"|'(POST|PUT|PATCH|DELETE)'|\b(post|postJson|postJsonReturn|captainPost|idPost|idPatch|idPut|idDelete|apiWrite)\s*\(/;

type Hit = { file: string; line: number; text: string };
const hits: Hit[] = [];
let scanned = 0;
for (const file of ROOTS.flatMap(files)) {
  const src = readFileSync(join(REPO, file), "utf8");
  for (const m of src.matchAll(LITERAL)) {
    const path = concrete(m[2]);
    if (!isWriting(path)) continue;
    scanned += 1;
    const before = src.slice(Math.max(0, m.index! - 200), m.index!);
    const lineStart = src.lastIndexOf("\n", m.index!) + 1;
    if (/^\s*(\*|\/\/|\/\*)/.test(src.slice(lineStart, m.index!))) continue; // a comment, not a call
    // The call the literal sits in: back to the previous statement, and on to its `{ method }`.
    const call = before.slice(Math.max(before.lastIndexOf(";"), before.lastIndexOf("}\n")) + 1)
      + src.slice(m.index!, m.index! + 160).split(/;|\n\s*\n/)[0];
    if (/loadPath\(\s*$/.test(before) || WRITE_CALL.test(call)) continue;
    hits.push({ file: relative(REPO, join(REPO, file)), line: src.slice(0, m.index!).split("\n").length, text: m[2] });
  }
}

describe("writing GETs are called by their POST twin", () => {
  it("knows the API's writing GETs, and the scan finds their callers", () => {
    expect(WRITING.length).toBeGreaterThanOrEqual(20);
    expect(scanned).toBeGreaterThan(10);
  });

  it("no console or api-client caller GETs one", () => {
    expect(hits.map((h) => `${h.file}:${h.line} ${h.text}`)).toEqual([]);
  });

  it("loadPath puts /load before the query", () => {
    expect(loadPath("/v1/kit?project_id=a&check=stale")).toBe("/v1/kit/load?project_id=a&check=stale");
    expect(loadPath("http://api:8000/v1/preview-sandboxes/")).toBe("http://api:8000/v1/preview-sandboxes/load");
    expect(loadPath("/v1/workflow-runs/wr_1/chain")).toBe("/v1/workflow-runs/wr_1/chain/load");
  });

  it("the api-client reads each writing GET it wraps through its twin", () => {
    const client = readFileSync(join(REPO, "packages/api-client/src/community.ts"), "utf8");
    for (const twin of ["/v1/meeting-roles/load", "/v1/previews/load", "/v1/mcp/servers/load", "/v1/status/load",
      "/v1/elastic/status/load", "/v1/elastic/instances/load", "/v1/provider-usage/load", "/v1/provider-usage/summary/load",
      "/v1/decisions/inbox/load", "/v1/hardware/places/load", "/v1/hardware/resolve/load"]) {
      expect(client, twin).toMatch(new RegExp(`"POST",\\s*"${twin.replace(/\//g, "\\/")}"`));
    }
  });
});
