// @vitest-environment happy-dom
/**
 * Tune a rule (P1-C): one surface (the rule sheet) reached from every rule in an app's Rules list
 * and from the alert itself. A repo rule says where it lives (path:line) and is proposed as a
 * pull request; a console rule is saved; Preview runs the NEW values over the last 24 h.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NOISE_ALERT, historyWire, ruleWire, rulesWire } from "./_helpers/alerts-fixtures";
import { $, $$, click, settle, unmount } from "./_helpers/live-render";
import { openObs, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

async function type(el: Element | null, value: string) {
  const input = el as HTMLInputElement;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await settle();
}

const WHERE = { path: "deploy/boss.deploy.yaml", line: 12, end_line: 14,
                href: "https://github.com/acme/shop/blob/6bf2673/deploy/boss.deploy.yaml#L12-L14" };
const rules = (id: string) => rulesWire(id, { rules: [
  ruleWire("error_rate", { tune_via: "console" }),
  ruleWire("latency_p95", { tune_via: "pull_request", managed_by: "code", where: WHERE }),
] });
const PREVIEW = { window: "24h", now: { fired: 6, word: "Would have fired 6 times in the last 24 h" },
                  tuned: { fired: 0, word: "Would have fired 0 times in the last 24 h" } };

describe("Tune on the Rules list", () => {
  it("every rule offers Tune; a repo rule says where it lives and proposes a pull request", async () => {
    const rec = await openObs("?view=apps&app=t_down&atab=alerts", {
      rules,
      writes: { "/arule_latency_p95/tune-preview": { body: PREVIEW },
                "/arule_latency_p95/propose": { body: { pull_request: { url: "https://github.com/acme/shop/pull/9", number: 9 } } } },
    });
    expect($$("[data-testid='obs-rule-edit']").map((b) => text(b))).toEqual(["Tune", "Tune"]);
    expect($("[data-testid='obs-rule-latency_p95'] [data-testid='obs-rule-repo']")?.getAttribute("title"))
      .toContain("deploy/boss.deploy.yaml:12");
    await click($("[data-testid='obs-rule-latency_p95'] [data-testid='obs-rule-edit']"));
    const sheet = $("[data-testid='obs-rule-sheet']")!;
    const where = sheet.querySelector("[data-testid='obs-rule-where'] a")!;
    expect(text(where)).toBe("deploy/boss.deploy.yaml:12");
    expect(where.getAttribute("href")).toBe(WHERE.href);
    expect(text(sheet.querySelector("[data-testid='obs-rule-save']"))).toBe("Propose PR");

    await type(sheet.querySelector("[data-testid='obs-rule-threshold']"), "1200");
    await click(sheet.querySelector("[data-testid='obs-rule-preview']"));
    await settle();
    const pv = rec.writes.find((w) => w.url.endsWith("/tune-preview"))!;
    expect(pv.url).toBe("/v1/apps/t_down/alert-rules/arule_latency_p95/tune-preview");
    expect(pv.body).toEqual({ project_id: "p1", change: { params: { threshold: 1200, window_minutes: 5, min_requests: 20 }, for: "0m" } });
    expect(text(sheet.querySelector("[data-testid='obs-rule-preview-word']"))).toBe("Would have fired 0 times in the last 24 h");
    expect(text(sheet.querySelector("[data-testid='obs-rule-preview-now']"))).toBe("now: Would have fired 6 times in the last 24 h");

    await type(sheet.querySelector("[data-testid='obs-rule-reason']"), "noisy at night");
    await click(sheet.querySelector("[data-testid='obs-rule-save']"));
    await settle();
    const pr = rec.writes.find((w) => w.url.endsWith("/propose"))!;
    // only what changed goes to the pull request; the rule's row is not written
    expect(pr.body).toEqual({ project_id: "p1", actor: "ceo", reason: "noisy at night", change: { params: { threshold: 1200 } } });
    expect(rec.writes.some((w) => w.method === "PUT")).toBe(false);
    const done = $("[data-testid='obs-rule-proposed'] a")!;
    expect(text(done)).toBe("PR #9 ↗");
    expect(done.getAttribute("href")).toBe("https://github.com/acme/shop/pull/9");
  });

  it("a console rule is saved here (PUT), with Save", async () => {
    const rec = await openObs("?view=apps&app=t_down&atab=alerts", { rules });
    await click($("[data-testid='obs-rule-error_rate'] [data-testid='obs-rule-edit']"));
    const sheet = $("[data-testid='obs-rule-sheet']")!;
    expect(sheet.querySelector("[data-testid='obs-rule-where']")).toBeNull();
    expect(text(sheet.querySelector("[data-testid='obs-rule-save']"))).toBe("Save");
    await type(sheet.querySelector("[data-testid='obs-rule-threshold']"), "9");
    await click(sheet.querySelector("[data-testid='obs-rule-save']"));
    await settle();
    expect(rec.writes.find((w) => w.method === "PUT")?.url).toBe("/v1/apps/t_down/alert-rules");
    expect(rec.writes.some((w) => w.url.endsWith("/propose"))).toBe(false);
  });
});

describe("Tune on the alert", () => {
  it("a noise-marked alert offers Tune, which opens straight on the rule sheet", async () => {
    await openObs("?view=apps&app=t_down&atab=alerts", { rules, history: () => historyWire([NOISE_ALERT]) });
    const row = $("[data-testid='obs-alert-alrt_lat']")!;
    const tune = row.querySelector("[data-testid='obs-alert-tune']")!;
    expect(text(tune)).toBe("Tune");
    expect(tune.getAttribute("data-tone")).toBe("amber");
    await click(tune);
    await settle();
    const sheet = $("[data-testid='obs-rule-sheet']")!;
    expect(sheet.getAttribute("aria-label")).toBe("p95 latency alert");
    expect(text(sheet.querySelector("[data-testid='obs-rule-save']"))).toBe("Propose PR");
    expect($("[data-testid='obs-alert-rule-sheet']")).toBeNull();
  });
});
