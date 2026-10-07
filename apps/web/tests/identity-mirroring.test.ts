/**
 * PERMISSION MIRRORING ACROSS THE WHOLE CONSOLE (IDENTITY.md §6.5, R4).
 *
 *  (a) every export of every *-actions.ts declares a catalogue permission (declareAction or
 *      guardedAction), and lib/identity/action-permissions.ts says the same thing;
 *  (b) every <ActionForm>, <SubmitButton> and callAction( in the console carries its permission —
 *      on itself, or from the <ActionForm permission> it sits in;
 *  (c) every console area has an access, and the shell filters the rail and guards a direct URL
 *      with it;
 *  (d) the draft area table never disagrees with what S2 has declared;
 *  (e) every permission literal in apps/web is a catalogue key;
 *  (f) the console shell always mounts the identity provider.
 * The API stays the authority: these keep the console's MIRROR whole, they authorize nothing.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { AREA_IDS } from "../lib/area-ids";
import { AREA_ACCESS } from "../lib/identity/areas";
import { accessForArea } from "../lib/identity/area-access";
import { ACTION_PERMISSIONS } from "../lib/identity/action-permissions";
import { declareAction, guardedAction, identityRefusal, permissionOf } from "../lib/identity/guarded-action";
import { isPermission } from "../lib/identity/permissions.gen";

const ROOT = process.cwd();

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(join(ROOT, dir))) {
    if (name === "node_modules" || name.startsWith(".") || name.startsWith("__")) continue; // __probe_tmp: other tests' scratch files
    const path = join(dir, name);
    if (statSync(join(ROOT, path)).isDirectory()) out.push(...walk(path));
    else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) out.push(path);
  }
  return out;
}

const SOURCE = [...walk("app"), ...walk("components"), ...walk("lib"), "middleware.ts"];
const read = (f: string) => { try { return readFileSync(join(ROOT, f), "utf8"); } catch { return ""; } };
const ACTION_FILES = SOURCE.filter((f) => /-actions\.ts$|(^|\/)actions\.ts$/.test(f));

/** Exports of action files that are infrastructure, not actions (they call no route of their own
 *  or build guarded ones). Each is named, with why; nothing else may skip the declaration. */
const HELPERS: Record<string, string[]> = {
  "app/factory/cockpit-actions.ts": ["postJson"], // the POST every guarded cockpit action uses
  "app/infra/infra-actions.ts": ["apiSentence", "send", "read", "afterWrite"], // Infra's transport
  "app/infra/deploy-actions.ts": ["goConsole"], // navigation, no API call
  "app/agents/org-actions.ts": ["orgActions"], // factory over lib/kit-api and lib/agents calls
};

describe("(a) every action declares a permission", () => {
  it("finds the action files", () => {
    expect(ACTION_FILES.length).toBeGreaterThanOrEqual(17);
  });

  it.each(ACTION_FILES)("%s: every export is declared with a catalogue key", (file) => {
    const src = read(file);
    const exported = [...src.matchAll(/^export (?:async function|function|const) (\w+)/gm)].map((m) => m[1]);
    const declared = new Map<string, string>();
    for (const m of src.matchAll(/declareAction\((\w+), "([^"]+)"\)/g)) declared.set(m[1], m[2]);
    for (const m of src.matchAll(/export const (\w+) = guardedAction\("([^"]+)"/g)) declared.set(m[1], m[2]);
    for (const name of exported) {
      if ((HELPERS[file] ?? []).includes(name)) continue;
      expect(declared.get(name), `${file}#${name} declares no permission`).toBeTruthy();
      expect(isPermission(declared.get(name)!), `${file}#${name}: ${declared.get(name)}`).toBe(true);
      expect(ACTION_PERMISSIONS[`${file}#${name}`], `${file}#${name} missing from action-permissions.ts`).toBe(declared.get(name));
    }
  });

  it("action-permissions.ts names only real exports", () => {
    for (const key of Object.keys(ACTION_PERMISSIONS)) {
      const [file, name] = key.split("#");
      expect(read(file), key).toMatch(new RegExp(`export (?:async function|function|const) ${name}\\b`));
    }
  });

  it("declares at run time without changing the function", async () => {
    const fn = async (x: number) => x + 1;
    expect(declareAction(fn, "workflow:publish")).toBe(fn);
    expect(permissionOf(fn)).toBe("workflow:publish");
    const wrapped = guardedAction("deployment:create", async function deployIt(n: number) { return n * 2; });
    expect(await wrapped(4)).toBe(8);
    expect(permissionOf(wrapped)).toBe("deployment:create");
    expect(() => guardedAction("workflow:*" as never, fn)).toThrow();
    const infra = await import("../app/infra/infra-actions");
    expect(permissionOf(infra.deployTarget)).toBe("deployment:create");
    expect(permissionOf(infra.rollbackDeploy)).toBe("deployment:rollback");
  });

  it("reads the API's identity refusals and nothing else", () => {
    expect(identityRefusal(401, { error: "step_up_required" })).toEqual({ error: "step_up_required", permission: null });
    expect(identityRefusal(403, { error: "forbidden", permission: "deployment:production" }))
      .toEqual({ error: "forbidden", permission: "deployment:production" });
    expect(identityRefusal(403, { error: "forbidden", permission: "not:a_key" })?.permission).toBeNull();
    expect(identityRefusal(409, { error: "forbidden" })).toBeNull();
    expect(identityRefusal(403, { detail: "nope" })).toBeNull();
  });
});

describe("(b) every action call site carries its permission", () => {
  const OWNED_ELSEWHERE = /^(app\/identity\/|app\/\(auth\)\/|app\/account\/|components\/identity\/)/;
  const sites = SOURCE.filter((f) => f.endsWith(".tsx") || f.endsWith(".ts"))
    .filter((f) => !OWNED_ELSEWHERE.test(f) && f !== "lib/call-action.ts" && f !== "app/submit-button.tsx");

  it("every <ActionForm> passes a permission", () => {
    const bad: string[] = [];
    for (const f of sites) {
      for (const m of read(f).matchAll(/<ActionForm\b(?!Pending)[^>]*>/gs)) {
        if (!/permission=/.test(m[0])) bad.push(`${f}: ${m[0].slice(0, 80)}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it("every <SubmitButton> has a permission, or sits in an <ActionForm permission>", () => {
    const bad: string[] = [];
    for (const f of sites) {
      const src = read(f);
      for (const m of src.matchAll(/<SubmitButton\b[^>]*>/gs)) {
        if (/permission=/.test(m[0])) continue;
        const before = src.slice(0, m.index);
        const opens = [...before.matchAll(/<ActionForm\b(?!Pending)[^>]*>|<form\b[^>]*>/gs)];
        const last = opens[opens.length - 1];
        const closed = last && [...before.matchAll(/<\/ActionForm>|<\/form>/g)].some((c) => (c.index ?? 0) > (last.index ?? 0));
        if (last && !closed && last[0].startsWith("<ActionForm") && /permission=/.test(last[0])) continue;
        bad.push(`${f}:${before.split("\n").length}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it("every callAction( names the action's permission (ActionForm's generic call takes the form's)", () => {
    const bad: string[] = [];
    for (const f of sites) {
      for (const m of read(f).matchAll(/callAction\(\s*(\w+)([^)]*)\)/g)) {
        if (f === "app/client-forms.tsx" && m[1] === "action") continue; // ActionForm: its `permission` prop
        if (!/"[a-z_]+:[a-z_]+"/.test(m[2])) bad.push(`${f}: callAction(${m[1]}${m[2]})`);
      }
    }
    expect(bad).toEqual([]);
  });

  it("a call site's permission is the one its action declares", () => {
    const byName = new Map(Object.entries(ACTION_PERMISSIONS).map(([k, p]) => [k.split("#")[1], p]));
    const bad: string[] = [];
    for (const f of sites) {
      for (const m of read(f).matchAll(/callAction\(\s*(\w+),[^)]*"([a-z_]+:[a-z_]+)"\)/g)) {
        if (byName.has(m[1]) && byName.get(m[1]) !== m[2]) bad.push(`${f}: ${m[1]} ${m[2]} ≠ ${byName.get(m[1])}`);
      }
      for (const m of read(f).matchAll(/<ActionForm permission="([^"]+)"[^>]*action=\{(\w+)\}/gs)) {
        if (byName.has(m[2]) && byName.get(m[2]) !== m[1]) bad.push(`${f}: ${m[2]} ${m[1]} ≠ ${byName.get(m[2])}`);
      }
    }
    expect(bad).toEqual([]);
  });
});

describe("(c) every area has an access, and the shell mirrors it", () => {
  it.each([...AREA_IDS])("%s has an access", (id) => {
    const access = accessForArea(id);
    expect(access).toBeTruthy();
    if ("permission" in access) expect(isPermission(access.permission)).toBe(true);
  });

  it("the rail shows only openable areas and a direct URL to another renders <AccessDenied>", () => {
    const shell = read("app/ConsoleShell.tsx");
    expect(shell).toMatch(/import \{ accessForArea \} from "\.\.\/lib\/identity\/area-access"/);
    expect(shell).toMatch(/!OFF_THE_NAV\.has\(a\.id\) && areaCan\(a\.id\)\.allowed/);
    expect(shell).toMatch(/!routeMode && !areaCan\(active\)\.allowed/);
    expect(shell).toMatch(/<AccessDenied permission=\{access\.permission\}/);
    // and the server neither fetches nor renders a denied area (page.tsx `openable`)
    const page = read("app/page.tsx");
    expect(page).toMatch(/const shows = \(area: string\) => loadedAreas\.has\(area\) && openable\(area\)/);
    expect(page).toMatch(/const needs = \(\.\.\.areas: string\[\]\) => areas\.some\(\(area\) => loadedAreas\.has\(area\) && openable\(area\)\)/);
  });
});

describe("(d) S2's table is the only one", () => {
  it("every area reads its access from AREA_ACCESS, and no draft table is left", () => {
    for (const [id, access] of Object.entries(AREA_ACCESS)) {
      expect(accessForArea(id as keyof typeof AREA_ACCESS), id).toEqual(access);
    }
    expect(read("lib/identity/area-access.ts")).not.toMatch(/DRAFT_AREA_ACCESS/);
  });
});

describe("(e) every permission literal in the console is a catalogue key", () => {
  const PATTERNS = [
    /permission="([^"]+)"/g, /permission: "([^"]+)"/g, /useCan\("([^"]+)"/g, /declareAction\(\w+, "([^"]+)"\)/g,
    /guardedAction\("([^"]+)"/g, /callAction\([^)]*"([a-z_]+:[a-z_]+)"\)/g, /check\("([a-z_]+:[a-z_]+)"\)/g,
  ];
  it("finds no misspelt permission", () => {
    const bad: string[] = [];
    let seen = 0;
    for (const f of SOURCE) {
      const src = read(f);
      for (const re of PATTERNS) {
        for (const m of src.matchAll(re)) {
          if (!/^[a-z_]+:[a-z_]+$/.test(m[1])) continue; // a variable or a non-permission "permission"
          seen += 1;
          if (!isPermission(m[1])) bad.push(`${f}: ${m[1]}`);
        }
      }
    }
    expect(seen).toBeGreaterThan(300);
    expect(bad).toEqual([]);
  });
});

describe("(f) the console always mounts the identity provider", () => {
  it("ConsoleShell's default export wraps everything in <IdentityProvider>", () => {
    const shell = read("app/ConsoleShell.tsx");
    const start = shell.indexOf("export default function ConsoleShell(");
    const body = shell.slice(start, shell.indexOf("\nfunction ConsoleShellInner", start));
    expect(body).toMatch(/<IdentityProvider[\s\S]*<ConsoleShellInner \{\.\.\.props\} \/>[\s\S]*<\/IdentityProvider>/);
  });

  it("the client mirror reaches non-component code (lib/call-action.ts)", () => {
    expect(read("components/identity/identity-provider.tsx")).toMatch(/installMirror\(\{ check:/);
    expect(read("lib/call-action.ts")).toMatch(/currentMirror\(\)/);
  });
});
