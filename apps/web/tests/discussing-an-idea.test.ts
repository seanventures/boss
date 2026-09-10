/**
 * A place to think about an idea before committing to it.
 *
 * "BOSS has no good back-and-forth surface for an idea... I want to discuss back and forth just
 * like discussing with chatgpt."
 *
 * The chat existed; its head was in the wrong place. The system prompt is entirely about the
 * running factory — report status, create work, ground every claim in a tool result — so asked
 * whether an idea was worth building it went and read deployments. Correct behaviour for the
 * question it thought it had, useless for the one asked.
 *
 * What makes this a discussion rather than a second answer box:
 *   it ASKS      — the value is being asked what you had not considered
 *   it DISAGREES — the best sentence BOSS has produced about an idea was "your differentiator
 *                  is factually wrong, a competitor already ships it"
 *   it STARTS NOTHING — a surface for thinking that quietly commits is not one
 *   it ENDS IN SOMETHING — the conversation becomes the brief, in one press
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const HERO = codeOnly("app/ask-boss-hero.tsx");
const DOCK = codeOnly("app/agent-ui.tsx");
const ROUTER = codeOnly("../../engine/boss_engine/services/intent_router.py");

describe("the idea surface exists and is reachable", () => {
  it("Discuss is offered where an idea is typed", () => {
    const at = HERO.indexOf("others={[");
    const block = HERO.slice(at, at + 900);
    expect(block).toContain('id: "discuss"');
    expect(block).toContain('cost: "free"');
  });

  it("and it opens a conversation rather than answering once", () => {
    expect(HERO).toContain('url.searchParams.set("dock_mode", "discuss")');
    expect(HERO).toContain('url.searchParams.set("dock_ask", seed)');
  });

  it("Ask survives, renamed for what it actually does", () => {
    // It answers from live control-plane state. Calling it "Ask" beside an idea box implied it
    // would think about the idea; it reads deployments.
    expect(HERO).toContain('label: "Ask about the system"');
  });

  it("the dock carries the mode on every turn of the thread", () => {
    // Read from the URL, not held in state, so reopening the thread keeps its purpose.
    expect(DOCK).toContain('fd.set("mode", "discuss")');
    expect(DOCK).toContain('get("dock_mode") === "discuss"');
  });
});

describe("what the discussing agent is told", () => {
  it("it is told this is not the factory", () => {
    expect(ROUTER).toContain("YOU ARE DISCUSSING AN IDEA, NOT OPERATING THE FACTORY");
  });

  it("to ask, and to disagree", () => {
    expect(ROUTER).toContain("Ask the question that decides it");
    expect(ROUTER).toContain("Disagree when you have reason to");
  });

  it("to say when the answer needs evidence it does not have", () => {
    // Guessing confidently about market size is the failure mode of a chat that wants to be
    // helpful.
    expect(ROUTER).toContain("say which fact would settle it and offer");
  });

  it("and to start nothing at all", () => {
    // Not even offered as a button: this is the stage before committing.
    expect(ROUTER).toContain("No build, no research run, no code task, no ");
    expect(ROUTER).toContain("not even offered as a button");
  });

  it("the mode is off by default, so the operating agent is unchanged", () => {
    expect(ROUTER).toContain('mode: str = "operate"');
    expect(ROUTER).toContain('_discussion_brief() if mode == "discuss" else ""');
  });
});

describe("the conversation becomes the thing you build", () => {
  it("a brief the agent wraps is offered as one press", () => {
    expect(DOCK).toContain("BOSS-BRIEF");
    expect(DOCK).toContain('data-testid="agent-brief-use"');
  });

  it("and it is still a press — the chat never starts a build itself", () => {
    const at = DOCK.indexOf("const useBrief");
    const fn = DOCK.slice(at, at + 700);
    expect(fn).toContain('url.searchParams.set("compose", brief)');
    expect(fn).not.toContain("assign(");
    expect(DOCK).toContain("Nothing starts until you press Build");
  });

  it("the composer picks it up instead of losing it", () => {
    // Arriving at an empty box would throw away the part that took the effort.
    expect(HERO).toContain('url.searchParams.get("compose")');
    expect(HERO).toContain("setInput(brief)");
  });

  it("and the marker is not left in the prose", () => {
    expect(DOCK).toContain("withoutBrief");
  });
});
