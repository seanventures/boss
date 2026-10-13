// @vitest-environment happy-dom
/**
 * "Remove app" (services/app_removal.py) is a confirm sheet: it says what goes, keeps the data
 * unless a separate box is ticked, needs the app's name typed, and is a gated action whose
 * permission is declared.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { RemoveAppSheet } from "../app/infra/target-drawer";
import { ACTION_PERMISSIONS } from "../lib/identity/action-permissions";
import type { TargetDetail } from "../lib/infra-view";
import { mount, type Mounted } from "./_helpers/infra-tab-render";

let m: Mounted | null = null;
afterEach(async () => { await m?.unmount(); m = null; });

const target = {
  id: "deptgt_1", app: "HelpDesk", env: "staging", kind: "ssh",
  where: { host: { id: "dh_1", name: "192.0.2.202" }, repo: null, ref: null, manifest: null, domain: null,
           publicUrl: null, account: null },
} as unknown as TargetDetail;

describe("Remove app", () => {
  it("says what goes, keeps data by default, and waits for the typed name", async () => {
    m = await mount(<RemoveAppSheet t={target} actor="ceo" onClose={() => {}} onPlanned={() => {}} />, "?area=infra");
    const root = document.body;
    expect(root.querySelector('[data-testid="inf-target-remove-what"]')?.textContent)
      .toContain("HelpDesk on 192.0.2.202");
    const data = root.querySelector('[data-testid="inf-target-remove-data"]') as HTMLInputElement;
    expect(data).not.toBeNull();
    expect(data.checked).toBe(false);
    expect(root.textContent).toContain("can't be undone");
    const submit = root.querySelector('[data-testid="inf-target-remove-submit"]') as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
  });

  it("is a declared, gated action that calls the governed remove route", () => {
    expect(ACTION_PERMISSIONS["app/infra/infra-actions.ts#removeTargetApp"]).toBe("deployment:create");
    const src = readFileSync(join(process.cwd(), "app/infra/infra-actions.ts"), "utf8");
    expect(src).toContain("/v1/deployment-targets/${enc(targetId)}/remove");
  });
});
