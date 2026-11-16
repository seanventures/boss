// @vitest-environment happy-dom
/**
 * The default overview opens on the build bench (BOSSAPP-1273). The three factory
 * routes are mocked: templates, attachments, launch.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { landingArea, summarize } from "../lib/setup-server";
import { board, firstRun } from "./_helpers/setup-fixtures";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { BuildHome } = await import("../app/build-home");

type Call = { url: string; init?: RequestInit };

let root: Root | null = null;
let host: HTMLElement | null = null;

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

function install(routes: {
  templates?: () => Response | Promise<Response>;
  launch?: (init?: RequestInit) => Response | Promise<Response>;
  attachments?: (init?: RequestInit) => Response | Promise<Response>;
} = {}) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = urlOf(input);
    calls.push({ url, init });
    if (url.includes("/api/factory/templates")) return routes.templates ? routes.templates() : Response.json({ templates: [] });
    if (url.includes("/api/factory/launch")) return routes.launch ? routes.launch(init) : Response.json({ project_id: "proj_1", run_id: "run_1" });
    if (url.includes("/api/factory/attachments")) return routes.attachments ? routes.attachments(init) : Response.json({ id: "att_1" });
    return new Response("not found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

async function flush() {
  for (let i = 0; i < 8; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function renderHome() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(<BuildHome />); });
  await flush();
}

async function press(selector: string) {
  const el = document.querySelector(selector);
  if (!el) throw new Error(`missing ${selector}`);
  await act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
  await flush();
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the build bench is the default overview", () => {
  it("leads the overview and does not take a first run", () => {
    const page = codeOnly("app/page.tsx");
    const overview = page.slice(page.indexOf('data-area="overview"'), page.indexOf('data-area="setup"'));
    expect(overview).toContain("<BuildHome");
    expect(overview.indexOf("<BuildHome")).toBeLessThan(overview.indexOf("<CheckinCard"));
    expect(overview.indexOf("<BuildHome")).toBeLessThan(overview.indexOf("<CommandCenter"));
    expect(page.split("<BuildHome").length - 1).toBe(1);
    expect(landingArea(undefined, summarize(firstRun()))).toBe("projects");
    expect(landingArea(undefined, summarize(board()))).toBeNull();
  });

  it("says the bench copy and does not name Gemini", async () => {
    install();
    await renderHome();
    const prompt = document.querySelector<HTMLTextAreaElement>("[data-testid=build-prompt]");
    expect(prompt?.placeholder).toBe("Describe an app and let BOSS do the rest");
    expect(document.querySelector("[data-testid=build-lucky]")?.textContent).toBe("Easy Mode");
    expect(document.body.textContent).toContain("Build with BOSS");
    expect(document.body.textContent).not.toMatch(/gemini/i);
    await press("[data-testid=build-add]");
    const items = [...document.querySelectorAll("[role=menuitem]")].map((el) => el.textContent?.replace(/\s+/g, " ").trim());
    expect(items.some((text) => text?.startsWith("Import from GitHub"))).toBe(true);
    expect(items.some((text) => text?.startsWith("Drive"))).toBe(true);
    expect(items).toContain("Upload files");
    expect(items).toContain("Camera");
    for (const chip of ["Build an Android app", "Maps", "Create a game", "Three.js 3D scenes", "Build an XR app"]) {
      expect(document.body.textContent).toContain(chip);
    }
    expect(document.querySelector("[data-testid=build-mic]")).toBeNull();
    expect(document.querySelector("[data-testid=build-camera-input]")?.getAttribute("capture")).toBe("environment");
  });
});

describe("Easy Mode", () => {
  it("posts the body, including an empty prompt", async () => {
    const calls = install();
    await renderHome();
    await press("[data-testid=build-lucky]");
    const launch = calls.find((call) => call.url.includes("/api/factory/launch"));
    expect(launch?.init?.method).toBe("POST");
    expect(String(launch?.init?.headers && (launch.init.headers as Record<string, string>)["content-type"])).toContain("application/json");
    expect(JSON.parse(String(launch?.init?.body))).toEqual({
      prompt: "",
      mode: "build",
      template_id: null,
      attachment_ids: [],
    });
    expect(document.querySelector("[data-testid=build-project-id]")?.textContent).toBe("proj_1");
    expect(document.querySelector("[data-testid=build-run-id]")?.textContent).toBe("run_1");
    expect(document.querySelector("[data-testid=build-status]")?.textContent).toContain("Build started");
  });

  it("posts the prompt, mode, template, and attachment ids together", async () => {
    const calls = install({
      templates: () => Response.json({
        templates: [{ id: "tpl_maps", title: "City map", category: "Maps", blurb: "A map you can walk.", prompt: "A walking map of the docks" }],
      }),
      attachments: () => Response.json({ id: "att_spec" }),
    });
    await renderHome();
    await press("[data-testid=build-chip]");
    const mode = document.querySelector<HTMLSelectElement>("[data-testid=build-mode]");
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
      set?.call(mode, "prototype");
      mode?.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await press('[data-testid=build-template][data-template-id="tpl_maps"]');
    const input = document.querySelector<HTMLInputElement>("[data-testid=build-upload-input]");
    const file = new File(["# spec"], "brief.md", { type: "text/markdown" });
    await act(async () => {
      const list = {
        0: file,
        length: 1,
        item: (index: number) => (index === 0 ? file : null),
        [Symbol.iterator]: function* () { yield file; },
      } as unknown as FileList;
      Object.defineProperty(input, "files", { configurable: true, value: list });
      input?.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();
    calls.length = 0;
    await press("[data-testid=build-lucky]");
    const launch = calls.find((call) => call.url.includes("/api/factory/launch"));
    expect(JSON.parse(String(launch?.init?.body))).toEqual({
      prompt: "A walking map of the docks",
      mode: "prototype",
      template_id: "tpl_maps",
      attachment_ids: ["att_spec"],
    });
  });

  it("does not show a project or a run when the launch is refused", async () => {
    install({
      launch: () => Response.json(
        { project_id: "proj_secret", run_id: "run_secret", message: "bench refused" },
        { status: 422 },
      ),
    });
    await renderHome();
    await press("[data-testid=build-lucky]");
    expect(document.querySelector("[data-testid=build-launch]")).toBeNull();
    expect(document.body.textContent).not.toContain("proj_secret");
    expect(document.body.textContent).not.toContain("run_secret");
    expect(document.querySelector("[data-testid=build-launch-error]")?.textContent).toBe("bench refused");
    expect(document.querySelector("[data-testid=build-status]")?.textContent).toContain("Not started");
  });
});

describe("chips", () => {
  it("fills the prompt and does not start a build", async () => {
    const calls = install();
    await renderHome();
    const chip = [...document.querySelectorAll("[data-testid=build-chip]")].find((el) => el.textContent === "Create a game");
    await act(async () => { chip?.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    await flush();
    expect(document.querySelector<HTMLTextAreaElement>("[data-testid=build-prompt]")?.value).toBe("Create a game");
    expect(calls.some((call) => call.url.includes("/api/factory/launch"))).toBe(false);
    expect(document.querySelector("[data-testid=build-launch]")).toBeNull();
  });
});

describe("add menu", () => {
  it("records an uploaded file id and treats a spec as a file", async () => {
    const calls = install({ attachments: () => Response.json({ id: "att_spec" }) });
    await renderHome();
    const input = document.querySelector<HTMLInputElement>("[data-testid=build-upload-input]");
    expect(input?.hasAttribute("accept")).toBe(false);
    const file = new File(["spec"], "brief.md", { type: "text/markdown" });
    await act(async () => {
      const list = {
        0: file,
        length: 1,
        item: () => file,
        [Symbol.iterator]: function* () { yield file; },
      } as unknown as FileList;
      Object.defineProperty(input, "files", { configurable: true, value: list });
      input?.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();
    const posted = calls.find((call) => call.url.includes("/api/factory/attachments"));
    expect(posted?.init?.method).toBe("POST");
    expect(posted?.init?.body).toBeInstanceOf(FormData);
    expect((posted?.init?.body as FormData).get("file")).toBe(file);
    const row = document.querySelector("[data-testid=build-attachment]");
    expect(row?.getAttribute("data-attachment-id")).toBe("att_spec");
    expect(row?.textContent).toContain("brief.md");
  });

  it("keeps Drive visible and does not claim a file", async () => {
    const calls = install();
    await renderHome();
    await press("[data-testid=build-add]");
    const drive = document.querySelector("[data-testid=build-add-drive]");
    expect(drive?.textContent).toContain("Drive");
    expect(drive?.textContent).toContain("Drive is not connected");
    await press("[data-testid=build-add-drive]");
    expect(document.querySelector("[data-testid=build-drive-note]")?.textContent).toBe("Drive is not connected");
    expect(document.querySelector("[data-attachment-id]")).toBeNull();
    expect(calls.some((call) => call.url.includes("/api/factory/attachments"))).toBe(false);
    expect(calls.some((call) => call.url.includes("/api/factory/launch"))).toBe(false);
    expect(document.body.textContent).not.toMatch(/imported from drive/i);
  });

  it("opens the existing GitHub import and does not invent an OAuth app", async () => {
    const calls = install();
    await renderHome();
    await press("[data-testid=build-add]");
    await press("[data-testid=build-add-github]");
    expect(new URLSearchParams(window.location.search).get("new_project")).toBe("import");
    expect(document.querySelector("[data-attachment-id]")).toBeNull();
    expect(calls.some((call) => /oauth|github\.com\/login/i.test(call.url))).toBe(false);
  });
});

describe("templates", () => {
  it("selects a card id and can fill the prompt", async () => {
    install({
      templates: () => Response.json({
        templates: [{ id: "tpl_xr", title: "Room scale", category: "XR", blurb: "A room you can walk through.", prompt: "Build an XR room" }],
      }),
    });
    await renderHome();
    const card = document.querySelector("[data-testid=build-template]");
    expect(card?.textContent).toContain("Room scale");
    expect(card?.textContent).toContain("XR");
    expect(card?.textContent).toContain("A room you can walk through.");
    await press('[data-testid=build-template][data-template-id="tpl_xr"]');
    expect(document.querySelector("[data-testid=build-home]")?.getAttribute("data-template-id")).toBe("tpl_xr");
    expect(card?.getAttribute("aria-pressed")).toBe("true");
    expect(document.querySelector<HTMLTextAreaElement>("[data-testid=build-prompt]")?.value).toBe("Build an XR room");
    expect(document.querySelector("[data-testid=build-launch]")).toBeNull();
  });

  it("shows an empty gallery without inventing cards", async () => {
    install({ templates: () => Response.json({ templates: [] }) });
    await renderHome();
    expect(document.querySelector("[data-testid=build-templates-empty]")?.textContent).toContain("No templates yet");
    expect(document.querySelector("[data-testid=build-template]")).toBeNull();
  });

  it("shows a template error and no cards", async () => {
    install({ templates: () => Response.json({ message: "templates offline" }, { status: 503 }) });
    await renderHome();
    expect(document.querySelector("[data-testid=build-templates-error]")?.textContent).toContain("templates offline");
    expect(document.querySelector("[data-testid=build-template]")).toBeNull();
    expect(document.querySelector("[data-testid=build-templates-empty]")).toBeNull();
  });
});
