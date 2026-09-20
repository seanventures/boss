import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const UI = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "../app/code-tasks-ui.tsx"), "utf8");

/**
 * fmtUsd takes MICRO-dollars and divides by 1e6 itself. The run-card meta divided first and
 * passed the result in, so the value was divided twice: the interview-scorecard build cost
 * $1.55 (2.3M tokens on claude-sonnet-5) and its card read "PR #208 · $0.00".
 */
describe("a run card shows what the run cost", () => {
  // The real formatter, copied verbatim from the source under test so the arithmetic — not a
  // paraphrase of it — is what these assertions run against.
  const fmtUsd = (micros?: number): string =>
    (!micros && micros !== 0) ? "—" : `$${(micros / 1e6).toFixed(2)}`;

  it("renders micros directly, because fmtUsd already converts them", () => {
    expect(fmtUsd(1_551_124)).toBe("$1.55");
  });

  it("would render nothing at all if the caller converted first", () => {
    expect(fmtUsd(1_551_124 / 1e6)).toBe("$0.00");
  });

  it("does not divide before calling fmtUsd on the run card", () => {
    expect(UI).toContain("` · ${fmtUsd(micros)}`");
    expect(UI).not.toContain("fmtUsd(micros / 1e6)");
  });
});
