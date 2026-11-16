import { readFileSync } from "node:fs";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { LatencyObjectiveLine, latencyObjectiveWords } from "../app/projects/tab-health";
import { parseHealth, type HealthView } from "../lib/project-experience";

const env = (extra: Record<string, unknown> = {}) => ({
  key: "staging", label: "Staging", target_id: "tgt", url: "", health: "healthy",
  uptime_pct: 99.2, error_rate: 0.4, p95_ms: 180,
  series: { errors: [], latency: [] },
  slo: { target: 99.5, burn: "ok", budget_used_pct: 22, hours_left: 1, budget_words: "1 hours left." },
  ...extra,
});

type Env = HealthView["environments"][number];

function line(parsed: Env): string {
  return renderToStaticMarkup(<LatencyObjectiveLine e={parsed} />);
}

describe("a latency objective is copied only when the payload included it", () => {
  it("leaves today's card alone when the key is missing", () => {
    const parsed = parseHealth({ environments: [env()] }).environments[0];
    expect(parsed).not.toHaveProperty("latency_objective_ms");
    expect(parsed).not.toHaveProperty("latency_words");
    expect(parsed.p95_ms).toBe(180);
    expect(parsed.slo.hours_left).toBe(1);
    expect(parsed.slo.budget_words).toBe("1 hours left.");
    expect(latencyObjectiveWords(parsed)).toBeNull();
    expect(line(parsed)).toBe("");
  });

  it("shows the stored milliseconds, and an explicit null says none is stored", () => {
    const stored = parseHealth({ environments: [env({
      latency_objective_ms: 250, latency_words: "Latency objective is 250 ms.",
    })] }).environments[0];
    expect(stored.latency_objective_ms).toBe(250);
    expect(stored.latency_words).toBe("Latency objective is 250 ms.");
    expect(latencyObjectiveWords(stored)).toBe("Latency objective is 250 ms.");
    const html = line(stored);
    expect(html).toContain('data-testid="px-h-env-latency-staging"');
    expect(html).toContain("Latency objective is 250 ms.");

    const none = parseHealth({ environments: [env({
      latency_objective_ms: null, latency_words: "No latency objective is stored.",
    })] }).environments[0];
    expect(none.latency_objective_ms).toBeNull();
    expect(none.latency_words).toBe("No latency objective is stored.");
    expect(latencyObjectiveWords(none)).toBe("No latency objective is stored.");
    expect(line(none)).toContain("No latency objective is stored.");
  });

  it("does not invent a number from a string, and does not copy one key as the other", () => {
    const text = parseHealth({ environments: [env({ latency_objective_ms: "300" })] }).environments[0];
    expect(text).not.toHaveProperty("latency_objective_ms");
    expect(text.p95_ms).toBe(180);
    expect(latencyObjectiveWords(text)).toBeNull();

    const onlyMs = parseHealth({ environments: [env({ latency_objective_ms: 80 })] }).environments[0];
    expect(onlyMs.latency_objective_ms).toBe(80);
    expect(onlyMs).not.toHaveProperty("latency_words");
    expect(latencyObjectiveWords(onlyMs)).toBe("Latency objective is 80 ms.");

    const onlyWords = parseHealth({ environments: [env({
      latency_words: "Latency objective is 80 ms.",
    })] }).environments[0];
    expect(onlyWords.latency_words).toBe("Latency objective is 80 ms.");
    expect(onlyWords).not.toHaveProperty("latency_objective_ms");
    expect(latencyObjectiveWords(onlyWords)).toBeNull();
  });

  it("sits on the p95 latency number and does not touch the hours-left line", () => {
    const src = readFileSync(new URL("../app/projects/tab-health.tsx", import.meta.url), "utf8");
    const p95 = src.slice(src.indexOf("px-h-env-p95-"), src.indexOf("px-h-env-slo-"));
    expect(p95).toContain('{e.p95_ms ?? "—"}');
    expect(p95).toContain("<LatencyObjectiveLine e={e} />");
    expect(p95).not.toContain("300");
    const hours = src.slice(src.indexOf("px-h-env-hours-"), src.indexOf("px-h-env-hours-") + 180);
    expect(hours).toContain("{e.slo.budget_words}");
  });
});
