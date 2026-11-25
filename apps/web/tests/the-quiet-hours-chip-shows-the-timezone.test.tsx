// @vitest-environment happy-dom
/**
 * The policy quiet-hours chip names its timezone in the text, not only the title.
 * A blank timezone stays `quiet {start}, {end}` with no trailing comma zone.
 * The chip's children are quietChipText. A sentence over six words is a paragraph.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const src = readFileSync(join(__dirname, "../app/observability/notify.tsx"), "utf8");

let root: Root | null = null;
let host: HTMLElement | null = null;

async function show(text: string): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(<span>{text}</span>); });
  return host;
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

describe("the quiet hours chip shows the timezone", () => {
  it("names America/New_York in the chip text", async () => {
    const notify = await import("../app/observability/notify");
    expect(typeof notify.quietChipText).toBe("function");
    const text = notify.quietChipText({ start: "22:00", end: "07:00", timezone: "America/New_York" });
    const shown = await show(text);
    expect(shown.textContent).toContain("America/New_York");
    expect(text).toBe("quiet 22:00, 07:00, America/New_York");
    expect(src).toMatch(/data-testid="obs-policy-quiet"[^>]*>\{quietChipText\(hours\)\}/);
    expect(src).toContain("<QuietHoursChip hours={p.quietHours} />");
    expect(src).toMatch(/<p className="obs-chip" data-testid="obs-policy-quiet"/);
  });

  it("does not add a trailing comma zone when the timezone is empty", async () => {
    const notify = await import("../app/observability/notify");
    expect(typeof notify.quietChipText).toBe("function");
    for (const timezone of ["", "   "]) {
      const text = notify.quietChipText({ start: "22:00", end: "07:00", timezone });
      const shown = await show(text);
      expect(shown.textContent).toBe("quiet 22:00, 07:00");
      expect(text).toBe("quiet 22:00, 07:00");
      expect(text).not.toMatch(/,\s*$/);
    }
  });
});
