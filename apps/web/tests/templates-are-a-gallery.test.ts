import { describe, expect, it } from "vitest";

import {
  audienceOf, doneWhenOf, facetsOf, mustDoOf, noteOf, summaryOf,
} from "../app/template-brief";
import { CRAFT, DETAILS, buildBrief } from "../app/template-details";
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

describe("sixteen do not need filtering", () => {
  const HERO = codeOnly("app/ask-boss-hero.tsx");

  it("all sixteen can be seen at once", () => {
    expect(HERO).toContain('data-testid="askboss-grid-toggle"');
    expect(HERO).toContain('data-grid={gridMode ? "true" : undefined}');
  });

  it("and the facet chips are gone", () => {
    // Added by analogy with Lovable, which has 204 templates and genuinely needs them. Sixteen
    // that fit on one screen do not: the chips cost a row of the panel to save nobody a scroll.
    expect(HERO).not.toContain("askboss-facets");
    expect(HERO).not.toContain("tagFilter");
  });
});

describe("clicking opens a page, and reading is not the same as buying", () => {
  const HERO = codeOnly("app/ask-boss-hero.tsx");
  const CSS = codeOnly("app/globals.css");

  it("the card opens the brief rather than pasting it", () => {
    expect(HERO).toContain("onClick={() => showTemplate(i)}");
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
    expect(HERO).toContain("draftIdea({ ...openIdea,");
    expect(HERO).toContain("closeTemplate();");
  });

  it("the artwork is on the detail view, not only on the card", () => {
    // The first version showed the drawing on a 232px card and a tag on the detail view —
    // "the image is missing in the enlarge view" — so the one place with room for it had none.
    expect(HERO).toContain('<IdeaCover idea={openIdea} scope="page" />');
    expect(CSS).toContain(".tpl-shot");
  });

  it("and it paints with the first frame rather than a filter pass later", () => {
    // "the image renders late" — two circles under feGaussianBlur are a filter primitive, and
    // the browser rasterises that region on its own pass. At detail-page size that pass lands
    // after everything else. Radial gradients give the same orb with no filter graph at all.
    expect(HERO).not.toContain("feGaussianBlur");
    expect(HERO).toContain("radialGradient");
    // Nor does the container scale under it, which would force a re-raster mid-animation.
    const at = CSS.indexOf("@keyframes tpl-page-in");
    expect(at).toBeGreaterThan(-1);
    expect(CSS.slice(at, at + 160)).not.toContain("scale(");
  });

  it("asking first is offered beside building", () => {
    // Reading the brief is not the same as being sure. Asking costs a conversation; being
    // wrong costs a build.
    expect(HERO).toContain('data-testid="askboss-sheet-ask"');
  });

  it("it is a page, not a dialog floating over the dashboard", () => {
    // A cover image, a pitch, five feature cards, a scope list and eight standards do not fit
    // in an 88vh scroll box with the console showing around the edges.
    expect(HERO).toContain('className="tpl-page"');
    expect(HERO).toContain("← Back to templates");
    expect(CSS).not.toContain(".askboss-sheet-scrim");
    const at = CSS.indexOf(".tpl-page {");
    expect(CSS.slice(at, at + 200)).toContain("position: fixed");
  });

  it("but not for anyone who asked not to be moved", () => {
    const at = CSS.indexOf("prefers-reduced-motion");
    expect(at).toBeGreaterThan(-1);
    expect(CSS.slice(at, at + 220)).toContain("animation: none");
  });

  it("it is somewhere you can be, so Back gets you out of it", () => {
    // A full-screen view the Back button walks out of is worse than a dialog: the muscle
    // memory for "get me out of this" would leave the console entirely.
    expect(HERO).toContain('url.searchParams.set("template"');
    expect(HERO).toContain('window.addEventListener("popstate", sync)');
    // And a link to one opens on it.
    expect(HERO).toContain('searchParams.get("template")');
  });

  it("and Escape closes it, without the page underneath scrolling", () => {
    expect(HERO).toContain('if (e.key === "Escape") closeTemplate();');
    expect(HERO).toContain('document.body.style.overflow = "hidden"');
    // Restored to what it was, not to a hardcoded value — the console sets its own.
    expect(HERO).toContain("document.body.style.overflow = previous;");
  });
});

/**
 * "the content is very less, can we add more requirements for the same project so BOSS can
 * build much more quality features than some MVP solution"
 *
 * Five bullets and an acceptance test specify an MVP, and an MVP is exactly what came back.
 * The fix is not a longer page — it is a longer SPECIFICATION, because on this product the two
 * are the same text: what the operator reads is appended to the brief BOSS is held to.
 */
describe("what you read is what gets built", () => {
  const HERO = codeOnly("app/ask-boss-hero.tsx");
  /** Every template title, read out of the gallery itself. */
  const TITLES = [...HERO.matchAll(/title: "([^"]+)", glyph:/g)].map((m) => m[1]);

  it("there are sixteen of them", () => {
    expect(TITLES).toHaveLength(16);
  });

  it("every one has the richer detail — no template quietly falls back to five bullets", () => {
    const missing = TITLES.filter((t) => !DETAILS[t]);
    expect(missing).toEqual([]);
  });

  it("and no detail entry is orphaned by a renamed template", () => {
    // A key that matches nothing is content nobody will ever see, and the failure is silent:
    // the sheet just falls back to the thin version.
    const orphans = Object.keys(DETAILS).filter((k) => !TITLES.includes(k));
    expect(orphans).toEqual([]);
  });

  it("each carries enough to build from, not a tagline", () => {
    for (const [title, d] of Object.entries(DETAILS)) {
      expect(d.pitch.length, title).toBeGreaterThan(120);
      expect(d.designedFor.length, title).toBeGreaterThanOrEqual(3);
      expect(d.features.length, title).toBeGreaterThanOrEqual(5);
      expect(d.surfaces.length, title).toBeGreaterThanOrEqual(3);
      // A feature title with no detail is a bullet wearing a card's clothes.
      for (const f of d.features) expect(f.detail.length, `${title}/${f.title}`)
        .toBeGreaterThan(60);
    }
  });

  it("the quality bar covers what the first builds actually shipped without", () => {
    const bar = CRAFT.join(" ").toLowerCase();
    for (const owed of ["empty state", "keyboard", "responsive", "error"]) {
      expect(bar, owed).toContain(owed);
    }
  });

  it("the outgoing brief contains everything the sheet showed", () => {
    const base = "Build an incident timeline builder.\nWho it is for: someone.";
    const brief = buildBrief("Incident timeline builder", base);
    expect(brief).toContain(base.trim());
    for (const f of DETAILS["Incident timeline builder"].features) {
      expect(brief).toContain(f.title);
      expect(brief).toContain(f.detail);
    }
    for (const c of CRAFT) expect(brief).toContain(c);
    expect(brief).toContain("Timeline editor");
  });

  it("and a template with no detail entry still gets its original brief", () => {
    // The fallback has to be the brief unchanged, not an empty one — a typo in a key must not
    // silently ship a build with no specification at all.
    expect(buildBrief("Not a template", "Build a thing.")).toBe("Build a thing.");
  });
});
