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
const SHELL = codeOnly("app/ConsoleShell.tsx");
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

  it("there is ONE entry, not one per kind of question", () => {
    // "Discuss it" and "Ask about the system" were the same model over the same tools writing
    // into the same threads, separated by a paragraph of prompt — so the menu asked the
    // operator to classify their own question before they were allowed to ask it.
    expect(HERO).toContain('label: "Talk to BOSS"');
    expect(HERO).not.toContain('label: "Ask about the system"');
  });

  it("and the console no longer classifies the question on the wire", () => {
    expect(DOCK).not.toContain('fd.set("mode", "discuss")');
    // The flag that remains says how the dock was OPENED — with a subject in hand — not which
    // of two agents is answering.
    expect(DOCK).toContain('get("dock_mode") === "discuss"');
  });
});

describe("a discussion is a new conversation with nobody in particular", () => {
  const SHELL = codeOnly("app/ConsoleShell.tsx");

  it("it does not open as the Deployment Manager", () => {
    // The general dock DEFAULTS to that role — reasonable for operating the factory, wrong
    // here. "Discuss it" opened under "Continuing with the Deployment Manager" and resumed
    // that role's week-old thread about probe latencies with the new idea typed into it.
    expect(SHELL).toContain("roleName={dockDiscussing");
    const at = SHELL.indexOf("roleName={dockDiscussing");
    expect(SHELL.slice(at, at + 240)).toContain("? undefined");
  });

  it("and the header says what the panel is for", () => {
    expect(SHELL).toContain('"Talking to BOSS"');
  });

  it("a stale role in the URL cannot leak into it either", () => {
    expect(HERO).toContain('url.searchParams.delete("dock_role")');
  });

  it("it starts a NEW thread rather than resuming the last one", () => {
    // The heading was the visible half; the real damage is that a resumed thread's history
    // goes into the model's context, so an idea gets answered next to probe latencies.
    expect(HERO).toContain('url.searchParams.set("dock_fresh", "1")');
    expect(SHELL).toContain("setDockFresh((n) => n + 1)");
    expect(SHELL).toContain("${dockFresh}");
  });

  it("and the fresh flag is consumed once, not left in the URL", () => {
    // Left there it would reset the thread on every re-render and every back button, wiping a
    // discussion mid-sentence.
    const at = SHELL.indexOf('params.get("dock_fresh")');
    expect(SHELL.slice(at, at + 400)).toContain('searchParams.delete("dock_fresh")');
  });
});

describe("what the one agent is told", () => {
  it("it is told to work out which kind of question this is", () => {
    expect(ROUTER).toContain("TWO KINDS OF QUESTION ARRIVE HERE");
    expect(ROUTER).toContain("re-decide every turn");
  });

  it("and that operating behaviour is not softened by sharing a door", () => {
    // The whole risk of merging: a prompt that says "start nothing" bleeding into "fix it".
    expect(ROUTER).toContain("Nothing ");
    expect(ROUTER).toContain("below changes any of that");
    expect(ROUTER).toContain("This restriction is about IDEAS only");
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

  it("and to start nothing for an IDEA", () => {
    // Not even offered as a button: this is the stage before committing.
    expect(ROUTER).toContain("- Start NOTHING for an idea");
    expect(ROUTER).toContain("no build, no research run, no code task, no spend");
    expect(ROUTER).toContain("even offered as a button");
  });

  it("it applies to the general conversation only", () => {
    // A run-scoped thread is about that run and a role thread is about that role's remit;
    // neither is where somebody arrives with a product idea.
    expect(ROUTER).toContain("_one_conversation_brief() if not role and not external_ref");
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

describe("a new idea gets a new conversation", () => {
  // The header said DISCUSSING AN IDEA and underneath it sat "Is anything starving or stuck in a
  // queue?" with its old answer — the dock had reopened the newest general thread and put the new
  // idea below it. Remounting the panel was not enough: the panel restores a thread on mount.
  it("the dock asks for a fresh one when Discuss is PRESSED — not for the whole mode", () => {
    // `fresh={dockDiscussing}` lost a real conversation: discuss mode lives in the URL, so
    // every later mount asked for a new thread and the panel refused to open the one in the
    // database.
    expect(SHELL).toContain("fresh={startFresh}");
    expect(SHELL).toContain("dockFresh > spentFresh.current");
    expect(SHELL).not.toContain("fresh={dockDiscussing}");
  });

  it("and the panel declines to reopen the last thread when asked", () => {
    expect(DOCK).toContain("if (fresh) {");
    const at = DOCK.indexOf("if (fresh) {");
    // Before the restore, not after it — otherwise the old thread loads anyway.
    expect(at).toBeLessThan(DOCK.indexOf("openThreadAndContext(initialTaskId || GENERAL, ths)"));
  });

  it("the restore still runs for every other way of opening the dock", () => {
    // A run, a role, a returning operator: those want their history back.
    expect(DOCK).toContain("openThreadAndContext(initialTaskId || GENERAL, ths)");
  });

  it("and the mode does not outlive the panel it was set for", () => {
    // Left in the URL, the next dock opened for any reason arrives as an idea discussion.
    const at = SHELL.indexOf("const closeDock");
    expect(SHELL.slice(at, at + 600)).toContain('url.searchParams.delete("dock_mode")');
  });
});

describe("the panel reads as a discussion, not the general assistant", () => {
  it("it does not open by offering to start things", () => {
    // "code, research, a meeting, a PRD, a deployment" is the invitation this surface exists
    // to postpone.
    const at = DOCK.indexOf("</h2>");
    expect(DOCK.slice(at, at + 1400)).toContain("discussing ?");
    expect(DOCK).toContain("Nothing starts until you press Build");
  });

  it("and the starters are about the idea, not about the console", () => {
    expect(DOCK).toContain("What is the smallest version worth building?");
    const at = DOCK.indexOf("if (!discussing) return;");
    expect(at).toBeGreaterThan(-1);
    expect(DOCK.slice(at, at + 500)).toContain("setSuggestions");
  });

  it("the dashboard starters still stand everywhere else", () => {
    expect(DOCK).toContain('"Which runs failed and why?"');
  });
});

describe("what a turn offered survives the conversation being reopened", () => {
  const SERVICE = codeOnly("../../engine/boss_engine/services/agent_service.py");
  const API = codeOnly("../../apps/api/app/main.py");
  const SCHEMA = codeOnly("../../db/schema.sql");

  it("the offers are stored on the message, not only returned once", () => {
    // "I've put two buttons above" over no buttons: they lived in the HTTP response alone.
    expect(SCHEMA).toContain("proposed_actions JSONB NOT NULL DEFAULT '[]'::jsonb");
    expect(SERVICE).toContain("proposed_actions: Optional[list[dict[str, Any]]] = None");
    expect(SERVICE).toContain('"proposed": json.dumps(proposed_actions or [])');
  });

  it("and they are read back with the history", () => {
    // MATCHED BY COLUMN, NOT BY SPELLING. This asserted the whole SELECT on one line, so it
    // broke the moment per-agent history added `agent_role` and the list wrapped -- a passing
    // test turned red without anything being wrong, which teaches people to ignore it.
    const from = SERVICE.indexOf("SELECT id, role, content");
    expect(from).toBeGreaterThan(-1);
    // Bound the slice by the FROM that follows THIS select. An earlier query in the same file
    // also reads agent_messages, so an unanchored indexOf finds it and the slice runs
    // backwards into an empty string -- which then passes every `toContain` after it.
    const select = SERVICE.slice(from, SERVICE.indexOf("FROM agent_messages", from));
    expect(select).toContain("SELECT");
    for (const column of ["tool_calls", "actions", "proposed_actions", "created_at"]) {
      expect(select).toContain(column);
    }
    expect(SERVICE).toContain('"proposed_actions": value("proposed_actions")');
    expect(API).toContain('"proposed_actions": offers');
  });

  it("only the newest reply's offers stand", () => {
    // An offer two turns back was already answered by the turns after it.
    const at = API.indexOf("offers: list = []");
    expect(API.slice(at, at + 300)).toContain("for message in reversed(history)");
  });

  it("the console restores them", () => {
    expect(DOCK).toContain("d.proposed_actions");
  });
});

describe("work started from a conversation is reachable", () => {
  it("by being a workflow run, not a loose artefact", () => {
    // A bare discovery belongs to no run, and the console deliberately has no standing page
    // for one — so the dossier was reachable from nothing. Fixed at the source: the chat agent
    // starts the workflow, and the rail stays as it was.
    expect(SHELL).toContain('const OFF_THE_NAV = new Set<AreaId>(["discovery", "meetings"])');
  });
});
