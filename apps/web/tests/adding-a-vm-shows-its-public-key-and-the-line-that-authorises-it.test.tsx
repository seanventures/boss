// @vitest-environment happy-dom
/**
 * Adding a VM in Infra shows its public key and the line that authorises it — then Verify.
 *
 * The operator's words (2026-10-03): "In the Infra menu I can add a new VM … you enable BOSS to
 * perform all these steps." Adding a VM was two moves (Register, then a separate "Generate a key
 * for me") and the screen that followed showed only a push command that assumes the operator's
 * laptop can already SSH to the VM. Now one press records the VM and mints its key, and the next
 * thing on screen is the PUBLIC key, a copy button, and the exact line to add it to
 * authorized_keys — with `I ran it` beside it.
 *
 * Rewritten for the Fleet (spec §4.3 `+ Add › Host`, §4.4 SETUP): the sheet records and mints in
 * one press, then the new machine's drawer opens on its Authorise stage.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { AUTHORISE_LINE, hostFixture, PUBLIC_KEY, type Route } from "./_helpers/infra-fixtures";
import { byTestId, click, mountFleet, submit, type, unmountInfra } from "./_helpers/infra-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));

afterEach(() => unmountInfra());

/** The new machine, as the view answers once its key is minted: stage Authorise. */
const NEW_HOST: Route = { match: /^\/v1\/infra\/hosts\/h_new$/, etag: true, reply: () => ({
  ...hostFixture("h_setup"), id: "h_new", name: "203.0.113.7",
  state: { word: "Setting up · 2/5", tone: "accent" }, stage: { stage: "install", index: 2, label: "Authorise" },
  facts: { address: "203.0.113.7", user: "ubuntu", port: 2222, arch: null, labels: [], port_min: null, port_max: null, max_apps: null, key_fingerprint: null },
}) };

describe("Add host", () => {
  it("records the VM with its address, user and port, mints its key, and shows the key to authorise", async () => {
    const mock = await mountFleet("view=fleet", { routes: [NEW_HOST] });
    await click("infra-add");
    await click("infra-add-host");
    expect(byTestId("inf-host-sheet")).not.toBeNull();
    await type("inf-host-address", "203.0.113.7");
    await type("inf-host-user", "ubuntu");
    await type("inf-host-port", "2222");
    await submit("inf-host-address");

    const writes = mock.writes();
    expect(writes.map((w) => w.path)).toEqual(["/v1/deployment-hosts", "/v1/deployment-hosts/h_new/key"]);
    expect(writes[0].body).toMatchObject({ project_id: "project_1", address: "203.0.113.7", ssh_user: "ubuntu", ssh_port: 2222,
                                           // A blank name is the address — one less field to invent.
                                           name: "203.0.113.7" });
    // Durable by default: `file` loses the key on the next redeploy of BOSS itself.
    expect(writes[1].body).toMatchObject({ backend: "database" });

    // The new machine's drawer is open on Authorise: the key, the line, the copies.
    expect(byTestId("inf-host-sheet")).toBeNull();
    expect(byTestId("inf-host-drawer")).not.toBeNull();
    expect(new URLSearchParams(window.location.search).get("host")).toBe("h_new");
    expect(byTestId("inf-stage-public-key")?.textContent).toBe(PUBLIC_KEY);
    expect(byTestId("inf-stage-authorise-line")?.textContent).toBe(AUTHORISE_LINE);
    expect(byTestId("inf-stage-copy-key")?.textContent).toBe("Copy key");
    expect(byTestId("inf-stage-copy-line")?.textContent).toBe("Copy line");
    expect((byTestId("inf-step-install") as HTMLDetailsElement).open).toBe(true);

    await click("inf-stage-ran");
    expect(mock.writes().map((w) => w.path).at(-1)).toBe("/v1/deployment-hosts/h_new/verify");
  });

  it("the authorise line is exactly the server's, which appends this key with the modes sshd insists on", () => {
    expect(AUTHORISE_LINE).toBe("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
      + `printf '%s\\n' '${PUBLIC_KEY}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys`);
  });
});
