// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * "Ask Qa about this" opens Qa's conversation about THIS — not Qa's general thread.
 *
 * Review of 374ba07: AgentChat mounted with both a subject (`initialTaskId`) and a role loaded
 * the role's general thread (`external_ref === "role:Qa"`) and ITS proposed actions — Qa's
 * unrelated history and still-clickable buttons above a question about run B's Preview step —
 * while the turn landed in the (subject, role) thread. An earlier answer about the same step
 * was never shown. This mounts the real panel against a fake API holding all three threads.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;
const { AgentChat } = await import("../app/agent-ui");

const SUBJECT = "workflow_step:wfr_b0b0b0b0b0b0b0b0b0b0:preview";
const THREADS = [
  { thread_id: "athr_role", external_ref: "role:Qa", agent_role: "qa", title: "general" },
  { thread_id: "athr_eng", external_ref: SUBJECT, agent_role: "engineer", title: "eng" },
  { thread_id: "athr_subject", external_ref: SUBJECT, agent_role: "qa", title: "about B" },
];
const MESSAGES: Record<string, unknown> = {
  athr_role: { messages: [{ role: "assistant", content: "QA GENERAL: the release checklist" }],
               proposed_actions: [{ id: "pa_role", tool: "redeploy", args: {},
                                    label: "Redeploy HookWatch", status: "proposed" }] },
  athr_eng: { messages: [{ role: "assistant", content: "ENGINEER ON B: rebuild it" }],
              proposed_actions: [] },
  athr_subject: { messages: [{ role: "assistant", content: "QA ON B: approvals.py imports wrong" }],
                  proposed_actions: [{ id: "pa_subject", tool: "retry_workflow_step", args: {},
                                       label: "Try Preview again", status: "proposed" }] },
};

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = new URL(String(input), "http://console.test");
    const view = url.searchParams.get("view");
    if (view === "workspace") return Response.json({ tasks: [], threads: THREADS });
    if (view === "messages") {
      return Response.json(MESSAGES[url.searchParams.get("thread_id") ?? ""] ?? {});
    }
    return Response.json({});
  }));
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 20; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount(props: Record<string, unknown>) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <AgentChat projectId="p1" actor="ceo" {...(props as object)} />));
  await settle();
  return host.innerHTML;
}

describe("an Ask door with a subject and a role", () => {
  it("shows that agent's conversation about the subject, and only its offers", async () => {
    const html = await mount({ initialTaskId: SUBJECT, roleName: "Qa",
                               initialMessage: "Why did the Preview step fail?" });
    expect(html).toContain("QA ON B: approvals.py imports wrong");
    expect(html).not.toContain("QA GENERAL");
    expect(html).not.toContain("ENGINEER ON B");
    expect(html).not.toContain("Redeploy HookWatch");
  });

  it("and a door with only a role still resumes the role's own conversation", async () => {
    const html = await mount({ roleName: "Qa" });
    expect(html).toContain("QA GENERAL: the release checklist");
    expect(html).not.toContain("QA ON B");
  });
});
