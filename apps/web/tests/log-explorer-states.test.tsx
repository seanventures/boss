// @vitest-environment happy-dom
/**
 * Every state of the Activity log, by its exact words (activity-govern spec §4.2.10), rendered
 * from fixtures through the real component and the real route-handler URLs.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { FACETS, fakeLogApi, histogramOf, newLog, row, rows, type FakeLog } from "./_helpers/log-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));

const { LogExplorer } = await import("../app/activity/log-explorer");

let log: FakeLog;
const at = (search: string) => window.history.replaceState({}, "", `/${search}`);
const text = (sel: string) => $(sel)?.textContent ?? null;

beforeEach(() => {
  log = newLog();
  vi.stubGlobal("fetch", fakeLogApi(log));
  setConsoleLive(true);
  at("?org=o1&area=activity");
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); setConsoleLive(true); });

describe("the Log says what it has, exactly", () => {
  it("first load is a skeleton, with no words", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    const skeleton = $("[data-testid=lx-loading]")!;
    expect(skeleton.getAttribute("aria-busy")).toBe("true");
    expect(skeleton.textContent).toBe("");
  });

  it("loaded rows, with the header count from the pulse", async () => {
    log.page.items = rows(3);
    log.histogram = histogramOf([10, 20, 30]);
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect($$("[data-testid=lx-row]")).toHaveLength(3);
    expect(text("[data-testid=lx-count]")).toBe("61 lines in 24h");
    expect(text("[data-testid=lx-tail]")).toBe("Live");
  });

  it("a count that is a floor says ≥", async () => {
    log.page.items = rows(1);
    log.histogram = { ...histogramOf([5]), total_is_exact: false };
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect(text("[data-testid=lx-count]")).toBe("≥5 lines in 24h");
  });

  it("an empty range offers the next wider one", async () => {
    log.histogram = histogramOf([0, 0]);
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect(text("[data-testid=lx-empty]")).toBe("No lines in this rangeWiden to 7d");
    await click($("[data-testid=lx-widen]"));
    expect(new URLSearchParams(window.location.search).get("range")).toBe("7d");
  });

  it("filters that match nothing offer to clear them", async () => {
    at("?area=activity&level=error");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect(text("[data-testid=lx-empty]")).toBe("Nothing matchesClear filters");
    await click($("[data-testid=lx-clear-filters]"));
    expect(new URLSearchParams(window.location.search).has("level")).toBe(false);
  });

  it("a log with nothing in 30 days is a fresh install: start work", async () => {
    at("?area=activity&range=30d");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect($("[data-testid=lx-empty]")!.textContent).toContain("Nothing logged yet");
    expect($("[data-testid=lx-empty] a")!.getAttribute("href")).toContain("area=delivery_workflows");
  });

  it("an unreachable API is one banner with Retry, never two", async () => {
    log.status = 502;
    log.error = "connect ECONNREFUSED";
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect($$("[data-testid=lx-unreachable]")).toHaveLength(1);
    expect(text("[data-testid=lx-unreachable]")).toBe("Can't reach BOSSRetry");
    expect($("[data-testid=lx-unreachable] span")!.getAttribute("title")).toBe("connect ECONNREFUSED");
  });

  it("a query the server refuses says the server's sentence under the bar", async () => {
    log.status = 422;
    log.error = "step_id needs workflow_run_id: a step id is only unique within its run.";
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect(text("[data-testid=lx-query-error]")).toBe(log.error);
  });

  it("Paused says so, and reads nothing new", async () => {
    log.page.items = rows(2);
    setConsoleLive(false);
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect(text("[data-testid=lx-tail]")).toBe("Paused");
    expect($("[data-testid=lx-tail]")!.getAttribute("title")).toBe("Console is paused. Nothing updates.");
  });

  it("a work scope from the river reads its name before the server's labels (D7)", async () => {
    at("?area=activity&act=act_9&run_name=Ship+it+%23182");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect(text("[data-testid=lx-token-act]")).toBe("work: Ship it #182");
    expect(new URLSearchParams(window.location.search).get("run_name")).toBe("Ship it #182");
  });

  it("a finished step with no lines says so without \"yet\" (D16)", async () => {
    log.page.labels = { "wfr_1/plan@state": "finished" };
    at("?area=activity&wfr=wfr_1&step=plan&step_name=Plan+it");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect(text("[data-testid=lx-empty]")).toBe("Nothing logged for this step");
  });

  it("a step's log is two tokens with names, Whole run, and its own empty words", async () => {
    at("?area=activity&wfr=wfr_1&step=plan&step_name=Plan+it&run_name=Lead+Triage+Inbox");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect(text("[data-testid=lx-token-wfr]")).toBe("wfr: Lead Triage Inbox");
    expect(text("[data-testid=lx-token-step]")).toBe("step: Plan it");
    expect(text("[data-testid=lx-empty]")).toBe("Nothing logged for this step yet");
    const list = log.asked.filter((u) => u.startsWith("/api/activity?"));
    expect(list.at(-1)).toContain("workflow_run_id=wfr_1");
    expect(list.at(-1)).toContain("step_id=plan");
    await click($("[data-testid=lx-whole-run]"));
    expect(new URLSearchParams(window.location.search).has("step")).toBe(false);
    expect($("[data-testid=lx-token-wfr]")).not.toBeNull();
  });

  it("a from= that isn't a date is one state: dropped, said once, and the log reads (D14a)", async () => {
    log.page.items = rows(3);
    at("?area=activity&from=yesterday-ish&to=nope");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect(text("[data-testid=lx-query-error]")).toBe("That range isn't a date. Showing the last 24h.");
    expect($("[data-testid=lx-unreachable]")).toBeNull();
    expect($$("[data-testid=lx-row]").length).toBeGreaterThan(0);
    expect(log.asked.filter((u) => u.includes("yesterday-ish") || u.includes("since=nope"))).toEqual([]);
  });

  it("the gone-row route answers 200, so the browser logs no failed resource (D14b)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "This line is gone." }, { status: 404 })));
    const { GET } = await import("../app/api/activity/row/[id]/route");
    const got = await GET(new Request("http://console.test/api/activity/row/evt_pruned?project_id=p1"),
                          { params: Promise.resolve({ id: "evt_pruned" }) });
    expect(got.status).toBe(200);
    expect(await got.json()).toEqual({ item: null, gone: true, labels: {} });
  });

  it("a deep link to a line that is gone says so", async () => {
    log.page.items = rows(2);
    at("?area=activity&row=evt_pruned");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect(log.asked.some((u) => u.startsWith("/api/activity/row/evt_pruned"))).toBe(true);
    expect(text("[data-testid=lx-row-gone]")).toBe("This line is gone");
    expect($("[data-testid=lx-row-gone]")!.getAttribute("title")).toBe("Older than the retention window");
  });

  it("the rail counts come from the server's facets, not the page", async () => {
    log.page.items = rows(2);
    log.facets = FACETS;
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect($("[data-testid=lx-level-error] .lx-count")!.textContent).toBe("12");
    expect($("[data-testid=lx-level-info] .lx-count")!.textContent).toBe("4,100");
    expect($("[data-testid=lx-hide-verbose]")!.textContent).toContain("900 hidden");
    expect($("[data-testid=lx-agent-software_engineer]")!.textContent).toContain("300");
    expect($("[data-testid=lx-run-crun_7]")!.textContent).toContain("Ship it #182");
  });

  it("an export downloads the current filter and says when it was capped", async () => {
    log.page.items = rows(1, () => ({ level: "error" }));
    at("?area=activity&level=error");
    const exported: string[] = [];
    const base = fakeLogApi(log);
    vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
      const u = String(input);
      if (u.startsWith("/api/activity/export")) {
        exported.push(u);
        return new Response("created_at,level\n", { headers: { "x-truncated": "1" } });
      }
      return base(input, init);
    }));
    const toasts: string[] = [];
    window.addEventListener("boss:toast", (e) => toasts.push((e as CustomEvent).detail.message));
    (URL as unknown as { createObjectURL: () => string }).createObjectURL = () => "blob:x";
    (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = () => {};
    // The download itself is the browser's; a test must not navigate the page to a blob.
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    await click($("[data-testid=lx-export]"));
    await click($("[data-testid=lx-export-csv]"));
    await settle();
    expect(exported).toHaveLength(1);
    const q = new URLSearchParams(exported[0].split("?")[1]);
    expect(q.get("level")).toBe("error");
    expect(q.get("format")).toBe("csv");
    expect(toasts).toContain("First 50,000 lines exported");
    expect(anchorClick).toHaveBeenCalledTimes(1);
    anchorClick.mockRestore();
  });
});

describe("a line, read", () => {
  it("opening a line writes row= and the drawer shows its facts and links", async () => {
    log.page.items = [row("evt_1", { source: "llm_call", kind: "llm.implement", level: "error", title: "Model call failed",
                                    agent: "software_engineer", run_id: "crun_7", workflow_run_id: "wfr_1",
                                    body: "claude · 3 turns · rate limited",
                                    data: { cost_usd: 0.03, latency_ms: 2400, act_id: "act_1" } })];
    log.page.labels = { crun_7: "Ship it #182", wfr_1: "Lead Triage" };
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    const line = $("[data-testid=lx-row]")!;
    expect(line.getAttribute("aria-label")).toMatch(/^\d\d:\d\d:\d\d, Error, .+, Model call failed$/);
    expect(line.querySelector(".lx-run")!.textContent).toBe("Lead Triage");
    expect(line.querySelector(".lx-cost")!.textContent).toBe("$0.03");
    expect(line.querySelector(".lx-took")!.textContent).toBe("2.4s");
    await click(line);
    expect(new URLSearchParams(window.location.search).get("row")).toBe("evt_1");
    const drawer = $("[data-testid=lx-drawer]")!;
    expect(drawer.querySelector("h2")!.textContent).toBe("Model call failed");
    const links = $$("[data-testid^=lx-link-]").map((a) => a.getAttribute("data-testid"));
    expect(links).toEqual(["lx-link-run", "lx-link-build", "lx-link-studio", "lx-link-work"]);
    expect($("[data-testid=lx-link-build]")!.getAttribute("href")).toContain("run=crun_7");
    await click($("[data-testid=lx-around]"));
    expect(log.asked.some((u) => u.startsWith("/api/activity/context?") && u.includes("id=evt_1"))).toBe(true);
    expect($("[data-testid=lx-around-list]")).not.toBeNull();
  });

  it("a clipped turn says it was clipped, with the real size", async () => {
    log.page.items = [row("t1", { source: "agent_turn", level: "verbose", body: "x".repeat(50),
                                  data: { truncated: true, content_bytes: 120000 } })];
    at("?area=activity&verbose=1&row=t1");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect(text("[data-testid=lx-clip]")).toBe("Clipped when stored · 120,000 bytes captured");
    expect($("[data-testid=lx-clip]")!.getAttribute("title")).toBe("A clipped line is not a short one");
  });
});
