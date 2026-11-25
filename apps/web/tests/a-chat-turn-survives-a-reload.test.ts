import { describe, expect, it } from "vitest";

import { awaitTurn, outcomeOf, resumeFrom, startedTurnOf, type TurnRead } from "../lib/chat-turn";
import { codeOnly } from "./_helpers/source";

/**
 * A chat turn the console can lose sight of and find again (services/chat_turns.py).
 *
 * A console turn was one plain fetch held in React state: a reload, a closed drawer or a dead
 * worker lost it, and the API's single transaction rolled the operator's question back with
 * the failure. The API now commits the question and a turn record first, answers a background
 * send with {thread_id, turn_id}, and the dock polls the turn — and resumes it from the thread
 * after a reload.
 */
describe("a background send is a turn to wait on", () => {
  it("recognises the 202 'running' answer, and only that", () => {
    expect(startedTurnOf({ thread_id: "athr_1", turn_id: "aturn_1", status: "running" }))
      .toEqual({ turnId: "aturn_1", threadId: "athr_1" });
    // A synchronous reply (an older API, or a turn that answered) is the answer itself.
    expect(startedTurnOf({ thread_id: "athr_1", turn_id: "aturn_1", reply: "hi", status: "running" }))
      .toBeNull();
    expect(startedTurnOf({ thread_id: "athr_1", reply: "hi" })).toBeNull();
    expect(startedTurnOf(null)).toBeNull();
  });

  it("waits through running reads and failed reads, then hands back the response", async () => {
    const reads: Array<TurnRead | null> = [
      { status: "running" }, null /* a proxy hiccup is not an outcome */, { status: "running" },
      { status: "done", result: { thread_id: "athr_1", reply: "Here it is." } },
    ];
    let n = 0;
    const ended = await awaitTurn(async () => reads[n++] ?? null, "aturn_1",
                                  { sleep: async () => undefined });
    expect(ended).toEqual({ ok: true, data: { thread_id: "athr_1", reply: "Here it is." } });
    expect(n).toBe(4);
  });

  it("a failed turn is its sentence, with the technical line apart", () => {
    expect(outcomeOf({ status: "failed", error: "x",
                       failure: { sentence: "BOSS couldn't finish this turn. Try again.",
                                  technical: "KeyError: 'k'" } }))
      .toEqual({ ok: false, error: "BOSS couldn't finish this turn. Try again.",
                 data: { technical: "KeyError: 'k'" } });
    expect(outcomeOf({ status: "running" })).toBeNull();
  });

  it("stops when the caller stops waiting", async () => {
    let stop = false;
    const ended = await awaitTurn(async () => { stop = true; return { status: "running" }; },
                                  "aturn_1", { sleep: async () => undefined, shouldStop: () => stop });
    expect(ended).toBeNull();
  });
});

describe("a reload resumes the running turn", () => {
  it("finds the running turn on the thread and the question it answers", () => {
    const thread = {
      messages: [
        { role: "user", content: "first" }, { role: "assistant", content: "answer" },
        { role: "user", content: "Why did the deploy fail?" },
      ],
      turn: { turn_id: "aturn_2", status: "running" },
    };
    expect(resumeFrom(thread)).toEqual({ turnId: "aturn_2", waitingFor: "Why did the deploy fail?" });
  });

  it("nothing to resume when no turn is in flight", () => {
    expect(resumeFrom({ messages: [{ role: "user", content: "q" }], turn: null })).toBeNull();
    expect(resumeFrom({ messages: [], turn: { turn_id: "aturn_3", status: "done" } })).toBeNull();
    expect(resumeFrom({})).toBeNull();
  });

  it("every thread read in the dock resumes, and the send asks for the background turn", () => {
    const UI = codeOnly("app/agent-ui.tsx");
    // openSession, openThreadAndContext and the role door's own read.
    expect(UI.split("resumeIfRunning(d)").length - 1).toBeGreaterThanOrEqual(3);
    expect(UI).toContain('fd.set("background", "1")');
    expect(UI).toContain('view: "turn"');
    const actions = codeOnly("app/actions.ts");
    const ask = actions.slice(actions.indexOf("export async function askBossAction"));
    expect(ask.slice(0, ask.indexOf("\n}\n"))).toContain("background:");
  });
});
