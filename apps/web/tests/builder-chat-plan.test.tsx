// @vitest-environment happy-dom
/**
 * Chat and Plan sit on the builder thread. A plan turn asks for no code,
 * and Build is requested only after a person approves the plan.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BuilderChatPlan } from "../app/builder/chat-plan";
import { $, click, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const THREAD = "bt_aabbccddeeff0011";

type Call = { url: string; body: Record<string, unknown> | null };

let calls: Call[] = [];

function json(status: number, body: unknown): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  }));
}

function installFetch(): void {
  calls = [];
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : null;
    calls.push({ url: String(url), body });
    if (String(url).endsWith("/v1/builder/threads") && init?.method === "POST") {
      return json(201, {
        thread_id: THREAD, wrote_code: false, code_files: [], turns: [], plan: null, approval: null,
      });
    }
    if (String(url).endsWith("/approvals")) {
      return json(200, {
        approved: true,
        wrote_code: false,
        code_files: [],
        approval: { id: "apr_1", plan_id: "plan_1", person_id: "sam" },
      });
    }
    if (body?.mode === "plan") {
      return json(200, {
        mode: "plan",
        wrote_code: false,
        code_files: [],
        plan: { id: "plan_1", steps: [String(body.text)], writes_code: false },
        approval: null,
        turns: [{
          mode: "plan",
          text: String(body.text),
          reply: "Plan only. No code was written.",
          wrote_code: false,
        }],
      });
    }
    if (body?.mode === "build") {
      return json(200, {
        mode: "build",
        wrote_code: true,
        deduped: false,
        code_files: ["src/approved_build.py"],
        turns: [{
          mode: "build",
          text: "",
          reply: "Wrote src/approved_build.py.",
          wrote_code: true,
        }],
      });
    }
    return json(500, { error: "unexpected", message: `unexpected ${url}` });
  });
}

async function until(ready: () => boolean): Promise<void> {
  for (let i = 0; i < 30; i += 1) {
    if (ready()) return;
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }
  throw new Error("the builder thread did not become ready");
}

async function typeMessage(value: string): Promise<void> {
  const box = $('[data-testid="bcp-message"]') as HTMLTextAreaElement | null;
  if (!box) throw new Error("no message box");
  const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  await act(async () => {
    set.call(box, value);
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("builder thread chat and plan", () => {
  it("shows Chat and Plan, and a plan turn creates no code files", async () => {
    installFetch();
    await mount(<BuilderChatPlan projectId="p1" />);
    await until(() => !$('[data-testid="bcp-send"]')?.hasAttribute("disabled"));

    expect($('[data-testid="bcp-mode-chat"]')).not.toBeNull();
    expect($('[data-testid="bcp-mode-plan"]')).not.toBeNull();
    expect(document.body.textContent).toContain("They do not write code.");
    expect($('[data-testid="bcp-files"]')?.textContent).toContain("No code files.");
    expect($('[data-testid="bcp-build"]')?.hasAttribute("disabled")).toBe(true);

    await click($('[data-testid="bcp-mode-plan"]'));
    await typeMessage("Add a note the person can read.");
    await click($('[data-testid="bcp-send"]'));

    expect(calls.filter((call) => call.body?.mode === "build")).toHaveLength(0);
    expect(calls.some((call) => call.body?.mode === "plan")).toBe(true);
    expect($('[data-testid="bcp-files"]')?.textContent).toContain("No code files.");
    expect($('[data-testid="bcp-plan"]')?.textContent).toContain("Add a note the person can read.");
    expect($('[data-testid="bcp-build"]')?.hasAttribute("disabled")).toBe(true);
  });

  it("asks to write code only after a person approves the plan", async () => {
    installFetch();
    await mount(<BuilderChatPlan />);
    await until(() => !$('[data-testid="bcp-send"]')?.hasAttribute("disabled"));

    await click($('[data-testid="bcp-mode-plan"]'));
    await typeMessage("Add a note the person can read.");
    await click($('[data-testid="bcp-send"]'));
    await click($('[data-testid="bcp-build"]'));
    expect(calls.filter((call) => call.body?.mode === "build")).toHaveLength(0);
    expect($('[data-testid="bcp-files"]')?.textContent).toContain("No code files.");

    await click($('[data-testid="bcp-approve"]'));
    expect(calls.filter((call) => call.body?.mode === "build")).toHaveLength(0);
    expect($('[data-testid="bcp-files"]')?.textContent).toContain("No code files.");
    expect($('[data-testid="bcp-build"]')?.hasAttribute("disabled")).toBe(false);

    await click($('[data-testid="bcp-build"]'));
    expect(calls.filter((call) => call.body?.mode === "build")).toHaveLength(1);
    expect($('[data-testid="bcp-files"]')?.textContent).toContain("src/approved_build.py");
  });
});
