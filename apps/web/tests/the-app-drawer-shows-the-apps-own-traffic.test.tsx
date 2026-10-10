// @vitest-environment happy-dom
/**
 * The app drawer's TRAFFIC section (engine app_metrics): the app's own request rate, 5xx error
 * rate and p50/p95 from its declared /metrics, the last scrape in words ("Metrics: scraped 20s
 * ago", or exactly what went wrong), and the error-rate alert with its rule in the tooltip.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { TrafficSection } from "../app/observability/app-drawer";
import { parseAppDetail, parseAppMetrics } from "../lib/obs-view";
import { $, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => unmount());

const NOW = Date.parse("2026-10-09T12:00:00Z");
const wire = (over: Record<string, unknown> = {}) => ({
  declared: true, path: "/metrics", via: "auto", interval_s: 120, state: "ok", word: "Scraped",
  last_scrape_at: "2026-10-09T11:59:40Z", last_ok_at: "2026-10-09T11:59:40Z", route: "public",
  url: "http://203.0.113.7:8000/metrics", naming: "prometheus", requests: 220, errors: 22,
  error_rate: 0.1, rate_per_min: 110, p50_ms: 62.5, p95_ms: 442.9, window_minutes: 5, series: [],
  alert: { state: "firing", word: "Alerting", incident_id: "inc_1", since: "2026-10-09T11:58:00Z",
           rule: { error_rate_percent: 5, window_minutes: 5, min_requests: 20, enabled: true } },
  ...over,
});
const txt = (sel: string) => ($(sel)?.textContent ?? "").replace(/\s+/g, " ").trim();

describe("the app's own traffic", () => {
  it("shows rate, errors, p50/p95 and how long ago it was scraped", async () => {
    await mount(<TrafficSection m={parseAppMetrics(wire())} now={NOW} />);
    const facts = txt("[data-testid='obs-app-metrics']");
    expect(facts).toContain("Requests110/min");
    expect(facts).toContain("Errors10.0%");
    expect(facts).toContain("p5063 ms");
    expect(facts).toContain("p95443 ms");
    expect(txt("[data-testid='obs-metrics-word']")).toBe("Metrics: scraped 20s ago");
    const alert = $("[data-testid='obs-metrics-alert']");
    expect(alert?.textContent).toBe("Alerting");
    expect(alert?.getAttribute("data-tone")).toBe("red");
    expect(alert?.getAttribute("title")).toContain("more than 5% of requests fail (5xx) over 5 min");
  });

  it("says what went wrong with the scrape in words", async () => {
    await mount(<TrafficSection m={parseAppMetrics(wire({ state: "failing", word: "Metrics endpoint returned 404",
      requests: null, errors: null, error_rate: null, rate_per_min: null, p50_ms: null, p95_ms: null }))} now={NOW} />);
    expect(txt("[data-testid='obs-metrics-word']")).toBe("Metrics endpoint returned 404");
    expect($("[data-testid='obs-metrics-word']")?.getAttribute("data-tone")).toBe("red");
    expect(txt("[data-testid='obs-app-metrics']")).toContain("Requests—");
  });

  it("an app that declares nothing says how to declare it", async () => {
    await mount(<TrafficSection m={parseAppMetrics({ declared: false, word: "Not declared",
      tip: "Add metrics: {path: /metrics} to boss.deploy.yaml and redeploy" })} now={NOW} />);
    const el = $("[data-testid='obs-metrics-word']");
    expect(el?.textContent).toBe("Metrics not declared");
    expect(el?.getAttribute("title")).toContain("boss.deploy.yaml");
  });

  it("an older server with no metrics block parses as not declared", () => {
    expect(parseAppDetail({ target_id: "t" }).metrics.declared).toBe(false);
  });
});
