// @vitest-environment happy-dom
/**
 * Skills & MCP's overlays (tester K3, K11, K23).
 *
 * On a phone the drawer was "a bottom sheet" in CSS only: an ancestor with a backdrop-filter made
 * its `position: fixed` relative to the panel, so it rendered ~2000px down, under every card, and
 * a tapped card or a deep link showed nothing. A floating drawer and every modal are now
 * portalled to <body>. Opening one moves focus into it; Esc closes it and focus goes back.
 */
import React from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, cleanup, click, render, settle } from "./_helpers/kit-render";
import { installKitApi } from "./_helpers/kit-api";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { KitHome } = await import("../app/skills/kit-home");

function phone(on: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: on && query.includes("max-width"), media: query,
    addEventListener: () => {}, removeEventListener: () => {},
  }));
}

async function esc() {
  await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
  await settle();
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

describe("the drawer floats over the page on a phone", () => {
  it("a deep link opens it as a sheet on <body>, not inside the panel", async () => {
    phone(true);
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills&ability=email");
    const host = await render(<KitHome projectId="p1" actor="ceo" />);
    const drawer = $('[data-testid="kit-drawer-email"]')!;
    expect(drawer).not.toBeNull();
    expect(drawer.dataset.floating).toBe("true");
    expect(host.contains(drawer)).toBe(false);
    expect(drawer.closest(".kit-sheet-layer")!.parentElement).toBe(document.body);
  });

  it("on a wide screen it stays a column beside the cards", async () => {
    phone(false);
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills&ability=email");
    const host = await render(<KitHome projectId="p1" actor="ceo" />);
    const drawer = $('[data-testid="kit-drawer-email"]')!;
    expect(host.contains(drawer)).toBe(true);
    expect(drawer.dataset.floating).toBeUndefined();
  });

  it("the stylesheet makes the floating drawer a bottom sheet and Essentials one line", () => {
    const css = readFileSync(resolve(__dirname, "../app/skills/kit.css"), "utf8");
    const phoneRules = css.slice(css.indexOf("@media (max-width: 719px)"));
    expect(phoneRules).toMatch(/\.kit-drawer\[data-floating\][^}]*bottom: 0[^}]*max-height: 85vh/);
    expect(phoneRules).toMatch(/\.kit-ess-head, \.kit-ess-lane \{ display: none; \}/);
    expect(phoneRules).toMatch(/\.kit-ess-compact \{ display: flex; \}/);
  });

  it("Essentials has a one-line form: four dots, the count, and the next step's button", async () => {
    phone(true);
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills");
    await render(<KitHome projectId="p1" actor="ceo" />);
    const compact = $('[data-testid="kit-essentials-compact"]')!;
    expect(compact.querySelectorAll(".kit-ess-dot")).toHaveLength(4);
    expect(compact.textContent).toContain("1/4");
    expect($('[data-testid="kit-ess-compact-action"]')!.textContent).toBe("Connect repo ↗");
  });
});

describe("focus goes into an overlay and comes back", () => {
  it("opening a card focuses the drawer; Esc closes it and focuses the card again", async () => {
    phone(false);
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills");
    await render(<KitHome projectId="p1" actor="ceo" />);
    const card = $('[data-testid="kit-card-web_research"]')!;
    card.focus();
    await click(card);
    const drawer = $('[data-testid="kit-drawer-web_research"]')!;
    expect(drawer.contains(document.activeElement)).toBe(true);
    await esc();
    expect($('[data-testid="kit-drawer-web_research"]')).toBeNull();
    expect(document.activeElement).toBe($('[data-testid="kit-card-web_research"]'));
  });

  it("Test crew closes on Esc", async () => {
    phone(false);
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills");
    await render(<KitHome projectId="p1" actor="ceo" />);
    await click('[data-testid="kit-test-crew"]');
    const modal = $('[data-testid="kit-crew-modal"]')!;
    expect(modal.parentElement).toBe(document.body);
    expect(modal.contains(document.activeElement)).toBe(true);
    await esc();
    expect($('[data-testid="kit-crew-modal"]')).toBeNull();
    expect($$('[role="dialog"]')).toHaveLength(0);
  });
});
