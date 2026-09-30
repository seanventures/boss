import { describe, expect, it } from "vitest";

import { actionLabel } from "../app/agent-ui";
import { codeOnly } from "./_helpers/source";

/**
 * "Application error: a client-side exception has occurred while loading 127.0.0.1"
 *
 * The whole console, white, on the setup page. The cause was one line in the transcript:
 *
 *     {m.actions.map((a, j) => <span …>{a.kind.replaceAll("_", " ")}</span>)}
 *
 * Two writers record an action and they do not agree on the shape — the router writes
 * {kind, args, result}, AgentService writes {id, tool, ok} — so a thread containing one of the
 * second kind threw `Cannot read properties of undefined (reading 'replaceAll')` during
 * render. React unmounted the tree and the operator lost the page, the dock and the setup
 * form, for a label.
 *
 * A transcript is a log of what happened. One unfamiliar row in it must never be able to take
 * the page down.
 */
describe("labelling a recorded action", () => {
  it("uses kind when the router wrote it", () => {
    expect(actionLabel({ kind: "test_github_access" })).toBe("test github access");
  });

  it("and tool when AgentService wrote it — the shape that crashed", () => {
    expect(actionLabel({ id: "act_1", tool: "test_github_access", ok: false } as never))
      .toBe("test github access");
  });

  it("and never throws on a row it has never seen", () => {
    for (const row of [undefined, null, {}, { kind: "" }, { kind: undefined }, { ok: true }]) {
      expect(() => actionLabel(row as never)).not.toThrow();
    }
    expect(actionLabel({} as never)).toBe("action");
  });
});

describe("the render no longer reaches into the row", () => {
  const UI = codeOnly("app/agent-ui.tsx");

  it("labels through the helper rather than a property access", () => {
    expect(UI).toContain("{actionLabel(a)}");
    expect(UI).not.toContain("a.kind.replaceAll");
  });

  it("and the type says a recorded action may carry either name", () => {
    expect(UI).toContain("type RecordedAction");
    expect(UI).toContain("kind?: string; tool?: string");
  });
});
