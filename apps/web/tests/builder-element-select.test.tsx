// @vitest-environment happy-dom
/**
 * Mark one piece of a preview: edit, restyle, comment, or regenerate.
 * The screen shows one receipt. A repeat of the same change stays on that receipt.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ElementSelect, MarkError, type MarkClient, type MarkReceipt } from "../app/builder/element-select";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.unstubAllGlobals();
});

async function render(node: React.ReactElement) {
  await act(async () => {
    root!.render(node);
  });
  return host!;
}

async function click(selector: string) {
  const el = document.querySelector<HTMLElement>(selector);
  expect(el, selector).not.toBeNull();
  await act(async () => {
    el!.click();
  });
}

async function typeChange(value: string) {
  const area = document.querySelector<HTMLTextAreaElement>('[data-testid="change-text"]');
  expect(area).not.toBeNull();
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    setter?.call(area, value);
    area!.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function receipt(over: Partial<MarkReceipt> = {}): MarkReceipt {
  return {
    id: "rcpt_price_edit",
    dedupeKey: "element-select:org_1:proj_1:proof-hearth:price:edit:abc123",
    reused: false,
    effectKind: "element_edit",
    reservedUsdMicros: 50_000,
    status: "succeeded",
    rendered: "Fourteen dollars",
    style: null,
    comment: null,
    effectCount: 1,
    ...over,
  };
}

function longTextOutsideParagraphs(scope: ParentNode): string[] {
  const bad: string[] = [];
  const elements = [scope, ...scope.querySelectorAll("*")];
  for (const el of elements) {
    if (!(el instanceof HTMLElement)) continue;
    if (el.closest("p, textarea, script, style, [data-user-text]")) continue;
    const own = [...el.childNodes]
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent ?? "")
      .join("");
    const words = own.trim().split(/\s+/).filter(Boolean);
    if (words.length > 6) bad.push(own.trim());
  }
  return bad;
}

function longTitles(scope: ParentNode): string[] {
  const bad: string[] = [];
  for (const el of scope.querySelectorAll("[title]")) {
    const words = (el.getAttribute("title") ?? "").trim().split(/\s+/).filter(Boolean);
    if (words.length > 14) bad.push(el.getAttribute("title") ?? "");
  }
  return bad;
}

function expectCopy() {
  const screen = document.querySelector('[data-testid="element-select"]');
  expect(screen).not.toBeNull();
  expect(longTextOutsideParagraphs(screen!)).toEqual([]);
  expect(longTitles(screen!)).toEqual([]);
}

describe("mark a piece of the proof", () => {
  it("shows the proof and will not record until a piece, a verb, and a change are set", async () => {
    const apply = vi.fn();
    await render(<ElementSelect projectId="proj_1" actor="founder" orgId="org_1" client={{ apply }} />);
    expect(document.querySelector('[data-testid="proof-headline"]')?.textContent).toContain(
      "Bread that still remembers the oven.",
    );
    expect(document.querySelector('[data-testid="proof-price"]')?.textContent).toContain("$14 / loaf");
    const record = document.querySelector<HTMLButtonElement>('[data-testid="record-change"]');
    expect(record?.disabled).toBe(true);
    await click('[data-testid="proof-price"]');
    expect(document.querySelector('[data-testid="proof-price"]')?.getAttribute("aria-pressed")).toBe("true");
    expect(record?.disabled).toBe(true);
    await click('[data-testid="action-edit"]');
    expect(apply).not.toHaveBeenCalled();
    expectCopy();
  });

  it("records one edit and a repeat stays on the same receipt", async () => {
    const seen: string[] = [];
    const client: MarkClient = {
      apply: vi.fn(async (mark) => {
        seen.push(`${mark.elementId}:${mark.action}:${mark.change}`);
        return receipt({
          reused: seen.length > 1,
          rendered: mark.action === "edit" ? mark.change.trim() : "$14 / loaf",
        });
      }),
    };
    await render(
      <ElementSelect projectId="proj_1" actor="founder" orgId="org_1" workspaceId="ws_1" client={client} />,
    );
    await click('[data-testid="proof-price"]');
    await click('[data-testid="action-edit"]');
    await typeChange("Fourteen dollars");
    expect(document.querySelector<HTMLButtonElement>('[data-testid="record-change"]')?.disabled).toBe(false);
    await click('[data-testid="record-change"]');
    expect(client.apply).toHaveBeenCalledTimes(1);
    expect(vi.mocked(client.apply).mock.calls[0][0]).toMatchObject({
      projectId: "proj_1",
      actor: "founder",
      orgId: "org_1",
      workspaceId: "ws_1",
      previewId: "proof-hearth",
      elementId: "price",
      action: "edit",
      change: "Fourteen dollars",
    });
    const slip = document.querySelector<HTMLElement>('[data-testid="element-receipt"]');
    expect(slip?.getAttribute("data-reused")).toBe("false");
    expect(slip?.textContent).toContain("Recorded once");
    expect(document.querySelector('[data-testid="receipt-id"]')?.textContent).toBe("rcpt_price_edit");
    expect(document.querySelector('[data-testid="dedupe-key"]')?.textContent).toContain("element-select:");
    expect(document.querySelector('[data-testid="effect-count"]')?.textContent).toBe("1");
    expect(document.querySelector('[data-testid="proof-price"] .em-copy')?.textContent).toBe("Fourteen dollars");

    await click('[data-testid="record-change"]');
    expect(client.apply).toHaveBeenCalledTimes(2);
    expect(document.querySelector('[data-testid="element-receipt"]')?.getAttribute("data-reused")).toBe("true");
    expect(document.querySelector('[data-testid="element-receipt"]')?.textContent).toContain("Already recorded");
    expect(document.querySelector('[data-testid="receipt-id"]')?.textContent).toBe("rcpt_price_edit");
    expect(document.querySelector('[data-testid="effect-count"]')?.textContent).toBe("1");
    expectCopy();
  });

  it("restyle keeps the words, comment does not rewrite them, regenerate replaces the piece", async () => {
    const client: MarkClient = {
      apply: vi.fn(async (mark) => {
        if (mark.action === "restyle") {
          return receipt({
            effectKind: "element_restyle",
            reservedUsdMicros: 80_000,
            rendered: "$14 / loaf",
            style: "ember",
          });
        }
        if (mark.action === "comment") {
          return receipt({
            id: "rcpt_comment",
            effectKind: "element_comment",
            reservedUsdMicros: 0,
            rendered: "$14 / loaf",
            comment: mark.change,
            effectCount: 2,
          });
        }
        return receipt({
          id: "rcpt_headline",
          effectKind: "element_regenerate",
          reservedUsdMicros: 250_000,
          rendered: "The crust cracks when you listen.",
          effectCount: 3,
        });
      }),
    };
    await render(<ElementSelect projectId="proj_1" actor="founder" client={client} />);
    await click('[data-testid="proof-price"]');
    await click('[data-testid="action-restyle"]');
    await typeChange("ember");
    await click('[data-testid="record-change"]');
    expect(document.querySelector('[data-testid="proof-price"]')?.getAttribute("data-style")).toBe("ember");
    expect(document.querySelector('[data-testid="proof-price"] .em-copy')?.textContent).toBe("$14 / loaf");

    await click('[data-testid="action-comment"]');
    await typeChange("Too loud.");
    await click('[data-testid="record-change"]');
    expect(document.querySelector('[data-testid="proof-price"] .em-copy')?.textContent).toBe("$14 / loaf");
    expect(document.querySelector('[data-testid="element-receipt"]')?.textContent).toContain("Too loud.");

    await click('[data-testid="proof-headline"]');
    await click('[data-testid="action-regenerate"]');
    await typeChange("Try again.");
    await click('[data-testid="record-change"]');
    expect(document.querySelector('[data-testid="proof-headline"] .em-copy')?.textContent).toBe(
      "The crust cracks when you listen.",
    );
    expect(document.querySelectorAll('[data-testid^="action-"]')).toHaveLength(4);
    expectCopy();
  });

  it("shows a budget refusal with no retry, and a failed effect with a retry", async () => {
    const refused: MarkClient = {
      apply: vi.fn(async () => {
        throw new MarkError("The budget cannot cover this change.", {
          retryable: false,
          code: "budget_denied",
          deadLetterId: "dl_budget",
        });
      }),
    };
    await render(<ElementSelect projectId="proj_1" actor="founder" client={refused} />);
    await click('[data-testid="proof-headline"]');
    await click('[data-testid="action-regenerate"]');
    await typeChange("Again.");
    await click('[data-testid="record-change"]');
    const alert = document.querySelector('[data-testid="mark-error"]');
    expect(alert?.tagName).toBe("P");
    expect(alert?.textContent).toContain("The budget cannot cover this change.");
    expect(document.querySelector('[data-testid="dead-letter"]')?.textContent).toContain("dl_budget");
    expect(document.querySelector('[data-testid="mark-retry"]')).toBeNull();
    expect(document.querySelector('[data-testid="element-receipt"]')).toBeNull();
    expectCopy();

    let calls = 0;
    const flaky: MarkClient = {
      apply: vi.fn(async () => {
        calls += 1;
        if (calls === 1) {
          throw new MarkError("The edit did not land. renderer down", {
            retryable: true,
            code: "effect_failed",
            deadLetterId: "dl_effect",
          });
        }
        return receipt();
      }),
    };
    await render(<ElementSelect projectId="proj_1" actor="founder" client={flaky} />);
    await click('[data-testid="proof-price"]');
    await click('[data-testid="action-edit"]');
    await typeChange("Fourteen dollars");
    await click('[data-testid="record-change"]');
    expect(document.querySelector('[data-testid="mark-error"]')?.textContent).toContain("renderer down");
    expect(document.querySelector('[data-testid="mark-retry"]')).not.toBeNull();
    await click('[data-testid="mark-retry"]');
    expect(flaky.apply).toHaveBeenCalledTimes(2);
    expect(document.querySelector('[data-testid="element-receipt"]')?.textContent).toContain("Recorded once");
    expect(document.querySelector('[data-testid="mark-error"]')).toBeNull();
    expectCopy();
  });

  it("posts the mark and reads a reused receipt from the response", async () => {
    let n = 0;
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      n += 1;
      const sent = JSON.parse(String(init?.body ?? "{}"));
      if (sent.action === "regenerate") {
        return new Response(
          JSON.stringify({
            detail: {
              message: "The budget cannot cover this change.",
              code: "budget_denied",
              retryable: false,
              dead_letter_id: "dl_9",
            },
          }),
          { status: 409, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          id: "rcpt_from_api",
          dedupe_key: "element-select:local:proj_1:proof-hearth:price:edit:abc",
          reused: n > 1,
          effect_kind: "element_edit",
          reserved_usd_micros: 50_000,
          status: "succeeded",
          rendered: sent.change,
          style: null,
          comment: null,
          effect_count: 1,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    await render(<ElementSelect projectId="proj_1" actor="founder" />);
    await click('[data-testid="proof-price"]');
    await click('[data-testid="action-edit"]');
    await typeChange("Fourteen dollars");
    await click('[data-testid="record-change"]');
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/element-select/apply",
      expect.objectContaining({ method: "POST" }),
    );
    const sent = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(sent).toMatchObject({
      org_id: "local",
      workspace_id: "local",
      project_id: "proj_1",
      preview_id: "proof-hearth",
      element_id: "price",
      action: "edit",
      change: "Fourteen dollars",
      actor: "founder",
    });
    expect(document.querySelector('[data-testid="receipt-id"]')?.textContent).toBe("rcpt_from_api");
    await click('[data-testid="record-change"]');
    expect(document.querySelector('[data-testid="element-receipt"]')?.getAttribute("data-reused")).toBe("true");
    expect(document.querySelector('[data-testid="effect-count"]')?.textContent).toBe("1");

    await click('[data-testid="action-regenerate"]');
    await typeChange("Again.");
    await click('[data-testid="record-change"]');
    expect(document.querySelector('[data-testid="mark-error"]')?.textContent).toContain(
      "The budget cannot cover this change.",
    );
    expect(document.querySelector('[data-testid="mark-retry"]')).toBeNull();
    expect(document.querySelector('[data-testid="dead-letter"]')?.textContent).toContain("dl_9");
    expectCopy();
  });
});
