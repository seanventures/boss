import { describe, expect, it } from "vitest";

import {
  audienceOf, doneWhenOf, facetsOf, mustDoOf, noteOf, summaryOf,
} from "../app/template-brief";
import { codeOnly } from "./_helpers/source";

/**
 * "compare the templates with lovable.dev/templates ... the template looks very normal in
 * Command center" — and then: "in lovable when I click on the template it enlarges more and
 * have more description with lots of details including animation options".
 *
 * The comparison, honestly:
 *
 *                    lovable.dev/templates          BOSS before
 *   count            204                            16
 *   layout           filterable grid                horizontal conveyor
 *   facets           type · ~21 categories · persona  none — the tags were decoration
 *   card             screenshot, title, tagline      drawing, title, tag
 *   click            detail page                     dumped raw markdown in the composer
 *
 * The odd part is how much of the missing information was already written. Every brief opens
 * "Who it is for:" — a better tagline than a screenshot conveys — and closes "Done when:",
 * which is the acceptance test. Both were in the payload; neither reached the screen.
 *
 * Where this deliberately does NOT copy them: their card shows a screenshot of a finished
 * thing. Ours cannot, because it does not exist yet — BOSS builds it, to this specification.
 * So the specification IS the preview, and what an operator about to spend money needs to read
 * is what it must do and how they will know it worked.
 */
const BRIEF = `Build an incident timeline builder.
Who it is for: an on-call engineer writing the post-mortem the morning after.
What it must do:
- Take an alert log and a chat transcript and lay them on one timeline.
- Mark detection, escalation, mitigation and all-clear.
- Let a human correct every milestone the tool proposes.
Done when: a real incident log produces a timeline with all four milestones.
Storage is one JSON file per incident, so it can live in the repo beside the code.`;

describe("the brief already held what the card never showed", () => {
  it("who it is for — the tagline", () => {
    expect(audienceOf(BRIEF))
      .toBe("an on-call engineer writing the post-mortem the morning after");
  });

  it("what it must do — as a list, not a wall", () => {
    expect(mustDoOf(BRIEF)).toHaveLength(3);
    expect(mustDoOf(BRIEF)[0]).toMatch(/^Take an alert log/);
  });

  it("done when — the sentence the build is judged against", () => {
    expect(doneWhenOf(BRIEF)).toBe(
      "a real incident log produces a timeline with all four milestones.");
  });

  it("and the design note the author left underneath it", () => {
    expect(noteOf(BRIEF)).toMatch(/^Storage is one JSON file/);
  });

  it("the title without its imperative", () => {
    expect(summaryOf(BRIEF)).toBe("incident timeline builder");
  });

  it("a brief missing a section yields nothing rather than nonsense", () => {
    const facets = facetsOf("Build a thing.");
    expect(facets).toEqual({ audience: "", mustDo: [], doneWhen: "", note: "" });
  });

  it("and an empty brief does not throw", () => {
    expect(() => facetsOf("")).not.toThrow();
  });
});

describe("the tags finally do something", () => {
  const HERO = codeOnly("app/ask-boss-hero.tsx");

  it("every tag is a filter, with its count", () => {
    // Eight of them, one per card, rendered as a label and never as a filter — so the only way
    // to find the money one among sixteen was to read sixteen titles.
    expect(HERO).toContain("const TEMPLATE_TAGS =");
    expect(HERO).toContain('data-testid="askboss-facets"');
    expect(HERO).toContain("IDEAS.filter((i) => i.tag === t).length");
  });

  it("and the filter actually filters", () => {
    expect(HERO).toContain("IDEAS.filter((i) => !tagFilter || i.tag === tagFilter)");
  });

  it("all sixteen can be seen at once", () => {
    expect(HERO).toContain('data-testid="askboss-grid-toggle"');
    expect(HERO).toContain('data-grid={gridMode ? "true" : undefined}');
  });
});

describe("clicking enlarges it, and reading is not the same as buying", () => {
  const HERO = codeOnly("app/ask-boss-hero.tsx");
  const CSS = codeOnly("app/globals.css");

  it("the card opens the brief rather than pasting it", () => {
    expect(HERO).toContain("onClick={() => setOpenIdea(i)}");
  });

  it("the enlarged view shows what it must do and how you know it worked", () => {
    expect(HERO).toContain("What it must do");
    expect(HERO).toContain("Done when");
    expect(HERO).toContain('className="askboss-sheet-done"');
  });

  it("and what pressing build actually starts", () => {
    // Every other gallery hands you a finished artefact. This one starts a governed pipeline
    // with an approval at each gate, and somebody about to spend money should be told before.
    expect(HERO).toContain("What BOSS will do");
    expect(HERO).toContain("you approve it");
  });

  it("two deliberate presses stand between a card and a build", () => {
    expect(HERO).toContain("onClick={() => { draftIdea(openIdea); setOpenIdea(null); }}");
  });

  it("asking first is offered beside building", () => {
    // Reading the brief is not the same as being sure. Asking costs a conversation; being
    // wrong costs a build.
    expect(HERO).toContain('data-testid="askboss-sheet-ask"');
  });

  it("it enlarges rather than appearing", () => {
    expect(CSS).toContain("@keyframes askboss-sheet-in");
    expect(CSS).toContain("scale(.965)");
  });

  it("but not for anyone who asked not to be moved", () => {
    const at = CSS.indexOf("prefers-reduced-motion");
    expect(at).toBeGreaterThan(-1);
    expect(CSS.slice(at, at + 220)).toContain("animation: none");
  });

  it("and it closes — on the scrim and on a button", () => {
    expect(HERO).toContain("onClick={() => setOpenIdea(null)}");
    expect(HERO).toContain('aria-label="Close"');
    // A click inside the sheet must not close it.
    expect(HERO).toContain("onClick={(e) => e.stopPropagation()}");
  });
});
