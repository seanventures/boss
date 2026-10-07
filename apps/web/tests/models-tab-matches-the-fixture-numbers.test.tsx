// @vitest-environment happy-dom
/**
 * Observability › Models shows exactly the view's numbers (spec §4.10): the stat strip, the
 * table, `est.` only when a price was estimated, `Included in plan` (never `$0`) when everything
 * ran on a subscription; the group switch and the metric live in the URL; a band (or a row)
 * opens that group's drawer with its RECENT CALLS from the ledger, paged by `Show older`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { emptyModelsWire, includedModelsWire } from "./_helpers/obs-fixtures";
import { act } from "react";
import { $, $$, click, settle, unmount } from "./_helpers/live-render";
import { openObs, params, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const stat = (id: string) => text($(`[data-testid='obs-stat-${id}-value']`));

describe("the Models numbers", () => {
  it("the stat strip says the totals, each with its sparkline", async () => {
    await openObs("?view=models");
    expect(stat("calls")).toBe("1,284");
    expect(stat("tokens")).toBe("4.1M in · 610k out");
    expect(stat("spend")).toBe("$3.12 est.");
    expect($("[data-testid='obs-stat-spend-value'] [title]")?.getAttribute("title")).toBe("Some prices were estimated, not billed");
    expect(stat("p95")).toBe("18 s");
    expect(stat("errors")).toBe("2.1% · 27");
    for (const id of ["calls", "tokens", "spend", "p95", "errors"]) {
      expect($(`[data-testid='obs-spark-${id}'] path.obs-spark-line`)).not.toBeNull();
    }
  });

  it("the table has one row per group, Spend first, and says Included for subscription work", async () => {
    await openObs("?view=models");
    const rows = $$("[data-testid='obs-group-row']");
    expect(rows.map((r) => r.getAttribute("data-key"))).toEqual(
      ["claude-sonnet-4-5", "gpt-5-mini", "gemini-2.5-pro", "gpt-4o", "claude-opus-4", "qwen-local"]);
    const spend = (key: string) => text($(`[data-testid='obs-group-row'][data-key='${key}'] [data-testid='obs-group-spend']`));
    expect(spend("claude-sonnet-4-5")).toBe("$2.41 est.");
    expect(spend("gpt-5-mini")).toBe("$0.65");
    expect(spend("gpt-4o")).toBe("<$0.01");
    expect(spend("claude-opus-4")).toBe("Included");
    expect(spend("qwen-local")).toBe("—");
    const sonnet = rows[0].querySelectorAll("td");
    expect([...sonnet].slice(1, 9).map((td) => text(td))).toEqual(["612", "2.9M", "402k", "$2.41 est.", "9.2 s", "21 s", "1.1%", "14"]);
    expect(document.body.textContent).not.toMatch(/\$0(?![.\d])/);
  });

  it("sorting by a column reorders, and says which way", async () => {
    await openObs("?view=models");
    await click($("[data-testid='obs-sort-calls']"));
    expect($$("[data-testid='obs-group-row']")[0].getAttribute("data-key")).toBe("claude-sonnet-4-5");
    await click($("[data-testid='obs-sort-calls']"));
    expect($$("[data-testid='obs-group-row']")[0].getAttribute("data-key")).toBe("gpt-4o");
    expect($("[data-testid='obs-sort-calls']")?.closest("th")?.getAttribute("aria-sort")).toBe("ascending");
  });

  it("all on a subscription: Spend reads Included in plan, never $0", async () => {
    await openObs("?view=models", { models: () => includedModelsWire() });
    expect(stat("spend")).toBe("Included in plan");
    expect($("[data-testid='obs-included']")?.getAttribute("title")).toBe("Ran on a subscription. No metered spend.");
    expect($$("[data-testid='obs-band']")).toHaveLength(1);
    expect($$("[data-testid='obs-legend-item']").map((b) => text(b))).toEqual(["claude-opus-4"]);
  });

  it("no calls: one line", async () => {
    await openObs("?view=models", { models: () => emptyModelsWire() });
    expect(text($("[data-testid='obs-models-empty']"))).toBe("No model calls yet");
  });

  it("the stream is the top 5 plus Other, each band keyed to the table", async () => {
    await openObs("?view=models");
    const bands = $$("[data-testid='obs-band']");
    expect(bands).toHaveLength(6);
    expect(bands.map((b) => b.getAttribute("data-slot"))).toEqual(["1", "2", "3", "4", "5", "other"]);
    const sw = (key: string) => $(`[data-testid='obs-group-row'][data-key='${key}'] .obs-swatch`)?.getAttribute("data-slot");
    expect(sw("claude-sonnet-4-5")).toBe("1");
    expect(sw("gpt-4o")).toBe("none");
  });

  it("the group switch and the metric live in the URL", async () => {
    const rec = await openObs("?view=models");
    await click($("[data-testid='obs-group-role']"));
    expect(params().get("group")).toBe("role");
    expect(rec.calls.some((u) => u.startsWith("/v1/observability/models?") && u.includes("group=role"))).toBe(true);
    expect(text($("#obs-models-table-head"))).toBe("AGENTS");
    await click($("[data-testid='obs-metric-latency']"));
    expect(params().get("metric")).toBe("latency");
    await click($("[data-testid='obs-group-model']"));
    expect(params().get("group")).toBeNull();
  });

  it("a band click opens that group's drawer with its recent calls, Show older pages", async () => {
    const rec = await openObs("?view=models");
    await act(async () => { $$("[data-testid='obs-band']")[0].dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    await settle();
    expect(params().get("model")).toBe("model:claude-sonnet-4-5");
    const drawer = $("[data-testid='obs-group-drawer']");
    expect(drawer).not.toBeNull();
    expect(text($("[data-testid='obs-group-stats']"))).toContain("612");
    const first = rec.calls.find((u) => u.startsWith("/v1/llm-calls?"))!;
    const q = new URLSearchParams(first.split("?")[1]);
    expect(q.get("model")).toBe("claude-sonnet-4-5");
    expect(q.get("since")).toBeTruthy();
    expect(q.get("limit")).toBe("20");
    expect($$("[data-testid='obs-call-row']")).toHaveLength(20);
    await click($("[data-testid='obs-calls-older']"));
    expect($$("[data-testid='obs-call-row']")).toHaveLength(26);
    expect($("[data-testid='obs-calls-older']")).toBeNull();
    expect($("[data-testid='obs-group-logs']")?.getAttribute("href")).toMatch(/^\?area=activity&src=llm_call/);
  });

  it("a row click opens the same drawer; Agent rows wear their face", async () => {
    await openObs("?view=models&group=role");
    const row = $("[data-testid='obs-group-row'][data-key='qa']")!;
    expect(row.querySelector(".ag-face")).not.toBeNull();
    await click(row);
    expect(params().get("model")).toBe("role:qa");
    expect($("[data-testid='obs-group-drawer']")).not.toBeNull();
  });

  it("a guest runner is a chip, not part of its name", async () => {
    await openObs("?view=models&group=engine");
    const row = $("[data-testid='obs-group-row'][data-key='guest:codex']")!;
    expect(text(row.querySelector(".obs-gname"))).toBe("codexguest");
  });

  it("FAILED CALLS and ROUTING read the view; a routing filter asks the route log", async () => {
    const rec = await openObs("?view=models");
    expect($$("[data-testid='obs-failed-row']")).toHaveLength(2);
    // Two of the 27 failed calls are listed: the head says so, and links to all of them.
    expect($("[data-testid='obs-failed-logs']")?.textContent?.trim()).toBe("2 of 27 · All ↗");
    expect($("[data-testid='obs-failed-head-count']")).toBeNull();
    expect($$("[data-testid='obs-route-row']")).toHaveLength(8);
    const sel = $("[data-testid='obs-route-outcome']") as HTMLSelectElement;
    sel.value = "denied";
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    await click($("[data-testid='obs-routing']"));
    const call = rec.calls.find((u) => u.startsWith("/v1/model-route-decisions?"));
    expect(call).toContain("denied_only=true");
    await click($("[data-testid='obs-route-all']"));
    expect($("[data-testid='obs-routing-sheet']")).not.toBeNull();
  });
});
