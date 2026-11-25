// @vitest-environment happy-dom
/**
 * The default overview opens on the build bench (BOSSAPP-1273). The three factory
 * routes are mocked: templates, attachments, launch.
 */
import { readFileSync } from "node:fs";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { landingArea, summarize } from "../lib/setup-server";
import { board, firstRun } from "./_helpers/setup-fixtures";
import { codeOnly } from "./_helpers/source";
import { declarations } from "./_helpers/css-declarations";

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
    for (const chip of ["Build a web app", "Maps", "Create a game", "Three.js 3D scenes", "Build an XR app"]) {
      expect(document.body.textContent).toContain(chip);
    }
    // BOSS has no Android SDK, emulator or Xcode: the bench never offers a phone app.
    expect(document.body.textContent).not.toMatch(/android|\biOS\b/i);
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

describe("every control carries an icon beside its words", () => {
  const decorative = (svg: Element | null | undefined) => {
    expect(svg?.tagName.toLowerCase()).toBe("svg");
    expect(svg?.getAttribute("aria-hidden")).toBe("true");
  };

  it("Add, Mode, Easy Mode and the menu each show an icon and keep their words", async () => {
    install();
    await renderHome();
    const add = document.querySelector("[data-testid=build-add]");
    decorative(add?.querySelector("[data-testid=build-add-icon]"));
    expect(add?.textContent).toBe("Add");
    const lucky = document.querySelector("[data-testid=build-lucky]");
    decorative(lucky?.querySelector("[data-testid=build-lucky-icon]"));
    expect(lucky?.textContent).toBe("Easy Mode");
    const mode = document.querySelector<HTMLSelectElement>("[data-testid=build-mode]");
    expect(mode?.getAttribute("aria-label")).toBe("Mode");
    decorative(document.querySelector("[data-testid=build-mode-icon]"));
    // The mode's icon follows the chosen mode.
    const before = document.querySelector("[data-testid=build-mode-icon]")?.innerHTML;
    await act(async () => {
      mode!.value = "prototype";
      mode!.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();
    expect(document.querySelector(".bh-mode")?.getAttribute("data-mode")).toBe("prototype");
    expect(document.querySelector("[data-testid=build-mode-icon]")?.innerHTML).not.toBe(before);
    await press("[data-testid=build-add]");
    for (const item of document.querySelectorAll("[role=menuitem]")) decorative(item.querySelector("svg"));
  });

  it("each chip carries its gallery category's icon and tint, and keeps its words", async () => {
    install();
    await renderHome();
    const chips = [...document.querySelectorAll("[data-testid=build-chip]")];
    expect(chips.map((chip) => chip.getAttribute("data-kind"))).toEqual(["web", "maps", "game", "3d", "xr"]);
    for (const chip of chips) decorative(chip.querySelector("svg"));
    expect(chips.map((chip) => chip.textContent)).toEqual(["Build a web app", "Maps", "Create a game", "Three.js 3D scenes", "Build an XR app"]);
  });

  it("the mic shows a mic, then a stop while it records, and keeps its label", async () => {
    class FakeRec {
      lang = ""; interimResults = false;
      onresult: unknown = null; onerror: unknown = null; onend: (() => void) | null = null;
      start() {}
      stop() { this.onend?.(); }
    }
    vi.stubGlobal("webkitSpeechRecognition", FakeRec);
    install();
    await renderHome();
    const mic = () => document.querySelector("[data-testid=build-mic]");
    expect(mic()?.getAttribute("aria-label")).toBe("Dictate");
    decorative(mic()?.querySelector("[data-testid=build-mic-icon]"));
    const idle = mic()?.querySelector("svg")?.innerHTML;
    expect(mic()?.textContent).toBe("Mic");
    await press("[data-testid=build-mic]");
    expect(mic()?.getAttribute("aria-pressed")).toBe("true");
    expect(mic()?.textContent).toBe("Stop");
    expect(mic()?.querySelector("svg")?.innerHTML).not.toBe(idle);
    await press("[data-testid=build-mic]");
    expect(mic()?.getAttribute("aria-pressed")).toBe("false");
  });

  it("every animation, transition and hover lift has its own exact rule under prefers-reduced-motion", () => {
    const css = readFileSync("app/build-home.css", "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const at = css.indexOf("@media (prefers-reduced-motion: reduce)");
    expect(at).toBeGreaterThan(0);
    const open = css.indexOf("{", at);
    let depth = 0;
    let close = open;
    for (; close < css.length; close += 1) {
      if (css[close] === "{") depth += 1;
      if (css[close] === "}" && --depth === 0) break;
    }
    const inside = declarations(css.slice(open + 1, close));
    // The composer's rules; the template gallery below (.bh-card, .bh-more) is out of scope here.
    const outside = declarations(css.slice(0, at) + css.slice(close + 1))
      .filter((d) => !d.selector.startsWith("@") && !/^(from|to|\d)/.test(d.selector))
      .filter((d) => !/^\.bh-(card|more)/.test(d.selector));
    const reduced = (selector: string, prop: string) =>
      inside.find((d) => d.prop === prop && d.selector.split(",").map((x) => x.trim()).includes(selector))?.value;
    const each = (prop: string, keep: (d: { selector: string; value: string }) => boolean) =>
      outside.filter((d) => d.prop === prop && keep(d)).flatMap((d) => d.selector.split(",").map((x) => x.trim()));

    const animated = each("animation", () => true);
    expect(animated.sort()).toEqual([
      ".bh-lucky[aria-busy=\"true\"] .bh-lucky-icon",
      ".bh-menu",
      ".bh-mic[aria-pressed=\"true\"]",
      ".bh-status[data-state=\"starting\"] .bh-dot::after",
    ]);
    for (const sel of animated) expect(reduced(sel, "animation"), sel).toBe("none");
    for (const sel of each("transition", (d) => !d.selector.includes(":hover"))) {
      expect(reduced(sel, "transition"), sel).toBe("none");
    }
    const lifts = each("transform", (d) => /:hover|:active/.test(d.selector));
    expect(lifts.length).toBeGreaterThan(5);
    for (const sel of lifts) {
      // The sheen's hover rule goes back to its resting place; every other lift is removed.
      const rest = sel.endsWith("::after") ? "translateX(-120%)" : "none";
      expect(reduced(sel, "transform"), sel).toBe(rest);
    }
  });
});

describe("Easy Mode stays readable (WCAG AA 4.5:1 for its bold --fs-ui label)", () => {
  type RGB = [number, number, number];
  const root = new Map<string, string>();
  for (const file of ["app/globals.css", "app/design-system.css"]) {
    const src = readFileSync(file, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    for (const block of src.matchAll(/(?<=^|\})\s*:root\s*\{([^}]*)\}/g)) {
      for (const m of block[1]!.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/gi)) root.set(m[1]!.toLowerCase(), m[2]!.trim());
    }
  }
  const hex = (value: string): RGB => {
    const ref = value.match(/^var\((--[a-z0-9-]+)\)$/);
    if (ref) { const next = root.get(ref[1]!); if (!next) throw new Error(`no :root token ${ref[1]}`); return hex(next); }
    const m = value.match(/^#([0-9a-f]{6})$/i);
    if (!m) throw new Error(`not a colour: ${value}`);
    return [0, 2, 4].map((i) => parseInt(m[1]!.slice(i, i + 2), 16)) as RGB;
  };
  const mix = (a: RGB, b: RGB, p: number): RGB => a.map((v, i) => v * p + b[i]! * (1 - p)) as RGB;
  const colour = (value: string): RGB => {
    const m = value.match(/^color-mix\(in srgb, (var\(--[a-z0-9-]+\)) (\d+)%, (var\(--[a-z0-9-]+\))\)$/);
    return m ? mix(hex(m[1]!), hex(m[3]!), Number(m[2]) / 100) : hex(value);
  };
  const lum = (c: RGB) => {
    const [r, g, b] = c.map((v) => { const x = v / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
  };
  const ratio = (a: RGB, b: RGB) => (Math.max(lum(a), lum(b)) + 0.05) / (Math.min(lum(a), lum(b)) + 0.05);
  const css = declarations(readFileSync("app/build-home.css", "utf8"));
  const decl = (selector: string, prop: string) => css.find((d) => d.selector === selector && d.prop === prop)?.value ?? "";
  const stops = (gradient: string) => {
    const m = gradient.match(/^linear-gradient\(120deg, (.+)\)$/);
    expect(m, gradient).not.toBeNull();
    // Split at the commas between stops, not the ones inside color-mix(...).
    const parts: string[] = [];
    let depth = 0;
    let from = 0;
    const body = m![1]!;
    for (let i = 0; i < body.length; i += 1) {
      if (body[i] === "(") depth += 1;
      else if (body[i] === ")") depth -= 1;
      else if (body[i] === "," && depth === 0) { parts.push(body.slice(from, i)); from = i + 1; }
    }
    parts.push(body.slice(from));
    return parts.map((stop) => colour(stop.trim().replace(/\s+\d+%$/, "")));
  };

  it("pins the resting and hover gradients, with no brightness filter", () => {
    expect(decl(".bh-lucky", "background")).toBe(
      "linear-gradient(120deg, var(--accent) 0%, color-mix(in srgb, var(--accent) 70%, var(--boss-c-1b3a6b)) 100%)");
    expect(decl(".bh-lucky:hover:not(:disabled)", "background")).toBe(
      "linear-gradient(120deg, color-mix(in srgb, var(--accent) 80%, var(--boss-c-1b3a6b)) 0%, color-mix(in srgb, var(--accent) 50%, var(--boss-c-1b3a6b)) 100%)");
    expect(css.filter((d) => d.selector.startsWith(".bh-lucky") && d.prop === "filter")).toEqual([]);
    expect(decl(".bh-lucky", "color")).toBe("var(--accent-ink)");
  });

  it("every stop, at rest, on hover and under the sheen, is at least 4.5:1 against the label", () => {
    const ink = hex("var(--accent-ink)");
    const rest = stops(decl(".bh-lucky", "background"));
    const hover = stops(decl(".bh-lucky:hover:not(:disabled)", "background"));
    for (const stop of [...rest, ...hover]) expect(ratio(ink, stop)).toBeGreaterThanOrEqual(4.5);
    // The sheen sits under the label at a set strength of the label colour; at its peak over the
    // lightest hover stop the label still clears 4.5:1.
    expect(decl(".bh-lucky::after", "z-index")).toBe("-1");
    expect(decl(".bh-lucky", "isolation")).toBe("isolate");
    const sheen = decl(".bh-lucky::after", "background").match(/color-mix\(in srgb, var\(--accent-ink\) (\d+)%, transparent\)/);
    expect(sheen).not.toBeNull();
    const alpha = Number(sheen![1]) / 100;
    for (const stop of hover) expect(ratio(ink, mix(ink, stop, alpha))).toBeGreaterThanOrEqual(4.5);
  });

  it("a pulse means work is moving: only a starting build breathes", () => {
    const pulsing = css.filter((d) => d.prop === "animation" && d.value.startsWith("bh-breathe")).map((d) => d.selector);
    expect(pulsing).toEqual([".bh-status[data-state=\"starting\"] .bh-dot::after"]);
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

describe("a template this install cannot build", () => {
  it("is shown, marked with what it needs and the command, and cannot be picked", async () => {
    const calls = install({
      templates: () => Response.json({
        templates: [
          { id: "tile-swap", title: "Tile swap", category: "game", blurb: "Match three.", prompt: "Build Tile Swap.", available: true, needs: null },
          {
            id: "fixture-clock", title: "Fixture clock", category: "internal", blurb: "A frozen clock.", prompt: "Build a Go API.",
            available: false,
            needs: { toolchain: "go", label: "Needs Go", enable: "boss toolchains enable go", message: "Fixture clock is built in Go, and Go is not turned on for this BOSS. Turn it on with `boss toolchains enable go` on the BOSS host, then pick it again." },
          },
        ],
      }),
    });
    await renderHome();
    const go = document.querySelector<HTMLButtonElement>("[data-template-id=fixture-clock]");
    const ok = document.querySelector<HTMLButtonElement>("[data-template-id=tile-swap]");
    // Reachable by keyboard (no native disabled), announced as unavailable, with the reason on screen.
    expect(go?.disabled).toBe(false);
    expect(go?.getAttribute("aria-disabled")).toBe("true");
    expect(go?.hasAttribute("aria-pressed")).toBe(false);
    expect(go?.getAttribute("data-unavailable")).toBe("true");
    // The reason is said once: on screen and through aria-describedby, not again in a tooltip.
    expect(go?.hasAttribute("title")).toBe(false);
    const why = go?.querySelector("[data-testid=build-template-needs]");
    expect(why?.id).toBeTruthy();
    expect(go?.getAttribute("aria-describedby")).toBe(why?.id);
    expect(document.getElementById(go!.getAttribute("aria-describedby")!)).toBe(why);
    expect(why?.textContent).toContain("Needs Go");
    expect(why?.textContent).toContain("Go is not turned on for this BOSS");
    expect(why?.querySelector("code")?.textContent).toBe("boss toolchains enable go");
    expect(ok?.disabled).toBe(false);
    expect(ok?.hasAttribute("aria-disabled")).toBe(false);
    expect(ok?.hasAttribute("aria-describedby")).toBe(false);
    expect(ok?.querySelector("[data-testid=build-template-needs]")).toBeNull();
    // Still drawn, so the gallery does not show a blank hole.
    expect(go?.querySelector(".bh-card-art svg[aria-hidden=true]")).not.toBeNull();

    go?.focus();
    expect(document.activeElement).toBe(go);
    await press("[data-template-id=fixture-clock]");
    expect(document.querySelector("[data-testid=build-home]")?.getAttribute("data-template-id")).toBe("");
    expect(document.querySelector<HTMLTextAreaElement>("[data-testid=build-prompt]")?.value).toBe("");
    await press("[data-template-id=tile-swap]");
    expect(document.querySelector("[data-testid=build-home]")?.getAttribute("data-template-id")).toBe("tile-swap");
    expect(calls.some((call) => call.url.includes("/api/factory/launch"))).toBe(false);
  });

  it("says the launch refusal in words, not the code", async () => {
    install({
      launch: () => Response.json({ error: "toolchain_not_enabled" }, { status: 409 }),
    });
    await renderHome();
    await press("[data-testid=build-lucky]");
    expect(document.body.textContent).toContain("Not started: this template needs a toolchain this BOSS has not turned on.");
    expect(document.body.textContent).not.toContain("toolchain_not_enabled");
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

describe("the gallery folds to two rows", () => {
  const many = (n: number) => Array.from({ length: n }, (_, i) => ({
    id: i === 0 ? "tile-swap" : `tpl_${i}`, title: `Template ${i}`, category: i % 2 ? "web" : "brand-new", blurb: "A thing.", prompt: `Build ${i}`,
  }));

  it("shows two rows' worth, then Show all expands and Show fewer folds", async () => {
    install({ templates: () => Response.json({ templates: many(21) }) });
    await renderHome();
    const list = document.querySelector("[data-testid=build-template-list]")!;
    const cols = Number(list.getAttribute("data-columns"));
    expect(cols).toBeGreaterThan(0);
    expect(document.querySelectorAll("[data-testid=build-template]").length).toBe(cols * 2);
    const more = document.querySelector<HTMLButtonElement>("[data-testid=build-templates-more]")!;
    expect(more.textContent).toBe("Show all 21 templates");
    expect(more.getAttribute("aria-expanded")).toBe("false");
    expect(more.getAttribute("aria-controls")).toBe(list.id);

    await press("[data-testid=build-templates-more]");
    expect(document.querySelectorAll("[data-testid=build-template]").length).toBe(21);
    expect(more.textContent).toBe("Show fewer");
    expect(more.getAttribute("aria-expanded")).toBe("true");

    await press("[data-testid=build-templates-more]");
    expect(document.querySelectorAll("[data-testid=build-template]").length).toBe(cols * 2);
  });

  it("never folds the selected card away", async () => {
    install({ templates: () => Response.json({ templates: many(21) }) });
    await renderHome();
    const cols = Number(document.querySelector("[data-testid=build-template-list]")!.getAttribute("data-columns"));
    await press("[data-testid=build-templates-more]");
    await press('[data-testid=build-template][data-template-id="tpl_12"]');
    await press("[data-testid=build-templates-more]");
    const shown = [...document.querySelectorAll<HTMLElement>("[data-testid=build-template]")];
    expect(shown.length).toBe(cols * 2);
    const picked = document.querySelector('[data-testid=build-template][data-template-id="tpl_12"]');
    expect(picked?.getAttribute("aria-pressed")).toBe("true");
    expect(shown[shown.length - 1]).toBe(picked);
    expect(document.querySelector("[data-testid=build-home]")?.getAttribute("data-template-id")).toBe("tpl_12");
  });

  it("counts the grid's real columns", async () => {
    const real = window.getComputedStyle.bind(window);
    vi.spyOn(window, "getComputedStyle").mockImplementation((el: Element, pseudo?: string | null) => {
      const style = real(el, pseudo);
      if ((el as HTMLElement).dataset?.testid !== "build-template-list") return style;
      return new Proxy(style, { get: (t, k) => (k === "gridTemplateColumns" ? "240px 240px 240px 240px" : Reflect.get(t, k)) });
    });
    install({ templates: () => Response.json({ templates: many(21) }) });
    await renderHome();
    expect(document.querySelector("[data-testid=build-template-list]")?.getAttribute("data-columns")).toBe("4");
    expect(document.querySelectorAll("[data-testid=build-template]").length).toBe(8);
  });

  it("has no Show all when everything fits in two rows", async () => {
    install({ templates: () => Response.json({ templates: many(4) }) });
    await renderHome();
    expect(document.querySelectorAll("[data-testid=build-template]").length).toBe(4);
    expect(document.querySelector("[data-testid=build-templates-more]")).toBeNull();
  });

  it("every card opens with a decorative drawing: its own, its category's, or the generic one", async () => {
    install({ templates: () => Response.json({ templates: many(3) }) });
    await renderHome();
    const kinds = [...document.querySelectorAll("[data-testid=build-template] .bh-card-art svg")].map((svg) => {
      expect(svg.getAttribute("aria-hidden")).toBe("true");
      return svg.getAttribute("data-preview");
    });
    expect(kinds).toEqual(["template", "category", "generic"]);
  });
});

