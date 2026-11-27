// @vitest-environment happy-dom
/**
 * BOSSAPP-1237: Staged Ask-dock proposals survive dock close/reopen (including spaced role
 * names like "Product Manager" -> "product_manager") and arrive when a stalled turn lands via
 * the `landing` poller.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { mountAs, settle, stubFetch, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as Record<string, unknown>).React = React;
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

const askMock = vi.fn();
vi.mock("../app/actions", () => ({
  runAgentActionAction: vi.fn(async () => ({ ok: true, data: { ok: true } })),
  askBossAction: (...args: unknown[]) => askMock(...args),
}));

import { AgentChat } from "../app/agent-ui";

const OFFER = {
  id: "pa_decide_1",
  tool: "decide",
  label: "Revise decision",
  args: { decision_id: "dec_1", action: "revise", note: "Handle weekends." },
};

afterEach(() => {
  unmount();
  askMock.mockReset();
  vi.useRealTimers();
});

describe("Ask-dock staged proposals on reopen and landing arrival", () => {
  it("normalizes spaced roleName in threadFor so reopening the dock restores staged proposals", async () => {
    const setupFetch = () =>
      stubFetch((c: Call) => {
        if (c.path !== "/api/agent") return { body: {} };
        const view = new URLSearchParams(c.search).get("view");
        if (view === "workspace") {
          return {
            body: {
              tasks: [],
              threads: [
                {
                  thread_id: "th_pm_1",
                  external_ref: "crun_123",
                  agent_role: "product_manager",
                  title: "SLA timer",
                  message_count: 3,
                },
              ],
            },
          };
        }
        if (view === "messages") {
          return {
            body: {
              thread_id: "th_pm_1",
              messages: [
                { role: "user", content: "Revise the merge decision." },
                { role: "assistant", content: "Staged Revise decision." },
                { role: "assistant", content: "Not run: needs your consent first." },
              ],
              proposed_actions: [OFFER],
              next_steps: [],
              working: false,
            },
          };
        }
        return { body: {} };
      });

    // Mount, unmount (close dock), then remount (reopen dock).
    setupFetch();
    await mountAs(
      <AgentChat projectId="prj_desk" actor="ceo" initialTaskId="crun_123" roleName="Product Manager" />,
      "owner",
    );
    for (let i = 0; i < 4; i += 1) await settle();
    expect(document.querySelector("[data-testid='agent-proposal-decide']")).not.toBeNull();

    unmount();
    setupFetch();

    await mountAs(
      <AgentChat projectId="prj_desk" actor="ceo" initialTaskId="crun_123" roleName="Product Manager" />,
      "owner",
    );
    for (let i = 0; i < 4; i += 1) await settle();
    const btn = document.querySelector("[data-testid='agent-proposal-decide']") as HTMLButtonElement | null;
    expect(btn, "staged proposal restored on dock reopen").not.toBeNull();
    expect(btn?.textContent).toContain("Revise decision");
  });

  it("syncs proposed_actions, next_steps, and pending_confirmation when landing poller sees the answer", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    let pollCount = 0;
    stubFetch((c: Call) => {
      if (c.path !== "/api/agent") return { body: {} };
      const view = new URLSearchParams(c.search).get("view");
      if (view === "workspace") {
        return {
          body: {
            tasks: [],
            threads: [{ thread_id: "th_land", title: "Merge", message_count: 1 }],
          },
        };
      }
      if (view === "messages") {
        pollCount += 1;
        if (pollCount === 1) {
          return {
            body: {
              thread_id: "th_land",
              messages: [{ role: "user", content: "Revise the decision." }],
              proposed_actions: [],
              next_steps: [],
              working: true,
              turn: { turn_id: "aturn_1", status: "running" },
            },
          };
        }
        return {
          body: {
            thread_id: "th_land",
            messages: [
              { role: "user", content: "Revise the decision." },
              { role: "assistant", content: "I staged Revise decision for you." },
            ],
            proposed_actions: [OFFER],
            next_steps: [{ id: "ns_1", label: "Check PR", message: "Check the PR status." }],
            pending_confirmation: null,
            working: false,
            turn: null,
          },
        };
      }
      return { body: {} };
    });

    await mountAs(<AgentChat projectId="prj_desk" actor="ceo" />, "owner");
    for (let i = 0; i < 4; i += 1) await settle();
    expect(document.querySelector("[data-testid='agent-proposal-decide']")).toBeNull();

    // Advance past the 4000ms landing poll interval, then let fetch + React state settle.
    await React.act(async () => {
      await vi.advanceTimersByTimeAsync(4100);
    });
    for (let i = 0; i < 4; i += 1) await settle();
    const btn = document.querySelector("[data-testid='agent-proposal-decide']") as HTMLButtonElement | null;
    expect(btn, "landing poller synced proposed_actions").not.toBeNull();
    expect(btn?.textContent).toContain("Revise decision");
    expect(document.body.textContent).toContain("Check PR");
  });
});
