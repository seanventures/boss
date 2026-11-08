/**
 * LOCAL-FIRST IN THE CONSOLE (BOSSAPP-916/917/921): the pure rules the two doors, the Code box,
 * Publish to GitHub and Try a sample decide by.
 */
import { describe, expect, it, vi } from "vitest";

import { bringChoices, defaultWhere, doorOf, repoCheckFix, repoSlug, type RepoCheck } from "../lib/add-project";
import { publishSteps } from "../lib/local-first";
import { createFresh, stepFromParam } from "../lib/start-project";

const check = (over: Partial<RepoCheck>): RepoCheck =>
  ({ ok: false, full_name: "acme/shop", reason: "", words: "", exists: false, owner_type: null, ...over });

describe("the two doors", () => {
  it("opens the door the link names, the doors otherwise; words handed over are a new app", () => {
    expect(doorOf("idea")).toBe("new");
    expect(doorOf("repo")).toBe("existing");
    expect(doorOf("folder")).toBe("existing");
    expect(doorOf("import")).toBe("existing");
    expect(doorOf("sample")).toBe("choose");
    expect(doorOf("choose")).toBe("choose");
    expect(doorOf("choose", true)).toBe("new");
    expect(stepFromParam("new")).toBe("idea");
    expect(stepFromParam("existing")).toBe("import");
    expect(stepFromParam("1")).toBe("choose");
  });
});

describe("where a new app's code lives", () => {
  it("names the repo from the project's name", () => {
    expect(repoSlug("Expense Tracker: receipts")).toBe("expense-tracker");
    expect(repoSlug("Café  Orders!!")).toBe("cafe-orders");
    expect(repoSlug("—")).toBe("app");
    expect(repoSlug("x".repeat(80)).length).toBe(60);
  });

  it("defaults to this machine without GitHub or when BOSS cannot make the repo, else a new GitHub repo", () => {
    expect(defaultWhere(false, null)).toBe("local");
    expect(defaultWhere(true, null)).toBe("github_new");
    expect(defaultWhere(true, check({ ok: true }))).toBe("github_new");
    expect(defaultWhere(true, check({ reason: "personal_account" }))).toBe("local");
  });

  it("gives each GitHub refusal its one fix", () => {
    expect(repoCheckFix(check({ ok: true }))).toBeNull();
    expect(repoCheckFix(check({ reason: "no_github_credential" }))).toMatchObject({ label: "Repo and models", external: false });
    expect(repoCheckFix(check({ reason: "no_admin_permission" }))!.href).toContain("focus=repository");
    const personal = repoCheckFix(check({ reason: "personal_account", full_name: "me/shop" }))!;
    expect(personal.external).toBe(true);
    expect(personal.href).toBe("https://github.com/new?owner=me&name=shop&visibility=private");
    expect(repoCheckFix(check({ reason: "exists" }))).toBeNull();
    expect(repoCheckFix({ ok: false, full_name: "acme/x", error: "github_not_connected" })!.label).toBe("Repo and models");
  });
});

describe("how a picked folder comes in", () => {
  it("offers Keep using GitHub only with a remote, read-in-place only on a server, and picks for the person", () => {
    expect(bringChoices("local", null, false)).toEqual({ choices: ["copy"], pick: "copy" });
    expect(bringChoices("local", "acme/notes", true)).toEqual({ choices: ["github", "copy"], pick: "github" });
    expect(bringChoices("local", "acme/notes", false)).toEqual({ choices: ["github", "copy"], pick: "copy" });
    expect(bringChoices("host", null, false)).toEqual({ choices: ["copy", "in_place"], pick: "copy" });
    expect(bringChoices("host", "acme/ledger", true)).toEqual({ choices: ["github", "copy", "in_place"], pick: "github" });
  });
});

describe("Publish to GitHub, as a diagram", () => {
  it("runs, finishes, skips what does not apply, and stops at the step that refused", () => {
    expect(publishSteps({ create: true })).toEqual({ repository: "todo", push: "todo", pull_requests: "todo", switch: "todo" });
    expect(publishSteps({ create: true, busy: true })).toMatchObject({ repository: "doing" });
    expect(publishSteps({ create: false, busy: true })).toMatchObject({ repository: "skipped", push: "doing" });
    expect(publishSteps({ create: true, busy: true, openChanges: 0 })).toMatchObject({ pull_requests: "skipped" });
    const done = publishSteps({ create: true, result: { project_id: "p", status: "published", full_name: "acme/x", pull_requests: { 1: "u" },
      steps: [{ key: "repository", ok: true, words: "" }], words: "" } });
    expect(done).toEqual({ repository: "done", push: "done", pull_requests: "done", switch: "done" });
    const none = publishSteps({ create: true, result: { project_id: "p", status: "published", full_name: "acme/x", pull_requests: {},
      steps: [{ key: "repository", ok: true, words: "" }, { key: "push", ok: true, words: "" }, { key: "switch", ok: true, words: "" }], words: "" } });
    expect(none.pull_requests).toBe("skipped");
    expect(publishSteps({ create: true, refusal: { error: "target_not_empty", message: "", step: "push" } }))
      .toEqual({ repository: "done", push: "failed", pull_requests: "todo", switch: "todo" });
    expect(publishSteps({ create: true, refusal: { error: "personal_account", message: "", step: "repository" } }).repository).toBe("failed");
    expect(publishSteps({ create: false, refusal: { error: "github_not_connected", message: "", step: "switch" } }))
      .toEqual({ repository: "skipped", push: "done", pull_requests: "done", switch: "failed" });
  });
});

describe("Try a sample, again and again", () => {
  it("takes the next free name with no cap (Hello World 21 after twenty)", async () => {
    const taken = new Set(["hello-world", ...Array.from({ length: 19 }, (_, i) => `hello-world-${i + 2}`)]);
    const create = vi.fn(async (name: string) => ({ id: name.toLowerCase().replace(/\s+/g, "-"), name }) as never);
    const made = await createFresh("Hello World", {}, create, async () => taken);
    expect(create).toHaveBeenCalledWith("Hello World 21", {});
    expect(made).toMatchObject({ name: "Hello World 21" });
  });

  it("moves on when a name was taken meanwhile (a race), and says so if every try loses", async () => {
    let n = 0;
    const create = vi.fn(async (name: string) => {
      if (n++ < 2) throw Object.assign(new Error("taken"), { exists: true });
      return { id: "x", name } as never;
    });
    await createFresh("Hello World", {}, create, async () => new Set());
    expect(create.mock.calls.map((c) => c[0])).toEqual(["Hello World", "Hello World 2", "Hello World 3"]);
    const never = vi.fn(async () => { throw Object.assign(new Error("taken"), { exists: true }); });
    await expect(createFresh("Hello World", {}, never as never, async () => new Set())).rejects.toThrow(/taken/);
  });
});
