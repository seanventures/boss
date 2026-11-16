// @vitest-environment happy-dom
/**
 * Field gates (BOSSAPP-1268). The builder sets who can read or change each field, then
 * opens the app as that person. The open pane shows the server's fields only: a field the
 * open left out is not in the document, and a field marked not writable has no editor.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render, type } from "./_helpers/kit-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DESK = {
  id: "app_personnel",
  project_id: "p1",
  name: "Personnel card",
  created_by: "builder",
  fields: [
    { key: "salary", label: "Salary", position: 0 },
    { key: "title", label: "Title", position: 1 },
    { key: "notes", label: "Notes", position: 2 },
  ],
  grants: [
    { field_key: "salary", subject_kind: "role", subject_id: "payroll", can_read: true, can_change: true },
    { field_key: "title", subject_kind: "user", subject_id: "ada", can_read: true, can_change: true },
    { field_key: "title", subject_kind: "role", subject_id: "payroll", can_read: true, can_change: true },
    { field_key: "notes", subject_kind: "user", subject_id: "ada", can_read: true, can_change: false },
    { field_key: "notes", subject_kind: "role", subject_id: "payroll", can_read: true, can_change: true },
    { field_key: "notes", subject_kind: "user", subject_id: "builder", can_read: true, can_change: true },
  ],
  members: [
    { user_id: "ada", roles: ["employee"] },
    { user_id: "rio", roles: ["payroll"] },
  ],
  values: { salary: "180000", title: "Engineer", notes: "On the rota" },
};

function adaOpen(over: Record<string, unknown> = {}) {
  return {
    app_id: "app_personnel", project_id: "p1", name: "Personnel card", as_user: "ada",
    fields: [
      { key: "title", label: "Title", writable: true },
      { key: "notes", label: "Notes", writable: false },
    ],
    values: { title: "Engineer", notes: "On the rota" },
    ...over,
  };
}

function rioOpen() {
  return {
    app_id: "app_personnel", project_id: "p1", name: "Personnel card", as_user: "rio",
    fields: [
      { key: "salary", label: "Salary", writable: true },
      { key: "title", label: "Title", writable: true },
      { key: "notes", label: "Notes", writable: true },
    ],
    values: { salary: "180000", title: "Engineer", notes: "On the rota" },
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function mount(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal("fetch", fetchMock);
  const { FieldAcl } = await import("../app/builder/field-acl");
  await render(<FieldAcl projectId="p1" actor="builder" />);
}

describe("field gates", () => {
  it("does not render a field the open left out, and does not edit a read-only one", async () => {
    const calls: string[] = [];
    const fetchMock = vi.fn(async (url: string) => {
      calls.push(String(url));
      if (String(url).includes("/open")) return json(adaOpen());
      return json({ apps: [DESK] });
    });
    await mount(fetchMock);
    expect($("[data-testid='gate-desk'] [data-field='salary']")).not.toBeNull();
    await click("[data-testid='open-as-go']");
    const opened = $("[data-testid='opened-app']");
    expect(opened?.getAttribute("data-as-user")).toBe("ada");
    expect(opened?.querySelector("[data-field='salary']")).toBeNull();
    expect(opened?.textContent ?? "").not.toMatch(/180000/);
    expect(opened?.textContent ?? "").not.toMatch(/salary/i);
    const title = opened?.querySelector("input[name='title']") as HTMLInputElement;
    expect(title.value).toBe("Engineer");
    const notes = opened?.querySelector("[data-field='notes']");
    expect(notes?.querySelector("input, textarea")).toBeNull();
    expect(notes?.textContent).toContain("On the rota");
    const openUrl = calls.find((url) => url.includes("/open")) ?? "";
    expect(openUrl).toContain("as_user=ada");
    expect(openUrl).toContain("actor=builder");
    expect(openUrl).not.toMatch(/bypass|all_fields|include_hidden/);
  });

  it("follows the server writable flag, not the desk grant", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (String(url).includes("/open")) {
        return json(adaOpen({
          fields: [
            { key: "title", label: "Title", writable: false },
            { key: "notes", label: "Notes", writable: false },
          ],
        }));
      }
      return json({ apps: [DESK] });
    });
    await mount(fetchMock);
    await click("[data-testid='open-as-go']");
    const opened = $("[data-testid='opened-app']");
    expect(opened?.querySelector("input, textarea")).toBeNull();
    expect(opened?.querySelector("[data-field='title']")?.textContent).toContain("Engineer");
    expect($("[data-testid='save-opened']")).toBeNull();
  });

  it("saves only the fields that person can change", async () => {
    const writes: unknown[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes("/record") && init?.method === "PUT") {
        writes.push(JSON.parse(String(init.body)));
        return json(adaOpen({ values: { title: "Lead", notes: "On the rota" } }));
      }
      if (String(url).includes("/open")) return json(adaOpen());
      return json({ apps: [DESK] });
    });
    await mount(fetchMock);
    await click("[data-testid='open-as-go']");
    await type("[data-testid='opened-app'] input[name='title']", "Lead");
    await click("[data-testid='save-opened']");
    expect(writes).toEqual([{
      project_id: "p1", actor: "builder", as_user: "ada", values: { title: "Lead" },
    }]);
  });

  it("shows a refused write instead of swallowing it", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes("/record")) {
        return json({ error: "not_writable", message: "You can't change that field." }, 403);
      }
      if (String(url).includes("/open")) return json(adaOpen());
      return json({ apps: [DESK] });
    });
    await mount(fetchMock);
    await click("[data-testid='open-as-go']");
    await type("[data-testid='opened-app'] input[name='title']", "Lead");
    await click("[data-testid='save-opened']");
    const alert = $("[data-testid='opened-app'] [role='alert']");
    expect(alert?.textContent).toContain("You can't change that field.");
    expect($("[data-testid='opened-app'] [data-field='notes']")?.textContent).toContain("On the rota");
  });

  it("opening as someone else uses that open, then ada's open drops salary again", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      const asUser = new URL(String(url), "http://local").searchParams.get("as_user");
      if (String(url).includes("/open")) return json(asUser === "rio" ? rioOpen() : adaOpen());
      return json({ apps: [DESK] });
    });
    await mount(fetchMock);
    const select = $("[data-testid='open-as']") as HTMLSelectElement;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
      setter?.call(select, "rio");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await click("[data-testid='open-as-go']");
    expect($("[data-testid='opened-app'] [data-field='salary'] input")).not.toBeNull();
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
      setter?.call(select, "ada");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await click("[data-testid='open-as-go']");
    expect($("[data-testid='opened-app']")?.getAttribute("data-as-user")).toBe("ada");
    expect($("[data-testid='opened-app'] [data-field='salary']")).toBeNull();
  });

  it("saves read without change, and turning read off drops change", async () => {
    const saved: unknown[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).endsWith("/grants") && init?.method === "PUT") {
        saved.push(JSON.parse(String(init.body)));
        return json({ grants: [] });
      }
      return json({ apps: [DESK] });
    });
    await mount(fetchMock);
    expect($("button[aria-label='ada can change Title']")?.getAttribute("aria-pressed")).toBe("true");
    expect($("button[aria-label='ada can read Salary']")?.getAttribute("aria-pressed")).toBe("false");
    await click("button[aria-label='ada can read Salary']");
    expect($("button[aria-label='ada can change Salary']")?.getAttribute("aria-pressed")).toBe("false");
    await click("[data-testid='save-gates']");
    const first = (saved[0] as { grants: Array<Record<string, unknown>> }).grants;
    expect(first.find((g) => g.field_key === "salary" && g.subject_id === "ada")).toEqual({
      field_key: "salary", subject_kind: "user", subject_id: "ada", can_read: true, can_change: false,
    });
    expect(first.find((g) => g.field_key === "salary" && g.subject_id === "payroll")?.can_change).toBe(true);

    await click("button[aria-label='ada can read Title']");
    expect($("button[aria-label='ada can change Title']")?.getAttribute("aria-pressed")).toBe("false");
    await click("[data-testid='save-gates']");
    const second = (saved[1] as { grants: Array<Record<string, unknown>> }).grants;
    expect(second.find((g) => g.field_key === "title" && g.subject_id === "ada")).toBeUndefined();
  });

  it("creates an app from the field labels", async () => {
    const posts: unknown[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        return json({ ...DESK, id: "app_new" });
      }
      return json({ apps: [] });
    });
    await mount(fetchMock);
    await type("input[aria-label='App name']", "Personnel card");
    await type("input[aria-label='Field label']", "Salary");
    await click("[data-testid='create-app']");
    expect(posts).toEqual([{
      project_id: "p1", actor: "builder", name: "Personnel card",
      fields: [{ key: "salary", label: "Salary" }],
    }]);
    expect($("[data-testid='gate-desk'] [data-field='salary']")).not.toBeNull();
  });
});
