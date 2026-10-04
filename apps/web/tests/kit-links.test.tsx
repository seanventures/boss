// @vitest-environment happy-dom
/**
 * Deep links (skills-mcp spec §5.1, test 7): `&ability=email` opens the Email drawer; `&tab=who
 * &role=pm` highlights Juno's row; the retired Capabilities area lands in the Library's MCP list;
 * the links in point at the Kit.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { resolveLegacyArea } from "../app/legacy-areas";
import { codeOnly } from "./_helpers/source";
import { $, cleanup, render } from "./_helpers/kit-render";
import { installKitApi } from "./_helpers/kit-api";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const { KitHome } = await import("../app/skills/kit-home");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("links into Skills & MCP", () => {
  it("&ability=email opens the Email drawer", async () => {
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills&ability=email");
    await render(<KitHome projectId="p1" actor="ceo" />);
    expect($('[data-testid="kit-drawer-email"]')).not.toBeNull();
  });

  it("&tab=who&role=pm opens Who can on Juno's row", async () => {
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills&tab=who&role=pm");
    await render(<KitHome projectId="p1" actor="ceo" />);
    expect($('[data-testid="kit-who"]')).not.toBeNull();
    expect($('[data-testid="kit-row-pm"]')!.dataset.focus).toBe("true");
  });

  it("&tab=library&lib=mcp opens the Library's MCP list", async () => {
    installKitApi();
    window.history.replaceState(null, "", "/?area=skills&tab=library&lib=mcp");
    await render(<KitHome projectId="p1" actor="ceo" />);
    expect($('[data-testid="kit-mcp-list"]')).not.toBeNull();
  });

  it("the retired Capabilities area lands in the Library's MCP list", () => {
    const next = resolveLegacyArea(new URLSearchParams("area=capabilities&org=o1"))!;
    expect(Object.fromEntries(next)).toEqual({ area: "skills", tab: "library", lib: "mcp", org: "o1" });
  });

  it("the links in say where to go", () => {
    expect(codeOnly("app/skills/agent-abilities.tsx")).toContain("?area=skills&tab=who&role=");
    expect(codeOnly("app/work-orchestration-ui.tsx")).toContain('href="?area=skills&ability=file_tickets"');
    expect(codeOnly("lib/use-kit-abilities.ts")).toContain("`?area=skills&ability=${");
  });
});
