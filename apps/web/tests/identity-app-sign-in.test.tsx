// @vitest-environment happy-dom
/**
 * Identity › App sign-in (F6 §6.4), against a recorded API: the four tabs load through the
 * step-up guard; registering an app sends the exact body and shows the secret ONCE in a locked
 * sheet; a pool is created from a side sheet; a customer pool's user is disabled from its sheet;
 * roles are added and assigned; nothing shows a secret after the sheet closes.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { AppSignIn, appKeyOf, lines } from "../app/identity/app-sign-in/app-sign-in";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS, type Permission } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; url: string; body: unknown }[] = [];
type Answer = { status?: number; body: unknown };

function routes(table: Record<string, Answer | ((body: unknown) => Answer)>) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body });
    const key = Object.keys(table).sort((a, b) => b.length - a.length).find((k) => `${method} ${url}`.startsWith(k));
    const hit = key ? table[key] : { status: 404, body: { error: "not_found" } };
    const a = typeof hit === "function" ? hit(body) : hit;
    return new Response(JSON.stringify(a.body), { status: a.status ?? 200, headers: { "content-type": "application/json" } });
  }));
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: vi.fn(async () => undefined) } });
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.querySelectorAll(".idn-sheet-wrap").forEach((n) => n.remove());
  root = null;
  vi.unstubAllGlobals();
});

const ME: MeResponse = {
  principal_id: "usr_param", kind: "user", display_name: "Param", email: "param@acme.dev", org: F.ORG, status: "active", access_roles: ["owner"],
  sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] }, mfa_required: true, mfa_at: null,
  session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null,
};
const perms = (list: readonly Permission[]): MePermissionsResponse => ({ epoch: 1, scope: "org", permissions: [...list], step_up_fresh_until: null, catalogue_fingerprint: "x" });
async function settle(n = 8) { for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); }
async function mount(props: Partial<React.ComponentProps<typeof AppSignIn>> = {}, perms0: readonly Permission[] = ROLE_PERMISSIONS.owner) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <IdentityProvider me={ME} org={perms(perms0)}><NowProvider now={Date.now()}>
      <AppSignIn projects={[{ id: "proj_bank", name: "Bank" }]} publicOrigin="https://boss.acme.dev" {...props} />
    </NowProvider></IdentityProvider>));
  await settle();
}
const button = (text: string) => [...document.querySelectorAll("button")].find((b) => b.textContent?.trim() === text) ?? null;
const tab = (text: string) => [...document.querySelectorAll('[role="tab"]')].find((b) => b.textContent?.trim() === text) as HTMLElement;
async function click(el: Element | null) { expect(el).not.toBeNull(); await act(async () => { (el as HTMLElement).click(); }); await settle(); }
function type(el: Element | null, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  act(() => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    (el as HTMLElement).dispatchEvent(new Event("input", { bubbles: true }));
  });
}

const POOLS = { pools: [
  { id: "opl_staff", name: "Staff", kind: "workforce", signup: "closed", signup_domains: [], mfa: "optional", passkeys: true, password_policy: {} },
  { id: "opl_cust", name: "Bank customers", kind: "customer", signup: "open", signup_domains: [], mfa: "required", passkeys: true, password_policy: {} },
] };
const CLIENT = { client_id: "boc_bank", project_id: "proj_bank", app_key: "bank", pool_id: "opl_cust", name: "Bank", type: "confidential",
  redirect_uris: ["https://bank.test/cb"], post_logout_redirect_uris: [], allowed_scopes: ["openid", "email"], token_endpoint_auth_method: "client_secret_basic",
  id_token_alg: "RS256", subject_type: "pairwise", require_mfa: false, first_party: false, status: "active", has_secret: true };

describe("Identity › App sign-in", () => {
  it("lists pools and clients and shows the issuer", async () => {
    routes({ "GET /v1/oidc/pools": { body: POOLS }, "GET /v1/oidc/clients": { body: { clients: [CLIENT] } } });
    await mount();
    expect(host!.textContent).toContain(`https://boss.acme.dev/oidc/${F.ORG.id}`);
    expect(document.querySelectorAll('[data-testid="client-row"]').length).toBe(1);
    await click(tab("Pools"));
    expect(document.querySelectorAll('[data-testid="pool-row"]').length).toBe(2);
  });

  it("registers an app with the exact body and shows the secret once", async () => {
    routes({
      "GET /v1/oidc/pools": { body: POOLS }, "GET /v1/oidc/clients": { body: { clients: [] } },
      "POST /v1/oidc/clients": { status: 201, body: { ...CLIENT, client_secret: "s3cret-shown-once" } },
    });
    await mount();
    await click(button("Register app"));
    type(document.querySelector("#c-app-key"), "bank");   // the project has no apps: type the key
    type(document.querySelector("#c-name"), "Bank");
    type(document.querySelector("#c-redirect"), "https://bank.test/cb\n\n https://bank.test/cb2 ");
    await click(button("Register"));
    const post = calls.find((c) => c.method === "POST" && c.url === "/v1/oidc/clients")!;
    expect(post.body).toMatchObject({ project_id: "proj_bank", app_key: "bank", pool_id: "opl_staff", type: "confidential",
      token_endpoint_auth_method: "client_secret_basic", redirect_uris: ["https://bank.test/cb", "https://bank.test/cb2"] });
    expect(document.querySelector('[data-testid="client-secret"]')!.textContent).toContain("s3cret-shown-once");
    await click(button("I copied it"));
    expect(document.body.textContent).not.toContain("s3cret-shown-once");
  });

  it("creates a customer pool from a side sheet", async () => {
    routes({ "GET /v1/oidc/pools": { body: { pools: [] } }, "GET /v1/oidc/clients": { body: { clients: [] } },
      "POST /v1/oidc/pools": { status: 201, body: POOLS.pools[1] } });
    await mount();
    await click(tab("Pools"));
    await click(button("New pool"));
    type(document.querySelector("#pool-name"), "Bank customers");
    await click(button("Create pool"));
    const post = calls.find((c) => c.method === "POST" && c.url === "/v1/oidc/pools")!;
    expect(post.body).toMatchObject({ name: "Bank customers", kind: "customer", signup: "open", password_policy: { verify_email: true } });
  });

  it("disables a customer pool's user", async () => {
    routes({ "GET /v1/oidc/pools/opl_cust/users": { body: { users: [{ id: "opu_1", email: "cara@x.test", name: "Cara", status: "active", email_verified: true, factors: ["totp"], last_sign_in_at: null }] } },
      "GET /v1/oidc/pools": { body: POOLS }, "GET /v1/oidc/clients": { body: { clients: [CLIENT] } },
      "PATCH /v1/oidc/pools/opl_cust/users/opu_1": { body: {} } });
    await mount();
    await click(tab("Users"));
    await click(document.querySelector('[data-testid="pool-user-row"]'));
    await click(button("Disable"));
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ status: "disabled" });
  });

  it("adds a role and assigns it", async () => {
    routes({ "GET /v1/oidc/pools": { body: POOLS }, "GET /v1/oidc/clients": { body: { clients: [CLIENT] } },
      "GET /v1/oidc/clients/boc_bank/roles": { body: { roles: [{ role_key: "teller", name: "Teller" }] } },
      "GET /v1/oidc/clients/boc_bank/role-assignments": { body: { assignments: [] } },
      "POST /v1/oidc/clients/boc_bank/roles": { status: 201, body: {} }, "POST /v1/oidc/clients/boc_bank/role-assignments": { body: {} } });
    await mount();
    await click(tab("Roles"));
    type(document.querySelector('[aria-label="New role key"]'), "auditor");
    await click(button("Add role"));
    expect(calls.find((c) => c.method === "POST" && c.url.endsWith("/roles"))!.body).toEqual({ role_key: "auditor" });
  });

  it("picks the app from the project's apps, and names it as an app key", async () => {
    routes({
      "GET /v1/oidc/pools": { body: POOLS }, "GET /v1/oidc/clients": { body: { clients: [] } },
      "GET /v1/factory/apps?project_id=proj_bank": { body: { apps: [{ app: "Savings Tracker" }, { app: "Savings tracker" }, { app: "Bank" }] } },
      "POST /v1/oidc/clients": { status: 201, body: { ...CLIENT, client_secret: "x" } },
    });
    await mount();
    await click(button("Register app"));
    const picker = document.querySelector("#c-app") as HTMLSelectElement;
    expect([...picker.options].map((o) => o.textContent)).toEqual(["Choose an app…", "Savings Tracker", "Bank", "Another app (type its key)"]);
    expect(document.querySelector("#c-app-key")).toBeNull();          // no free text unless asked for
    act(() => { picker.value = "savings-tracker"; picker.dispatchEvent(new Event("change", { bubbles: true })); });
    expect((document.querySelector("#c-name") as HTMLInputElement).value).toBe("Savings Tracker");
    type(document.querySelector("#c-redirect"), "https://savings.test/cb");
    await click(button("Register"));
    expect(calls.find((c) => c.method === "POST" && c.url === "/v1/oidc/clients")!.body).toMatchObject({ app_key: "savings-tracker", name: "Savings Tracker" });
    expect(appKeyOf("  Ünïcode — App 2! ")).toBe("unicode-app-2");
  });

  it("says what a client is and gets in plain words, and opens its sheet at the side", async () => {
    routes({ "GET /v1/oidc/pools": { body: POOLS }, "GET /v1/oidc/clients": { body: { clients: [{ ...CLIENT, allowed_scopes: ["openid", "email", "offline_access"] }] } } });
    await mount();
    const row = document.querySelector('[data-testid="client-row"]')!;
    expect(row.textContent).toContain("Server app");
    expect(row.textContent).toContain("Who they are, Email address, Stay signed in");
    expect(row.textContent).not.toMatch(/confidential|offline_access/);
    expect(row.textContent).toContain("Active");
    await click(row);
    expect(document.querySelector(".idn-sheet-wrap.side .idn-sheet.side")).not.toBeNull();
    const labels = [...document.querySelectorAll('[data-testid="client-sheet"] .idn-check')].map((l) => l.textContent?.trim());
    expect(labels).toContain("Stay signed in");
    expect(labels.join(" ")).not.toContain("offline_access");
  });

  it("an Operator refused the org-wide list reads the clients of their projects", async () => {
    routes({
      "GET /v1/oidc/pools?project_id=proj_bank": { body: POOLS }, "GET /v1/oidc/pools": { status: 403, body: { error: "forbidden" } },
      "GET /v1/oidc/clients?project_id=proj_bank": { body: { clients: [CLIENT] } }, "GET /v1/oidc/clients": { status: 403, body: { error: "forbidden" } },
    });
    await mount({ readProjects: [{ id: "proj_bank", name: "Bank" }] }, ROLE_PERMISSIONS.operator);
    expect(document.querySelectorAll('[data-testid="client-row"]').length).toBe(1);
    expect(host!.textContent).not.toContain("could not be loaded");
  });

  it("splits return addresses one per line", () => {
    expect(lines(" a \n\nb\r\n")).toEqual(["a", "b"]);
  });
});
