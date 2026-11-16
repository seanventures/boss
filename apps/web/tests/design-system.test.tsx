// @vitest-environment happy-dom
/**
 * Workspace enamel (BOSSAPP-1254): save a name, colors, type and spacing.
 * A new app in the workspace reads that spec. A failed save stays on screen.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DesignSystemScreen } from "../app/design-system/screen";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

afterEach(() => { unmount(); vi.restoreAllMocks(); });

const SAVED = {
  id: "wds_harbor",
  name: "Harbor enamel",
  colors: { ground: "#123044", metal: "#C9D0D4", signal: "#E3A008", ink: "#1C1915" },
  type: { display: "Barlow Condensed", body: "Public Sans", utility: "IBM Plex Mono" },
  spacing: { tight: 4, stack: 12, band: 28 },
};

function typeInto(selector: string, value: string) {
  const el = $(selector) as HTMLInputElement;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function serve(saved: Record<string, unknown> | null, failSave = false) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method || "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ url: String(url), method, body });
    if (method === "GET") {
      if (!saved) return new Response(JSON.stringify({ error: "not_found", message: "No design system for that workspace." }), { status: 404 });
      return new Response(JSON.stringify(saved), { status: 200 });
    }
    if (method === "PUT") {
      if (failSave) {
        return new Response(JSON.stringify({ error: "invalid_color", message: "signal needs a #RRGGBB color." }), { status: 422 });
      }
      saved = { id: "wds_harbor", ...body };
      return new Response(JSON.stringify(saved), { status: 200 });
    }
    return new Response(JSON.stringify({
      id: "wda_pantry", name: body.name, design_system: saved,
    }), { status: 201 });
  }));
  return calls;
}

function fill() {
  typeInto('[data-testid="ds-name"]', "Harbor enamel");
  typeInto('[data-testid="ds-color-ground"]', "#123044");
  typeInto('[data-testid="ds-color-metal"]', "#C9D0D4");
  typeInto('[data-testid="ds-color-signal"]', "#E3A008");
  typeInto('[data-testid="ds-color-ink"]', "#1C1915");
  typeInto('[data-testid="ds-type-display"]', "Barlow Condensed");
  typeInto('[data-testid="ds-type-body"]', "Public Sans");
  typeInto('[data-testid="ds-type-utility"]', "IBM Plex Mono");
  typeInto('[data-testid="ds-space-tight"]', "4");
  typeInto('[data-testid="ds-space-stack"]', "12");
  typeInto('[data-testid="ds-space-band"]', "28");
}

describe("workspace design system", () => {
  it("saves the spec and shows what a new app read", async () => {
    const calls = serve(null);
    await mount(<DesignSystemScreen workspaceId="ws_harbor" />);
    expect($('[data-testid="ds-intro"]')?.tagName).toBe("P");
    expect($('[data-testid="ds-blade-name"]')?.textContent).toBe("Name this spec");
    expect($('[data-testid="ds-unread"]')?.tagName).toBe("P");
    const style = document.querySelector("style")?.textContent || "";
    expect(style).toContain("Barlow Condensed");
    expect(style).toContain("Public Sans");
    expect(style).toContain("#102833");
    expect(style.toLowerCase()).not.toContain("#f4f1ea");
    expect(style).not.toContain("linear-gradient");
    expect(style).toContain("prefers-reduced-motion");
    for (const el of document.querySelectorAll("[title]")) {
      const words = (el.getAttribute("title") || "").trim().split(/\s+/).filter(Boolean);
      expect(words.length).toBeLessThanOrEqual(14);
    }

    fill();
    await settle();
    await click($('[data-testid="ds-save"]'));
    const put = calls.find((c) => c.method === "PUT");
    expect(put?.url).toBe("/v1/workspaces/ws_harbor/design-system");
    expect(put?.body).toEqual({
      name: "Harbor enamel",
      colors: SAVED.colors,
      type: SAVED.type,
      spacing: SAVED.spacing,
    });
    expect($('[data-testid="ds-blade-name"]')?.textContent).toBe("Harbor enamel");
    expect($('[data-testid="ds-saved"]')?.textContent).toBe("Design system saved.");

    typeInto('[data-testid="ds-app-name"]', "Pantry");
    await settle();
    await click($('[data-testid="ds-create"]'));
    const post = calls.find((c) => c.method === "POST");
    expect(post?.url).toBe("/v1/workspaces/ws_harbor/apps");
    expect(post?.body).toEqual({ name: "Pantry" });
    const read = $('[data-testid="ds-app-read"]');
    expect(read?.tagName).toBe("P");
    expect(read?.textContent).toBe("Pantry reads Harbor enamel.");
    expect($('[data-testid="ds-read-signal"]')?.getAttribute("data-color")).toBe("#E3A008");
  });

  it("shows a failed save instead of swallowing it", async () => {
    serve(null, true);
    await mount(<DesignSystemScreen workspaceId="ws_harbor" />);
    fill();
    await settle();
    await click($('[data-testid="ds-save"]'));
    const alert = $('[role="alert"]');
    expect(alert?.tagName).toBe("P");
    expect(alert?.textContent).toBe("signal needs a #RRGGBB color.");
    expect($('[data-testid="ds-saved"]')).toBeNull();
  });

  it("loads the spec this workspace already saved", async () => {
    serve(SAVED);
    await mount(<DesignSystemScreen workspaceId="ws_harbor" />);
    expect(($('[data-testid="ds-name"]') as HTMLInputElement).value).toBe("Harbor enamel");
    expect(($('[data-testid="ds-color-signal"]') as HTMLInputElement).value).toBe("#E3A008");
    expect(($('[data-testid="ds-type-display"]') as HTMLInputElement).value).toBe("Barlow Condensed");
    expect(($('[data-testid="ds-space-band"]') as HTMLInputElement).value).toBe("28");
    expect($('[data-testid="ds-blade-name"]')?.textContent).toBe("Harbor enamel");
  });
});
