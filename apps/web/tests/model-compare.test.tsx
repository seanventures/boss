// @vitest-environment happy-dom
/**
 * BOSSAPP-1262: one prompt, lanes side by side, a receipt under each lane.
 * A failed lane stays visible and does not hide the receipt that stood.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ModelCompare, type CompareResult } from "../app/models/compare";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;

async function render(ui: React.ReactNode) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(ui);
  });
}

async function setValue(selector: string, value: string) {
  const input = host!.querySelector(selector) as HTMLInputElement | HTMLTextAreaElement;
  const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

afterEach(async () => {
  if (root) await act(async () => root!.unmount());
  host?.remove();
  root = null;
  host = null;
});

const MIXED: CompareResult = {
  prompt: "Name the quiet part.",
  columns: [
    {
      model: "oxide",
      status: "failed",
      text: null,
      error: "The oxide lane refused this prompt and did not spend.",
      receipt_id: "rcpt_oxide",
      dedupe_key: "model.compare:oxide",
      usd_micros: null,
      reused: false,
    },
    {
      model: "filament",
      status: "succeeded",
      text: "still here",
      error: null,
      receipt_id: "rcpt_filament",
      dedupe_key: "model.compare:filament",
      usd_micros: 1500,
      reused: false,
    },
  ],
};

describe("model compare", () => {
  it("puts one prompt in lanes and keeps a failed lane beside a standing receipt", async () => {
    const compare = vi.fn(async () => MIXED);
    await render(<ModelCompare projectId="p1" actor="ada" compare={compare} />);

    const intro = host!.querySelector("[data-testid=compare-intro]");
    expect(intro?.tagName).toBe("P");
    expect(intro!.textContent!.trim().split(/\s+/).length).toBeGreaterThan(6);

    const tip = host!.querySelector("[data-testid=compare-submit]")!.getAttribute("title") || "";
    expect(tip.trim().split(/\s+/).length).toBeLessThanOrEqual(14);

    await setValue("[data-testid=compare-prompt]", "Name the quiet part.");
    await setValue("[data-testid=compare-model-0]", "oxide");
    await setValue("[data-testid=compare-model-1]", "filament");
    await act(async () => {
      (host!.querySelector("[data-testid=compare-submit]") as HTMLButtonElement).click();
    });

    expect(compare).toHaveBeenCalledWith({
      projectId: "p1",
      actor: "ada",
      prompt: "Name the quiet part.",
      models: ["oxide", "filament"],
    });

    const lanes = host!.querySelector("[data-testid=compare-lanes]") as HTMLElement;
    expect(getComputedStyle(lanes).display).toBe("grid");
    const columns = [...host!.querySelectorAll("[data-testid=compare-column]")];
    expect(columns.map((node) => node.getAttribute("data-model"))).toEqual(["oxide", "filament"]);
    expect(columns[0].getAttribute("data-status")).toBe("failed");
    expect(columns[0].querySelector("p")!.textContent).toContain("refused this prompt");
    expect(columns[0].textContent).toContain("rcpt_oxide");
    expect(columns[1].getAttribute("data-status")).toBe("succeeded");
    expect(columns[1].textContent).toContain("still here");
    expect(columns[1].textContent).toContain("rcpt_filament");
    expect(columns[1].textContent).toContain("1500");
    const receiptTip = columns[1].querySelector("[data-testid=compare-receipt]")!.getAttribute("title") || "";
    expect(receiptTip.trim().split(/\s+/).length).toBeLessThanOrEqual(14);
  });

  it("refuses a single model before it asks to compare", async () => {
    const compare = vi.fn(async () => MIXED);
    await render(<ModelCompare projectId="p1" compare={compare} />);
    await setValue("[data-testid=compare-prompt]", "Name the quiet part.");
    await setValue("[data-testid=compare-model-0]", "filament");
    await act(async () => {
      (host!.querySelector("[data-testid=compare-submit]") as HTMLButtonElement).click();
    });
    expect(compare).not.toHaveBeenCalled();
    expect(host!.querySelector("[data-testid=compare-form-error]")!.tagName).toBe("P");
    expect(host!.textContent).toContain("two");
  });
});
