import { describe, expect, it } from "vitest";

import { activityLogSearchParams, mapActivityLog } from "../lib/activity-log";
import { codeOnly } from "./_helpers/source";

/**
 * "Which BOSS process said this" — api or worker.
 *
 * Container logs from two processes land in one stream. Without this facet, reading the API's
 * request handling and a worker's build timings meant reading them interleaved, which is
 * exactly when an operator gives up and asks for a shell on the box.
 */
const UI = codeOnly("app/activity-log-ui.tsx");
const ROUTE = codeOnly("app/api/activity/route.ts");

describe("the filter reaches the API", () => {
  it("forwards `service` as a query param", () => {
    const params = activityLogSearchParams("boss-dogfood", { service: "worker" });
    expect(params.get("service")).toBe("worker");
  });

  it("omits it entirely when nothing is selected", () => {
    // An empty string would narrow to rows whose service is "", which is the opposite of
    // "every process".
    expect(activityLogSearchParams("boss-dogfood", {}).has("service")).toBe(false);
    expect(activityLogSearchParams("boss-dogfood", { service: "" }).has("service")).toBe(false);
  });

  it("is on the route's forward list", () => {
    // A param missing here is dropped silently: the dropdown would move and nothing would
    // narrow, which reads as a broken filter rather than a missing passthrough.
    expect(ROUTE).toContain('"service"');
  });
});

describe("the response carries the process back", () => {
  it("maps `service` onto the item", () => {
    const log = mapActivityLog(
      { items: [{ id: "alog_1", source: "app_log", service: "worker",
                 created_at: "2026-08-30" }] }, "boss-dogfood");
    expect(log.items[0].service).toBe("worker");
  });

  it("leaves it empty rather than guessing, for sources with no process", () => {
    // Events, receipts and jobs do not record one. Defaulting them to "api" would make the
    // filter assert an origin BOSS never observed.
    const log = mapActivityLog(
      { items: [{ id: "evt_1", source: "event", created_at: "2026-08-30" }] },
      "boss-dogfood");
    expect(log.items[0].service).toBe("");
  });
});

describe("the control", () => {
  it("offers a fixed api/worker list, not one derived from the feed", () => {
    // Deriving options from the rows on screen is self-defeating for a two-value facet: after
    // picking "api" the feed holds only api rows, so "worker" would disappear from the very
    // control used to switch back.
    const block = UI.slice(UI.indexOf("activity-log-service-filter") - 700,
                           UI.indexOf("activity-log-service-filter") + 400);
    expect(block).toContain('value="api"');
    expect(block).toContain('value="worker"');
    expect(block).toContain('<option value="">');
  });

  it("re-fetches when it changes", () => {
    // A filter left out of the effect's dependencies moves but never queries.
    const deps = UI.slice(UI.indexOf("}, [projectId, debouncedSearch"));
    expect(deps.slice(0, 200)).toContain("serviceFilter");
  });
});

describe("an empty result says which kind of empty it is", () => {
  it("counts every narrowing facet as a filter", () => {
    // This decides between "nothing matches these filters" and "nothing was ever recorded".
    // The Process filter excludes every source that does not record a process, so it lands on
    // an empty result more often than any other — and claiming BOSS had recorded nothing at
    // all would be flatly untrue.
    const line = UI.slice(UI.indexOf("const hasFilters"), UI.indexOf("const stuckCount"));
    for (const facet of ["level", "source", "agentFilter", "serviceFilter", "scopedRun"]) {
      expect(line).toContain(facet);
    }
  });
});
