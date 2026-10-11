// @vitest-environment happy-dom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mapEvent, mapTurn, readEvents, useConversation } from "../lib/agent-converse";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const unknown = [
  ["missing", undefined], ["null", null], ["numeric text", "0"], ["blank", ""],
  ["boolean", false], ["object", {}], ["negative", -0.01], ["NaN", NaN],
  ["positive infinity", Infinity], ["negative infinity", -Infinity],
] as const;
const turn = (cost: unknown) => ({ turn_id: "turn-owned", status: "complete",
  ...(cost === undefined ? {} : { cost_usd: cost }) });

describe("canonical conversation cost mapping", () => {
  it.each(unknown)("keeps a recorded %s cost unknown", (_label, cost) => {
    expect(mapTurn(turn(cost)).costUsd).toBeNull();
  });
  it.each(unknown)("keeps a terminal %s cost unknown", (_label, cost) => {
    expect(mapEvent("turn_end", turn(cost))).toMatchObject({ type: "turn_end", costUsd: null });
  });
  it.each([0, 0.0042, 3.75])("preserves a recorded numeric cost of %s", (cost) => {
    expect(mapTurn(turn(cost)).costUsd).toBe(cost);
    expect(mapEvent("turn_end", turn(cost))).toMatchObject({ costUsd: cost });
  });
  it("keeps unknown, confirmed zero and paid terminal costs distinct through split SSE bytes", async () => {
    const encoder = new TextEncoder();
    const text = [null, 0, 0.03, "0", -1].map((cost, i) =>
      `event: turn_end\ndata: ${JSON.stringify({ ...turn(cost), turn_id: `t-${i}` })}\n\n`).join("");
    const bytes = encoder.encode(text);
    const body = new ReadableStream<Uint8Array>({ start(c) {
      for (let offset = 0; offset < bytes.length; offset += 7) c.enqueue(bytes.slice(offset, offset + 7));
      c.close();
    } });
    const costs = [];
    for await (const event of readEvents(body)) if (event.type === "turn_end") costs.push(event.costUsd);
    expect(costs).toEqual([null, 0, 0.03, null, null]);
  });
});

let root: Root | undefined;
let host: HTMLElement | undefined;
afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  host?.remove();
  host = undefined;
  vi.unstubAllGlobals();
});
async function flush() {
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
describe("mounted conversation cost lifecycle", () => {
  it.each([
    { settled: null, status: "error" }, { settled: null, status: "complete" },
    { settled: 0, status: "complete" }, { settled: 0.025, status: "complete" },
  ])("starts unknown and applies the canonical terminal record $status/$settled", async ({ settled, status }) => {
    let current!: ReturnType<typeof useConversation>;
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const encoder = new TextEncoder();
    const recorded = {
      conversation_id: "conversation-owned", kind: "talk", seats: ["qa"], lead_role_key: "qa",
      title: "Bounded test", status: "open", turns: [] as Record<string, unknown>[],
    };
    vi.stubGlobal("fetch", vi.fn(async (_url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") {
        const body = new ReadableStream<Uint8Array>({ start(c) { controller = c; } });
        init.signal?.addEventListener("abort", () => {
          try { controller.error(new DOMException("Aborted", "AbortError")); } catch { /* already closed */ }
        });
        return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
      }
      return Response.json(recorded);
    }));
    function Probe() {
      current = useConversation("project-owned", "conversation-owned", "owner-owned");
      return null;
    }
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root!.render(React.createElement(Probe)); });
    await flush();
    await act(async () => { void current.send("Review one bounded change."); });
    await flush();
    const push = async (event: string, data: unknown) => {
      await act(async () => { controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)); });
      await flush();
    };
    await push("turn_start", { turn_id: "agent-owned", seq: 2, speaker_kind: "agent", role_key: "qa" });
    const agent = () => current.conversation?.turns.find((t) => t.turnId === "agent-owned");
    expect(agent()).toMatchObject({ status: "streaming", costUsd: null });
    await push("token", { turn_id: "agent-owned", text: "Recorded answer" });
    expect(agent()).toMatchObject({ status: "streaming", content: "Recorded answer", costUsd: null });
    await push("turn_end", { turn_id: "agent-owned", status, cost_usd: settled });
    expect(agent()).toMatchObject({ status, costUsd: settled });
    recorded.turns = [{ turn_id: "agent-owned", speaker_kind: "agent", status, cost_usd: settled }];
    await act(async () => { controller.close(); });
    await flush();
    expect(agent()?.costUsd).toBe(settled);
    expect(current.busy).toBe(false);
  });
});
