// @vitest-environment happy-dom
/**
 * The person's feedback (docs/product/RUN_SCORECARDS.md): one tap, never blocking, never nagging.
 * RateIt asks openly at most three times a day, then is a quiet "Rate"; the reasons come only
 * after a verdict; the check-in is dismissible; Setup asks once; Share this run sends only on
 * the press. And the stylesheet is tokens only, fits 390px, and has a still.
 */
import { readFileSync } from "node:fs";
import path from "node:path";

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { fakeImproveApi, newImprove } from "./_helpers/improve-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { RateIt } = await import("../app/improve/rate-it");
const { CheckinCard } = await import("../app/improve/checkin-card");
const { SharingAsk } = await import("../app/improve/sharing-ask");
const { ShareRunSheet } = await import("../app/improve/share-run");

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("RateIt", () => {
  it("one tap records the verdict; reasons appear only after it", async () => {
    const f = newImprove();
    vi.stubGlobal("fetch", fakeImproveApi(f));
    await mount(<RateIt projectId="p1" actor="ceo" subject="build" id="crun_1" />);
    expect($("[data-testid=rate-build]")!.textContent).toContain("Would you ship this?");
    expect($$("[data-testid^=rate-chip-]")).toHaveLength(0);
    expect(f.calls.filter((c) => c.op === "prompt_shown")).toHaveLength(1); // an open ask is counted
    await click($("[data-testid=rate-build-no]"));
    await settle();
    expect(f.calls.find((c) => c.op === "feedback")?.body).toMatchObject(
      { subject_kind: "build", subject_id: "crun_1", rating: "no", chips: [] });
    await click($("[data-testid=rate-chip-too_slow]"));
    await settle();
    expect(f.calls.filter((c) => c.op === "feedback").at(-1)?.body).toMatchObject({ rating: "no", chips: ["too_slow"] });
    expect(overBudget($("[data-testid=rate-build]")!)).toEqual([]);
  });

  it("after three asks today it is a quiet Rate link, and many on a page share one read", async () => {
    const f = newImprove();
    f.prompts.ask_open = false;
    vi.stubGlobal("fetch", fakeImproveApi(f));
    await mount(<div><RateIt projectId="p1" actor="ceo" subject="build" id="a" />
      <RateIt projectId="p1" actor="ceo" subject="research" id="b" /></div>);
    expect($$("[data-testid^=rate-] .im-rate-btn[data-quiet]")).toHaveLength(2);
    expect(f.calls.filter((c) => c.view === "prompts")).toHaveLength(1);
    expect(f.calls.some((c) => c.op === "prompt_shown")).toBe(false);
  });

  it("answers get thumbs that never count as an ask", async () => {
    const f = newImprove();
    f.prompts.ask_open = false;
    vi.stubGlobal("fetch", fakeImproveApi(f));
    await mount(<RateIt projectId="p1" actor="ceo" subject="answer" id="turn_1" />);
    expect($("[data-testid=rate-answer-up]")).toBeTruthy();
    expect(f.calls.some((c) => c.op === "prompt_shown")).toBe(false);
  });
});

describe("the monthly check-in", () => {
  it("shows when due, counts as the month's ask, and a dismissal is one tap", async () => {
    const f = newImprove();
    vi.stubGlobal("fetch", fakeImproveApi(f));
    await mount(<CheckinCard projectId="p1" actor="ceo" />);
    expect($("[data-testid=checkin]")).toBeTruthy();
    expect(f.calls.find((c) => c.op === "prompt_shown")?.body).toMatchObject({ prompt: "checkin" });
    await click($("[data-testid=checkin-dismiss]"));
    await settle();
    expect(f.calls.find((c) => c.op === "checkin")?.body).toMatchObject({ dismiss: true });
    expect($("[data-testid=checkin]")).toBeNull();
  });

  it("is nothing at all when not due", async () => {
    const f = newImprove();
    f.prompts.checkin_due = false;
    vi.stubGlobal("fetch", fakeImproveApi(f));
    await mount(<CheckinCard projectId="p1" actor="ceo" />);
    expect($("[data-testid=checkin]")).toBeNull();
    expect(f.calls.some((c) => c.op === "prompt_shown")).toBe(false);
  });
});

describe("Setup asks once", () => {
  it("asks in the doc's words, records Not now, and names the heartbeat's opt-out", async () => {
    const f = newImprove();
    vi.stubGlobal("fetch", fakeImproveApi(f));
    await mount(<SharingAsk projectId="p1" actor="ceo" />);
    expect($("[data-testid=im-consent]")).toBeTruthy();
    expect($("[data-testid=su-heartbeat-optout]")!.getAttribute("href")).toBe("/?area=budget&tab=sharing");
    await click($("[data-testid=su-sharing-no]"));
    await settle();
    expect(f.calls.find((c) => c.op === "switch")?.body).toMatchObject({ switch: "improve", on: false });
    expect($("[data-testid=im-consent]")).toBeNull(); // asked: not again
    expect($("[data-testid=su-sharing-state]")!.textContent).toBe("Off");
  });
});

describe("Share this run", () => {
  it("shows the redacted bundle to edit and sends only on Send", async () => {
    const f = newImprove();
    vi.stubGlobal("fetch", fakeImproveApi(f));
    await mount(<ShareRunSheet projectId="p1" actor="ceo" unitKind="build" unitId="crun_9" onClose={() => {}} />);
    await settle();
    expect(($("[data-testid=share-run-bundle]") as HTMLTextAreaElement).value).toContain("[SECRET]");
    expect(f.calls.some((c) => c.op === "share_run")).toBe(false);
    await click($("[data-testid=share-run-send]"));
    await settle();
    expect(f.calls.filter((c) => c.op === "share_run")).toHaveLength(1);
  });
});

describe("the stylesheet", () => {
  const css = readFileSync(path.resolve(__dirname, "../app/improve/improve.css"), "utf8");
  it("is tokens only", () => {
    expect(css.match(/#[0-9a-f]{3,8}\b/gi) ?? []).toEqual([]);
    expect(css.match(/\brgba?\(|\bhsla?\(/gi) ?? []).toEqual([]);
  });
  it("fits a phone and has a still", () => {
    expect(css).toMatch(/@media \(max-width: 719px\)/);
    expect(css).toMatch(/min-height: 44px/);
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)/);
  });
  it("the new screens carry no inline styles, raw colours or paragraphs", () => {
    for (const f of ["app/govern/sharing-tab.tsx", "app/observability/scorecards.tsx", "app/observability/installs.tsx",
                     "app/improve/rate-it.tsx", "app/improve/checkin-card.tsx", "app/improve/share-run.tsx",
                     "app/improve/sharing-ask.tsx", "app/improve/consent.tsx"]) {
      const src = readFileSync(path.resolve(__dirname, "..", f), "utf8");
      expect(src, f).not.toMatch(/style=\{\{/);
      expect(src, f).not.toMatch(/#[0-9a-f]{6}\b/i);
      expect(src, f).not.toMatch(/<p[\s>]/);
      expect(src, f).not.toMatch(/setInterval\(|router\.refresh/);
    }
  });
});
