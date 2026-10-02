// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The dock shows the first answer as soon as it lands, and offers "Investigate further" and
 * "Do the fix" — which are the only way the long investigating loop starts.
 *
 * boss-1, 2026-10-02: "Ask the Qa" on a failed Preview step (Vite: Could not resolve entry module
 * "index.html") ran the multi-step loop for 347 s and answered "Budget spent; answering from
 * evidence gathered…". The owner: "these things should be quick … very annoying". A question
 * about a record is now answered in one call from the record (engine subject_answer.py); the
 * turn returns `next_steps`, and pressing one sends its message with depth="investigate".
 *
 * Mounts the real panel; the server action is faked and every FormData it receives is kept.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const sent: Array<Record<string, string>> = [];
const SUBJECT = "workflow_step:wfr_475bf68eb18f485b8c52:preview";
const QUESTION = "Why is it in this state, and what is the fix?";
const FIRST = "Cause: the Vite build found no index.html.\nFix: add index.html at the app root.";
const NEXT = [
  { id: "investigate", label: "Investigate further", depth: "investigate",
    message: "Investigate further: check what that answer could not confirm." },
  { id: "do_fix", label: "Do the fix", depth: "investigate",
    message: "Do the fix you recommended." },
];

vi.mock("../app/actions", () => ({
  askBossAction: vi.fn(async (_prev: unknown, fd: FormData) => {
    const fields: Record<string, string> = {};
    fd.forEach((v, k) => { fields[k] = String(v); });
    sent.push(fields);
    const first = sent.length === 1;
    return { ok: true, data: {
      thread_id: "athr_1",
      messages: [{ role: "user", content: fields.message },
                 { role: "assistant", content: first ? FIRST : "The loop's deeper answer." }],
      proposed_actions: [], tool_calls: [],
      next_steps: first ? NEXT : [],
    } };
  }),
  runAgentActionAction: vi.fn(async () => ({ ok: true, data: {} })),
}));

const { AgentChat, nextStepsOf } = await import("../app/agent-ui");

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  sent.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = new URL(String(input), "http://console.test");
    if (url.pathname.endsWith("/api/agent-thread")) return Response.json({ thread_id: "athr_1" });
    if (url.searchParams.get("view") === "workspace") return Response.json({ tasks: [], threads: [] });
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
  for (let i = 0; i < 30; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function askAboutTheStep() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <AgentChat projectId="p1" actor="ceo" initialTaskId={SUBJECT} roleName="Qa"
               initialMessage={QUESTION} />));
  await settle();
  const send = host.querySelector('[data-testid="agent-send"]') as HTMLButtonElement;
  await act(async () => { send.click(); });
  await settle();
  return host;
}

describe("a question about a record", () => {
  it("is sent without a depth, so the server answers first in one call", async () => {
    await askAboutTheStep();
    expect(sent).toHaveLength(1);
    expect(sent[0].message).toBe(QUESTION);
    expect(sent[0].external_ref).toBe(SUBJECT);
    expect(sent[0].depth).toBeUndefined();
  });

  it("shows the answer and offers Investigate further and Do the fix", async () => {
    const el = await askAboutTheStep();
    expect(el.innerHTML).toContain("the Vite build found no index.html");
    const offered = [...el.querySelectorAll('[data-testid="agent-next-steps"] button')]
      .map((b) => b.textContent);
    expect(offered).toEqual(["Investigate further", "Do the fix"]);
  });

  it("starts the investigating loop only when Investigate further is pressed", async () => {
    const el = await askAboutTheStep();
    const button = el.querySelector('[data-testid="agent-next-investigate"]') as HTMLButtonElement;
    await act(async () => { button.click(); });
    await settle();
    expect(sent).toHaveLength(2);
    expect(sent[1].depth).toBe("investigate");
    expect(sent[1].message).toBe(NEXT[0].message);
    expect(sent[1].external_ref).toBe(SUBJECT);
    // The offer belongs to the answer it came from; the deeper answer carries none.
    expect(el.innerHTML).toContain("The loop's deeper answer.");
    expect(el.querySelector('[data-testid="agent-next-steps"]')).toBeNull();
  });
});

describe("nextStepsOf", () => {
  it("keeps only well-formed steps and tolerates a turn without any", () => {
    expect(nextStepsOf({ next_steps: [NEXT[0], { label: 3 }, null] })).toEqual([NEXT[0]]);
    expect(nextStepsOf({})).toEqual([]);
    expect(nextStepsOf(null)).toEqual([]);
  });
});
