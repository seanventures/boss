// @vitest-environment happy-dom
/**
 * PROJECT › SETTINGS AS SCREEN 13 DRAWS IT (ACCEPTANCE 13-01 … 13-35), on the engine's own answer
 * (engine/boss_engine/services/project_experience/settings_view.py): the seven sections in the
 * wireframe's order; demo logins per environment (Staging / Preview / Prod) with Admin first, the
 * users' source and Rotate; one row per deploy value, in words, write-only Set / Rotate / Remove;
 * targets with their server and Production's Pick host; the project's autonomy level and the week's
 * ambient budget; repos with their access; who is told about what. Every write is the route the
 * engine names, and no value is ever on the page.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { SettingsTab } from "../app/projects/tab-settings";
import { MovedToProjectSettings } from "../app/projects/moved";
import { FX_OVERVIEW } from "../lib/project-experience-fixtures";
import { parseOverview } from "../lib/project-experience";
import { $$, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const type = async (el: Element | null, value: string) => {
  if (!el) throw new Error("no field");
  await act(async () => {
    const input = el as HTMLInputElement;
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    set.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle(2);
};
const Q = (p: Record<string, string>) => `?${new URLSearchParams({ project_id: P, ...p })}`;
const reveal = (env: string) => ({ href: `/v1/projects/${P}/demo-logins/reveal?env=${env}`, requires: "step_up", method: "GET" });
const login = (user: string, label: string, env: string) => ({ user, label, env, password_key: "DEMO_PASSWORD", reveal: reveal(env) });

/** What GET /v1/projects/{id}/settings answers for the seeded HelpDesk AI Demo (settings_view.view). */
const SETTINGS = { sections: [
  { key: "demo_logins", label: "Demo logins", icon: "key", users_from: "manifest", users_from_words: "boss.deploy.yaml", hint: null,
    items: [login("customer@demo.test", "Customer", "staging")],
    environments: [
      { key: "staging", label: "Staging", env: "staging", empty: null, rotate: { href: `/v1/projects/${P}/demo-logins/rotate?env=staging`, method: "POST" },
        logins: [login("customer@demo.test", "Customer", "staging"), login("agent@demo.test", "Agent", "staging"), login("admin@demo.test", "Admin", "staging")] },
      { key: "preview", label: "Preview", env: "preview", empty: null, rotate: { href: `/v1/projects/${P}/demo-logins/rotate?env=preview`, method: "POST" },
        logins: [login("admin@demo.test", "Admin", "preview")] },
      { key: "production", label: "Prod", env: null, logins: [], rotate: null, empty: "No production yet: demo logins appear once production is deployed." }],
    actions: [] },
  { key: "deploy_values", label: "Deploy values", icon: "lock", apps: ["helpdesk-ai-demo"],
    add: { label: "Add value", method: "PUT", href: "/v1/apps/helpdesk-ai-demo/secrets/{name}", body: { project_id: P, env: "deploy", mode: "test" } },
    items: [
      { name: "DATABASE_URL", words: "BOSS manages · staging", state: "done", tag: "generated", managed_by: "boss", set: true, actions: [] },
      { name: "DEMO_PASSWORD", words: "BOSS manages · per env", state: "done", tag: "generated", managed_by: "boss", set: true, actions: [] },
      { name: "OPENAI_API_KEY", words: "you set · missing in staging", state: "you", tag: null, managed_by: "you", set: false,
        actions: [{ label: "Set", method: "PUT", href: "/v1/apps/helpdesk-ai-demo/secrets/OPENAI_API_KEY", body: { project_id: P, env: "deploy", mode: "test" } }] },
      { name: "SMTP_URL", words: "you set · rotated 12d ago", state: "done", tag: null, managed_by: "you", set: true,
        actions: [{ label: "Rotate", method: "PUT", href: "/v1/apps/helpdesk-ai-demo/secrets/SMTP_URL", body: { project_id: P, env: "deploy", mode: "test" } },
                  { label: "Remove", method: "DELETE", href: `/v1/apps/helpdesk-ai-demo/secrets/SMTP_URL?project_id=${P}&env=deploy` }] }],
    actions: [{ label: "Open app secrets", method: "GET", href: "/v1/apps/helpdesk-ai-demo/secrets", open: Q({ area: "code_tasks", view: "apps" }) }] },
  { key: "targets", label: "Targets & hosts", icon: "server",
    items: [{ kind: "target", id: "dept_1", env: "staging", env_label: "Staging", words: "docker · port 8004", state: "done", host: "203.0.113.20",
              host_name: "Staging server", host_href: Q({ area: "infra", host: "dhost_1" }), href: Q({ area: "infra", target: "dept_1" }) },
            { kind: "missing", id: "production", env: "production", env_label: "Production", words: "no host yet", state: "you",
              action: { label: "Pick host", open: Q({ area: "infra", add: "target" }) } }],
    actions: [{ label: "Add a target", method: "POST", href: "/v1/deployment-targets", open: Q({ area: "infra", add: "target" }) }] },
  { key: "captain", label: "Captain & squad", icon: "user", items: [], candidates: [], squad_href: Q({ area: "workforce" }), actions: [] },
  { key: "autonomy", label: "Autonomy & budget", icon: "gauge", items: [], level: "A1",
    levels: [{ key: "A0", label: "A0 Ask", name: "Ask", meaning: "Watches and asks; changes nothing" }, { key: "A1", label: "A1 Propose", name: "Propose", meaning: "Proposes the change; a person decides" },
             { key: "A2", label: "A2 Act", name: "Act", meaning: "Acts up to a draft (a branch or draft PR); a person approves" }, { key: "A3", label: "A3 Ship", name: "Ship", meaning: "Acts and ships on its own, then reports" }],
    ambient: { daily_cap_usd: 7.14, week_cap_usd: 50, week_spent_usd: 14 }, govern_href: Q({ area: "budget", tab: "limits" }), actions: [],
    review_builds: { install: false, project: null, classes: {}, effective: false, source: "install",
                     captain_authority: { state: "on", sponsor: "Param" } } },
  { key: "repos", label: "Repos", icon: "git-branch",
    items: [{ full_name: "paramg/helpdesk-ai-demo", branch: "main", access_ok: true, access_words: "access ✓", external_url: "https://github.com/paramg/helpdesk-ai-demo" }],
    actions: [{ label: "Add", method: "PUT", href: `/v1/projects/${P}/repos/{repo_id}`, open: Q({ area: "setup", focus: "repository", open: "add" }) }] },
  { key: "notifications", label: "Notifications", icon: "bell", href: Q({ area: "observability", view: "notify" }),
    routes: [
      { event: "needs_you", label: "Needs you", words: "Email · Slack", contact_point_ids: ["cp_mail", "cp_slack"], set: { href: `/v1/projects/${P}/notification-routes/needs_you` } },
      { event: "incidents", label: "Incidents", words: "Slack", contact_point_ids: ["cp_slack"], set: { href: `/v1/projects/${P}/notification-routes/incidents` } },
      { event: "pr_ready", label: "PR ready", words: "Email", contact_point_ids: ["cp_mail"], set: { href: `/v1/projects/${P}/notification-routes/pr_ready` } }],
    contact_points: [{ id: "cp_console", kind: "console", name: "Console", word: "Console" }, { id: "cp_mail", kind: "email", name: "Project owner", word: "Email" },
                     { id: "cp_slack", kind: "slack_webhook", name: "#helpdesk", word: "Slack" }],
    items: [], actions: [] },
] };

function engine(over: (c: Call) => { status?: number; body: unknown } | undefined = () => undefined) {
  return stubFetch((c) => over(c) ?? (c.method === "GET" && c.path === `/v1/projects/${P}/settings` ? { body: SETTINGS } : c.method !== "GET" ? { body: { ok: true } } : undefined));
}

async function open(role: "owner" | "viewer" = "owner", q = "") {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&tab=settings${q}`);
  const ctx = { projectId: P, actor: "ceo", orgId: "org_1", overview: parseOverview(FX_OVERVIEW), demo: false, work: null, workState: "ready", refreshWork: () => undefined,
                onboarding: null, tab: () => undefined, open: () => undefined, ask: () => undefined } as unknown as Parameters<typeof SettingsTab>[0]["ctx"];
  await mountAs(<SettingsTab ctx={ctx} />, role);
  await settle();
}

// 13-12: no native confirm() anywhere on the page — a call fails the test.
beforeEach(() => { vi.stubGlobal("confirm", () => { throw new Error("window.confirm() was called"); }); });
afterEach(() => { unmount(); });

describe("Settings (screen 13) on the engine's answer", () => {
  it("13-01: the seven sections in the wireframe's order, each a nav button", async () => {
    engine();
    await open();
    expect($$(".px-set-nav button").map((b) => b.textContent)).toEqual(
      ["Demo logins", "Deploy values", "Targets & hosts", "Captain & squad", "Autonomy & budget", "Repos", "Notifications"]);
  });

  it("13-02…13-06: demo logins switch environment, Admin first, the users' source, Rotate posts that environment", async () => {
    const calls = engine();
    await open();
    expect($$("[data-testid='px-demo-logins'] .px-cred").map((r) => r.getAttribute("data-testid"))).toEqual(
      ["px-login-admin@demo.test", "px-login-agent@demo.test", "px-login-customer@demo.test"]);
    expect(tid("px-login-admin@demo.test")!.querySelector(".px-tag")!.getAttribute("data-tone")).toBe("blue");
    expect(tid("px-demo-logins")!.textContent).toContain("Users from boss.deploy.yaml");
    await press(tid("px-set-demo-env-preview"));
    expect($$("[data-testid='px-demo-logins'] .px-cred")).toHaveLength(1);
    await press(tid("px-set-demo-env-production"));
    expect(tid("px-demo-env-empty")!.textContent).toContain("No production yet");
    expect(tid("px-set-demo-rotate")).toBeNull();
    await press(tid("px-set-demo-env-staging"));
    await press(tid("px-set-demo-rotate"));
    // 13-06: Rotate asks first in the shared confirm sheet; nothing is posted until confirmed.
    expect(tid("px-set-demo-rotate-sheet")!.getAttribute("role")).toBe("dialog");
    expect(tid("px-set-demo-rotate-sheet-environment")!.textContent).toBe("Staging");
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await press(tid("px-set-demo-rotate-sheet-cancel"));
    expect(tid("px-set-demo-rotate-sheet")).toBeNull();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await press(tid("px-set-demo-rotate"));
    await press(tid("px-set-demo-rotate-sheet-go"));
    expect(calls.find((c) => c.method === "POST")?.path).toBe(`/v1/projects/${P}/demo-logins/rotate`);
    expect(calls.find((c) => c.method === "POST")?.search).toBe("?env=staging");
  });

  it("13-08…13-13: one row per value in words; Set and Add are write-only; Remove asks and deletes; no value on the page", async () => {
    const calls = engine();
    await open();
    expect($$("[data-testid='px-set-values'] .px-ck-i").map((r) => r.getAttribute("data-testid"))).toEqual(
      ["px-set-value-DATABASE_URL", "px-set-value-DEMO_PASSWORD", "px-set-value-OPENAI_API_KEY", "px-set-value-SMTP_URL"]);
    expect(tid("px-set-value-DEMO_PASSWORD")!.textContent).toContain("BOSS manages · per env");
    expect(tid("px-set-value-OPENAI_API_KEY")!.getAttribute("data-s")).toBe("you");
    await press(tid("px-set-value-set-OPENAI_API_KEY"));
    await type(tid("px-set-value-input"), "sk-test-123");
    await act(async () => { tid("px-set-value-entry")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    await settle();
    const put = calls.find((c) => c.method === "PUT" && c.path === "/v1/apps/helpdesk-ai-demo/secrets/OPENAI_API_KEY");
    expect(put?.body).toEqual({ project_id: P, env: "deploy", mode: "test", value: "sk-test-123" });
    expect(document.body.textContent).not.toContain("sk-test-123");
    await press(tid("px-set-value-remove-SMTP_URL"));
    // 13-12: Remove asks in the console's own sheet; nothing is sent until it is confirmed.
    expect(tid("px-set-value-remove-sheet")).not.toBeNull();
    expect(tid("px-set-value-remove-sheet")!.getAttribute("role")).toBe("dialog");
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await press(tid("px-set-value-remove-sheet-cancel"));
    expect(tid("px-set-value-remove-sheet")).toBeNull();
    await press(tid("px-set-value-remove-SMTP_URL"));
    await press(tid("px-set-value-remove-sheet-go"));
    expect(calls.find((c) => c.method === "DELETE")?.path).toBe("/v1/apps/helpdesk-ai-demo/secrets/SMTP_URL");
    await press(tid("px-set-add-value"));
    await type(tid("px-set-value-name"), "smtp_password");
    await type(tid("px-set-value-input"), "hunter2");
    await act(async () => { tid("px-set-value-entry")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    await settle();
    expect(calls.some((c) => c.method === "PUT" && c.path === "/v1/apps/helpdesk-ai-demo/secrets/SMTP_PASSWORD")).toBe(true);
  });

  it("13-14…13-16: Staging names its server and opens it in Infra; Production asks for a host", async () => {
    engine();
    await open();
    expect(tid("px-set-target-staging")!.textContent).toContain("docker · port 8004");
    expect(new URLSearchParams(tid("px-set-host-staging")!.getAttribute("href")!.split("?")[1]).get("host")).toBe("dhost_1");
    expect(tid("px-set-pick-host")!.getAttribute("data-kind")).toBe("amber");
  });

  it("13-20/13-21: the level saves this project's autonomy; the week's budget shows spent / cap and saves", async () => {
    const calls = engine();
    await open();
    expect(tid("px-set-level-A1")!.getAttribute("aria-checked")).toBe("true");
    expect(tid("px-set-budget")!.textContent).toBe("$14 / $50");
    await press(tid("px-set-level-A2"));
    expect(calls.find((c) => c.method === "PUT" && c.path === `/v1/projects/${P}/autonomy`)?.body).toEqual({ level: "A2" });
    await press(tid("px-set-budget-edit"));
    await type(tid("px-set-budget-input"), "70");
    await press(tid("px-set-budget-save"));
    expect(calls.find((c) => c.method === "PUT" && c.path === "/v1/ambient/settings")?.body).toEqual({ project_id: P, daily_cap_usd_micros: 10_000_000 });
  });

  it("always-on builds start on the Captain's authority unless the project asks to review them first", async () => {
    const calls = engine();
    await open();
    const toggle = tid("px-set-review-builds-toggle")!;
    expect(toggle.getAttribute("role")).toBe("switch");
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(tid("px-set-review-builds-why")!.textContent).toContain("Captain authority (sponsored by Param)");
    expect(tid("px-set-review-builds-why")!.textContent).toContain("merging and production deploys still need you");
    await press(toggle);
    expect(calls.find((c) => c.method === "PUT" && c.path === "/v1/ambient/review-builds")?.body).toEqual({ project_id: P, review: true });
    expect(tid("px-set-review-builds-toggle")!.getAttribute("aria-checked")).toBe("true");
    expect(tid("px-set-review-builds-why")!.textContent).toContain("waits on one card");
  });

  it("13-24…13-28: repos show their access; each route says its channels and can be changed", async () => {
    const calls = engine();
    await open();
    expect(tid("px-set-repos")!.textContent).toContain("access ✓");
    expect(tid("px-set-route-needs_you")!.textContent).toContain("Email · Slack");
    await press(tid("px-set-route-edit-pr_ready"));
    const boxes = [...tid("px-set-route-picker")!.querySelectorAll("input[type=checkbox]")] as HTMLInputElement[];
    await press(boxes[2]);
    await press(tid("px-set-route-save"));
    expect(calls.find((c) => c.method === "PUT" && c.path === `/v1/projects/${P}/notification-routes/pr_ready`)?.body)
      .toEqual({ contact_point_ids: ["cp_mail", "cp_slack"] });
  });

  it("13-07 / X-86: a viewer sees every write greyed with its reason", async () => {
    engine();
    await open("viewer");
    for (const id of ["px-login-reveal-admin@demo.test", "px-set-value-set-OPENAI_API_KEY", "px-set-level-A2", "px-set-route-edit-needs_you", "px-set-demo-rotate"]) {
      expect(tid(id)!.getAttribute("aria-disabled"), id).toBe("true");
    }
  });

  it("13-30: an old home shows the bar and Open lands on that section", async () => {
    await mountAs(<MovedToProjectSettings section="autonomy" projectId={P} testid="moved" />);
    expect(tid("moved")!.textContent).toContain("Moved to Project › Settings");
    expect(tid("moved")!.textContent).toContain("Open");
    const q = new URLSearchParams(tid("moved")!.getAttribute("href")!.split("?")[1]);
    expect([q.get("project_id"), q.get("area"), q.get("tab"), q.get("section")]).toEqual([P, "project", "settings", "autonomy"]);
  });
});

describe("Settings round 2 (13-03 / X-73, 13-18, 13-20)", () => {
  it("13-03 / X-73: a refused reveal says what is wrong in words, in red, with the fix — never the exception class", async () => {
    engine((c) => (c.path.startsWith(`/v1/projects/${P}/demo-logins/reveal`) ? { status: 503, body: {
      error: "vault_not_set_up", detail: "BOSS's secrets vault isn't set up on this install (no master key), so saved values can't be read or changed. Run boss doctor on the BOSS host to set it up.",
      fix: { label: "Run boss doctor on the BOSS host", command: "boss doctor" } } } : undefined));
    await open();
    await press(tid("px-login-reveal-admin@demo.test"));
    const line = tid("px-said-line")!;
    expect(line.textContent).toContain("secrets vault isn't set up");
    expect(line.hasAttribute("data-ok")).toBe(false);
    expect(tid("px-said")!.getAttribute("role")).toBe("alert");
    expect(tid("px-said-fix")!.textContent).toBe("boss doctor");
    expect(document.body.textContent).not.toMatch(/VaultNotFound|MasterKeyMissing|see the API log/);
  });

  it("X-73: a 500 never prints its exception class; it says BOSS hit an error and the request id", async () => {
    engine((c) => (c.path.startsWith(`/v1/projects/${P}/demo-logins/reveal`) ? { status: 500, body: {
      error: "internal_error", detail: "VaultNotFound (see the API log, request req_abc)", request_id: "req_abc" } } : undefined));
    await open();
    await press(tid("px-login-reveal-admin@demo.test"));
    expect(tid("px-said-line")!.textContent).toContain("BOSS hit an error it didn't expect (request req_abc");
    expect(document.body.textContent).not.toContain("VaultNotFound");
  });

  it("13-07: a refused Rotate shows red, never green", async () => {
    engine((c) => (c.method === "POST" && c.path.includes("/demo-logins/rotate") ? { status: 503, body: {
      error: "vault_not_set_up", detail: "BOSS's secrets vault isn't set up on this install (no master key), so saved values can't be read or changed.", fix: { label: "Run boss doctor", command: "boss doctor" } } } : undefined));
    await open();
    await press(tid("px-set-demo-rotate"));
    await press(tid("px-set-demo-rotate-sheet-go"));
    // The refusal is red inside the sheet (it stays open) and on the section's line.
    expect(tid("px-set-demo-rotate-sheet-error")!.textContent).toContain("vault");
    const line = $$("[data-testid='px-said-line']").find((x) => x.textContent!.includes("vault"))!;
    expect(line.hasAttribute("data-ok")).toBe(false);
  });

  it("13-18 / PX-43: Edit squad opens the squad editor here; Save states the project's team", async () => {
    const calls = engine((c) => {
      if (c.path.startsWith("/api/project-agents") && c.method === "GET") return { body: { agents: [{ role_key: "product_manager" }, { role_key: "software_engineer" }] } };
      if (c.path.startsWith("/api/agent")) return { body: { items: [{ role_key: "product_manager", display_name: "Juno" }, { role_key: "software_engineer", display_name: "Forge" }, { role_key: "qa_engineer", display_name: "Vera" }] } };
      return undefined;
    });
    await open();
    expect(tid("px-set-squad")!.tagName).toBe("BUTTON");
    await press(tid("px-set-squad"));
    expect(tid("px-set-squad-editor")).not.toBeNull();
    await press(tid("px-set-squad-product_manager")!.querySelector("button"));
    await act(async () => {
      const sel = tid("px-set-squad-add") as HTMLSelectElement;
      sel.value = "qa_engineer";
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await settle();
    await press(tid("px-set-squad-save"));
    const post = calls.find((c) => c.method === "POST" && c.path.startsWith("/api/project-agents"));
    expect(post?.body).toMatchObject({ project_id: P, app: "*", action: "set", role_keys: ["software_engineer", "qa_engineer"] });
  });

  it("13-18: a link with edit=squad lands with the editor open", async () => {
    engine((c) => (c.path.startsWith("/api/") ? { body: { agents: [], items: [] } } : undefined));
    await open("owner", "&section=captain&edit=squad");
    expect(tid("px-set-squad-editor")).not.toBeNull();
  });

  it("13-20: the level's tooltip says the same name as its label, and what it allows", async () => {
    engine();
    await open();
    const a2 = tid("px-set-level-A2")!;
    expect(a2.textContent).toBe("A2 Act");
    expect(a2.getAttribute("title")).toBe("A2 Act — Acts up to a draft (a branch or draft PR); a person approves");
    expect(document.body.innerHTML).not.toMatch(/Act to draft|Act and report|Observe/);
  });
});
