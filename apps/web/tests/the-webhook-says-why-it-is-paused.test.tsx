import { readFileSync } from "node:fs";
import { join } from "node:path";

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * A disabled webhook already says "Paused: renew authority" and hides last_error on the title.
 * That error has to be readable beside the pause line. Blank and whitespace are nothing.
 * A trimmed error is the sentence. More than six words is a paragraph. The pause line and
 * its title stay. The load failure ("Couldn't load webhooks") is not this sentence.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const triggers = await import("../app/workflows/editor/inspector/webhook-triggers");

const webhookPauseSentence = (triggers as {
  webhookPauseSentence?: (error: string | null | undefined) => string | null;
}).webhookPauseSentence!;

const WebhookPauseWhy = (triggers as {
  WebhookPauseWhy?: (props: { sentence: string | null }) => React.ReactNode;
}).WebhookPauseWhy!;

const { WebhookTriggers } = triggers;

const LONG = "the standing grant ended yesterday morning early";
const PADDED = `  ${LONG}  `;
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "grant ended";

let root: Root | null = null;
let host: HTMLElement | null = null;

function clearHost() {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.unstubAllGlobals();
}

afterEach(() => {
  clearHost();
});

function mountWhy(sentence: string | null): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => { root!.render(<WebhookPauseWhy sentence={sentence} />); });
  return host;
}

function hook(id: string, enabled: boolean, lastError: string | null) {
  return {
    id, workflow_key: "orders", name: id, path: `/v1/webhooks/inbound/workflow/${id}`,
    enabled, last_error: lastError,
  };
}

async function mountScreen(webhooks: ReturnType<typeof hook>[]) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("view=webhooks")) {
      return new Response(JSON.stringify({ webhooks }), {
        status: 200, headers: { "content-type": "application/json" },
      });
    }
    return new Response(JSON.stringify({ error: "unexpected" }), {
      status: 500, headers: { "content-type": "application/json" },
    });
  }));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<WebhookTriggers projectId="p1" workflowKey="orders" actor="ceo" />);
  });
  for (let i = 0; i < 8; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
  return host;
}

function row(el: HTMLElement, id: string): HTMLElement {
  const found = el.querySelector(`[data-testid="wf-webhook-${id}"]`);
  expect(found).not.toBeNull();
  return found as HTMLElement;
}

function paused(el: HTMLElement): HTMLElement {
  const found = el.querySelector(`[data-testid="wf-webhook-paused"]`);
  expect(found).not.toBeNull();
  return found as HTMLElement;
}

function why(el: HTMLElement): HTMLElement | null {
  return el.querySelector(`[data-testid="wf-webhook-pause-why"]`);
}

describe("webhookPauseSentence", () => {
  it("is nothing when the error is blank", () => {
    expect(webhookPauseSentence(null)).toBeNull();
    expect(webhookPauseSentence(undefined)).toBeNull();
    expect(webhookPauseSentence("")).toBeNull();
  });

  it("is nothing when the error is whitespace", () => {
    expect(webhookPauseSentence("   ")).toBeNull();
    expect(webhookPauseSentence(" \n\t ")).toBeNull();
  });

  it("is the trimmed error", () => {
    expect(webhookPauseSentence(PADDED)).toBe(LONG);
    expect(webhookPauseSentence(` \n ${SHORT} \t `)).toBe(SHORT);
    expect(webhookPauseSentence(SIX)).toBe(SIX);
    expect(webhookPauseSentence(SEVEN)).toBe(SEVEN);
  });
});

describe("the pause sentence on the row", () => {
  it("shows a long error as a paragraph and a short one as a span", () => {
    expect(LONG.split(/\s+/).filter(Boolean).length).toBe(7);
    const long = mountWhy(LONG);
    const paragraph = why(long);
    expect(paragraph).not.toBeNull();
    expect(paragraph!.tagName).toBe("P");
    expect(paragraph!.textContent).toBe(LONG);
    expect(paragraph!.hasAttribute("data-user-text")).toBe(true);

    clearHost();

    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);
    const six = mountWhy(SIX);
    const span = why(six);
    expect(span).not.toBeNull();
    expect(span!.tagName).toBe("SPAN");
    expect(span!.textContent).toBe(SIX);
    expect(span!.hasAttribute("data-user-text")).toBe(true);

    clearHost();

    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);
    const seven = mountWhy(SEVEN);
    expect(why(seven)!.tagName).toBe("P");
    expect(why(seven)!.textContent).toBe(SEVEN);

    clearHost();

    const blank = mountWhy(null);
    expect(why(blank)).toBeNull();
    expect(blank.textContent).not.toContain("Couldn't load webhooks");
  });
});

describe("the webhook says why it is paused", () => {
  it("shows the trimmed error beside the paused sentence and keeps the title", async () => {
    const el = await mountScreen([
      hook("wht_long", false, PADDED),
      hook("wht_six", false, SIX),
      hook("wht_seven", false, SEVEN),
      hook("wht_short", false, `  ${SHORT}  `),
      hook("wht_blank", false, " \n\t "),
      hook("wht_on", true, LONG),
      hook("wht_clear", false, null),
    ]);

    expect(el.textContent).not.toContain("Couldn't load webhooks");

    const long = row(el, "wht_long");
    const longPaused = paused(long);
    expect(longPaused.textContent).toBe("Paused: renew authority");
    expect(longPaused.getAttribute("title")).toBe(PADDED);
    expect(longPaused.querySelector(`[data-testid="wf-webhook-pause-why"]`)).toBeNull();
    const longWhy = why(long);
    expect(longWhy).not.toBeNull();
    expect(longWhy!.tagName).toBe("P");
    expect(longWhy!.textContent).toBe(LONG);
    expect(longWhy!.hasAttribute("data-user-text")).toBe(true);
    expect(longPaused.nextElementSibling).toBe(longWhy);

    const six = row(el, "wht_six");
    expect(paused(six).textContent).toBe("Paused: renew authority");
    expect(why(six)!.tagName).toBe("SPAN");
    expect(why(six)!.textContent).toBe(SIX);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);

    const seven = row(el, "wht_seven");
    expect(paused(seven).textContent).toBe("Paused: renew authority");
    expect(why(seven)!.tagName).toBe("P");
    expect(why(seven)!.textContent).toBe(SEVEN);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);

    const short = row(el, "wht_short");
    expect(paused(short).getAttribute("title")).toBe(`  ${SHORT}  `);
    expect(paused(short).textContent).toBe("Paused: renew authority");
    expect(why(short)!.tagName).toBe("SPAN");
    expect(why(short)!.textContent).toBe(SHORT);

    const blank = row(el, "wht_blank");
    expect(paused(blank).textContent).toBe("Paused: renew authority");
    expect(paused(blank).getAttribute("title")).toBe(" \n\t ");
    expect(why(blank)).toBeNull();

    expect(why(row(el, "wht_on"))).toBeNull();
    expect(row(el, "wht_on").querySelector(`[data-testid="wf-webhook-paused"]`)).toBeNull();
    expect(why(row(el, "wht_clear"))).toBeNull();
    expect(row(el, "wht_clear").querySelector(`[data-testid="wf-webhook-paused"]`)).toBeNull();
  });

  it("calls webhookPauseSentence with the hook last error beside the paused sentence", () => {
    const src = readFileSync(join(__dirname, "../app/workflows/editor/inspector/webhook-triggers.tsx"), "utf8");
    const start = src.indexOf("export function WebhookTriggers");
    const end = src.indexOf("function Deliveries");
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const screen = src.slice(start, end);
    const at = screen.indexOf('data-testid="wf-webhook-paused"');
    expect(at).toBeGreaterThan(-1);
    const block = screen.slice(Math.max(0, at - 200), at + 500);
    expect(block).toContain("title={hook.last_error}");
    expect(block).toContain("Paused: renew authority");
    expect(block).toContain("webhookPauseSentence(hook.last_error)");
    expect(block).toContain("WebhookPauseWhy");
    expect(block).not.toContain("Couldn't load webhooks");
    expect(src).toContain('data-testid="wf-webhook-pause-why"');
    expect(src).toContain("data-user-text");
  });
});
