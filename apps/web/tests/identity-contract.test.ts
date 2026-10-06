/**
 * THE CONSOLE'S IDENTITY CONTRACT HOLDS (docs/product/IDENTITY_BUILD.md §2.5).
 *
 *  - the generated permission union is the catalogue, closed (no `${string}`);
 *  - the TS mirror of /v1/identity/me and /me/permissions names exactly the Python contract's fields;
 *  - every console area is declared, or is still on the shrinking list (and only those);
 *  - every permission literal in the console's identity code is a catalogue key;
 *  - can() answers "cannot" for anything it does not positively know.
 */
import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { AREA_IDS } from "../lib/area-ids";
import { AREA_ACCESS, AREA_ACTIONS, IDENTITY_PAGE_ACCESS } from "../lib/identity/areas";
import { can, snapshotFrom, type MePermissionsResponse, type MeResponse } from "../lib/identity/contract";
import { isPermission, PERMISSIONS, PERMISSION_META, ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";

const CONTRACTS_PY = readFileSync("../../engine/boss_engine/identity/contracts.py", "utf8");
const CONTRACT_TS = readFileSync("lib/identity/contract.ts", "utf8");

function pyFields(model: string): string[] {
  const start = CONTRACTS_PY.indexOf(`class ${model}(`);
  const body = CONTRACTS_PY.slice(start).split("\n").slice(1);
  const out: string[] = [];
  for (const line of body) {
    if (/^\S/.test(line)) break; // next top-level statement
    const m = /^    ([a-z_]+): /.exec(line);
    if (m) out.push(m[1]);
  }
  return out.sort();
}

function tsFields(iface: string): string[] {
  const start = CONTRACT_TS.indexOf(`export interface ${iface} {`);
  const body = CONTRACT_TS.slice(start, CONTRACT_TS.indexOf("\n}", start));
  return [...body.matchAll(/^  ([a-z_]+): /gm)].map((m) => m[1]).sort();
}

describe("the generated permission union", () => {
  it("is the 180-entry catalogue with the reviewed role sizes", () => {
    expect(PERMISSIONS.length).toBe(180);
    expect(new Set(PERMISSIONS).size).toBe(180);
    expect(ROLE_PERMISSIONS.owner.length).toBe(175);
    expect(ROLE_PERMISSIONS.admin.length).toBe(166);
    expect(ROLE_PERMISSIONS.operator.length).toBe(83);
    expect(ROLE_PERMISSIONS.viewer.length).toBe(46);
    expect(ROLE_PERMISSIONS.auditor.length).toBe(59);
  });

  it("is closed: anything outside the catalogue is not a Permission", () => {
    expect(isPermission("workflow:publish")).toBe(true);
    expect(isPermission("workflow:*")).toBe(false);
    expect(isPermission("workflow:publish ")).toBe(false);
    const code = readFileSync("lib/identity/permissions.gen.ts", "utf8")
      .split("\n").filter((l) => !l.trimStart().startsWith("//")).join("\n");
    expect(code).not.toContain("${string}");
  });
});

describe("the /me contract", () => {
  it.each([
    ["MeResponse", "MeResponse"],
    ["MePermissionsResponse", "MePermissionsResponse"],
    ["MeSignInMethods", "MeSignInMethods"],
    ["MeOrg", "MeOrg"],
    ["MeBanner", "MeBanner"],
  ])("TS %s names exactly the Python %s fields", (ts, py) => {
    expect(tsFields(ts)).toEqual(pyFields(py));
  });
});

describe("console area coverage", () => {
  it("every area is declared (AREA_ACCESS is total; the shrinking list is gone)", () => {
    const undeclared = AREA_IDS.filter((id) => !(id in AREA_ACCESS));
    expect(undeclared).toEqual([]);
    expect(existsSync("../../tests/identity/undeclared_console_areas.txt")).toBe(false);
  });

  it("every declared area, identity page and action names a catalogue permission", () => {
    const pages = [...Object.values(AREA_ACCESS), ...Object.values(IDENTITY_PAGE_ACCESS)];
    for (const access of pages) {
      if ("permission" in access) expect(isPermission(access.permission)).toBe(true);
    }
    for (const actions of Object.values(AREA_ACTIONS)) {
      for (const permission of Object.values(actions)) expect(isPermission(permission)).toBe(true);
    }
  });
});

describe("can()", () => {
  const response: MePermissionsResponse = {
    epoch: 4, scope: "project:acme-web", permissions: ["workflow:read", "workflow:publish", "not:a_key"],
    step_up_fresh_until: null, catalogue_fingerprint: "x",
  };
  const snap = snapshotFrom(response);
  const here = { kind: "project", projectId: "acme-web" } as const;

  it("drops keys the catalogue does not know", () => {
    expect(snap.permissions.size).toBe(2);
  });

  it("allows what is held, with step-up flagged for step-up permissions", () => {
    expect(can(snap, "workflow:read", here)).toEqual({ allowed: true, reason: "", needsStepUp: false });
    expect(PERMISSION_META["workflow:publish"].stepUp).toBe(true);
    expect(can(snap, "workflow:publish", here).needsStepUp).toBe(true);
    expect(can({ ...snap, stepUpFreshUntil: 2_000 }, "workflow:publish", here, 1_000).needsStepUp).toBe(false);
  });

  it("says why it refuses, and never guesses across scopes", () => {
    expect(can(snap, "deployment:production", here)).toMatchObject({ allowed: false, reason: "Needs deployment:production" });
    expect(can(snap, "workflow:read", { kind: "project", projectId: "other" }).allowed).toBe(false);
    expect(can(snap, "workflow:read").allowed).toBe(false);
    expect(can(null, "workflow:read").allowed).toBe(false);
  });

  it("type-checks a MeResponse literal against the contract", () => {
    const me: MeResponse = {
      principal_id: "usr_1", kind: "user", display_name: "Param", email: null,
      org: { id: "org_boss", name: "BOSS" }, status: "active", access_roles: ["owner"],
      sign_in: { password: true, totp: false, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] },
      mfa_required: true, mfa_at: null, session_id: "ses_1", session_expires_at: null,
      csrf_token: "c", epoch: 4, network_bound: false, banners: [],
    };
    expect(me.kind).toBe("user");
  });
});
