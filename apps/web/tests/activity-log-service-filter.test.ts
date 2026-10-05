import { describe, expect, it } from "vitest";

import { mapActivityLog } from "../lib/activity-log";
import { LOG_FORWARD } from "../lib/activity-proxy";
import { apiParams, readLogParams } from "../lib/log-url";
import { SERVICE_LABEL } from "../app/activity/log-model";

/**
 * "Which BOSS process said this" — api or worker (the rail's PROCESS facet, token `svc:`).
 *
 * Container logs from two processes land in one stream; without this facet the API's request
 * handling and a worker's build timings are read interleaved.
 */
describe("the filter reaches the API", () => {
  it("a svc token is sent as `service`, and nothing is sent without one", () => {
    expect(apiParams(readLogParams("?svc=worker")).service).toBe("worker");
    expect(apiParams(readLogParams("")).service).toBeUndefined();
  });

  it("is on the route's forward list", () => {
    expect(LOG_FORWARD as readonly string[]).toContain("service");
  });

  it("names the two processes as an operator does", () => {
    expect(SERVICE_LABEL).toEqual({ api: "API", worker: "Worker" });
  });
});

describe("a row knows its process, or says nothing", () => {
  it("maps `service`, and leaves it empty rather than guessing", () => {
    const log = mapActivityLog({ items: [
      { id: "a", source: "app_log", service: "worker", created_at: "2026-10-04T10:00:00Z" },
      { id: "b", source: "agent_turn", created_at: "2026-10-04T10:00:00Z" },
    ] });
    expect(log.items.map((i) => i.service)).toEqual(["worker", ""]);
  });
});
