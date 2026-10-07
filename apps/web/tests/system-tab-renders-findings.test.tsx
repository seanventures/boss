// @vitest-environment happy-dom
/**
 * Observability › System (spec §4.11, on the stability work's data): the system doctor's
 * findings are AttentionCards — an option button only where the finding offers one, and a risky
 * option asks first; the gauges draw a bar only from a real percent (or load per core against
 * 1.0) and say `Not measurable here` where the machine gave nothing; a shadow stall reads
 * `would … (shadow)`; the host advice line is here too.
 */
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { NOW_MS, systemWire, unmeasuredSystemWire } from "./_helpers/obs-fixtures";
import { $, $$, click, unmount } from "./_helpers/live-render";
import { openObs, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("findings", () => {
  it("option buttons only where a finding offers them", async () => {
    await openObs("?view=system");
    const cards = $$("[data-testid='obs-finding']");
    expect(cards).toHaveLength(2);
    const mem = cards[0];
    expect(mem.querySelector("[data-testid='ag-att-option-free_caches']")).not.toBeNull();
    expect(mem.querySelector("[data-testid='ag-att-option-stop_idle_previews']")).not.toBeNull();
    expect(cards[1].querySelectorAll("[data-testid^='ag-att-option-']")).toHaveLength(0);
    expect(text($("#obs-findings-head"))).toBe("FINDINGS");
    expect(text($("[data-testid='obs-findings-head-count']"))).toBe("2");
  });

  it("a risky option asks before it acts", async () => {
    await openObs("?view=system");
    await click($("[data-testid='ag-att-option-stop_idle_previews']"));
    expect($("[data-testid='ag-att-option-confirm']")).not.toBeNull();
  });

  it("no findings: Nothing found", async () => {
    await openObs("?view=system", { system: systemWire({ findings: [] }) });
    expect(text($("[data-testid='obs-findings-none']"))).toBe("Nothing found");
  });

  it("a new finding enters once; the ones already there hold still", async () => {
    await openObs("?view=system");
    expect($$("[data-testid='obs-finding'][data-new]")).toHaveLength(0);
  });
});

describe("BOSS MACHINE", () => {
  it("gauges from the bands, the build, and which machine it measured", async () => {
    // The build's age is read against the clock: pin it to the fixture's NOW (built 2h before),
    // or the "· 2h" turns into "· 3d" as the calendar moves past the fixture date.
    vi.useFakeTimers({ toFake: ["Date"], now: NOW_MS });
    onTestFinished(() => { vi.useRealTimers(); });
    await openObs("?view=system");
    const mem = $("[data-testid='obs-gauge-memory']")!;
    expect(text(mem)).toBe("Memory78%High");
    expect(mem.querySelector("rect.obs-gauge-fill")?.getAttribute("width")).toBe("78");
    expect(text($("[data-testid='obs-gauge-cpu']"))).toBe("CPU0.41/coreOK");
    expect($("[data-testid='obs-gauge-cpu'] rect.obs-gauge-fill")?.getAttribute("width")).toBe("41");
    // Swap has no percent: a value, no bar.
    expect($("[data-testid='obs-gauge-swap'] svg")).toBeNull();
    expect(text($("[data-testid='obs-gauge-swap']"))).toBe("Swap0 MBOK");
    expect(text($("[data-testid='obs-build']"))).toBe("build 4c1d2e9 · 2h");
    expect($("[data-testid='obs-machine-scope']")?.getAttribute("title")).toContain("not your deploy hosts");
    expect(text($("[data-testid='obs-oom']"))).toBe("New OOM kills0");
  });

  it("where the machine gives nothing, each gauge says so", async () => {
    await openObs("?view=system", { system: unmeasuredSystemWire() });
    expect($$("[data-testid$='-absent']").map((e) => text(e))).toEqual(Array(4).fill("Not measurable here"));
    expect($("[data-testid='obs-oom']")).toBeNull();
    expect($("[data-testid='obs-machine-scope']")?.getAttribute("title")).toBe("Measured inside the BOSS container, not your deploy hosts");
    expect(text($("[data-testid='obs-stalls-none']"))).toBe("No stalls");
  });
});

describe("work, stalls and advice", () => {
  it("reads the queue and the stalls, a shadow stall said as one", async () => {
    await openObs("?view=system");
    expect(text($("[data-testid='obs-work'] .obs-work"))).toBe("Queue 3Leased 2Running 2In flight 4At once 2 (auto)");
    expect($$("[data-testid='obs-stall-strip'] rect[data-k='stall']")).toHaveLength(2);
    const shadow = $("[data-testid='obs-stall-shadow']");
    expect(text(shadow)).toBe("would retry (shadow)");
    expect(shadow?.getAttribute("title")).toBe("Watching only. Nothing was changed.");
    await click($("[data-testid='obs-stall-table-toggle']"));
    expect($$("[data-testid='obs-stall-table'] tbody tr")).toHaveLength(96);
  });

  it("the host advice line and what BOSS did", async () => {
    await openObs("?view=system");
    const adv = $("[data-testid='obs-system-advice']");
    expect(text(adv)).toContain("Run fewer builds at once");
    expect(adv?.getAttribute("title")).toBe("Two builds hit the memory cap twice today.");
    expect($$("[data-testid='obs-action-row']")).toHaveLength(2);
  });

  it("the System tab's dot says the machine is under pressure", async () => {
    await openObs("?view=system");
    const dot = $("[data-testid='obs-tab-system'] [role='img']");
    expect(dot?.getAttribute("aria-label")).toBe("BOSS's machine: Memory 78% used");
  });
});
