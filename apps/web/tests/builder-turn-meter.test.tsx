// @vitest-environment happy-dom
/**
 * Builder turn meter (BOSSAPP-1246): elapsed work, settled receipts only,
 * and a credit check-in before more spend. A decline does not reserve.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";
import { dollarsToMicros, formatSettled, TurnMeter, type TurnMeterView } from "../app/builder/turn-meter";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

function view(over: Partial<TurnMeterView> = {}): TurnMeterView {
  return {
    turn_id: "trn_plate",
    project_id: "p1",
    status: "working",
    elapsed_ms: 90_000,
    settled_usd_micros: null,
    settled_known: false,
    error: null,
    retry: false,
    checkin: null,
    ...over,
  };
}

function tipWords(root: Element): string[] {
  const bad: string[] = [];
  for (const el of root.querySelectorAll("[title]")) {
    const words = (el.getAttribute("title") || "").trim().split(/\s+/).filter(Boolean);
    if (words.length > 14) bad.push(el.getAttribute("title") || "");
  }
  return bad;
}

function stubFetch(pages: TurnMeterView[]) {
  const calls: { url: string; body: Record<string, unknown> | null }[] = [];
  const queue = [...pages];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : null;
    calls.push({ url: String(url), body });
    const next = queue.shift() ?? pages[pages.length - 1];
    const requestKey = body && typeof body.request_key === "string" ? body.request_key : "";
    const payload = next.checkin && requestKey
      ? { ...next, checkin: { ...next.checkin, request_key: requestKey } }
      : next;
    return { ok: payload.status !== "failed", json: async () => payload };
  }));
  return calls;
}

describe("dollars from the person, not a guessed figure", () => {
  it("parses a typed dollar amount into micros", () => {
    expect(dollarsToMicros("0.25")).toBe(250_000);
    expect(dollarsToMicros("1")).toBe(1_000_000);
    expect(dollarsToMicros("0")).toBeNull();
    expect(dollarsToMicros("")).toBeNull();
    expect(formatSettled(null)).toBe("Not settled");
    expect(formatSettled(250_000)).toBe("$0.250000");
  });
});

describe("TurnMeter", () => {
  it("shows elapsed work and refuses to invent settled spend", async () => {
    stubFetch([view()]);
    await mount(<TurnMeter projectId="p1" turnId="trn_plate" />);
    expect($("[data-testid=elapsed-figure]")!.textContent).toBe("01:30");
    expect($("[data-testid=settled-figure]")!.textContent).toBe("Not settled");
    expect($("[data-testid=turn-meter]")!.textContent).not.toMatch(/\$0/);
    expect(overBudget($("[data-testid=turn-meter]")!)).toEqual([]);
    expect(tipWords($("[data-testid=turn-meter]")!)).toEqual([]);
  });

  it("prints the receipt figure when one exists", async () => {
    stubFetch([view({ settled_usd_micros: 250_000, settled_known: true })]);
    await mount(<TurnMeter projectId="p1" turnId="trn_plate" />);
    expect($("[data-testid=settled-figure]")!.textContent).toBe("$0.250000");
    expect($("[data-testid=settled-stub]")!.getAttribute("data-known")).toBe("true");
  });

  it("asks with the typed amount, then a decline does not reserve", async () => {
    const paused = view({
      status: "paused_for_credit",
      checkin: {
        id: "chk_1", request_key: "ask-250000", dedupe_key: "turn-meter:p1:trn_plate:ask-250000",
        amount_usd_micros: 250_000, status: "pending", reservation_id: null, receipt_id: null,
        error: null, deadletter_id: null,
      },
    });
    const declined = view({
      status: "declined",
      checkin: { ...paused.checkin!, status: "declined", reservation_id: null },
    });
    const calls = stubFetch([view(), paused, declined]);
    await mount(<TurnMeter projectId="p1" turnId="trn_plate" />);
    const input = $("[data-testid=credit-amount]") as HTMLInputElement;
    input.value = "0.25";
    await click($("[data-testid=ask-credit]"));
    expect(calls[1].body).toMatchObject({ project_id: "p1", amount_usd_micros: 250_000 });
    const key = String(calls[1].body?.request_key || "");
    expect(key.startsWith("ask-250000-")).toBe(true);
    expect($("[data-testid=allow-credit]")).toBeTruthy();
    expect($("[data-testid=asked-figure]")!.textContent).toContain("$0.250000");
    await click($("[data-testid=stop-credit]"));
    expect(calls[2].body).toMatchObject({ request_key: key, decision: "decline" });
    expect($("[data-testid=turn-status]")!.textContent).toBe("Stopped");
    expect($("[data-testid=settled-figure]")!.textContent).toBe("Not settled");
    expect($("[data-testid=held-figure]")).toBeNull();
  });

  it("allow posts approve for the pending check-in", async () => {
    const paused = view({
      status: "paused_for_credit",
      checkin: {
        id: "chk_2", request_key: "slice-yes", dedupe_key: "k", amount_usd_micros: 1_500_000,
        status: "pending", reservation_id: null, receipt_id: null, error: null, deadletter_id: null,
      },
    });
    const allowed = view({
      status: "working",
      checkin: {
        ...paused.checkin!, status: "approved", reservation_id: "res_1", receipt_id: "rcpt_1",
      },
    });
    const calls = stubFetch([paused, allowed]);
    await mount(<TurnMeter projectId="p1" turnId="trn_plate" />);
    await click($("[data-testid=allow-credit]"));
    expect(calls[1].body).toMatchObject({ request_key: "slice-yes", decision: "approve" });
    expect($("[data-testid=held-figure]")!.textContent).toContain("$1.500000");
    expect($("[data-testid=settled-figure]")!.textContent).toBe("Not settled");
  });

  it("shows a long refusal as a paragraph", async () => {
    const sentence = "Budget cap exceeded and this turn cannot reserve more credit today";
    stubFetch([view({ status: "failed", error: sentence, checkin: null })]);
    await mount(<TurnMeter projectId="p1" turnId="trn_plate" />);
    await settle();
    const fault = $("p[data-testid=turn-error]")!;
    expect(fault.textContent).toBe(sentence);
    expect(fault.closest("button")).toBeNull();
  });
});
