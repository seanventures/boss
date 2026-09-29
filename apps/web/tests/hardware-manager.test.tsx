// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HardwareManager } from "../app/hardware-manager-ui";
import { EMPTY_EXTRAS_COPY } from "../lib/hardware";

const localPlace = {
  id: "place_local",
  kind: "local",
  name: "installer-host",
  hostname: "boss.local",
  arch: "arm64",
  os: "linux",
  agent_state: "installed" as const,
};

const providerPlace = {
  id: "place_do",
  kind: "provider",
  name: "spare-account",
  provider: "digitalocean",
};

type Call = { url: string; method: string; body?: Record<string, unknown> };

let host: HTMLDivElement;
let root: Root;
let calls: Call[];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function installFetch(places: unknown[], defaultPlaceId = "place_local") {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : undefined;
    calls.push({ url, method, body });
    if (method === "GET" && url.includes("/api/hardware/places")) {
      return json({ places, default_place_id: defaultPlaceId });
    }
    if (method === "GET" && url.includes("/api/hardware/resolve")) {
      return json(localPlace);
    }
    if (url.includes("/api/hardware/places/providers")) {
      return json({ id: "place_aws", kind: "provider", name: body?.name, provider: body?.provider });
    }
    if (url.includes("/api/hardware/places/agents")) {
      return json({ enroll_command: "scripts/install/node_agent.sh --enroll --token example" });
    }
    return json({ error: `unexpected ${method} ${url}` }, 500);
  }));
}

async function renderManager() {
  await act(async () => {
    root.render(<HardwareManager projectId="project_1" actor="operator" />);
  });
  for (let i = 0; i < 20; i += 1) {
    if (host.querySelector('[data-testid="hardware-default-place"]')) return;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
  throw new Error(`places did not render: ${host.textContent}`);
}

function button(label: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((el) => el.textContent?.trim() === label);
  if (!found) throw new Error(`missing button ${label}`);
  return found as HTMLButtonElement;
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

describe("hardware manager", () => {
  it("renders the local installer place selected, ahead of any other place", async () => {
    installFetch([providerPlace, localPlace]);
    await renderManager();

    const selected = host.querySelector<HTMLElement>('[data-testid="hardware-default-place"]');
    expect(selected?.getAttribute("data-selected")).toBe("true");
    expect(selected?.getAttribute("aria-current")).toBe("true");
    expect(selected?.textContent).toContain("This computer");
    expect(selected?.textContent).toContain("Where BOSS was installed");
    expect(selected?.textContent).toContain("installer-host");
    expect(selected?.querySelector('[data-testid="hardware-local-agent"]')?.getAttribute("data-agent-state"))
      .toBe("installed");
    expect(selected?.textContent).toContain("Hardware agent installed on this computer.");
    const extra = host.querySelector<HTMLElement>('[data-testid="hardware-extra-place"]');
    expect(extra?.getAttribute("data-selected")).toBe("false");
    expect(extra?.textContent).toContain("spare-account");
    expect(host.innerHTML.indexOf("hardware-default-place"))
      .toBeLessThan(host.innerHTML.indexOf("hardware-extra-place"));
  });

  it("shows the local agent as not ready when it is absent", async () => {
    installFetch([{ ...localPlace, agent_state: "absent" }]);
    await renderManager();
    const agent = host.querySelector('[data-testid="hardware-local-agent"]');
    expect(agent?.getAttribute("data-agent-state")).toBe("absent");
    expect(agent?.textContent).toContain("Hardware agent not ready on this computer.");
    expect(agent?.textContent).not.toContain("Hardware agent installed");
  });

  it("does not say there is nowhere to install when the only place is this computer", async () => {
    installFetch([localPlace]);
    await renderManager();

    expect(host.querySelector('[data-testid="hardware-extra-empty"]')?.textContent)
      .toContain(EMPTY_EXTRAS_COPY);
    expect(host.textContent?.toLowerCase()).not.toMatch(
      /nowhere to install|nowhere to deploy|no place to install|has nowhere/,
    );
    expect(host.textContent).toContain("This computer");
    expect(host.textContent).toContain("Where BOSS was installed");
  });

  it("sends secret_ref for a provider and refuses a pasted raw secret", async () => {
    installFetch([localPlace]);
    await renderManager();
    await act(async () => button("Connect a provider").click());

    const name = host.querySelector<HTMLInputElement>('input[name="name"]')!;
    const secret = host.querySelector<HTMLInputElement>('input[name="secret_ref"]')!;
    const provider = host.querySelector<HTMLSelectElement>('select[name="provider"]')!;
    name.value = "Production";
    secret.value = "pasted secret value";
    await act(async () => {
      provider.value = "aws";
      provider.dispatchEvent(new Event("change", { bubbles: true }));
      button("Register account").click();
    });

    expect(host.querySelector('[role="alert"]')?.textContent).toMatch(/secret reference|access key/i);
    expect(calls.filter((call) => call.url.includes("/providers"))).toHaveLength(0);
    expect(host.querySelector('input[name="secret"], input[name="token"], input[name="password"]')).toBeNull();

    secret.value = "env:BOSS_AWS_SECRET";
    await act(async () => button("Register account").click());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const posted = calls.filter((call) => call.method === "POST" && call.url.includes("/providers"));
    expect(posted).toHaveLength(1);
    expect(posted[0].body).toEqual({
      project_id: "project_1",
      actor: "operator",
      name: "Production",
      provider: "aws",
      secret_ref: "env:BOSS_AWS_SECRET",
    });
    expect(posted[0].body).not.toHaveProperty("secret");
    expect(posted[0].body).not.toHaveProperty("token");
    expect(posted[0].body).not.toHaveProperty("password");
  });

  it("shows the enroll command the API returns", async () => {
    installFetch([localPlace]);
    await renderManager();
    await act(async () => button("Add hardware").click());
    const name = host.querySelector<HTMLInputElement>('[data-testid="hardware-agent-panel"] input[name="name"]')!;
    name.value = "build-vm";
    await act(async () => button("Show enroll command").click());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(host.querySelector('[data-testid="hardware-enroll-command"]')?.textContent)
      .toBe("scripts/install/node_agent.sh --enroll --token example");
    const posted = calls.find((call) => call.url.includes("/places/agents"));
    expect(posted?.body).toEqual({ project_id: "project_1", actor: "operator", name: "build-vm" });
  });

  it("links to the hardware manager from delivery and leaves the SSH host wizard alone", () => {
    const delivery = readFileSync(resolve(__dirname, "../app/delivery-ui.tsx"), "utf8");
    const screen = readFileSync(resolve(__dirname, "../app/hardware-manager-ui.tsx"), "utf8");
    const fleet = readFileSync(resolve(__dirname, "../app/delivery-manage.tsx"), "utf8");
    expect(delivery).toContain("<HardwareManagerLink />");
    expect(screen).toContain('data-testid="hardware-manager-link"');
    expect(screen).toContain('href="#hardware-manager"');
    expect(fleet).toContain("export function FleetPanel");
    expect(fleet).toContain("No hosts yet. Register one to give BOSS somewhere to deploy.");
    expect(fleet).not.toContain("hardware-manager");
  });
});
