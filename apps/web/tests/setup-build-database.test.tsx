// @vitest-environment happy-dom
/**
 * "Give builds a database (Postgres)" — the project setting build_services, which had no console
 * control (only an MCP tool or a repo's boss.verify.yaml). Setup shows it, says what it does, and
 * one press sets it through PUT /v1/settings/build_services.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { click, fakeApi, mountBoard, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.restoreAllMocks(); });

const OFF = { key: "build_services", enabled: false, source: "default" as const, configured: true, permission: "settings:update" };

describe("Build database", () => {
  it("is visible, explains itself, and one press asks for a Postgres per build", async () => {
    const view = { ...board(), build_database: OFF };
    const api = fakeApi(view);
    mounted = await mountBoard(view);
    const toggle = document.querySelector('[data-testid="su-build-database-toggle"]')!;
    expect(toggle.textContent).toBe("Off");
    expect(toggle.getAttribute("title")).toContain("dropped when the build ends");
    expect(document.querySelector('[data-testid="su-row-build-database"]')!.textContent)
      .toContain("Give builds a database (Postgres)");
    await click(toggle);
    const write = api.calls.find((c) => c.op === "build_database");
    expect(write?.body).toMatchObject({ key: "build_services", value: "postgres" });
  });

  it("says so when the server has no build database service", async () => {
    const view = { ...board(), build_database: { ...OFF, enabled: true, source: "project" as const, configured: false } };
    fakeApi(view);
    mounted = await mountBoard(view);
    expect(document.querySelector('[data-testid="su-build-database-toggle"]')!.textContent).toBe("On");
    expect(document.querySelector('[data-testid="su-build-database-unconfigured"]')).not.toBeNull();
  });

  it("an older API without the setting shows no row", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    expect(document.querySelector('[data-testid="su-row-build-database"]')).toBeNull();
  });
});
