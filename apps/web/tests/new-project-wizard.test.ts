import { describe, expect, it } from "vitest";

import { briefFrom, criteriaFrom, linksFrom, blockingReason } from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";

/**
 * Starting a project was one textarea and a dropdown.
 *
 * Everything else a build needs — which repository the code goes in, what "done" means,
 * anything the builder should read first — was defaulted silently or never asked, and the first
 * chance to correct any of it was the spec review, after a panel had already been convened on a
 * guess. Starting a project is the one moment where a minute of the operator's attention buys
 * more than any later intervention, and it was the moment the console asked for the least.
 *
 * The wizard's rules are pure functions on the draft, so these are real tests of the behaviour
 * rather than assertions that some markup exists.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");

const draft = (over: Partial<Parameters<typeof briefFrom>[0]> = {}) => ({
  title: "", directive: "", audience: "", criteria: [""],
  // `agents` joined NewProjectDraft with the wizard's agents step and this helper was not
  // updated, so every call here was a type error while vitest (which does not typecheck) went
  // on passing. Empty means "whatever BOSS seeds", which is what the wizard starts from.
  // Same lesson as `agents` above, one field later: a draft field added without updating this
  // helper is a type error vitest never sees, because it does not typecheck.
  workflowKey: "", repository: "", links: [], agents: [], inputs: {}, ...over,
});

describe("the brief the pipeline receives", () => {
  it("is the operator's own words when that is all they gave", () => {
    expect(briefFrom(draft({ directive: "A shift-swap board." }))).toBe("A shift-swap board.");
  });

  it("carries who it is for, because that changes what gets built", () => {
    // "A duty manager on a phone, mid-shift" produces a different app from "staff", and the
    // panel can only argue about what it was told.
    const text = briefFrom(draft({
      directive: "A shift-swap board.", audience: "a duty manager on a phone",
    }));
    expect(text).toContain("A shift-swap board.");
    expect(text).toContain("Who it is for: a duty manager on a phone");
  });

  it("does not invent a section for an answer that was not given", () => {
    // An empty "Who it is for:" heading in the brief is worse than no heading: the panel reads
    // it as an audience nobody could name.
    expect(briefFrom(draft({ directive: "A board." }))).not.toContain("Who it is for");
  });

  it("ignores whitespace-only answers", () => {
    expect(briefFrom(draft({ directive: "A board.", audience: "   " })))
      .not.toContain("Who it is for");
  });
});

describe("what gets sent, and what does not", () => {
  it("blank criterion rows are UI, not requirements", () => {
    // The form always shows one empty row to type into. Sending it would give the build an
    // acceptance criterion of "".
    expect(criteriaFrom(draft({ criteria: ["a lead can swap a shift", "", "  "] })))
      .toEqual(["a lead can swap a shift"]);
  });

  it("a project with no criteria sends none rather than an empty one", () => {
    expect(criteriaFrom(draft())).toEqual([]);
  });

  it("only links an agent could actually open are sent", () => {
    // The builder is told to FETCH these. "see the design doc" is not fetchable, and passing
    // it invites a report that a document could not be opened when there was never a document.
    expect(linksFrom(draft({
      links: ["https://docs.example.com/api", "see the wiki", "", "http://internal/spec"],
    }))).toEqual(["https://docs.example.com/api", "http://internal/spec"]);
  });

  it("optional fields are omitted from the run input entirely when empty", () => {
    // Not sent as empty arrays: an empty `acceptance_criteria` reads downstream as a
    // deliberate choice of "no criteria" and can beat the panel's own.
    // `codeOnly` collapses `{}` to a space, so the empty branch of each ternary is not
    // matchable — assert the guard and the payload, which is the part that matters.
    const body = UI.slice(UI.indexOf("const start = async () =>"),
                          UI.indexOf('data-testid="dw-new-project"'));
    expect(body).toContain("criteria.length ? { acceptance_criteria: criteria }");
    expect(body).toContain("links.length ? { reference_links: links }");
    expect(body).toContain("draft.repository ? { repository: draft.repository }");
  });
});

describe("what the wizard insists on", () => {
  it("a description, because the panel argues from it", () => {
    expect(blockingReason(0, draft({ directive: "a board" }))).toContain("Describe what you want");
  });

  it("and nothing else", () => {
    // A wizard that demands ten answers is a wizard people route around. Every step after the
    // first has a defensible default.
    const filled = draft({ directive: "A shift-swap board where staff can offer shifts." });
    expect(blockingReason(0, filled)).toBe("");
    for (const step of [1, 2, 3]) expect(blockingReason(step, draft())).toBe("");
  });

  it("the reason is phrased as what to do, not as what is wrong", () => {
    const reason = blockingReason(0, draft({ directive: "x" }));
    expect(reason).toMatch(/^Describe/);
    expect(reason).not.toMatch(/error|invalid|required/i);
  });
});

describe("it can still be a one-liner", () => {
  it("Start is offered from the first step", () => {
    // The wizard is for the operator who wants it. Making somebody walk four screens to accept
    // four defaults would be ceremony, and ceremony is what gets routed around.
    expect(UI).toContain('data-testid="dw-wiz-start-now"');
  });

  it("and the keyboard shortcut every other composer uses still starts it", () => {
    expect(UI).toContain('(e.metaKey || e.ctrlKey) && e.key === "Enter"');
  });
});

describe("the choices show why one is right", () => {
  it("paths are cards carrying their description and gates, not a dropdown", () => {
    // A <select> hides the descriptions, and the descriptions are the entire basis for
    // choosing: the paths differ in how much argument and gating happens, which is a question
    // about risk rather than speed.
    expect(UI).toContain("entry.description");
    expect(UI).toContain("you approve: ");
    expect(UI).toContain("no approval gates");
  });

  it("the repository arrives already answered", () => {
    // The registry has always known which repo is primary; nothing over HTTP could read it, so
    // this form could not offer a choice and every build went to the global default.
    expect(UI).toContain('get<{ repositories: Repo[]; suggested: string; suggested_source: string }>');
    expect(UI).toContain('"repositories", projectId');
    expect(UI).toContain("suggested_source");
  });

  it("a project with no registry is a normal state, not an error", () => {
    const effect = UI.slice(UI.indexOf('"repositories", projectId'));
    expect(effect.slice(0, 400)).toContain('"error" in r');
  });

  it("reference links are described as fetched, never as a knowledge base", () => {
    // Nothing indexes or searches them. Calling this a knowledge base would promise retrieval
    // that does not exist, and the operator would size their expectations to the promise.
    expect(UI).toContain("not indexed or");
    expect(UI).not.toMatch(/knowledge base/i);
  });
});

describe("the last step says what the press will do", () => {
  it("it restates the decisions rather than repeating the form", () => {
    for (const id of ["dw-review-directive", "dw-review-workflow", "dw-review-repo"]) {
      expect(UI).toContain(`data-testid="${id}"`);
    }
  });

  it("and says plainly that it spends money and will stop before deploying", () => {
    expect(UI).toContain("spends model budget");
    expect(UI).toContain("before it deploys");
  });
});
