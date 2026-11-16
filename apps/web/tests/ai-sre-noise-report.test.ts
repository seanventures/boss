/**
 * The AI SRE noise card shows the report the API returned.
 * It never invents a reduction ratio, and a missing report is not a zero.
 */
import { describe, expect, it } from "vitest";

import { noiseCard, type AiSreNoiseReport } from "../lib/ai-sre";

function report(over: Partial<AiSreNoiseReport> = {}): AiSreNoiseReport {
  return {
    raw_signal_count: 0,
    clustered_count: 0,
    reduction: { ratio: null },
    top_signatures: [],
    ...over,
  };
}

describe("noiseCard", () => {
  it("zeros hide the ratio", () => {
    expect(noiseCard(report({
      raw_signal_count: 0,
      clustered_count: 0,
      reduction: { ratio: 0.95 },
    }))).toEqual({
      raw: 0,
      clustered: 0,
      showRatio: false,
      ratio: null,
      signatures: [],
    });
  });

  it("a raw count of 4 with ratio 0.5 shows it", () => {
    // 0.5 is the API's ratio, not (4 - 1) / 4.
    expect(noiseCard(report({
      raw_signal_count: 4,
      clustered_count: 1,
      reduction: { ratio: 0.5 },
    }))).toEqual({
      raw: 4,
      clustered: 1,
      showRatio: true,
      ratio: 0.5,
      signatures: [],
    });
  });

  it("more than 10 signatures are cut to 10", () => {
    const top_signatures = Array.from({ length: 12 }, (_, i) => ({
      signature: `sig-${i}`,
      kind: "error",
      signal_rows: 12 - i,
      event_count: 100 + i,
    }));
    const card = noiseCard(report({
      raw_signal_count: 12,
      clustered_count: 3,
      reduction: { ratio: 0.25 },
      top_signatures,
    }));
    expect(card?.signatures).toHaveLength(10);
    expect(card?.signatures.map((row) => row.signature)).toEqual(
      Array.from({ length: 10 }, (_, i) => `sig-${i}`),
    );
    expect(card?.signatures[0]).toEqual({ signature: "sig-0", kind: "error", signal_rows: 12 });
    expect(card?.signatures[9]).toEqual({ signature: "sig-9", kind: "error", signal_rows: 3 });
  });

  it("a null report is the not-loaded case", () => {
    expect(noiseCard(null)).toBeNull();
  });
});
