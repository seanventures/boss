import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api-write", () => ({ apiWrite: vi.fn() }));

import { apiWrite } from "../lib/api-write";
import { readSse, runPersonaTest } from "../lib/agent-world";

afterEach(() => vi.resetAllMocks());

describe("voice stream admission and uncertain outcomes", () => {
  it("ignores the admission comment while waiting for actual model data", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start(c) { controller = c; } });
    const events = readSse(new Response(body));
    let delivered = false;
    const pending = events.next().then((event) => { delivered = true; return event; });
    const encoder = new TextEncoder();
    controller.enqueue(encoder.encode(": voice intent admitted\n"));
    controller.enqueue(encoder.encode("\n"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(delivered).toBe(false);
    controller.enqueue(encoder.encode('event: token\ndata: {"text":"actual reply"}\n\n'));
    expect(await pending).toEqual({ done: false, value: { event: "token", data: { text: "actual reply" } } });
    controller.close();
    expect(await events.next()).toEqual({ done: true, value: undefined });
  });

  it("keeps the returned score and exposes an unknown judgement without another write", async () => {
    const headline = "The judgement outcome is unknown. Reconcile its recorded receipt before retrying.";
    vi.mocked(apiWrite).mockResolvedValue(new Response([
      ": voice intent admitted\n\n",
      'event: token\ndata: {"text":"actual reply"}\n\n',
      'event: result\ndata: {"voice":3,"in_remit":true,"says_ai":false,"fingerprint":"recorded","spend_status":"needs_reconciliation"}\n\n',
      `event: error\ndata: ${JSON.stringify({ error: { kind: "outcome_unknown", headline, retry: false } })}\n\n`,
    ].join(""), { headers: { "Content-Type": "text/event-stream" } }));
    const events = [];
    for await (const event of runPersonaTest("actual-project", "actual-viewer", "qa", "intro")) events.push(event);
    expect(events.map((event) => event.type)).toEqual(["token", "result", "error"]);
    expect(events[1]).toEqual({ type: "result", result: { voice: 3, inRemit: true, saysAi: false, fingerprint: "recorded" } });
    expect(events[2]).toEqual({ type: "error", message: headline, code: "outcome_unknown" });
    expect(apiWrite).toHaveBeenCalledTimes(1);
  });
});
