// @vitest-environment happy-dom
/**
 * "CONFIRM IT'S YOU" IS THE TOP LAYER (KG tester round 4, P0). The step-up sheet sat at z-index
 * 200 and the New project sheet at 1300, so the step-up opened blurred BEHIND the sheet that was
 * waiting on it and Create stayed on "Creating…". No project could be created without a fresh
 * second factor.
 *
 * The class, not the instance: every overlay layer comes from ONE z-order scale (the --z-*
 * tokens in app/globals.css), no file picks its own overlay number, and the step-up is the
 * highest layer on that scale — rendered at the end of <body>, outside the shell's isolated
 * stacking context. And when the step-up is cancelled, the button resets and says why.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

const createProject = vi.fn();
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("../lib/client", () => ({
  API_URL: "http://boss-api",
  client: { createProject: (...args: unknown[]) => createProject(...args) },
}));

import { BossApiError } from "@boss/api-client";

import { IdentityProvider } from "../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import { ProjectMenu } from "../app/project-menu";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const WEB = process.cwd();
const strip = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

function files(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) files(p, out);
    else if (/\.(css|tsx?)$/.test(name)) out.push(p);
  }
  return out;
}

const GLOBALS = readFileSync(join(WEB, "app/globals.css"), "utf8");
const SCALE: Record<string, number> = Object.fromEntries(
  [...strip(GLOBALS).matchAll(/(--z-[a-z-]+):\s*(\d+);/g)].map((m) => [m[1], Number(m[2])]));

const SOURCES = [...files(join(WEB, "app")), ...files(join(WEB, "components"))]
  .map((f) => ({ where: relative(WEB, f), src: strip(readFileSync(f, "utf8")) }));
/** A screen's own alias of a scale layer (`.ag-converse { --ag-z: var(--z-over-page); }`). */
const ALIASES: Record<string, number> = Object.fromEntries(SOURCES.flatMap(({ src }) =>
  [...src.matchAll(/(--[a-z][a-z0-9-]*):\s*var\((--z-[a-z-]+)\)/g)].map((m) => [m[1], SCALE[m[2]]] as const)));

/** Every z-index in the console's source, resolved through the scale (calc() offsets included). */
function everyLayer(): { where: string; raw: string; value: number | null }[] {
  const found: { where: string; raw: string; value: number | null }[] = [];
  for (const { where, src } of SOURCES) {
    for (const m of src.matchAll(/(?:z-index|zIndex)\s*:\s*["']?([^;,}"'\n]+)/g)) {
      const raw = m[1].trim();
      const named = (t: string) => SCALE[t] ?? ALIASES[t] ?? null;
      const c = raw.match(/^calc\(var\((--[a-z0-9-]+)\)\s*([+-])\s*(\d+)\)$/);
      const v = raw.match(/^var\((--[a-z0-9-]+)\)$/);
      const base = c ? named(c[1]) : null;
      const value = c ? (base === null ? null : base + (c[2] === "+" ? 1 : -1) * Number(c[3]))
        : v ? named(v[1]) : /^-?\d+$/.test(raw) ? Number(raw) : null;
      found.push({ where, raw, value });
    }
  }
  return found;
}

describe("one z-order scale, and the step-up on top of it", () => {
  it("the scale exists and the step-up is its highest layer", () => {
    expect(SCALE["--z-sheet"]).toBeGreaterThan(0);
    const top = Math.max(...Object.values(SCALE));
    expect(SCALE["--z-step-up"]).toBe(top);
    expect(Object.entries(SCALE).filter(([k, v]) => k !== "--z-step-up" && v === top)).toEqual([]);
  });

  it("no file picks its own overlay number: anything at a page's layer or above is a --z token", () => {
    const oneOff = everyLayer().filter((l) => /^-?\d+$/.test(l.raw) && Number(l.raw) >= 100);
    expect(oneOff.map((l) => `${l.where}: ${l.raw}`)).toEqual([]);
    // A layer that names a token resolves on the scale: directly, through an alias, or a calc().
    const unknown = everyLayer().filter((l) => /var\(/.test(l.raw) && l.value === null);
    expect(unknown.map((l) => `${l.where}: ${l.raw}`)).toEqual([]);
  });

  it("the step-up's layer is above every other overlay layer in the console", () => {
    const step = SCALE["--z-step-up"];
    const layers = everyLayer();
    expect(layers.filter((l) => l.raw === "var(--z-step-up)").map((l) => l.where)).toEqual(["app/identity/identity.css"]);
    const above = layers.filter((l) => l.raw !== "var(--z-step-up)" && (l.value ?? 0) >= step);
    expect(above.map((l) => `${l.where}: ${l.raw}`)).toEqual([]);
    expect(strip(readFileSync(join(WEB, "app/identity/identity.css"), "utf8")))
      .toMatch(/\.idn-sheet-wrap\[data-layer="step-up"\]\s*\{\s*z-index:\s*var\(--z-step-up\)/);
  });
});

const ME = {
  principal_id: "usr_x", kind: "user", display_name: "Ada", email: "a@acme.dev", org: { id: "org_1", name: "Acme" },
  status: "active", access_roles: ["admin"],
  sign_in: { password: true, totp: true, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
  mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1,
  network_bound: false, banners: [], avatar_seed: null,
} as MeResponse;
const PERMS: MePermissionsResponse = { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS.admin],
  step_up_fresh_until: null, catalogue_fingerprint: "x" } as MePermissionsResponse;

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; document.body.innerHTML = ""; });

describe("New project › Create and set up, with a step-up", () => {
  it("opens the step-up above the sheet, and a cancel resets the button and says why", async () => {
    window.history.replaceState(null, "", "/?org=org_1&area=overview");
    createProject.mockReset();
    createProject.mockRejectedValue(new BossApiError("step up", 401, { error: "step_up_required" }));
    host = document.createElement("div");
    host.className = "bx-shell"; // the isolated shell a fixed child can never paint out of
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(
      <IdentityProvider me={ME} org={PERMS}>
        <ProjectMenu projects={[{ id: "boss-dogfood", name: "Default project" }]} activeId="boss-dogfood" homeId="boss-dogfood" />
      </IdentityProvider>));
    await act(async () => host!.querySelector<HTMLButtonElement>('[data-testid="new-project"]')!.click());
    const name = document.querySelector<HTMLInputElement>('[data-testid="new-project-form"] input[name="name"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(name, "kg-tester-r4");
      name.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => document.querySelector<HTMLInputElement>('[data-testid="new-project-type-knowledge_graph"] input')!.click());
    const create = () => document.querySelector<HTMLButtonElement>('[data-testid="new-project-create"]')!;
    await act(async () => { create().click(); await new Promise((r) => setTimeout(r, 0)); });
    for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });

    const sheet = document.querySelector('[data-testid="step-up-sheet"]')!;
    expect(sheet).not.toBeNull();
    expect(create().textContent).toBe("Creating…");
    const layer = sheet.parentElement!;
    expect(layer.getAttribute("data-layer")).toBe("step-up");
    expect(layer.parentElement).toBe(document.body);          // never inside the isolated shell
    expect(layer.closest(".bx-shell")).toBeNull();
    // Above the New project sheet's layer on the one scale.
    expect(GLOBALS).toMatch(/\.bx-np-backdrop \{\s*position: fixed; inset: 0; z-index: var\(--z-sheet\)/);
    expect(SCALE["--z-step-up"]).toBeGreaterThan(SCALE["--z-sheet"]);

    const cancel = [...sheet.querySelectorAll("button")].find((b) => b.textContent === "Cancel")!;
    await act(async () => { cancel.click(); });
    for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(document.querySelector('[data-testid="step-up-sheet"]')).toBeNull();
    expect(create().textContent).toBe("Create and set up");
    expect(create().disabled).toBe(false);
    expect(document.querySelector('[data-testid="new-project-error"]')!.textContent).toMatch(/cancelled, so nothing changed/);
    expect(createProject).toHaveBeenCalledTimes(1);
    expect(document.querySelector<HTMLInputElement>('[data-testid="new-project-form"] input[name="name"]')!.value).toBe("kg-tester-r4");
  });
});
