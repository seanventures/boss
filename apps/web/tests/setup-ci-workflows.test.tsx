// @vitest-environment happy-dom
/**
 * "Builds may change CI workflows" on a repository line (services/ci_gate.py): a short label with
 * the full meaning in its tooltip, off by default, a github:connect write to change, and — when on —
 * the exact GitHub page where the App's new `workflows: write` permission is accepted.
 */
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { fakeApi, flush, mountInBoard, type Mounted } from "./_helpers/setup-board";
import { CiWorkflowsToggle } from "../app/setup/ci-workflows-toggle";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

const HELP = "Lets a build add or edit files under .github/workflows/.";
const OFF = { binding_id: "ghb1", repository: "owner/app", label: "Builds may change CI workflows",
              help: HELP, enabled: false, requires: "Owner or Admin, with step-up" };
const ON = { ...OFF, enabled: true,
             permissions_url: "https://github.com/organizations/owner/settings/apps/boss-x/permissions",
             installation_url: "https://github.com/organizations/owner/settings/installations/42",
             note: "Set Workflows to Read and write, then accept on the installation." };

describe("CI workflows opt-in", () => {
  it("is off by default, says what it means, and links GitHub once on", async () => {
    const view = board();
    const api = fakeApi(view);
    api.answer = (call) => (call.url.includes("ci_workflows=") ? { status: 200, body: OFF }
      : call.op === "ci_workflows" ? { status: 200, body: ON } : null);
    mounted = await mountInBoard(view, <CiWorkflowsToggle bindingId="ghb1" />);
    const row = document.body.querySelector('[data-testid="su-ci-workflows"]')!;
    expect(row).toBeTruthy();
    expect(row.querySelector("[data-tip]")!.getAttribute("data-tip")).toBe(HELP);
    const toggle = row.querySelector<HTMLButtonElement>('[data-testid="su-ci-workflows-toggle"]')!;
    expect(toggle.textContent).toBe("Off");
    expect(document.body.querySelector('[data-testid="su-ci-workflows-accept"]')).toBeNull();
    await act(async () => { toggle.click(); });
    await flush();
    const put = api.calls.find((c) => c.op === "ci_workflows")!;
    expect(put.body).toMatchObject({ binding_id: "ghb1", enabled: true });
    expect(toggle.textContent).toBe("Allowed");
    const accept = document.body.querySelector('[data-testid="su-ci-workflows-accept"]')!;
    expect(accept.getAttribute("href")).toBe(ON.permissions_url);
    expect(accept.getAttribute("title")).toBe(ON.note);
  });
});
