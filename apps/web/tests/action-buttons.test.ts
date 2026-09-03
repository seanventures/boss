import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * An offer is not a deed.
 *
 * BOSS proposed a redeploy in prose with no way to accept it, and separately wrote an invented
 * domain into the database. The operator wanted a third thing — clear the stale URL — which was
 * free and never offered.
 *
 * So: costly actions arrive as buttons, cheapest first, and clicking runs THAT tool with THOSE
 * arguments — no model in between, because re-asking a model to interpret a click lets it drift
 * into doing something adjacent, which is the opposite of what a confirmation button is for.
 */

const UI = readFileSync(resolve(__dirname, "../app/agent-ui.tsx"), "utf8");
const ACTIONS = readFileSync(resolve(__dirname, "../app/actions.ts"), "utf8");
const CSS = readFileSync(resolve(__dirname, "../app/globals.css"), "utf8");

describe("the chat renders offers", () => {
  it("keeps proposals separate from the transcript", () => {
    // Rendered inside a message bubble, an offer reads as something that already happened.
    expect(UI).toContain('data-testid="agent-proposals"');
    expect(CSS).toContain(".agent-proposals");
  });

  it("gives each proposal its own button", () => {
    expect(UI).toContain("proposed.map((a) =>");
    expect(UI).toContain("onClick={() => acceptAction(a)}");
  });

  it("labels the button with the action, not a generic confirm", () => {
    // "Confirm" tells the operator nothing about which of two offers they are accepting.
    expect(UI).toContain("{busy && ranAction === a.id ? \"…\" : a.label}");
  });

  it("shows what it will cost before they click", () => {
    expect(UI).toContain("{a.why ?");
  });

  it("says plainly that nothing has run", () => {
    expect(UI).toContain("nothing runs until you choose");
  });
});

describe("accepting one", () => {
  it("sends the tool and args verbatim", () => {
    expect(UI).toContain("runAgentActionAction(projectId, actor, action.tool, action.args");
  });

  it("does not route the click back through the model", () => {
    expect(ACTIONS).toContain('postJsonReturn("/v1/engine/agent/action"');
    const fn = ACTIONS.slice(ACTIONS.indexOf("runAgentActionAction"));
    expect(fn.slice(0, 700)).not.toContain("/v1/engine/agent/chat");
  });

  it("reports the outcome in the log", () => {
    // A button that changes nothing visible is a button the operator presses twice.
    expect(UI).toContain("✓ ${action.label}");
    expect(UI).toContain("failed:");
  });

  it("shows what the action actually did, not only that it ran", () => {
    // A tick and a label is enough for "cancel a job". For a command on a live host it is not:
    // after clicking "Delete all containers on hetzner-ci-01" the operator saw `✓` and had no
    // way to know the exit code, the output, or what had been removed — they had to ask
    // someone else to go and look at the box. The tool already returns all of it.
    expect(UI).toContain("payload.exit_code");
    expect(UI).toContain("payload.stdout");
    expect(UI).toContain("payload.stderr");
  });

  it("spends the offer either way", () => {
    // Succeeded, or failed and needs a different approach — neither leaves it clickable.
    expect(UI).toContain("list.filter((a) => a.id !== action.id)");
  });

  it("disables the buttons while one is running", () => {
    expect(UI).toContain("disabled={busy}");
  });
});

describe("the response carries them", () => {
  it("reads proposed_actions from the reply", () => {
    expect(UI).toContain("d.proposed_actions");
  });

  it("clears stale offers when a new turn arrives", () => {
    // Left behind, yesterday's button would run against today's state.
    expect(UI).toMatch(/setProposed\(Array\.isArray\(d\.proposed_actions\)/);
  });
});
