// @vitest-environment happy-dom
/**
 * The Ask dock has the agent in it (spec §3.10, S-runs acceptance).
 *
 *  - Its head is the agent: face, name, the AI badge, and what the conversation is about.
 *  - The face shows `thinking` while a turn is in flight and `speaking` when its words arrive,
 *    then settles — from the turn's real state, not a timer started on send.
 *  - The turn carries the agent's role KEY and the work it is about (`context.act_id`).
 *  - Work the answer cites is a chip, `I ran this, 14:02 `, that opens its Act card.
 *  - "Not about this work" drops the context for the next turn.
 *
 * The real panel is mounted; the server action is faked and holds the turn until released.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { wireAct } from "./_helpers/run-acts-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const sent: Array<Record<string, string>> = [];
let release: (() => void) | null = null;
let threadBeforeTurn = true;
const CITED = wireAct({ act_id: "act_cited", kind: "step", role_key: "qa", started_at: "2026-10-03T14:02:00Z",
                        label: "Test · Ship it #182", step_id: "test" });
const ABOUT = wireAct({ act_id: "act_about", kind: "step", role_key: "qa", label: "Build · Ship it #182", step_id: "build" });

vi.mock("../app/actions", () => ({
  askBossAction: vi.fn(async (_prev: unknown, fd: FormData) => {
    const fields: Record<string, string> = {};
    fd.forEach((v, k) => { fields[k] = String(v); });
    sent.push(fields);
    await new Promise<void>((r) => { release = r; });
    return { ok: true, data: {
      thread_id: "athr_1", act_id: "act_turn", cited_acts: ["act_cited"],
      messages: [{ role: "user", content: fields.message },
                 { role: "assistant", content: "The tests ran green at 14:02; I ran them myself." }],
      proposed_actions: [], tool_calls: [], next_steps: [],
    } };
  }),
  runAgentActionAction: vi.fn(async () => ({ ok: true, data: {} })),
}));

const { AgentChat } = await import("../app/agent-ui");
const { SPEAK_DECAY_MS } = await import("../app/agents/dock-agent");

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  sent.length = 0;
  release = null;
  threadBeforeTurn = true;
  window.history.replaceState(null, "", "/?dock_act=act_about");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = new URL(String(input), "http://console.test");
    if (url.pathname.endsWith("/api/agent-thread")) return Response.json(threadBeforeTurn ? { thread_id: "athr_1" } : {});
    if (url.pathname === "/api/run-acts") {
      const id = url.searchParams.get("act_id");
      return Response.json(id === "act_cited" ? CITED : ABOUT);
    }
    if (url.searchParams.get("view") === "workspace") return Response.json({ tasks: [], threads: [] });
    return Response.json({});
  }));
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

async function settle(n = 20) {
  for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
const head = () => host!.querySelector('[data-testid="ag-dock-head"]')!;
const headState = () => head().querySelector(".ag-face")?.getAttribute("data-state");

async function mount(props: Record<string, unknown> = {}) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <AgentChat projectId="p1" actor="ceo" roleName="QA" roleKey="qa" actId="act_about"
               initialTaskId="workflow_step:wfr_1:build" initialMessage="Did the tests pass?" {...(props as object)} />));
  await settle();
}

describe("the dock, with the agent in it", () => {
  it("heads the panel with the agent: face, name, AI, and what it is about", async () => {
    await mount();
    // One identity everywhere: the agent's callsign, the same name its chip and Passport show.
    expect(head().querySelector('[data-testid="ag-dock-name"]')?.textContent).toBe("Vera");
    expect(head().querySelector(".ag-face")?.getAttribute("data-role-key")).toBe("qa");
    expect(head().querySelector(".ag-face")?.getAttribute("role")).toBe("img");
    expect(head().querySelector('[data-testid="ag-ai-badge"]')?.textContent).toBe("AI");
    expect(head().querySelector('[data-testid="ag-dock-about"]')?.textContent).toContain("About: Build");
    expect(headState()).toBe("idle");
  });

  it("thinks while the turn is in flight, speaks when the words arrive, then settles", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] , shouldAdvanceTime: true });
    try {
      await mount();
      await act(async () => (host!.querySelector('[data-testid="agent-send"]') as HTMLButtonElement).click());
      await settle();
      expect(headState()).toBe("thinking");
      expect(head().querySelector('[data-testid="ag-dock-state"]')?.textContent).toBe("Thinking");
      await act(async () => { release?.(); });
      await settle(5);
      expect(headState()).toBe("speaking");
      await act(async () => { vi.advanceTimersByTime(SPEAK_DECAY_MS + 50); });
      await settle(5);
      expect(headState()).toBe("idle");
    } finally {
      vi.useRealTimers();
    }
  });

  it("sends the agent by key, and the work the conversation is about", async () => {
    await mount();
    await act(async () => (host!.querySelector('[data-testid="agent-send"]') as HTMLButtonElement).click());
    await settle();
    expect(sent[0].role_key).toBe("qa");
    expect(sent[0].context_act_id).toBe("act_about");
    expect(sent[0].role).toBe("QA");
    await act(async () => { release?.(); });
    await settle();
  });

  it("answers with its face in the gutter and its cited work as a chip that opens the card", async () => {
    await mount();
    await act(async () => (host!.querySelector('[data-testid="agent-send"]') as HTMLButtonElement).click());
    await settle();
    await act(async () => { release?.(); });
    await settle(30);
    const answer = [...host!.querySelectorAll('[data-testid="agent-message-assistant"]')].pop()!;
    expect(answer.querySelector('[data-testid="ag-dock-gutter"] .ag-face')?.getAttribute("data-role-key")).toBe("qa");
    const chip = answer.querySelector('[data-testid="ag-cite"]')!;
    expect(chip.getAttribute("data-act-id")).toBe("act_cited");
    expect(chip.querySelector(".ag-cite-text")?.textContent).toBe("I ran this, 14:02 ");
    await act(async () => (chip.querySelector('[data-testid="ag-act-face"]') as HTMLButtonElement).click());
    expect(document.querySelector('[role="dialog"] [data-testid="ag-act-card"]')?.getAttribute("data-act-id")).toBe("act_cited");
  });

  it("keeps the cited chip on the first answer of a brand-new conversation", async () => {
    // No thread before the turn: the reply is what brings the thread id, with its citations.
    threadBeforeTurn = false;
    await mount({ initialTaskId: undefined });
    await act(async () => (host!.querySelector('[data-testid="agent-send"]') as HTMLButtonElement).click());
    await settle();
    expect(sent[0].thread_id).toBeUndefined();
    await act(async () => { release?.(); });
    await settle(30);
    const answer = [...host!.querySelectorAll('[data-testid="agent-message-assistant"]')].pop()!;
    expect(answer.querySelector('[data-testid="ag-cite"]')?.getAttribute("data-act-id")).toBe("act_cited");
  });

  it("sends no role key it had to guess from a display name", async () => {
    await mount({ roleName: "QA Lead", roleKey: undefined, actId: undefined });
    expect(head().querySelector('[data-testid="ag-dock-name"]')?.textContent).toBe("Vera");
    await act(async () => (host!.querySelector('[data-testid="agent-send"]') as HTMLButtonElement).click());
    await settle();
    expect(sent[0].role).toBe("QA Lead");
    // Resolved through the kit's one namespace (the seat title "QA Lead" is the agent `qa`),
    // never made key-shaped from the name (`qa_lead` is nobody).
    expect(sent[0].role_key).toBe("qa");
    await act(async () => { release?.(); });
    await settle();
  });

  it("drops the work from the next turn when told it is not about that", async () => {
    await mount();
    await act(async () => (host!.querySelector('[data-testid="ag-dock-about-clear"]') as HTMLButtonElement).click());
    expect(host!.querySelector('[data-testid="ag-dock-about"]')).toBeNull();
    expect(new URL(window.location.href).searchParams.get("dock_act")).toBeNull();
    await act(async () => (host!.querySelector('[data-testid="agent-send"]') as HTMLButtonElement).click());
    await settle();
    expect(sent[0].context_act_id).toBeUndefined();
    await act(async () => { release?.(); });
    await settle();
  });

  it("is BOSS's own dock, unchanged, when no agent is addressed", async () => {
    await mount({ roleName: undefined, roleKey: undefined, actId: undefined });
    expect(host!.querySelector('[data-testid="ag-dock-head"]')).toBeNull();
  });
});
