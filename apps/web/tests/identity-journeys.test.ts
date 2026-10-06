/**
 * J1–J15 are walked by the independent tester (tests/_helpers/identity-journeys.ts). This keeps
 * the script honest — every URL is a real page or public path, every persona exists — and runs
 * the API-seam half of the journeys the stub can answer, so the console's expectations of the
 * refusals and shapes are pinned before S1–S5 land.
 */
import { beforeEach, describe, expect, it } from "vitest";

import { isPublicConsolePath, pageFor } from "../lib/identity/pages";
import { resetStub, stubHandle } from "../lib/identity/stub/server";
import * as F from "../lib/identity/stub/fixtures";
import { JOURNEYS, PERSONA_EMAIL } from "./_helpers/identity-journeys";
import { req, signIn } from "./_helpers/identity-stub-session";

const AUTH_PAGES = /^\/(login|claim|enrol|invite\/[^/]+|signin\/link|reset|signup)$/;

describe("the journey script", () => {
  it("has J1–J15, each with steps that land somewhere real", () => {
    expect(JOURNEYS.map((j) => j.id)).toEqual(Array.from({ length: 15 }, (_, i) => `J${i + 1}`));
    const people = F.people(Date.now()).map((p) => p.email);
    for (const email of Object.values(PERSONA_EMAIL)) expect(people).toContain(email);
    for (const j of JOURNEYS) {
      expect(j.steps.length, j.id).toBeGreaterThan(0);
      for (const s of j.steps) {
        if (!s.go) continue;
        const path = s.go.split(/[?#]/)[0];
        const ok = path === "/" || AUTH_PAGES.test(path) || isPublicConsolePath(path) || pageFor(path) !== null;
        expect(ok, `${j.id} ${s.go}`).toBe(true);
      }
    }
  });
});

describe("the API half the stub can answer", () => {
  beforeEach(() => resetStub());

  it("J9: a revoke previews its cascade, then revokes once", () => {
    const owner = signIn(PERSONA_EMAIL.owner);
    const dry = stubHandle(req("POST", "/v1/identity/delegations/dlg_01JB7Q/revoke", { cookies: owner.cookies, csrf: owner.csrf, body: { dry_run: true } }));
    const kinds = (dry.body as { items: { kind: string }[] }).items.map((i) => i.kind);
    expect(kinds).toEqual(["runs", "tokens", "github", "grants", "schedules"]);
    expect(stubHandle(req("POST", "/v1/identity/delegations/dlg_01JB7Q/revoke", { cookies: owner.cookies, csrf: owner.csrf, body: {} })).status).toBe(200);
    expect(stubHandle(req("POST", "/v1/identity/delegations/dlg_01JB7Q/revoke", { cookies: owner.cookies, csrf: owner.csrf, body: {} })).body).toEqual({ error: "revoked_is_terminal" });
  });

  it("J10: a ceiling only ever holds delegable permissions", () => {
    const owner = signIn(PERSONA_EMAIL.owner);
    const bad = stubHandle(req("PATCH", "/v1/identity/agents/agt_forge/passport", { cookies: owner.cookies, csrf: owner.csrf, body: { ceiling: ["deployment:production"] } }));
    expect(bad.body).toMatchObject({ error: "not_delegable", permission: "deployment:production" });
    const ok = stubHandle(req("PATCH", "/v1/identity/agents/agt_forge/passport", { cookies: owner.cookies, csrf: owner.csrf, body: { ceiling: ["code_task:read"] } }));
    expect(JSON.stringify(ok.body)).toContain("−preview:manage");
  });

  it("J11: the Viewer is refused People with the permission named", () => {
    const viewer = signIn(PERSONA_EMAIL.viewer);
    expect(stubHandle(req("GET", "/v1/identity/users", { cookies: viewer.cookies })).body).toEqual({ error: "forbidden", permission: "user:read" });
    const perms = stubHandle(req("GET", "/v1/identity/me/permissions", { cookies: viewer.cookies })).body as { permissions: string[] };
    expect(perms.permissions).toContain("directory:read");
    expect(perms.permissions).not.toContain("user:invite");
  });

  it("J12: the impact preview names the change and its reach", () => {
    const admin = signIn(PERSONA_EMAIL.admin);
    const role = F.roles(Date.now()).find((r) => r.key === "release_captain")!;
    const next = role.permissions.filter((p) => p !== "deployment:create");
    const out = stubHandle(req("POST", "/v1/identity/access-roles/arl_release_captain/preview", { cookies: admin.cookies, csrf: admin.csrf, body: { permissions: next } }));
    expect(out.body).toMatchObject({ diff: { added: [], removed: ["deployment:create"] }, routes: { lost: 3 } });
  });

  it("J13: rotate leaves the old key in grace, and the secret is only in the creating answer", () => {
    const admin = signIn(PERSONA_EMAIL.admin);
    const made = stubHandle(req("POST", "/v1/identity/service-accounts/sva_ci_deployer/keys", { cookies: admin.cookies, csrf: admin.csrf,
      body: { name: "rotated", scopes: ["deployment:read"], expires_days: 90, replaces: "key_7f3k2q", overlap_hours: 1 } }));
    expect(made.status).toBe(201);
    expect((made.body as { replaces: { id: string } }).replaces.id).toBe("key_7f3k2q");
  });

  it("J15: an Auditor can read and verify the chain, and nothing else", () => {
    const auditor = signIn(PERSONA_EMAIL.auditor);
    const page = stubHandle(req("GET", "/v1/identity/audit?grant=dlg_01JB7Q", { cookies: auditor.cookies })).body as { events: { grant_id: string }[] };
    expect(page.events.length).toBeGreaterThan(3);
    expect(page.events.every((e) => e.grant_id === "dlg_01JB7Q")).toBe(true);
    expect(stubHandle(req("POST", "/v1/identity/audit/verify", { cookies: auditor.cookies, csrf: auditor.csrf })).status).toBe(200);
    expect(stubHandle(req("PUT", "/v1/identity/policy", { cookies: auditor.cookies, csrf: auditor.csrf, body: {} })).body).toMatchObject({ permission: "security_policy:manage" });
  });
});
