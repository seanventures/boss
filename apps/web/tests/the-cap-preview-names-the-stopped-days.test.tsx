import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { CapPreview, stoppedDaysSentence } from "../app/govern/cap-preview";
import type { CapPreview as CapPreviewData } from "../lib/govern";

/**
 * The preview already counts stopped days and never names them. Naming drops a
 * trimmed day, reorders the list, reformats a date, or changes the count.
 */
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function preview(over: Partial<CapPreviewData> = {}): CapPreviewData {
  return {
    period: "day",
    cap_usd_micros: 1_000_000,
    days_considered: 30,
    days_with_spend: 3,
    days_stopped: 3,
    stopped_days: ["a", "b", "c"],
    not_enough_history: false,
    daily: [],
    ...over,
  };
}

describe("stoppedDaysSentence", () => {
  it("joins trimmed non-empty strings in the given order", () => {
    expect(stoppedDaysSentence([" a ", "b", "  c  "])).toBe("a, b, c");
    expect(stoppedDaysSentence(["c", "a", "b"])).toBe("c, a, b");
    expect(stoppedDaysSentence(["a", "a"])).toBe("a, a");
    expect(stoppedDaysSentence(["  2026-01-02  ", "a"])).toBe("2026-01-02, a");
  });

  it("is null when nothing remains", () => {
    expect(stoppedDaysSentence(null)).toBeNull();
    expect(stoppedDaysSentence(undefined)).toBeNull();
    expect(stoppedDaysSentence([])).toBeNull();
    expect(stoppedDaysSentence(["", "   ", "\t"])).toBeNull();
  });
});

describe("the cap preview names the stopped days", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;
  });

  async function mount(data: CapPreviewData) {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(<CapPreview preview={data} />);
    });
  }

  function named() {
    return host!.querySelector<HTMLElement>("[data-testid='gv-stopped-days']");
  }

  function count() {
    return host!.querySelector("[data-testid='gv-would-stop']")?.textContent ?? null;
  }

  it("names a, b, and c beside the unchanged count", async () => {
    await mount(preview({ stopped_days: ["a", "b", "c"], days_stopped: 3 }));
    const el = named();
    expect(el).not.toBeNull();
    expect(el!.tagName).toBe("SPAN");
    expect(el!.textContent).toBe("a, b, c");
    expect(el!.hasAttribute("data-user-text")).toBe(true);
    expect(count()).toBe("Would have stopped 3 days");
  });

  it("uses a span for six words and a paragraph for more", async () => {
    await mount(preview({
      days_stopped: 6,
      stopped_days: ["aa", "bb", "cc", "dd", "ee", "ff"],
    }));
    expect(named()!.tagName).toBe("SPAN");
    expect(named()!.textContent).toBe("aa, bb, cc, dd, ee, ff");
    expect(count()).toBe("Would have stopped 6 days");

    await mount(preview({
      days_stopped: 7,
      stopped_days: ["aa", "bb", "cc", "dd", "ee", "ff", "gg"],
    }));
    expect(named()!.tagName).toBe("P");
    expect(named()!.textContent).toBe("aa, bb, cc, dd, ee, ff, gg");
    expect(count()).toBe("Would have stopped 7 days");
  });

  it("shows nothing for a blank or empty list and keeps the count", async () => {
    await mount(preview({ days_stopped: 0, stopped_days: [] }));
    expect(named()).toBeNull();
    expect(count()).toBe("Would have stopped none");

    await mount(preview({ days_stopped: 2, stopped_days: ["", "  ", "\t"] }));
    expect(named()).toBeNull();
    expect(count()).toBe("Would have stopped 2 days");
  });
});
