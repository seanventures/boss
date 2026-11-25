// @vitest-environment happy-dom
/**
 * The workforce card says the agent's autonomy level beside the configured trust score.
 * A missing level adds no sentence. "Autonomy level 1." is three words, so a span is enough.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { WorkforceScreen, autonomyLevelSentence } from "../components/v2/WorkforceScreen";
import type { V2Agent, Workforce } from "../lib/v2/workforce";

const agent = (over: Partial<V2Agent> = {}): V2Agent => ({
  id: "coder",
  name: "Coder",
  role: "Coder",
  status: "WORKING",
  task: "implement",
  model: "claude-opus-5",
  tokens: null,
  spend_usd: null,
  calls: null,
  autonomy_level: 1,
  trust_score: 50,
  successful_runs: 4,
  violations: 0,
  hidden_failures: 0,
  last_seen_seconds: 42,
  run_id: "run_1",
  project: "Shorty",
  last_touched: "src/routes/shorten.ts",
  registered: true,
  ...over,
});

function workforce(a: V2Agent): Workforce {
  return {
    project_id: "boss-dogfood",
    window_hours: 24,
    agents: [a],
    handoffs: [],
    counts: { WORKING: 1, STUCK: 0, IDLE: 0, "AT GATE": 0, total: 1 },
    apiReachable: true,
  };
}

function draw(a: V2Agent = agent()): HTMLElement {
  document.body.innerHTML = renderToStaticMarkup(<WorkforceScreen workforce={workforce(a)} />);
  return document.body;
}

function card(body: HTMLElement): HTMLElement {
  const el = body.querySelector('[data-testid="v2-agent-coder"]');
  expect(el).not.toBeNull();
  return el as HTMLElement;
}

function trust(cardEl: HTMLElement): HTMLElement {
  const el = cardEl.querySelector('[title="configured trust level, not an observed score"]');
  expect(el).not.toBeNull();
  return el as HTMLElement;
}

function levelSpan(cardEl: HTMLElement, sentence: string): HTMLElement | undefined {
  return [...cardEl.querySelectorAll("span")].find((el) => el.textContent === sentence);
}

describe("autonomyLevelSentence", () => {
  it("names a numeric autonomy level", () => {
    expect(autonomyLevelSentence(1)).toBe("Autonomy level 1.");
    expect(autonomyLevelSentence(0)).toBe("Autonomy level 0.");
    expect(autonomyLevelSentence(3)).toBe("Autonomy level 3.");
  });

  it("returns null when the level is null or missing", () => {
    expect(autonomyLevelSentence(null)).toBeNull();
    expect(autonomyLevelSentence(undefined)).toBeNull();
  });
});

describe("the workforce card says the autonomy level", () => {
  it("shows Autonomy level 1. beside the trust score", () => {
    const cardEl = card(draw());
    const said = levelSpan(cardEl, "Autonomy level 1.");
    expect(said?.textContent).toBe("Autonomy level 1.");
    expect(said?.tagName).toBe("SPAN");
    expect(said?.previousElementSibling).toBe(trust(cardEl));
  });

  it("shows autonomy level 0, which is a level and not a missing one", () => {
    const cardEl = card(draw(agent({ autonomy_level: 0 })));
    const said = levelSpan(cardEl, "Autonomy level 0.");
    expect(said?.textContent).toBe("Autonomy level 0.");
    expect(said?.tagName).toBe("SPAN");
    expect(said?.previousElementSibling).toBe(trust(cardEl));
  });

  it("keeps the trust words and adds no sentence when the level is missing", () => {
    const cardEl = card(draw(agent({ autonomy_level: undefined as unknown as number })));
    const trustEl = trust(cardEl);
    expect(trustEl.textContent).toContain("trust");
    expect(trustEl.textContent).toContain("set");
    expect(trustEl.getAttribute("title")).toBe("configured trust level, not an observed score");
    expect(cardEl.textContent).not.toContain("Autonomy level");
  });
});
