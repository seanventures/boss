// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Adding a VM in Infra shows its public key and the line that authorises it — then Verify.
 *
 * The operator's words (2026-10-03): "In the Infra menu I can add a new VM … you enable BOSS to
 * perform all these steps." Adding a VM was two moves (Register, then a separate "Generate a key
 * for me") and the screen that followed showed only a push command that assumes the operator's
 * laptop can already SSH to the VM. Now one press records the VM and mints its key, and the next
 * thing on screen is the PUBLIC key, a copy button, and the exact line to add it to
 * authorized_keys — with Verify beside it.
 */

const PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExample boss@boss-vm";

const state: { hosts: Record<string, unknown>[] } = { hosts: [] };
const registered: FormData[] = [];
const minted: FormData[] = [];
const verified: FormData[] = [];

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

vi.mock("../app/actions", () => ({
  listDeploymentHostsAction: vi.fn(async () => ({
    ok: true, data: { hosts: state.hosts, preconditions: { blocker: null, host_writes_allowed: false } },
  })),
  registerDeploymentHostAction: vi.fn(async (_prev: unknown, fd: FormData) => {
    registered.push(fd);
    state.hosts = [{
      id: "host_1", name: String(fd.get("name")), address: String(fd.get("address")),
      ssh_user: String(fd.get("ssh_user")), ssh_port: Number(fd.get("ssh_port")), status: "registered",
      app_count: 0, apps: [], key_secret_ref: null, last_preflight: null,
    }];
    return { ok: true, data: state.hosts[0] };
  }),
  generateHostKeyAction: vi.fn(async (_prev: unknown, fd: FormData) => {
    minted.push(fd);
    const install = `ssh -o StrictHostKeyChecking=accept-new -p 2222 ubuntu@203.0.113.7 "… BOSS_KEY_INSTALLED"`;
    state.hosts = state.hosts.map((h) => ({ ...h, key_secret_ref: "sref_1", key_public: PUBLIC,
                                            install_command: install }));
    return { ok: true, data: { host_id: "host_1", public_key: PUBLIC, install_command: install } };
  }),
  verifyDeploymentHostAction: vi.fn(async (_prev: unknown, fd: FormData) => {
    verified.push(fd);
    return { ok: true, data: { status: "verified" } };
  }),
  adoptHostKeyAction: vi.fn(), installHostKeyAction: vi.fn(), provisionHostAction: vi.fn(),
  retireHostAction: vi.fn(),
  bindSecretAction: vi.fn(), createTargetAction: vi.fn(), deployObservabilityAction: vi.fn(),
  getDeliveryDefaultsAction: vi.fn(), listRefsAction: vi.fn(), listRepoAppsAction: vi.fn(),
  listReposAction: vi.fn(), updateTargetAction: vi.fn(), deployTargetAction: vi.fn(),
  generateKeypairAction: vi.fn(), putSecretAction: vi.fn(), setAutoDeployAction: vi.fn(),
  verifySecretAction: vi.fn(),
}));

const { FleetPanel, authorizeLine } = await import("../app/delivery-manage");

let host: HTMLDivElement;
let root: Root;

async function settle(until: () => boolean, what: string) {
  for (let i = 0; i < 40; i += 1) {
    if (until()) return;
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
  throw new Error(`${what} did not happen: ${host.textContent}`);
}

function button(label: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((el) => el.textContent?.trim() === label);
  if (!found) throw new Error(`missing button ${label}: ${host.textContent}`);
  return found as HTMLButtonElement;
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  state.hosts = [];
  registered.length = 0; minted.length = 0; verified.length = 0;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

describe("Add a VM", () => {
  it("the authorise line appends exactly this key, with the modes sshd insists on", () => {
    expect(authorizeLine(` ${PUBLIC} `)).toBe(
      "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
      + `printf '%s\\n' '${PUBLIC}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys`);
  });

  it("records the VM with its address, user and port, mints its key, and shows the key to authorise", async () => {
    await act(async () => { root.render(<FleetPanel projectId="project_1" />); });
    await settle(() => !!host.querySelector('[data-testid="fleet-panel"]'), "panel");
    await act(async () => { button("Add a VM").click(); });

    const form = [...host.querySelectorAll("form")].find((f) => f.querySelector('[name="address"]'))!;
    (form.querySelector('[name="address"]') as HTMLInputElement).value = "203.0.113.7";
    (form.querySelector('[name="ssh_user"]') as HTMLInputElement).value = "ubuntu";
    (form.querySelector('[name="ssh_port"]') as HTMLInputElement).value = "2222";
    await act(async () => { form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    await settle(() => !!host.querySelector('[data-testid="fleet-public-key"]'), "public key");

    expect(registered).toHaveLength(1);
    expect(registered[0].get("address")).toBe("203.0.113.7");
    expect(registered[0].get("ssh_user")).toBe("ubuntu");
    expect(registered[0].get("ssh_port")).toBe("2222");
    // A blank name is the address — one less field to invent before anything works.
    expect(registered[0].get("name")).toBe("203.0.113.7");
    expect(minted).toHaveLength(1);
    expect(minted[0].get("host_id")).toBe("host_1");
    // Durable by default: `file` loses the key on the next redeploy of BOSS itself.
    expect(minted[0].get("backend")).toBe("database");

    const block = host.querySelector('[data-testid="fleet-public-key"]')!;
    expect(block.textContent).toContain(PUBLIC);
    expect(block.textContent).toContain("ubuntu");
    expect(host.querySelector('[data-testid="fleet-authorize-line"]')?.textContent).toBe(authorizeLine(PUBLIC));
    const labels = [...block.querySelectorAll("button")].map((b) => b.textContent?.trim());
    expect(labels).toEqual(expect.arrayContaining(["Copy public key", "Copy line"]));

    await act(async () => { button("I've run it — verify").click(); });
    await settle(() => verified.length === 1, "verify");
    expect(verified[0].get("host_id")).toBe("host_1");
  });
});
