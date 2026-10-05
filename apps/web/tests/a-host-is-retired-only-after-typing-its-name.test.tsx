// @vitest-environment happy-dom
/**
 * A machine is retired only after its name is typed (spec §4.4, §7). The old panel retired a
 * host from a `<details>` with four checkboxes and no confirmation at all; now `Retire…` opens a
 * sheet with the same four choices and a typed-name confirm, the press stays disabled until the
 * name matches exactly, and the server receives the name too (it checks it again).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { byTestId, click, mountFleet, type, unmountInfra } from "./_helpers/infra-render";

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(window.location.search), useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));

afterEach(() => unmountInfra());

const submit = () => byTestId("inf-retire-submit") as HTMLButtonElement;

describe("retiring a machine", () => {
  it("stays disabled until the exact name is typed, then posts the choices and the name", async () => {
    const mock = await mountFleet("view=fleet&host=h_ready");
    expect(byTestId("inf-host-drawer")).not.toBeNull();
    await click("inf-host-retire");
    const sheet = byTestId("inf-retire-sheet")!;
    expect(sheet.getAttribute("aria-label")).toBe("Retire edge-1?");
    expect(sheet.textContent).toContain("Revoke BOSS key");
    expect(sheet.textContent).toContain("Uninstall Docker");
    expect(sheet.textContent).toContain("Delete record");
    expect(sheet.textContent).toContain("Detach 1 app");
    expect(submit().disabled).toBe(true);

    await type("inf-retire-confirm", "edge");
    expect(submit().disabled).toBe(true);
    await type("inf-retire-confirm", "Edge-1");
    expect(submit().disabled).toBe(true);
    await click(submit());
    expect(mock.writes()).toHaveLength(0);

    await type("inf-retire-confirm", "edge-1");
    expect(submit().disabled).toBe(false);
    await click("inf-retire-purge");
    await click("inf-retire-detach");
    await click(submit());
    const writes = mock.writes();
    expect(writes.map((w) => `${w.method} ${w.path}`)).toEqual(["POST /v1/infra/hosts/h_ready/retire"]);
    expect(writes[0].body).toMatchObject({
      project_id: "project_1", actor: "ceo", confirm_name: "edge-1",
      revoke_key: true, uninstall_docker: false, delete_record: true, detach_apps: true,
    });
    // Retired: the sheet and the drawer close.
    expect(byTestId("inf-retire-sheet")).toBeNull();
    expect(byTestId("inf-host-drawer")).toBeNull();
  });

  it("Cancel sends nothing", async () => {
    const mock = await mountFleet("view=fleet&host=h_ready");
    await click("inf-host-retire");
    await type("inf-retire-confirm", "edge-1");
    const cancel = [...byTestId("inf-retire-sheet")!.querySelectorAll("button")].find((b) => b.textContent === "Cancel")!;
    await click(cancel);
    expect(byTestId("inf-retire-sheet")).toBeNull();
    expect(mock.writes()).toHaveLength(0);
  });

  it("This computer has no retire, and nothing is retired where host writes are off", async () => {
    await mountFleet("view=fleet&host=this");
    expect(byTestId("inf-host-drawer")).not.toBeNull();
    expect(byTestId("inf-host-retire")).toBeNull();
    unmountInfra();
    await mountFleet("view=fleet&host=h_ready", { variant: "blocked" });
    expect(byTestId("inf-host-drawer")).not.toBeNull();
    expect(byTestId("inf-host-retire")).toBeNull();
    expect(byTestId("inf-host-reverify")).toBeNull();
  });
});
