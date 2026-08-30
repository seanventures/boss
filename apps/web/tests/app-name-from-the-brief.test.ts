import { describe, expect, it } from "vitest";

import { appIdentity, directiveName, groupIdentity } from "../app/app-identity";

/**
 * "Untitled app" — reported repeatedly, and each previous fix widened the title parsing.
 *
 * Widening could not work here. The run's title is `📄 Product Requirements Document`, and its
 * follow-up is `Follow-up: deferred review findings — 📄 Product Requirements Document`. Both
 * are document types. BOILERPLATE is RIGHT to refuse them, there is no tail to promote, and
 * every run in the group hits the same wall — so the card had nothing to fall back to.
 *
 * The name was never missing. `finalize_ctx.origin.title` held the brief a human actually
 * wrote — "Build a tool that turns meeting notes into draft tickets." — and nothing read it.
 *
 * These use the exact records from PR #114.
 */

const PARENT = {
  title: "📄 Product Requirements Document",
  finalize_ctx: {
    app_id: "app_09f2d26cb921e5ebe20d",
    web_app_build: true,
    origin: {
      kind: "meeting",
      title: "Build a tool that turns meeting notes into draft tickets.\n\nWho it is for: someon",
      task_id: "tsk_bc5d5c14717d4a93a021",
    },
  },
};

const FOLLOW_UP = {
  title: "Follow-up: deferred review findings — 📄 Product Requirements Document",
  finalize_ctx: { app_id: "app_09f2d26cb921e5ebe20d", is_followup: true },
};

describe("the card that read 'Untitled app'", () => {
  it("names itself from the brief instead", () => {
    expect(appIdentity(PARENT).name).not.toBe("Untitled app");
    expect(appIdentity(PARENT).name.toLowerCase()).toContain("meeting notes");
  });

  it("still refuses to call the app a document type", () => {
    // The old bug in the other direction: naming the product "Product Requirements Document".
    expect(appIdentity(PARENT).name).not.toMatch(/requirements document/i);
    expect(appIdentity(PARENT).name).not.toBe("PRD");
  });

  it("names the whole group, including the follow-up that leads it", () => {
    // groupIdentity takes members[0]; a follow-up sorts first and carries no name of its own.
    expect(groupIdentity([FOLLOW_UP, PARENT]).name).not.toBe("Untitled app");
  });

  it("keeps the leading run's label, so the card still says it is a follow-up", () => {
    expect(groupIdentity([FOLLOW_UP, PARENT]).label).toBe("follow-up");
  });
});

describe("turning a brief into a name", () => {
  it.each([
    ["Build a tool that turns meeting notes into draft tickets.", /meeting notes/i],
    ["Create an app for tracking on-call rotas", /on-call rotas/i],
    ["Make a dashboard that shows deploy health", /deploy health/i],
    ["build me a service to reconcile invoices", /reconcile invoices/i],
  ])("%s", (title, expected) => {
    expect(directiveName({ finalize_ctx: { origin: { title } } })).toMatch(expected);
  });

  it("uses only the first sentence — the rest of a brief is context, not a name", () => {
    const name = directiveName({
      finalize_ctx: {
        origin: { title: "Build a rota tool.\n\nWho it is for: on-call engineers who hate spreadsheets" },
      },
    });
    expect(name).not.toMatch(/who it is for/i);
    expect(name.toLowerCase()).toContain("rota");
  });

  it("stays short enough for a card heading", () => {
    const name = directiveName({
      finalize_ctx: { origin: { title: "Build a tool that does " + "very ".repeat(40) + "much" } },
    });
    expect(name.length).toBeLessThanOrEqual(46);
  });

  it("returns nothing when the brief is itself boilerplate, rather than a worse name", () => {
    expect(directiveName({ finalize_ctx: { origin: { title: "Product Requirements Document" } } }))
      .toBe("");
    expect(directiveName({ finalize_ctx: { origin: { title: "   " } } })).toBe("");
    expect(directiveName({})).toBe("");
  });

  it("preserves acronyms rather than title-casing them into nonsense", () => {
    expect(directiveName({ finalize_ctx: { origin: { title: "Build a PR review timer" } } }))
      .toContain("PR");
  });
});

describe("a real title always wins over the brief", () => {
  it("does not override a name the run already has", () => {
    const named = {
      title: "Shorty — a link shortener",
      finalize_ctx: { origin: { title: "Build something completely different" } },
    };
    expect(appIdentity(named).name).toBe("Shorty");
  });
});
