// @vitest-environment happy-dom
/**
 * LET AN AGENT ACT FOR ME, AND CONSENT FROM A LINK (operator, 2026-10-24, northpeak-bank).
 *
 *   - Projects is a picker of the org's projects by NAME that sends their IDS (free text split on
 *     spaces posted ["NorthPeak","Bank"]); the current project is picked by default.
 *   - An ability that pushes shows the picked projects' repositories, ticked, and sends them with
 *     the access a build needs (contents/pull_requests write) as `github`.
 *   - A refusal names the project by its name and the permissions you lack, never "That didn't work".
 *   - A 401 step_up_required on Grant asks "Confirm it's you" in place.
 *   - A consent link (/account/agents?consent=<id>) opens the same consent sheet, pre-filled.
 *   - A press of an agent's offer that needs consent asks, then presses once more.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider, Sheet } from "../components/identity/kit";
import { AgentsForMe, grantBody } from "../app/account/agents/agents-for-me";
import { CONSENT_EVENT, consentFromBody, withConsentResult, type ConsentRequest } from "../lib/identity/consent";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.parse("2026-10-24T11:00:00Z");
let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; url: string; body: unknown }[] = [];
type Answer = { status?: number; body: unknown };
function routes(table: Record<string, Answer | Answer[]>) {
  calls = [];
  const seen: Record<string, number> = {};
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body });
    const key = Object.keys(table).find((k) => `${method} ${url}`.startsWith(k));
    const entry = key ? table[key] : { status: 404, body: { error: "not_found" } };
    const n = (seen[key ?? ""] = (seen[key ?? ""] ?? 0) + 1);
    const a = Array.isArray(entry) ? entry[Math.min(n, entry.length) - 1] : entry;
    return new Response(JSON.stringify(a.body), { status: a.status ?? 200, headers: { "content-type": "application/json" } });
  }));
}
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.querySelectorAll(".idn-sheet-wrap").forEach((n) => n.remove());
  root = null;
  vi.unstubAllGlobals();
});

const ME: MeResponse = {
  principal_id: "usr_param", kind: "user", display_name: "Param", email: "p@np.dev", org: F.ORG, status: "active", access_roles: ["owner"],
  sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 8, sso_links: [] }, mfa_required: true, mfa_at: null,
  session_id: "ses_p", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null,
};
const PERMS: MePermissionsResponse = { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS.owner], step_up_fresh_until: null, catalogue_fingerprint: "x" };
const PROJECTS = { items: [{ id: "prj_np", name: "NorthPeak Bank" }, { id: "prj_web", name: "Acme web" }] };
const REPOS = { repositories: [{ full_name: "paramg/northpeak-fineract-b", role: "primary" }, { full_name: "paramg/northpeak-docs", role: "secondary" }] };

async function settle(n = 8) { for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); }
async function mount(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <IdentityProvider me={ME} org={PERMS} project={{ ...PERMS, scope: "project:prj_np" }} projectId="prj_np"><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>));
  await settle();
}
const q = <T extends Element = HTMLElement>(sel: string) => document.querySelector(sel) as T | null;
async function click(el: Element | null) { expect(el).not.toBeNull(); await act(async () => { (el as HTMLElement).click(); }); await settle(); }
const base = { delegations: [], agents: [{ id: "agt_se", name: "Software Engineer" }], connections: [], untilRevokedAllowed: false, openNew: true };
const COMMON = {
  "GET /v1/identity/me/permissions": { body: PERMS },
  "GET /v1/projects/prj_np/repositories": { body: REPOS },
  "GET /v1/projects": { body: PROJECTS },
  "GET /v1/identity/me/delegations": { body: { delegations: [] } },
  "GET /v1/auth/github": { body: { configured: false } },
};

describe("Let an agent act for me", () => {
  it("picks projects by name, sends their ids, and names the repositories with build access", async () => {
    routes({ ...COMMON, "POST /v1/identity/me/delegations": { status: 201, body: { id: "dlg_1" } } });
    await mount(<AgentsForMe {...base} />);
    const np = q("[data-testid='grant-projects'] [data-project='prj_np']")!;
    expect(np.textContent).toBe("NorthPeak Bank");
    expect(np.getAttribute("aria-pressed")).toBe("true");                       // the current project
    expect(q("[data-testid='grant-projects'] [data-project='prj_web']")!.getAttribute("aria-pressed")).toBe("false");
    const repos = [...document.querySelectorAll<HTMLInputElement>("[data-testid='grant-repos'] input[type=checkbox]")];
    expect(repos.map((r) => [r.dataset.repo, r.checked])).toEqual([["paramg/northpeak-fineract-b", true], ["paramg/northpeak-docs", true]]);
    expect(q("[data-testid='grant-repos']")!.textContent).toContain("Pull requests: write");
    expect(q<HTMLInputElement>("#grant-purpose")!.placeholder).toBe("Build and steer code in NorthPeak Bank");
    await click(repos[1]);                                                          // untick the docs repo
    await click(q("button.idn-agent-pick"));
    await click(q("[data-testid='grant-submit']"));
    const post = calls.find((c) => c.method === "POST" && c.url === "/v1/identity/me/delegations");
    expect(post?.body).toMatchObject({
      agents: ["agt_se"], projects: ["prj_np"], kind: "standing",
      github: { repos: ["paramg/northpeak-fineract-b"], contents: "write", pull_requests: "write" },
      purpose: "Build and steer code in NorthPeak Bank",
    });
    expect((post?.body as { permissions: string[] }).permissions).toEqual(expect.arrayContaining(["code_task:steer", "github:read", "github:sync", "repository:read"]));
  });

  it("says which project and permissions a refusal is about, by name", async () => {
    routes({ ...COMMON, "POST /v1/identity/me/delegations": { status: 403, body: { error: "exceeds_your_access", project: "prj_np", permissions: ["github:sync"] } } });
    await mount(<AgentsForMe {...base} />);
    await click(q("button.idn-agent-pick"));
    await click(q("[data-testid='grant-submit']"));
    const sheet = q("[data-testid='grant-sheet']")!.textContent ?? "";
    expect(sheet).toContain("in NorthPeak Bank");
    expect(sheet).not.toMatch(/didn't work|exceeds_your_access|github:sync/);
  });

  it("asks you to confirm it's you when Grant meets step_up_required", async () => {
    routes({ ...COMMON, "POST /v1/identity/me/delegations": { status: 401, body: { error: "step_up_required" } } });
    await mount(<AgentsForMe {...base} />);
    await click(q("button.idn-agent-pick"));
    await click(q("[data-testid='grant-submit']"));
    expect(q("[data-testid='step-up-sheet']"), "the step-up sheet opens in place").not.toBeNull();
  });

  it("builds the github block only for an ability that pushes", () => {
    const b = { agents: ["a"], permissions: ["preview:read" as const], projects: ["p"], repos: ["o/r"], days: 30, purpose: "x" };
    expect(grantBody({ ...b, pushes: false })).not.toHaveProperty("github");
    expect(grantBody({ ...b, pushes: true }).github).toEqual({ repos: ["o/r"], contents: "write", pull_requests: "write" });
  });
});

const ASK = consentFromBody({
  error: "consent_required", task: "Loan ledger", task_run_id: null,
  agents: [{ id: "agt_se", name: "Software Engineer", role_key: "builder" }],
  permissions: ["code_task:read", "code_task:steer", "github:read", "github:sync", "repository:read"], projects: ["prj_np"],
  github: { repo: "paramg/northpeak-fineract-b", repos: ["paramg/northpeak-fineract-b"], permissions: { contents: "write", pull_requests: "write" } },
  durations: [{ kind: "standing", label: "For 30 days", days: 30 }], max_days: 90, consent_id: "pcn_abc",
})!;

describe("a consent link opens the same sheet, pre-filled", () => {
  it("grants exactly what was asked and says what to do next", async () => {
    routes({ ...COMMON, "POST /v1/identity/me/delegations": { status: 201, body: { id: "dlg_2" } } });
    await mount(<AgentsForMe {...base} openNew={false} consentAsk={ASK} consentLink="open" />);
    expect(q("[data-testid='consent-sheet']")!.textContent).toContain("Loan ledger");
    await click(q("[data-testid='consent-standing']"));
    const post = calls.find((c) => c.method === "POST" && c.url === "/v1/identity/me/delegations");
    expect(post?.body).toEqual({
      agents: ["agt_se"], permissions: ASK.permissions, projects: ["prj_np"], kind: "standing", days: 30, purpose: "Loan ledger",
      github: { repos: ["paramg/northpeak-fineract-b"], contents: "write", pull_requests: "write" },
    });
    expect(q("[data-testid='consent-sheet']")).toBeNull();
    expect(q("[data-testid='consent-link-outcome']")!.textContent).toContain("/pick");
  });

  it("an expired or foreign link says so", async () => {
    routes(COMMON);
    await mount(<AgentsForMe {...base} openNew={false} consentAsk={null} consentLink="gone" />);
    expect(q("[data-testid='consent-link-outcome']")!.textContent).toContain("expired");
  });
});

describe("pressing an agent's Give consent offer", () => {
  it("asks for consent, then presses once more", async () => {
    const results = [{ ok: false, error: "Needs your consent", data: { consent: { error: "consent_required", task: "Loan ledger", agents: [{ id: "agt_se", name: "SE" }], permissions: ["code_task:read"], projects: ["prj_np"], durations: [{ kind: "standing", label: "30 days", days: 30 }] } } },
      { ok: true, data: { ok: true } }];
    const press = vi.fn(async () => results.shift()!);
    const on = (e: Event) => (e as CustomEvent<ConsentRequest>).detail.onDecided?.("dlg_9");
    window.addEventListener(CONSENT_EVENT, on);
    try {
      const r = await withConsentResult(press);
      expect(r.ok).toBe(true);
      expect(press).toHaveBeenCalledTimes(2);
    } finally { window.removeEventListener(CONSENT_EVENT, on); }
  });

  it("cancelled, it presses nothing more", async () => {
    const press = vi.fn(async () => ({ ok: false, data: { consent: { error: "consent_required", agents: [{ id: "a" }], durations: [] } } }));
    const on = (e: Event) => (e as CustomEvent<ConsentRequest>).detail.onDecided?.(null);
    window.addEventListener(CONSENT_EVENT, on);
    try {
      expect((await withConsentResult(press)).ok).toBe(false);
      expect(press).toHaveBeenCalledTimes(1);
    } finally { window.removeEventListener(CONSENT_EVENT, on); }
  });
});

describe("a sheet keeps your place", () => {
  it("focuses its first control once, not again on every re-render (the scroll jump)", async () => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    const view = (n: number) => (
      <Sheet title="T" onClose={() => n} testId="s">
        <button type="button" data-testid="first">first</button>
        <input data-testid="purpose" />
      </Sheet>
    );
    await act(async () => root!.render(view(1)));
    expect(document.activeElement?.getAttribute("data-testid")).toBe("first");
    (q("[data-testid='purpose']") as HTMLInputElement).focus();
    // A parent re-render passes a new inline onClose (and IdentityProvider re-checks every 30 s).
    await act(async () => root!.render(view(2)));
    await act(async () => root!.render(view(3)));
    expect(document.activeElement?.getAttribute("data-testid")).toBe("purpose");
  });
});
