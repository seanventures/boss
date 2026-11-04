// @vitest-environment happy-dom
/**
 * NEW PROJECT, in the top bar's project menu (POST /v1/projects, `project:create`).
 *
 * BOSS had no way to make a second project in an org. The menu beside the organization says which
 * project is open, lists the org's others, and offers New project: a name, an optional
 * description, Create. It mirrors `project:create` (an Operator sees it disabled with the reason);
 * the API decides, and a person's step-up answer opens "Confirm it's you" through ActionForm.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const createProject = vi.fn();
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("../lib/client", () => ({
  API_URL: "http://boss-api",
  client: { createProject: (...args: unknown[]) => createProject(...args) },
}));

import { BossApiError } from "@boss/api-client";

import { IdentityProvider } from "../components/identity/identity-provider";
import { ACTION_PERMISSIONS } from "../lib/identity/action-permissions";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { wantsStepUp } from "../lib/identity/mirror";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import { needsWords } from "../lib/identity/words";
import { createProjectAction } from "../app/project-actions";
import { ProjectMenu, projectHref } from "../app/project-menu";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;
beforeEach(() => { createProject.mockReset(); window.history.replaceState(null, "", "/?org=org_1&area=overview"); });
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; });

const ME = {
  principal_id: "usr_x", kind: "user", display_name: "Ada", email: "a@acme.dev", org: { id: "org_1", name: "Acme" },
  status: "active", access_roles: ["admin"],
  sign_in: { password: true, totp: true, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
  mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1,
  network_bound: false, banners: [], avatar_seed: null,
} as MeResponse;

function perms(role: keyof typeof ROLE_PERMISSIONS): MePermissionsResponse {
  return { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]], step_up_fresh_until: null,
    catalogue_fingerprint: "x" };
}

const PROJECTS = [{ id: "boss-dogfood", name: "Default project" }, { id: "northpeak-bank-a", name: "Northpeak Bank A" }];

async function mount(role: keyof typeof ROLE_PERMISSIONS, activeId = "boss-dogfood") {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <IdentityProvider me={ME} org={perms(role)}>
      <ProjectMenu projects={PROJECTS} activeId={activeId} homeId="boss-dogfood" />
    </IdentityProvider>));
  return host;
}

const form = (data: Record<string, string>) => {
  const fd = new FormData();
  for (const [k, v] of Object.entries(data)) fd.set(k, v);
  return fd;
};

describe("the project menu", () => {
  it("says which project is open and links to the org's others, keeping the org", async () => {
    const el = await mount("admin", "northpeak-bank-a");
    expect(el.querySelector("summary")!.textContent).toBe("Northpeak Bank A");
    expect(el.querySelector('[data-testid="project-menu-northpeak-bank-a"]')!.getAttribute("aria-current")).toBe("page");
    const home = new URLSearchParams(el.querySelector('[data-testid="project-menu-boss-dogfood"]')!.getAttribute("href")!.slice(1));
    expect(home.get("org")).toBe("org_1");
    expect(home.has("project_id")).toBe(false);
    expect(new URLSearchParams(projectHref("northpeak-bank-a", "boss-dogfood").slice(1)).get("project_id")).toBe("northpeak-bank-a");
  });

  it("New project opens a short form: Name, Description (optional), Create", async () => {
    const el = await mount("admin");
    const button = el.querySelector<HTMLButtonElement>('[data-testid="new-project"]')!;
    expect(button.getAttribute("data-can")).toBe("yes");
    expect(button.getAttribute("title")).toMatch(/new project in this organization/i);
    await act(async () => button.click());
    // BOSSAPP-810: New project opens its three doors first; this form is New app from an idea ›
    // "Knowledge graph or blank project".
    await act(async () => document.querySelector<HTMLButtonElement>('[data-testid="start-idea"]')!.click());
    await act(async () => document.querySelector<HTMLButtonElement>('[data-testid="idea-form"]')!.click());
    // A SHEET, outside the menu (round 2: the dropdown cut the type's text and scrolled sideways).
    const f = document.querySelector('[data-testid="new-project-form"]')!;
    expect(el.contains(f)).toBe(false);
    expect(f.getAttribute("role")).toBe("dialog");
    expect(f.getAttribute("data-permission")).toBe("project:create");
    expect(f.querySelector<HTMLInputElement>('input[name="name"]')!.required).toBe(true);
    expect(f.querySelector<HTMLInputElement>('input[name="description"]')!.required).toBe(false);
    expect(f.querySelector('button[type="submit"]')!.textContent).toBe("Create");
    expect(f.querySelector('button[type="submit"]')!.className).toBe("ag-btn ag-btn-primary");
    expect(f.querySelector('[data-testid="new-project-cancel"]')!.className).toBe("ag-btn");
  });

  it("is refused, with the reason, to a member without project:create", async () => {
    const el = await mount("operator");
    const button = el.querySelector<HTMLButtonElement>('[data-testid="new-project"]')!;
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(button.getAttribute("title")).toBe(needsWords("project:create"));
    await act(async () => button.click());
    expect(document.querySelector('[data-testid="new-project-form"]')).toBeNull();
  });

  it("sits in the top bar beside the organization, on the org's own projects", () => {
    const src = (p: string) => readFileSync(join(process.cwd(), p), "utf8");
    expect(src("app/ConsoleShell.tsx")).toMatch(/className="bx-org-chip"[\s\S]{0,400}<ProjectMenu projects=\{projects\.items\}/);
    expect(src("app/page.tsx")).toMatch(/projects\.items\.filter\(\(p\) => p\.orgId === activeOrgId\)/);
  });
});

describe("the Create action", () => {
  it("declares project:create, the permission POST /v1/projects checks", () => {
    expect(ACTION_PERMISSIONS["app/project-actions.ts#createProjectAction"]).toBe("project:create");
    const matrix = readFileSync(join(process.cwd(), "..", "..", "tests/identity/matrix.yaml"), "utf8");
    expect(matrix).toContain('"POST /v1/projects": ["permit project:create @ org", "OA"]');
  });

  it("creates the project and hands back its id to open", async () => {
    createProject.mockResolvedValue({ id: "northpeak-bank-b", name: "Northpeak Bank B" });
    const result = await createProjectAction(form({ name: " Northpeak Bank B ", description: "Arm B" }));
    expect(createProject).toHaveBeenCalledWith({ name: "Northpeak Bank B", description: "Arm B" });
    expect(result).toMatchObject({ ok: true, data: { projectId: "northpeak-bank-b" } });
  });

  it("a person's step-up refusal comes back as one, so the sheet opens and the press is sent again", async () => {
    createProject.mockRejectedValue(new BossApiError("step up", 401, { error: "step_up_required" }));
    const result = await createProjectAction(form({ name: "Northpeak Bank B" }));
    expect(wantsStepUp(result)).toBe(true);
  });

  it("a slug in use says so, naming the existing project", async () => {
    createProject.mockRejectedValue(new BossApiError("conflict", 409, {
      detail: { error: "project_exists", slug: "northpeak-bank-a", project_id: "northpeak-bank-a" } }));
    const result = await createProjectAction(form({ name: "Northpeak Bank A" }));
    expect(result.ok).toBe(false);
    expect(result.error).toContain("northpeak-bank-a already exists");
  });

  it("asks for a name before calling the API", async () => {
    const result = await createProjectAction(form({ name: "  " }));
    expect(result.ok).toBe(false);
    expect(createProject).not.toHaveBeenCalled();
  });
});

describe("a project's type (BOSSAPP-768 project flow)", () => {
  it("New project asks what it is for — an app, or a knowledge graph of an existing codebase", async () => {
    const el = await mount("admin");
    await act(async () => el.querySelector<HTMLButtonElement>('[data-testid="new-project"]')!.click());
    // BOSSAPP-810: New project opens its three doors first; this form is New app from an idea ›
    // "Knowledge graph or blank project".
    await act(async () => document.querySelector<HTMLButtonElement>('[data-testid="start-idea"]')!.click());
    await act(async () => document.querySelector<HTMLButtonElement>('[data-testid="idea-form"]')!.click());
    const radios = [...document.querySelectorAll<HTMLInputElement>('[data-testid="new-project-type"] input[name="project_type"]')];
    expect(radios.map((r) => r.value)).toEqual(["app", "knowledge_graph"]);
    expect(radios[0].checked).toBe(true);
    expect([...document.querySelectorAll('[data-testid="new-project-type"] b')].map((b) => b.textContent))
      .toEqual(["Build an app", "Knowledge Graph for an existing codebase"]);
    await act(async () => radios[1].click());
    expect(document.querySelector('[data-testid="new-project-form"] button[type="submit"]')!.textContent).toBe("Create and set up");
  });

  it("a refused or cancelled step-up keeps what was typed, and says why", async () => {
    // Round 2: cancelling the passkey closed the form and lost the name.
    createProject.mockRejectedValue(new BossApiError("conflict", 409, {
      detail: { error: "project_exists", slug: "fineract-map", project_id: null } }));
    const el = await mount("admin");
    await act(async () => el.querySelector<HTMLButtonElement>('[data-testid="new-project"]')!.click());
    // BOSSAPP-810: New project opens its three doors first; this form is New app from an idea ›
    // "Knowledge graph or blank project".
    await act(async () => document.querySelector<HTMLButtonElement>('[data-testid="start-idea"]')!.click());
    await act(async () => document.querySelector<HTMLButtonElement>('[data-testid="idea-form"]')!.click());
    const name = document.querySelector<HTMLInputElement>('[data-testid="new-project-form"] input[name="name"]')!;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(name, "Fineract map");
      name.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => document.querySelector<HTMLInputElement>('[data-testid="new-project-type-knowledge_graph"] input')!.click());
    // The menu closing around it (a passkey prompt's press outside) does not touch the sheet.
    await act(async () => document.body.dispatchEvent(new Event("pointerdown", { bubbles: true })));
    await act(async () => document.querySelector<HTMLButtonElement>('[data-testid="new-project-create"]')!.click());
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    const f = document.querySelector('[data-testid="new-project-form"]')!;
    expect(f).not.toBeNull();
    expect(f.querySelector<HTMLInputElement>('input[name="name"]')!.value).toBe("Fineract map");
    expect(f.querySelector<HTMLInputElement>('[data-testid="new-project-type-knowledge_graph"] input')!.checked).toBe(true);
    expect(f.querySelector('[data-testid="new-project-error"]')!.textContent).toBe("That name is taken. Choose another.");
    await act(async () => document.querySelector<HTMLButtonElement>('[data-testid="new-project-cancel"]')!.click());
    expect(document.querySelector('[data-testid="new-project-form"]')).toBeNull();
  });

  it("the action sends the type, and a knowledge-graph project is handed back as one", async () => {
    createProject.mockResolvedValue({ id: "fineract-map", name: "Fineract map", project_type: "knowledge_graph" });
    const result = await createProjectAction(form({ name: "Fineract map", project_type: "knowledge_graph" }));
    expect(createProject).toHaveBeenCalledWith({ name: "Fineract map", project_type: "knowledge_graph" });
    expect(result).toMatchObject({ ok: true, data: { projectId: "fineract-map", projectType: "knowledge_graph" } });
  });

  it("a knowledge-graph project carries its badge in the menu", async () => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(
      <IdentityProvider me={ME} org={perms("admin")}>
        <ProjectMenu projects={[...PROJECTS, { id: "fineract-map", name: "Fineract map", type: "knowledge_graph" }]}
                     activeId="fineract-map" homeId="boss-dogfood" />
      </IdentityProvider>));
    expect(host.querySelector('[data-testid="project-menu-type"]')!.textContent).toBe("Knowledge graph");
    expect(host.querySelector('[data-testid="project-menu-type-fineract-map"]')!.textContent).toBe("Knowledge graph");
    expect(host.querySelector('[data-testid="project-menu-type-northpeak-bank-a"]')).toBeNull();
  });
});
