// @vitest-environment happy-dom
/**
 * Command Center's workflow picker (tester P2): choosing a workflow is POST
 * /api/delivery-workflows {action: apply} (workflow:publish, step-up). The API's `401
 * step_up_required` reached the person as the words "step_up_required" under the picker. Now the
 * step-up sheet opens and, once they confirm, the same choice is sent again; cancelling says so.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

const stepUp = vi.fn(async () => true);
vi.mock("../components/identity/identity-provider", async (original) => ({
  ...(await original<typeof import("../components/identity/identity-provider")>()),
  useIdentity: () => ({ stepUp }),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { DefaultWorkflowPicker } = await import("../app/ask-boss-hero");

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); vi.unstubAllGlobals(); stepUp.mockReset(); });
const settle = () => act(async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0)); });

async function run(answers: Array<[number, unknown]>) {
  const posts: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });
    if (init?.method === "POST") { posts.push(JSON.parse(String(init.body))); const [s, b] = answers.shift()!; return json(b, s); }
    return json({ items: [{ id: "w1", name: "Security Review", workflow_key: "security_review" }] });
  }));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<DefaultWorkflowPicker projectId="p1" />));
  await settle();
  const select = host.querySelector("#askboss-wf-select") as HTMLSelectElement;
  await act(async () => { select.value = "security_review"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  await settle();
  return { posts, note: host.querySelector('[data-testid="askboss-wf-note"]')?.textContent ?? "" };
}

describe("the default workflow picker", () => {
  it("opens the step-up sheet on step_up_required and sends the choice again", async () => {
    stepUp.mockResolvedValue(true);
    const { posts, note } = await run([[401, { error: "step_up_required" }], [200, { in_force: "security_review" }]]);
    expect(stepUp).toHaveBeenCalledTimes(1);
    expect(posts).toHaveLength(2);
    expect(note).toBe("Saved");
    expect(host!.textContent).not.toContain("step_up_required");
  });

  it("cancelled, it says nothing was saved — never the code", async () => {
    stepUp.mockResolvedValue(false);
    const { posts, note } = await run([[401, { error: "step_up_required" }]]);
    expect(posts).toHaveLength(1);
    expect(note).toBe("Not saved: you cancelled the check");
  });
});
