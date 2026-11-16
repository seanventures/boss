// @vitest-environment happy-dom
/**
 * BOSSAPP-1270: a builder writes the sentence. The screen shows the saved rule
 * and the attempt result. It does not decide the action itself.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LiveRules, type AttemptResult, type LiveRule } from "../app/builder/live-rules";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

function words(value: string): string[] {
  return value.replace(/[·▸•|]/g, " ").trim().split(/\s+/).filter(Boolean);
}

function overBudget(scope: ParentNode): string[] {
  const skip = "textarea, input, script, style, [data-user-text], [aria-hidden='true']";
  const out: string[] = [];
  const elements = scope instanceof Element ? [scope, ...scope.querySelectorAll("*")] : [...scope.querySelectorAll("*")];
  for (const el of elements) {
    if (!(el instanceof Element) || el.closest(skip)) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("");
    if (words(own).length > 6 && el.tagName !== "P") out.push(own.trim());
  }
  return out;
}

function longTitles(scope: ParentNode): string[] {
  return [...scope.querySelectorAll("[title]")].flatMap((el) => {
    const title = el.getAttribute("title") ?? "";
    return words(title).length > 14 ? [title] : [];
  });
}

async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(node); });
  return host;
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null;
  host = null;
  vi.unstubAllGlobals();
});

function typeInto(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  if (!setter) throw new Error("no value setter");
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function rule(over: Partial<LiveRule> = {}): LiveRule {
  return {
    id: "lar_1",
    app_key: "storefront",
    description: "block refund when amount is over 50",
    effect: "block",
    action_pattern: "refund",
    predicate: { field: "amount", op: "gt", value: 50 },
    position: 100,
    ...over,
  };
}

describe("the builder records a sentence and sees the saved rule", () => {
  it("shows the saved effect, not its own reading of the sentence", async () => {
    const sent: string[] = [];
    const el = await render(<LiveRules projectId="p1" actor="ada" transport={{
      list: async () => ({ rules: [] }),
      record: async (body) => {
        sent.push(body.description);
        return { rule: rule({ description: body.description, effect: "block", action_pattern: "refund" }) };
      },
      attempt: async () => { throw new Error("not this test"); },
    }} />);
    expect(el.querySelector("[data-testid='lr-empty']")?.textContent).toBe("No rule yet.");
    const sentence = "allow refund when amount is under 50";
    await act(async () => { typeInto(el.querySelector("textarea")!, sentence); });
    await act(async () => { el.querySelector<HTMLButtonElement>("[data-testid='lr-record']")!.click(); });
    expect(sent).toEqual([sentence]);
    expect(el.querySelector("[data-testid='lr-effect']")?.textContent).toBe("BLOCK");
    expect(el.querySelector("[data-testid='lr-action']")?.textContent).toBe("refund");
    expect(overBudget(el)).toEqual([]);
    expect(longTitles(el)).toEqual([]);
  });

  it("says a blocked action did not run and an allowed one names the decision", async () => {
    const attempts: { action: string; payload: Record<string, string | number> }[] = [];
    const el = await render(<LiveRules projectId="p1" actor="ada" initialRules={[rule()]} transport={{
      list: async () => ({ rules: [rule()] }),
      record: async () => { throw new Error("not this test"); },
      attempt: async (body) => {
        attempts.push({ action: body.action, payload: body.payload });
        if (body.action === "refund") {
          const blocked: AttemptResult = {
            decision: "block", ran: false, rule_id: "lar_1", decision_id: null,
            action: "refund", result: null,
          };
          return blocked;
        }
        return {
          decision: "allow", ran: true, rule_id: "lar_2", decision_id: "lad_9",
          action: body.action, result: { status: "ran" },
        };
      },
    }} />);
    await act(async () => {
      typeInto(el.querySelector<HTMLInputElement>("[data-testid='lr-try-action']")!, "refund");
      typeInto(el.querySelector<HTMLInputElement>("[data-testid='lr-try-field']")!, "amount");
      typeInto(el.querySelector<HTMLInputElement>("[data-testid='lr-try-value']")!, "80");
    });
    await act(async () => { el.querySelector<HTMLButtonElement>("[data-testid='lr-try']")!.click(); });
    expect(attempts).toEqual([{ action: "refund", payload: { amount: 80 } }]);
    expect(el.querySelector("[data-testid='lr-result']")?.textContent).toContain("Did not run.");
    expect(el.querySelector("[data-testid='lr-decision']")).toBeNull();

    await act(async () => {
      typeInto(el.querySelector<HTMLInputElement>("[data-testid='lr-try-action']")!, "checkout");
      typeInto(el.querySelector<HTMLInputElement>("[data-testid='lr-try-field']")!, "");
      typeInto(el.querySelector<HTMLInputElement>("[data-testid='lr-try-value']")!, "");
    });
    await act(async () => { el.querySelector<HTMLButtonElement>("[data-testid='lr-try']")!.click(); });
    expect(el.querySelector("[data-testid='lr-result']")?.textContent).toContain("Ran.");
    expect(el.querySelector("[data-testid='lr-decision']")?.textContent).toContain("lad_9");
    expect(overBudget(el)).toEqual([]);
    expect(longTitles(el)).toEqual([]);
  });

  it("shows a failed record instead of dropping it", async () => {
    const el = await render(<LiveRules projectId="p1" actor="ada" transport={{
      list: async () => ({ rules: [] }),
      record: async () => { throw new Error("Write allow or block, then the action."); },
      attempt: async () => { throw new Error("not this test"); },
    }} />);
    await act(async () => { typeInto(el.querySelector("textarea")!, "refunds maybe"); });
    await act(async () => { el.querySelector<HTMLButtonElement>("[data-testid='lr-record']")!.click(); });
    const alert = el.querySelector("[data-testid='lr-error']");
    expect(alert?.tagName).toBe("P");
    expect(alert?.textContent).toContain("Write allow or block, then the action.");
  });

  it("posts the sentence to the rule route and does not call a model", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      urls.push(String(url));
      if (String(url).includes("/v1/live-action-rules?") && (!init?.method || init.method === "GET")) {
        return new Response(JSON.stringify({ rules: [] }), { status: 200 });
      }
      if (init?.method === "POST" && String(url).endsWith("/v1/live-action-rules")) {
        const body = JSON.parse(String(init.body));
        return new Response(JSON.stringify({ rule: rule({ description: body.description }) }), { status: 201 });
      }
      if (String(url).endsWith("/v1/live-action-rules/attempt")) {
        return new Response(JSON.stringify({
          decision: "block", ran: false, rule_id: "lar_1", decision_id: null,
          action: "refund", result: null,
        }), { status: 200 });
      }
      return new Response("missing", { status: 500 });
    }));
    const el = await render(<LiveRules projectId="p1" actor="ada" />);
    await act(async () => { typeInto(el.querySelector("textarea")!, "block refund"); });
    await act(async () => { el.querySelector<HTMLButtonElement>("[data-testid='lr-record']")!.click(); });
    await act(async () => {
      typeInto(el.querySelector<HTMLInputElement>("[data-testid='lr-try-action']")!, "refund");
    });
    await act(async () => { el.querySelector<HTMLButtonElement>("[data-testid='lr-try']")!.click(); });
    expect(urls.some((url) => url.includes("/v1/live-action-rules"))).toBe(true);
    expect(urls.some((url) => url.includes("/attempt"))).toBe(true);
    expect(urls.join(" ")).not.toMatch(/openai|anthropic|model/i);
    expect(el.querySelector("[data-testid='lr-result']")?.textContent).toContain("Did not run.");
  });
});
