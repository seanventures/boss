// @vitest-environment happy-dom
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { AgentCapacityRow, capacityReadError } from "../app/setup/agent-capacity-row";

const write = vi.hoisted(() => vi.fn());
vi.mock("../lib/setup-client", () => ({ setupWrite: write }));
vi.mock("../app/setup/parts", () => ({ useBoard: () => ({ projectId: "local-project" }), toast: vi.fn() }));
vi.mock("../components/gated", () => ({ GatedButton: ({ permission, ...props }: any) => <button {...props} /> }));
afterEach(() => vi.unstubAllGlobals());

it("separates job slots from heavy builds and saves bounded settings", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const state = { worker_slots: 3, builds_at_once: { value: 1, auto: false, pinned: 1, warning: null },
    next_build: { admitted: false, shortfall: "CPU pressure" } };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ admission: state }) }));
  write.mockResolvedValue(state);
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  try {
    await act(async () => root.render(<AgentCapacityRow />));
    expect(host.querySelector('[data-testid="su-capacity-now"]')!.getAttribute("data-tip")).toContain("Waiting: CPU pressure");
    expect(host.textContent).toContain("Now: 1 heavy build, waiting");
    expect(Array.from(host.querySelectorAll("select")).map(select => select.value)).toEqual(["3", "1"]);
    await act(async () => host.querySelector("button")!.click());
    expect(write).toHaveBeenCalledWith("local-project", "agent_capacity", { value: 1, worker_slots: 3 },
      { label: "Save agent capacity" });
  } finally {
    await act(async () => root.unmount());
    host.remove();
  }
});

it("says why capacity could not be read and what to do", async () => {
  expect(capacityReadError(403)).toContain("system:read");
  expect(capacityReadError(502)).toContain("HTTP 502");
  expect(capacityReadError(502)).toContain("Observability › System");
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 403, json: async () => ({}) }));
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  try {
    await act(async () => root.render(<AgentCapacityRow />));
    expect(host.querySelector('[role="alert"]')!.textContent).toBe("Couldn't read capacity");
    expect(host.querySelector('[data-testid="su-capacity-error"]')!.getAttribute("data-tip")).toContain("Ask an admin");
  } finally {
    await act(async () => root.unmount());
    host.remove();
  }
});

it("says why the machine runs that many builds, and what the doctor added", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const state = { worker_slots: 4, worker_slots_why: "2 heavy at 1.2 GB each leaves 4.1 GB for builds that are thinking",
    builds_at_once: { value: 3, auto: true, pinned: null, warning: null, boost: 1,
      why: "7.7 GB memory, 1.2 GB kept free: room for 5 at 1.2 GB each; 4 CPU cores: 2 (one per 2 cores)" },
    next_build: { admitted: true, shortfall: null } };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ admission: state }) }));
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  try {
    await act(async () => root.render(<AgentCapacityRow />));
    expect(host.textContent).toContain("Now: 3 heavy builds (+1 by the system doctor)");
    const why = host.querySelector('[data-testid="su-capacity-why"]')!;
    expect(why.textContent).toContain("measured on this machine");
    expect(why.getAttribute("data-tip")).toContain("4 CPU cores: 2 (one per 2 cores)");
    expect(why.getAttribute("data-tip")).toContain("Job slots: 2 heavy at 1.2 GB");
  } finally {
    await act(async () => root.unmount());
    host.remove();
  }
});
