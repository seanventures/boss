// @vitest-environment happy-dom
/**
 * Setup shows one installed adapter, and that skills and MCP config are delivered to it.
 * The row is absent when the install has none (an older API, or nothing approved).
 */
import { afterEach, describe, expect, it } from "vitest";

import type { InstalledAdapter } from "../lib/setup-types";
import { overBudget } from "./_helpers/copy-budget";
import { fakeApi, mountBoard, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

const acme: InstalledAdapter = {
  id: "acme.inhouse", name: "acme_inhouse", label: "Acme in-house", version: "1.0.0",
  kind: "process", source: "community",
  kit: { mcp_delivery: "env_manifest", skills_dir: "{env}", mcp_config: "delivered", skills: "delivered" },
};

describe("Setup › Installed adapters", () => {
  it("shows one installed adapter and that skills and MCP config are delivered", async () => {
    const view = { ...board(), harness_adapters: [acme] };
    fakeApi(view);
    mounted = await mountBoard(view);
    const row = document.body.querySelector('[data-testid="installed-adapter-acme.inhouse"]')!;
    expect(row.textContent).toContain("Acme in-house");
    expect(document.body.querySelector('[data-testid="installed-adapter-acme.inhouse-mcp"]')!.textContent)
      .toBe("MCP config delivered");
    expect(document.body.querySelector('[data-testid="installed-adapter-acme.inhouse-skills"]')!.textContent)
      .toBe("Skills delivered");
    expect(overBudget(document.body)).toEqual([]);
  });

  it("is absent when nothing is installed", async () => {
    const view = board();
    fakeApi(view);
    mounted = await mountBoard(view);
    expect(document.body.querySelector('[data-testid="su-row-installed-adapters"]')).toBeNull();
  });
});
