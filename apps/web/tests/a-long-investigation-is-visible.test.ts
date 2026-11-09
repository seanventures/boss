import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * "56s for a deep diagnostic against 2.4s before ==> this could be a concern, instead can we
 * display all the debugging info that it's doing during the investigation... just to show that
 * it's doing something rather giving the feeling to user that it's stuck."
 *
 * The chat turn already streamed its steps. The INVESTIGATION did not, and it is the long one:
 * it runs for minutes AFTER the turn has answered, so the console had stopped polling. The most
 * informative progress BOSS produces was written to a table nobody was reading.
 *
 * An operator who believes a system is stuck interrupts it. That is how the incident that
 * started all of this began.
 */
const UI = codeOnly("app/agent-ui.tsx");

describe("the indicator survives the turn that started the work", () => {
  it("polls while an investigation is running, not only while the turn is in flight", () => {
    expect(UI).toContain("!busy && !probe");
  });

  it("keeps showing the working line", () => {
    expect(UI).toContain("{busy || probe");
  });

  it("reads whether one is live from the same poll", () => {
    // One request, not two: this fires every 1.5s while anything is working.
    expect(UI).toContain("investigation?: { id: string; question: string } | null");
    expect(UI).toContain("const live = d.investigation ?? null;");
  });
});

describe("what it says while it works", () => {
  it("a real step beats any generic sentence", () => {
    expect(UI).toContain("steps.length\n                    ? steps[steps.length - 1]");
  });

  it("and the fallback says what a debugging session actually is", () => {
    expect(UI).toContain("Debugging, reading the code, the database and the host");
    expect(UI).toContain("This takes a few minutes");
  });

  it("no stall warning against a budget that does not apply", () => {
    // "Running long / ~120s" about work that is going exactly to plan teaches the operator to
    // ignore the warning on the turn that really has died.
    expect(UI).toContain('? `${elapsed}s${elapsed >= AGENT_NORMAL_SECONDS');
    expect(UI).toContain(': "investigating"');
  });
});

describe("the findings arrive without being asked for", () => {
  it("the thread reloads when the investigation ends", () => {
    expect(UI).toContain("if (was && !live) {");
    expect(UI).toContain('view: "messages"');
  });
});
