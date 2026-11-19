// @vitest-environment happy-dom
/**
 * recipe-box, 2026-11-19 ("did we remove the Preview feature?"): every preview was stopped for
 * idle and the project page had no way to the app. The Overview's Your app card (and the small
 * Open app on the projects list and Command Center) says which version it shows and opens it;
 * with no preview running it starts one through the one write path (startPreviewAction), says
 * "Starting… about a minute" and opens it. An asleep preview is never shown as gone.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseYourApp, opensDirectly, appInitials } from "../lib/your-app";
import { $, click, mount, unmount } from "./_helpers/live-render";

const forms: FormData[] = [];
const reply: { value: Record<string, unknown> } = { value: {} };
vi.mock("../app/actions", async (original) => ({
  ...(await original<typeof import("../app/actions")>()),
  startPreviewAction: async (_prev: unknown, form: FormData) => { forms.push(form); return reply.value; },
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { YourAppCard, OpenAppChip } = await import("../app/projects/your-app");

afterEach(() => { unmount(); vi.unstubAllGlobals(); vi.restoreAllMocks(); forms.length = 0; reply.value = {}; });

const RAW = {
  run_id: "crun_1", name: "Recipe box", title: "Recipe box", version: "main", version_label: "Main",
  version_words: "Main, as merged in change #2", pr_number: 2, pr_url: "https://github.com/a/b/pull/2",
  preview_id: "prev_old", state: "asleep", words: "Asleep · wakes when you open it",
  href: "/v1/previews/prev_old/web/", error: null, live_url: null,
};

describe("Your app", () => {
  it("parses the engine's card and only a running or starting preview opens directly", () => {
    const app = parseYourApp(RAW)!;
    expect(app.state).toBe("asleep");
    expect(opensDirectly(app)).toBe(false);
    expect(opensDirectly({ ...app, state: "running" })).toBe(true);
    expect(opensDirectly({ ...app, state: "running", href: "" })).toBe(false);
    expect(parseYourApp({})).toBeNull();
    expect(appInitials("Recipe box")).toBe("RB");
  });

  it("an asleep app shows its version and wakes through the one write path on Open app", async () => {
    const opened: string[] = [];
    vi.spyOn(window, "open").mockImplementation((u) => { opened.push(String(u)); return null; });
    reply.value = { url: "/v1/previews/prev_new/web/", id: "prev_new", starting: true };
    const started = vi.fn();
    await mount(<YourAppCard app={parseYourApp(RAW)!} projectId="p1" actor="ceo" onStarted={started} />);
    expect($('[data-testid="px-ov-app-name"]')?.textContent).toBe("Recipe box");
    expect($('[data-testid="px-ov-app-version"]')?.textContent).toMatch(/Shows Main · as merged in change #2/);
    expect($('[data-testid="px-ov-app-line"]')?.textContent).toMatch(/wakes when you open it/);
    expect($('[data-testid="px-ov-app"]')?.textContent).not.toMatch(/gone|not running/i);
    await click($('[data-testid="px-ov-app-open"]'));
    expect(forms).toHaveLength(1);
    expect(forms[0].get("run_id")).toBe("crun_1");
    expect(forms[0].get("actor")).toBe("ceo");
    expect(started).toHaveBeenCalled();
    expect(opened).toEqual(["/v1/previews/prev_new/web/"]);
    expect($('[data-testid="px-ov-app-line"]')?.textContent).toMatch(/Starting… about a minute/);
    expect($('[data-testid="px-ov-app-link"]')?.getAttribute("href")).toBe("/v1/previews/prev_new/web/");
  });

  it("a running app opens at once, with no start; a deployed one keeps Try it live", async () => {
    const opened: string[] = [];
    vi.spyOn(window, "open").mockImplementation((u) => { opened.push(String(u)); return null; });
    await mount(<YourAppCard app={parseYourApp({ ...RAW, state: "running", live_url: "https://staging.example.com" })!} projectId="p1" />);
    await click($('[data-testid="px-ov-app-open"]'));
    expect(forms).toHaveLength(0);
    expect(opened).toEqual(["/v1/previews/prev_old/web/"]);
    expect($('[data-testid="px-ov-app-live"]')?.getAttribute("href")).toBe("https://staging.example.com");
  });

  it("a refused start says so on the card and opens nothing", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    reply.value = { error: "Needs your consent" };
    await mount(<YourAppCard app={parseYourApp({ ...RAW, state: "none", href: null, preview_id: null })!} projectId="p1" />);
    await click($('[data-testid="px-ov-app-open"]'));
    expect($('[data-testid="px-ov-app-error"]')?.textContent).toMatch(/Couldn't start it: Needs your consent/);
    expect(open).not.toHaveBeenCalled();
  });

  it("the small Open app on a list card starts it the same way", async () => {
    vi.spyOn(window, "open").mockImplementation(() => null);
    reply.value = { url: "/v1/previews/prev_new/web/", id: "prev_new", starting: true };
    await mount(<OpenAppChip app={parseYourApp(RAW)!} projectId="p1" testid="chip" />);
    expect($('[data-testid="chip"]')?.textContent).toMatch(/Open app/);
    await click($('[data-testid="chip"] button'));
    expect(forms).toHaveLength(1);
    expect($('[data-testid="chip"]')?.textContent).toMatch(/Starting…/);
  });
});
