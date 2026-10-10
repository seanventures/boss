/**
 * A CONTROL THE CONSOLE OFFERS IS ONE THE API WILL ANSWER (identity console gaps, "hide actions the
 * API refuses"). "Onboard a new organization" declared `project:create` while POST /v1/orgs checks
 * `org:update`, so an Operator was offered a form the API refused; 27 more actions declared a
 * permission other than the one their API route checks.
 *
 * The API is the authority, so the console's declarations are checked against the API's own role
 * matrix (tests/identity/matrix.yaml, generated from the routing table):
 *
 *   1. every action that calls the control plane through the typed client (`client.x(...)`)
 *      declares a permission that route really checks;
 *   2. every <ActionForm permission="…" action={x}> mirrors what x declares;
 *   3. every callAction(x, …, "permission") mirrors what x declares.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { ACTION_PERMISSIONS } from "../lib/identity/action-permissions";

const WEB = process.cwd();
const REPO = join(WEB, "..", "..");

const client = readFileSync(join(REPO, "packages/api-client/src/community.ts"), "utf8");
const CLIENT_ROUTES = new Map<string, string>();
for (const m of client.matchAll(/\n {4}(\w+)\(([^)]*)\)\s*\{\s*\n\s*return request(?:<[\s\S]*?>)?\(\s*"(GET|POST|PUT|PATCH|DELETE)",\s*([`"])(.+?)\4/g)) {
  CLIENT_ROUTES.set(m[1], `${m[3]} ${m[5].replace(/\$\{[^}]*\}/g, "{}").split("?")[0]}`);
}

const MATRIX = new Map<string, string>();
for (const line of readFileSync(join(REPO, "tests/identity/matrix.yaml"), "utf8").split("\n")) {
  const m = /^"(\w+) (\S+)": \["([^"]+)", "([^"]+)"\]/.exec(line);
  if (m) MATRIX.set(`${m[1]} ${m[2].replace(/\{[^}]*\}/g, "{}")}`, m[3]);
}

const declaredByName = new Map<string, Set<string>>();
for (const [key, permission] of Object.entries(ACTION_PERMISSIONS)) {
  const name = key.split("#")[1];
  declaredByName.set(name, (declaredByName.get(name) ?? new Set()).add(permission));
}
const onlyDeclared = (name: string): string | null => {
  const set = declaredByName.get(name);
  return set && set.size === 1 ? [...set][0] : null;
};

function sources(dir: string): string[] {
  return readdirSync(join(WEB, dir)).flatMap((name) => {
    // Other scanner tests create and remove __probe_tmp concurrently. Their fixtures
    // are not shipped controls; scan the stable product tree, as identity-mirroring does.
    if (name.startsWith("__") || name.startsWith(".")) return [];
    const path = join(dir, name);
    if (statSync(join(WEB, path)).isDirectory()) return sources(path);
    return /\.tsx?$/.test(name) && !/\.test\./.test(name) ? [path] : [];
  });
}
const FILES = ["app", "components", "lib"].flatMap(sources);

describe("every control mirrors the permission the API checks", () => {
  it("reads the client, the matrix and the declarations (the scan is not silently empty)", () => {
    expect(CLIENT_ROUTES.size).toBeGreaterThan(300);
    expect(MATRIX.size).toBeGreaterThan(1000);
    expect(Object.keys(ACTION_PERMISSIONS).length).toBeGreaterThan(200);
  });

  it("an action that calls the API declares a permission its route checks", () => {
    const wrong: string[] = [];
    let checked = 0;
    for (const [key, declared] of Object.entries(ACTION_PERMISSIONS)) {
      const [file, name] = key.split("#");
      const src = readFileSync(join(WEB, file), "utf8");
      let start = src.indexOf(`function ${name}(`);
      if (start < 0) start = src.indexOf(`const ${name} `);
      if (start < 0) continue;
      const end = src.indexOf("\nexport ", start + 10);
      const body = src.slice(start, end > 0 ? end : undefined);
      const checks = new Set<string>();
      for (const call of body.matchAll(/client\.(\w+)\(/g)) {
        const route = CLIENT_ROUTES.get(call[1]);
        const check = route ? MATRIX.get(route) : undefined;
        if (check) checks.add(check);
      }
      if (!checks.size) continue;
      checked += 1;
      if (![...checks].some((c) => c.startsWith(`permit ${declared} @`))) {
        wrong.push(`${key} declares ${declared}; the API checks ${[...checks].join(" | ")}`);
      }
    }
    expect(checked).toBeGreaterThan(80);
    expect(wrong).toEqual([]);
  });

  // Round 2 (R2-WEB): the walk pressed enabled buttons the API refused — "Poll provider health"
  // declared model_provider:read while POST /v1/provider-health/poll checks system:operate. The
  // check above saw only typed-client calls; an action that posts a literal path is held too.
  it("an action that posts a literal /v1 path declares a permission that route checks", () => {
    const route = (method: string, path: string): string | undefined => {
      const segs = path.split("?")[0].split("/").filter(Boolean);
      let best: { literal: number; check: string } | null = null;
      for (const [key, check] of MATRIX) {
        const [m, p] = key.split(" ");
        if (m !== method) continue;
        const ks = p.split("/").filter(Boolean);
        if (ks.length !== segs.length || !ks.every((k, i) => k === "{}" || k === segs[i])) continue;
        const literal = ks.filter((k) => k !== "{}").length;
        if (!best || literal > best.literal) best = { literal, check };
      }
      return best?.check;
    };
    const flat = (t: string) => t.replace(/\$\{[^}]*\}/g, "x");
    const wrong: string[] = [];
    let checked = 0;
    for (const [key, declared] of Object.entries(ACTION_PERMISSIONS)) {
      const [file, name] = key.split("#");
      const src = readFileSync(join(WEB, file), "utf8");
      let start = src.indexOf(`function ${name}(`);
      if (start < 0) start = src.indexOf(`const ${name} `);
      if (start < 0) continue;
      const end = src.indexOf("\nexport ", start + 10);
      const body = src.slice(start, end > 0 ? end : undefined);
      const checks = new Set<string>();
      for (const call of body.matchAll(/client\.(\w+)\(/g)) {
        const r = CLIENT_ROUTES.get(call[1]);
        const check = r ? MATRIX.get(r) : undefined;
        if (check) checks.add(check);
      }
      for (const m of body.matchAll(/\b(?:postJson|postJsonReturn)\s*(?:<[^>]*>)?\(\s*[`"](\/v1\/[^`"]*)[`"]/g)) {
        const check = route("POST", flat(m[1]));
        if (check) checks.add(check);
      }
      for (const m of body.matchAll(/\b(?:apiWrite|apiFetch|fetch)\(\s*`\$\{API_URL\}(\/v1\/[^`]*)`\s*,\s*\{[^}]*?method:\s*"(\w+)"/g)) {
        const check = route(m[2], flat(m[1]));
        if (check) checks.add(check);
      }
      if (!checks.size) continue;
      checked += 1;
      if (![...checks].some((c) => c.startsWith(`permit ${declared} @`))) {
        wrong.push(`${key} declares ${declared}; the API checks ${[...checks].join(" | ")}`);
      }
    }
    expect(checked).toBeGreaterThan(100);
    expect(wrong).toEqual([]);
  });

  it("an ActionForm's permission is its action's", () => {
    const wrong: string[] = [];
    for (const file of FILES.filter((f) => f.endsWith(".tsx"))) {
      const src = readFileSync(join(WEB, file), "utf8");
      for (const m of src.matchAll(/<ActionForm\b([^<>]*?(?:=\{[^{}]*\}[^<>]*?)*)>/g)) {
        const permission = /permission="([^"]+)"/.exec(m[1])?.[1];
        const action = /action=\{(\w+)\}/.exec(m[1])?.[1];
        const declared = action ? onlyDeclared(action) : null;
        if (permission && declared && permission !== declared) wrong.push(`${file}: ${action} is ${declared}, the form says ${permission}`);
      }
    }
    expect(wrong).toEqual([]);
  });

  it("a callAction's permission is its action's", () => {
    const wrong: string[] = [];
    for (const file of FILES) {
      const src = readFileSync(join(WEB, file), "utf8");
      for (const m of src.matchAll(/callAction\(\s*(\w+)\s*,[^()]*?"([a-z_]+:[a-z_]+)"\s*\)/g)) {
        const declared = onlyDeclared(m[1]);
        if (declared && declared !== m[2]) wrong.push(`${file}: ${m[1]} is ${declared}, callAction says ${m[2]}`);
      }
    }
    expect(wrong).toEqual([]);
  });

  it("the onboard card is offered only to someone holding what POST /v1/orgs checks", () => {
    expect(MATRIX.get("POST /v1/orgs")).toMatch(/^permit org:update @/);
    const gate = readFileSync(join(WEB, "app/org-gate.tsx"), "utf8");
    expect(gate).toMatch(/\{mayOnboard \? \(\s*<button[^>]*data-testid="org-card-onboard"/);
    expect(readFileSync(join(WEB, "app/org-gate-permission.ts"), "utf8")).toContain('"org:update"');
  });
});
