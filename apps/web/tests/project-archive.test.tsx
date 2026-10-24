// @vitest-environment happy-dom
/**
 * ARCHIVE A PROJECT, in the console (BOSSAPP-768 S5; KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.8, §5.9).
 *
 * The project menu's "Archive project…" opens a styled confirm that NAMES WHAT STOPS for this
 * project (read from GET /v1/projects/{id}/archive), archives with an explicit confirmation, and
 * opens the home project after (an archived project is no longer listed). A refusal is said in the
 * API's words and the dialog stays. "Show archived" lists archived projects with Restore. An open
 * project that is archived says so in the menu and in the header strip, with Restore. Every
 * state is drawn: reading, failed + Retry, refused, working, empty. Archive and Restore mirror
 * project:delete (the owner's); the API decides.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("../lib/client", () => ({ API_URL: "http://boss-api", client: {} }));

import { IdentityProvider } from "../components/identity/identity-provider";
import { ACTION_PERMISSIONS } from "../lib/identity/action-permissions";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import { needsWords } from "../lib/identity/words";
import { ProjectMenu } from "../app/project-menu";
import { ProjectStrip } from "../app/project-strip";
import { archivedSentence } from "../app/project-archive";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ME = {
  principal_id: "usr_x", kind: "user", display_name: "Ada", email: "a@acme.dev", org: { id: "org_1", name: "Acme" },
  status: "active", access_roles: ["owner"],
  sign_in: { password: true, totp: true, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
  mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1,
  network_bound: false, banners: [], avatar_seed: null,
} as MeResponse;

function perms(role: keyof typeof ROLE_PERMISSIONS): MePermissionsResponse {
  return { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]], step_up_fresh_until: null,
    catalogue_fingerprint: "x" };
}

const PROJECTS = [{ id: "boss-dogfood", name: "Default project" }, { id: "northpeak-bank-a", name: "Northpeak Bank A" }];

const PREVIEW = {
  id: "northpeak-bank-a", name: "Northpeak Bank A", archived: false, archived_at: null, archived_by: null,
  counts: { schedules: 2, monitors: 1, repositories: 1, queued_jobs: 0, running_jobs: 0, wakeups: 0 },
  stops: ["New builds, workflow runs, code tasks and schedules are refused.",
          "2 workflow schedules stop firing.", "Monitoring stops for 1 app; no alerts are raised.",
          "Code indexing stops for 1 repository."],
  keeps: ["Everything stays readable: runs, apps, the knowledge graph, decisions and costs.",
          "Nothing is deleted. Restore brings it all back."],
};

type Route = { status?: number; body: unknown };
let routes: Record<string, Route | ((init?: RequestInit) => Route | Promise<Route>)> = {};
let calls: Array<{ url: string; method: string; body: unknown }> = [];
const assign = vi.fn();
const reload = vi.fn();

function respond(r: Route): Response {
  return new Response(JSON.stringify(r.body), { status: r.status ?? 200, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  routes = {};
  calls = [];
  assign.mockReset();
  reload.mockReset();
  window.history.replaceState(null, "", "/?org=org_1&area=overview");
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, assign, reload, search: "?org=org_1&area=overview", pathname: "/", hash: "" },
  });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    const route = routes[`${method} ${url}`];
    if (!route) return respond({ status: 404, body: { detail: `no route ${method} ${url}` } });
    return respond(typeof route === "function" ? await route(init) : route);
  }));
});

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; vi.unstubAllGlobals(); });

async function flush() {
  for (let i = 0; i < 5; i++) await act(async () => { await Promise.resolve(); });
}

async function mount(node: React.ReactNode, role: keyof typeof ROLE_PERMISSIONS = "owner") {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={ME} org={perms(role)}>{node}</IdentityProvider>));
  await flush();
  return host;
}

const menu = (activeId = "northpeak-bank-a", projects = PROJECTS) =>
  <ProjectMenu projects={projects} activeId={activeId} homeId="boss-dogfood" orgId="org_1" />;
const q = (el: ParentNode, id: string) => el.querySelector<HTMLElement>(`[data-testid="${id}"]`);

describe("Archive project… in the project menu", () => {
  it("opens a confirm that names what stops for THIS project, then archives and goes home", async () => {
    routes["GET /v1/projects/northpeak-bank-a/archive"] = { body: PREVIEW };
    routes["POST /v1/projects/northpeak-bank-a/archive"] = {
      body: { ...PREVIEW, archived: true, archived_at: "2026-10-24T08:00:00Z", archived_by: "ceo", changed: true } };
    const el = await mount(menu());
    const button = q(el, "archive-project")!;
    expect(button.textContent).toBe("Archive project…");
    expect(button.getAttribute("data-permission")).toBe("project:delete");
    await act(async () => button.click());
    await flush();

    const dialog = q(document, "archive-dialog")!;
    expect(dialog.getAttribute("role")).toBe("alertdialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.querySelector("h2")!.textContent).toBe("Archive Northpeak Bank A?");
    const stops = [...q(document, "archive-stops")!.querySelectorAll("li")].map((li) => li.textContent);
    expect(stops).toEqual(PREVIEW.stops);
    expect(q(document, "archive-keeps")!.textContent).toContain("Nothing is deleted");
    // Cancel has the focus: a stray Enter never archives.
    expect(document.activeElement).toBe(q(document, "archive-cancel"));

    const note = q(document, "archive-note") as HTMLInputElement;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(note, "case study finished");
      note.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => q(document, "archive-confirm")!.click());
    await flush();
    const posted = calls.find((c) => c.method === "POST")!;
    expect(posted.url).toBe("/v1/projects/northpeak-bank-a/archive");
    expect(posted.body).toEqual({ step_up: "confirmed", note: "case study finished" });
    // Archived projects are not listed, so the console opens the home project.
    expect(assign).toHaveBeenCalledWith(expect.not.stringContaining("project_id="));
  });

  it("a refusal is said in the API's words and the dialog stays open", async () => {
    routes["GET /v1/projects/northpeak-bank-a/archive"] = { body: PREVIEW };
    routes["POST /v1/projects/northpeak-bank-a/archive"] = { status: 409, body: { detail: {
      error: "archive_refused", message: "Northpeak Bank A is the only open project in Acme." } } };
    const el = await mount(menu());
    await act(async () => q(el, "archive-project")!.click());
    await flush();
    await act(async () => q(document, "archive-confirm")!.click());
    await flush();
    expect(q(document, "archive-dialog-refused")!.textContent).toBe("Northpeak Bank A is the only open project in Acme.");
    expect(q(document, "archive-dialog")).not.toBeNull();
    expect(assign).not.toHaveBeenCalled();
  });

  it("shows reading, then a failed read with Retry; Escape and Cancel close it", async () => {
    let answer: Route = { status: 503, body: { detail: "Postgres is not reachable." } };
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => { release = resolve; });
    routes["GET /v1/projects/northpeak-bank-a/archive"] = async () => { await held; return answer; };
    const el = await mount(menu());
    await act(async () => q(el, "archive-project")!.click());
    expect(q(document, "archive-dialog")!.getAttribute("data-state")).toBe("reading");
    expect(q(document, "archive-dialog-reading")!.textContent).toContain("Reading what this project runs");
    release();
    await flush();
    expect(q(document, "archive-dialog-failed")!.textContent).toContain("Postgres is not reachable.");
    expect((q(document, "archive-confirm") as HTMLButtonElement).disabled).toBe(true);
    answer = { body: PREVIEW };
    await act(async () => (q(document, "archive-dialog-failed")!.querySelector("button") as HTMLButtonElement).click());
    await flush();
    expect(q(document, "archive-stops")).not.toBeNull();
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    expect(q(document, "archive-dialog")).toBeNull();
    await act(async () => q(el, "archive-project")!.click());
    await flush();
    await act(async () => q(document, "archive-cancel")!.click());
    expect(q(document, "archive-dialog")).toBeNull();
  });

  it("the home project cannot be archived, and says why", async () => {
    const el = await mount(menu("boss-dogfood"));
    const button = q(el, "archive-project")!;
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(button.getAttribute("title")).toMatch(/home project can't be archived/);
    await act(async () => button.click());
    expect(q(document, "archive-dialog")).toBeNull();
  });

  it("is refused, with the reason, to anyone without project:delete (an Admin too)", async () => {
    const el = await mount(menu(), "admin");
    const button = q(el, "archive-project")!;
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(button.getAttribute("title")).toBe(needsWords("project:delete"));
    await act(async () => button.click());
    expect(q(document, "archive-dialog")).toBeNull();
  });
});

describe("Show archived, with Restore", () => {
  it("lists the org's archived projects, when and by whom, and Restore opens it", async () => {
    routes["GET /v1/projects?include_archived=true"] = { body: { items: [
      { id: "boss-dogfood", name: "Default project", org_id: "org_1", archived_at: null },
      { id: "northpeak-bank-b", name: "Northpeak Bank B", org_id: "org_1", archived_at: "2026-10-03T10:00:00Z", archived_by: "ceo" },
      { id: "elsewhere", name: "Other org", org_id: "org_2", archived_at: "2026-10-03T10:00:00Z", archived_by: "x" },
    ] } };
    routes["POST /v1/projects/northpeak-bank-b/unarchive"] = { body: { id: "northpeak-bank-b", archived: false, changed: true } };
    const el = await mount(menu());
    const toggle = q(el, "show-archived")!;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    await act(async () => toggle.click());
    await flush();
    expect(toggle.textContent).toBe("Hide archived");
    const rows = [...q(el, "archived-list")!.querySelectorAll("li")];
    expect(rows.map((r) => r.getAttribute("data-testid"))).toEqual(["archived-northpeak-bank-b"]);
    expect(rows[0].textContent).toContain("Archived 3 Oct 2026 by ceo");
    await act(async () => q(el, "restore-northpeak-bank-b")!.click());
    await flush();
    expect(calls.find((c) => c.method === "POST")).toMatchObject({
      url: "/v1/projects/northpeak-bank-b/unarchive", body: { step_up: "confirmed" } });
    expect(assign).toHaveBeenCalledWith(expect.stringContaining("project_id=northpeak-bank-b"));
  });

  it("says when there are none, and a failed read says why with Retry", async () => {
    routes["GET /v1/projects?include_archived=true"] = { body: { items: [] } };
    const el = await mount(menu());
    await act(async () => q(el, "show-archived")!.click());
    await flush();
    expect(q(el, "archived-empty")!.textContent).toBe("No archived projects.");
    routes["GET /v1/projects?include_archived=true"] = { status: 500, body: { detail: "boom" } };
    await act(async () => q(el, "show-archived")!.click());
    await act(async () => q(el, "show-archived")!.click());
    await flush();
    expect(q(el, "archived-failed")!.textContent).toContain("boom");
  });

  it("an open project that is archived says so in the menu, with Restore", async () => {
    routes["GET /v1/projects/northpeak-bank-b/archive"] = { body: {
      id: "northpeak-bank-b", name: "Northpeak Bank B", archived: true, archived_at: "2026-10-03T10:00:00Z", archived_by: "ceo" } };
    const el = await mount(menu("northpeak-bank-b"));
    expect(el.querySelector("summary")!.textContent).toBe("Northpeak Bank BArchived");
    expect(q(el, "archived-notice")!.textContent).toContain("takes no new work");
    expect(q(el, "restore-northpeak-bank-b")).not.toBeNull();
    // Nothing to archive: it already is.
    expect(q(el, "archive-project")).toBeNull();
  });
});

describe("the header strip", () => {
  it("carries the read-only notice and Restore while the open project is archived", async () => {
    routes["GET /api/projects-strip?project_id=northpeak-bank-b"] = { body: { projects: [], total: 0 } };
    routes["GET /v1/projects/northpeak-bank-b/archive"] = { body: {
      id: "northpeak-bank-b", name: "Northpeak Bank B", archived: true, archived_at: "2026-10-03T10:00:00Z", archived_by: "ceo" } };
    routes["POST /v1/projects/northpeak-bank-b/unarchive"] = { body: { id: "northpeak-bank-b", archived: false, changed: true } };
    const el = await mount(<ProjectStrip projectId="northpeak-bank-b" />);
    expect(q(el, "archived-notice")!.textContent).toContain("Northpeak Bank B takes no new work");
    await act(async () => q(el, "restore-northpeak-bank-b")!.click());
    await flush();
    expect(reload).toHaveBeenCalled();
  });

  it("shows nothing extra for an open project", async () => {
    routes["GET /api/projects-strip?project_id=northpeak-bank-a"] = { body: { projects: [], total: 0 } };
    routes["GET /v1/projects/northpeak-bank-a/archive"] = { body: PREVIEW };
    const el = await mount(<ProjectStrip projectId="northpeak-bank-a" />);
    expect(q(el, "archived-notice")).toBeNull();
  });
});

describe("the actions", () => {
  it("declare the permissions the API checks", () => {
    expect(ACTION_PERMISSIONS["app/project-archive-actions.ts#archiveProject"]).toBe("project:delete");
    expect(ACTION_PERMISSIONS["app/project-archive-actions.ts#restoreProject"]).toBe("project:delete");
    const matrix = readFileSync(join(process.cwd(), "..", "..", "tests/identity/matrix.yaml"), "utf8");
    expect(matrix).toContain('"POST /v1/projects/{project_id}/archive": ["permit project:delete @ org", "O"]');
    expect(matrix).toContain('"POST /v1/projects/{project_id}/unarchive": ["permit project:delete @ org", "O"]');
  });

  it("say a date a person reads", () => {
    expect(archivedSentence("2026-10-03T10:00:00Z", "ceo")).toBe("Archived 3 Oct 2026 by ceo");
    expect(archivedSentence(null)).toBe("Archived");
  });
});
