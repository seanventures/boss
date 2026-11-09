// @vitest-environment happy-dom
/**
 * A deploy step asks ONLY where: a machine from the Fleet, or "Ask me at deploy time" (the
 * default). No target, manifest path, port or URL: the first deploy creates them on that machine.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DeployWhere } from "../app/workflows/editor/inspector/step-inspector";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ""; });

const STEP = { id: "deploy", kind: "capability", next: [], binding: { capability: "deploy", inputs: {} } };

async function mount(hosts: unknown[], onPatch = vi.fn()) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(hosts), { status: 200 })));
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(<DeployWhere step={STEP as never} projectId="p1" onPatch={onPatch} />); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return { host, onPatch };
}

describe("a deploy step asks only where", () => {
  it("offers the Fleet's verified machines and Ask me at deploy time, and saves the choice as a value", async () => {
    const { host, onPatch } = await mount([
      { id: "dh_1", name: "vm-a", address: "10.0.0.1", status: "verified" },
      { id: "dh_2", name: "vm-b", address: "10.0.0.2", status: "verified" },
      { id: "dh_3", name: "vm-c", status: "registered" },
    ]);
    const select = host.querySelector("[data-testid=wf-deploy-where]") as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent)).toEqual(["Ask me at deploy time", "vm-a, 10.0.0.1", "vm-b, 10.0.0.2"]);
    await act(async () => { select.value = "dh_2"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(onPatch).toHaveBeenCalledWith({ input: { name: "host_id", source: { value: "dh_2" } } });
    expect(host.textContent).not.toMatch(/manifest|port|target/i);
  });

  it("with no machine yet says where to add one, and still saves", async () => {
    const { host } = await mount([]);
    expect(host.querySelector("[data-testid=wf-deploy-where-none] a")!.getAttribute("href")).toBe("/?area=infra&view=fleet&add=host");
  });
});

describe("Place app needs only the machine", () => {
  it("names the stack from the repository when no name is typed", async () => {
    const { defaultStackName } = await import("../app/infra/target-sheet");
    expect(defaultStackName("paramg/Shift_Swap.Board")).toBe("shift-swap-board");
    expect(defaultStackName("")).toBe("app");
  });
});
