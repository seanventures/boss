// @vitest-environment happy-dom
/**
 * Build a workflow from the library: an empty name stops, no steps stop, a filled walk
 * posts the editor's spec and shows the key, and a refused save shows the error.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkflowWizard } from "../app/workflows/wizard/workflow-wizard";
import { publishSpec } from "../lib/workflow-api";

vi.mock("../lib/workflow-api", () => ({
  publishSpec: vi.fn(),
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const publish = vi.mocked(publishSpec);

let root: Root | null = null;
let host: HTMLElement | null = null;
const opened: string[] = [];

async function settle() {
  for (let i = 0; i < 8; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
}

async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<WorkflowWizard projectId="p1" onClose={() => {}} onOpen={(key) => opened.push(key)} />);
  });
}

function el(testId: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(`[data-testid="${testId}"]`);
  expect(found, testId).not.toBeNull();
  return found!;
}

async function click(testId: string) {
  await act(async () => { el(testId).click(); });
}

async function type(testId: string, value: string) {
  const node = el(testId) as HTMLInputElement | HTMLTextAreaElement;
  await act(async () => {
    const proto = node instanceof HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(node, value);
    node.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

/** Name, a start sentence, one person step, one role step, and a yes to approval. */
async function fill() {
  await type("wf-wizard-name", "Weekly digest");
  await click("wf-wizard-continue");
  await type("wf-wizard-starts", "Every Monday morning");
  await click("wf-wizard-continue");
  await type("wf-wizard-step-name", "Gather");
  await type("wf-wizard-instruction", "Collect the week's notes");
  await click("wf-wizard-add-step");
  await type("wf-wizard-step-name", "Write");
  await click("wf-wizard-who-role");
  await type("wf-wizard-role", "program_manager");
  await type("wf-wizard-instruction", "Draft the digest");
  await click("wf-wizard-add-step");
  await click("wf-wizard-continue");
  await click("wf-wizard-approve-yes");
  await click("wf-wizard-continue");
}

beforeEach(() => {
  opened.length = 0;
  publish.mockReset();
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null;
  host = null;
});

describe("the workflow wizard", () => {
  it("refuses an empty name", async () => {
    await mount();
    await click("wf-wizard-continue");
    expect(el("wf-wizard-error").textContent).toBe("Name it first.");
    expect(document.querySelector('[data-testid="wf-wizard-starts"]')).toBeNull();
    await type("wf-wizard-name", "   ");
    await click("wf-wizard-continue");
    expect(el("wf-wizard-error").textContent).toBe("Name it first.");
    expect(publish).not.toHaveBeenCalled();
  });

  it("refuses to continue with no steps", async () => {
    await mount();
    await type("wf-wizard-name", "Weekly digest");
    await click("wf-wizard-continue");
    await click("wf-wizard-continue");
    await click("wf-wizard-continue");
    expect(el("wf-wizard-error").textContent).toBe("Add a step first.");
    expect(document.querySelector('[data-testid="wf-wizard-approve-yes"]')).toBeNull();
    expect(publish).not.toHaveBeenCalled();
  });

  it("posts the spec and shows the key", async () => {
    publish.mockResolvedValue({
      definition: { id: "def_1", workflow_key: "weekly_digest", name: "Weekly digest" },
      version: 1,
      created: true,
    });
    await mount();
    await fill();
    expect(el("wf-wizard-review-note").textContent).toBe("This is the description. The spec has no trigger.");
    await click("wf-wizard-save");
    await settle();
    expect(publish).toHaveBeenCalledTimes(1);
    expect(publish).toHaveBeenCalledWith("p1", {
      definition_id: null,
      name: "Weekly digest",
      description: "Every Monday morning",
      spec: {
        entry_step: "gather",
        steps: [
          {
            id: "gather",
            kind: "capability",
            title: "Gather",
            captain: "human",
            next: ["write"],
            binding: { agent_work: true, inputs: { instructions: "Collect the week's notes" } },
          },
          {
            id: "write",
            kind: "capability",
            title: "Write",
            captain: "program_manager",
            next: ["approval"],
            binding: { agent_work: true, inputs: { instructions: "Draft the digest" } },
          },
          {
            id: "approval",
            kind: "decision",
            title: "A person approves",
            next: [],
            question: "Approve before this run finishes?",
            guidance: "",
            approver_role: "human",
          },
        ],
      },
    });
    expect(el("wf-wizard-saved").textContent).toBe("Saved");
    expect(el("wf-wizard-saved-name").textContent).toBe("Weekly digest");
    expect(el("wf-wizard-key").textContent).toBe("weekly_digest");
    await click("wf-wizard-open-editor");
    expect(opened).toEqual(["weekly_digest"]);
  });

  it("shows a refused save and does not claim it was saved", async () => {
    publish.mockRejectedValue(new Error("Name in use"));
    await mount();
    await fill();
    await click("wf-wizard-save");
    await settle();
    expect(el("wf-wizard-error").textContent).toBe("Couldn't save the workflow: Name in use. Nothing was saved.");
    expect(document.querySelector('[data-testid="wf-wizard-saved"]')).toBeNull();
    expect(document.querySelector('[data-testid="wf-wizard-key"]')).toBeNull();
    expect(document.body.textContent).not.toMatch(/\bSaved\b/);
  });
});
