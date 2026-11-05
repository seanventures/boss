// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * A chat bound to a workflow step is about that run. The task block used to say "No projects
 * yet for this task" whenever the dock subject had no project list — including a deploy step
 * that already has an app, a PR and a deployment (BOSSAPP-570).
 *
 * Mounts the real panel. The break this catches: the empty-project sentence rendered for a
 * subject ref, or the run id missing from what the operator can read.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const { AgentChat } = await import("../app/agent-ui");

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = new URL(String(input), "http://console.test");
    const view = url.searchParams.get("view");
    if (view === "workspace") return Response.json({ tasks: [], threads: [] });
    if (view === "task-context") return Response.json({ task: null, code_tasks: [] });
    if (view === "messages") return Response.json({ messages: [] });
    return Response.json({});
  }));
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 20; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
}

async function panel(initialTaskId: string) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <AgentChat projectId="p1" actor="ceo" initialTaskId={initialTaskId} roleName="Deployment Manager" />));
  await settle();
  return host!.textContent ?? "";
}

describe("a chat bound to a run names that run", () => {
  it("a workflow step is named, and is not told it has no project", async () => {
    const text = await panel("workflow_step:wfr_475bf68eb18f485b8c52:deploy");
    expect(text).toContain("wfr_475bf68eb18f485b8c52");
    expect(text).toContain("deploy");
    expect(text).not.toContain("No projects yet for this task");
  });

  it("a build is named by its run, not as an empty task", async () => {
    const text = await panel("crun_fc2d5de15a064c128bc7");
    expect(text).toContain("crun_fc2d5de15a064c128bc7");
    expect(text).not.toContain("No projects yet for this task");
  });
});
