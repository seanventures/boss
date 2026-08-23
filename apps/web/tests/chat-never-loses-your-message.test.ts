import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { stripAppFacts } from "../app/agent-ui";

/**
 * The operator reported: "why my messages are disappearing".
 *
 * The console prepends a machine-addressed `[app context]` block so the tools know which run
 * the conversation is about, and strips it for display. The strip returned `""` for two real
 * inputs — a facts block with no message after it, and one followed by whitespace — and the
 * chat rendered an empty "You" bubble. From the operator's side their message was gone.
 *
 * A chat that loses what you typed cannot be trusted with anything you type next, so the rule
 * is absolute: a DISPLAY transform must never be able to erase its input.
 */

const FACTS = [
  "[app context] You are talking about ONE app in BOSS.",
  "run_id: crun_abc",
  "app: Shorty",
  "repo: paramg/boss-sandbox",
  "branch: boss/code/crun_abc",
  "status: completed (done)",
  "pull_request: https://github.com/x/y/pull/1",
  "Use your tools (inspect_app, diagnose_preview, diagnose_deployment) with run_id crun_abc when you need more.",
].join("\n");

describe("stripAppFacts", () => {
  it("hides the context block and keeps the message", () => {
    expect(stripAppFacts(`${FACTS}\n\nfix and retry the deployment`))
      .toBe("fix and retry the deployment");
  });

  it("keeps a message that itself contains blank lines", () => {
    expect(stripAppFacts(`${FACTS}\n\npara one\n\npara two`)).toBe("para one\n\npara two");
  });

  it("leaves a message with no context block untouched", () => {
    expect(stripAppFacts("fix and retry the deployment")).toBe("fix and retry the deployment");
  });

  // ---- the two inputs that produced an empty bubble ----
  it("never returns empty for a facts-only turn", () => {
    expect(stripAppFacts(FACTS)).not.toBe("");
  });

  it("never returns empty when only whitespace follows the block", () => {
    expect(stripAppFacts(`${FACTS}\n\n   `)).not.toBe("");
  });

  it("shows SOMETHING for every input that had content", () => {
    // The invariant, stated once: non-empty in, non-empty out.
    for (const input of [
      FACTS,
      `${FACTS}\n\n`,
      `${FACTS}\n\n \t `,
      `${FACTS}\n\nreal question`,
      "plain",
    ]) {
      expect(stripAppFacts(input).trim(), `erased: ${JSON.stringify(input.slice(0, 40))}`)
        .not.toBe("");
    }
  });

  it("handles null and undefined without throwing", () => {
    expect(stripAppFacts(undefined as unknown as string)).toBe("");
    expect(stripAppFacts(null as unknown as string)).toBe("");
  });
});

describe("the composer", () => {
  const src = readFileSync(resolve(__dirname, "../app/agent-ui.tsx"), "utf8");

  it("refuses a blank turn before it costs anything", () => {
    // An empty send produced an empty bubble AND a billed model call with nothing to answer.
    expect(src).toContain('const typed = String(fd.get("message") ?? "").trim();');
    expect(src).toContain("if (!typed) return;");
  });

  it("sends the trimmed text, not the raw field", () => {
    expect(src).toContain("`${appFacts}\\n\\n${typed}`");
  });
});
