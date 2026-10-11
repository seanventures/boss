// @vitest-environment happy-dom
/**
 * Build database is an OVERRIDE (Auto / Always / Never, default Auto): BOSS gives a build its own
 * Postgres when it detects the app needs one. Setup shows the three, says what Auto does, and a
 * press sets the project setting build_services through PUT /v1/settings/build_services.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { click, fakeApi, mountBoard, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.restoreAllMocks(); });

const AUTO = { key: "build_services", mode: "auto" as const, enabled: true, source: "default" as const,
               configured: true, permission: "settings:update" };

describe("Build database", () => {
  it("defaults to Auto, explains it, and Never is one press", async () => {
    const view = { ...board(), build_database: AUTO };
    const api = fakeApi(view);
    mounted = await mountBoard(view);
    expect(document.querySelector('[data-testid="su-build-database-auto"]')!.getAttribute("aria-checked")).toBe("true");
    expect(document.querySelector('[data-testid="su-row-build-database"]')!.textContent)
      .toContain("Postgres when needed");
    await click(document.querySelector('[data-testid="su-build-database-never"]'));
    expect(api.calls.find((c) => c.op === "build_database")?.body).toMatchObject({ key: "build_services", value: "never" });
  });

  it("says so when the server has no build database service", async () => {
    const view = { ...board(), build_database: { ...AUTO, configured: false } };
    fakeApi(view);
    mounted = await mountBoard(view);
    expect(document.querySelector('[data-testid="su-build-database-unconfigured"]')).not.toBeNull();
  });

  it("an older API without the setting shows no row", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    expect(document.querySelector('[data-testid="su-row-build-database"]')).toBeNull();
  });
});
