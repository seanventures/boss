/**
 * A card title has one job: let a person tell one app from another.
 *
 * Titles were built server-side by truncating whatever prose started the brief —
 * `("Web app — " + directive[:70])` — so a card read
 *
 *   "Web app — # Product Requirements Document (PRD) **Project:** Lightweight Self-"
 *
 * for a project the PRD names on its very next line: "Shorty". Four different benchmark apps
 * all displayed as "QA benchmark", because the label came first and the name was cut off.
 *
 * Every case below is a real title taken from the live system.
 */
import { describe, expect, it } from "vitest";
import { appIdentity, groupIdentity } from "../app/app-identity";

const t = (title: string) => appIdentity({ title });

describe("appIdentity — real titles from the running system", () => {
  it("reads the name the PRD gives itself", () => {
    const id = t(
      '# Product Requirements Document (PRD)  \n**Project:** Lightweight Self‑Hosted URL' +
        ' Shortener – “Shorty” (working title)  \n**Scope:** first slice',
    );
    expect(id.name).toBe("Shorty");
    expect(id.kind).toContain("URL Shortener");
  });

  it("never returns the document's own boilerplate as a name", () => {
    for (const title of [
      "Web app — # Product Requirements Document (PRD)   **Project:** Something Real",
      "Web app — # PRD: BugBox — Single-Project Issue Tracker **Status:** Ready",
    ]) {
      expect(t(title).name).not.toMatch(/product requirements|^PRD$|^Web app$/i);
    }
  });

  it("lifts the run kind into a label instead of using it as the name", () => {
    const hook = t("QA benchmark: HookWatch — Webhook inspector and replay tool · retry 1");
    expect(hook.name).toBe("HookWatch");
    expect(hook.label).toBe("benchmark");

    const approve = t("QA benchmark: ApproveIt — Expense approval workflow");
    expect(approve.name).toBe("ApproveIt");
  });

  it("does not collapse different apps onto one name", () => {
    const names = [
      "QA benchmark: HookWatch — Webhook inspector and replay tool",
      "QA benchmark: ApproveIt — Expense approval workflow",
      "QA benchmark: BugBox — Mini issue tracker",
    ].map((x) => t(x).name);
    expect(new Set(names).size).toBe(3);
  });

  it("pulls a quoted product name out of an instruction", () => {
    const id = t("Web app — Build 'Living Colors' — a VIBRANT, COLORFUL portfolio · retry 1");
    expect(id.name).toBe("Living Colors");
  });

  it("keeps a name that is already good", () => {
    expect(t("Web app — # BugBox – Product Requirements Document (PRD) *Version 1.0").name)
      .toBe("BugBox");
  });

  it("drops the retry suffix — an attempt is not part of a name", () => {
    expect(t("Build a simple todo list web app · retry 1").name).not.toMatch(/retry/i);
  });

  it("turns a bare phrase into something readable", () => {
    expect(t("Build a simple todo list web app · retry 1").name).toBe("Simple Todo List");
  });

  it("strips nested labels", () => {
    const id = t("Follow-up: deferred review findings — e2e smoke — /healthz endpoint");
    expect(id.name).not.toMatch(/^Follow-up$/i);
    expect(id.label).toBe("follow-up");
  });

  it("prefers an explicit benchmark identity when the run carries one", () => {
    const id = appIdentity({
      title: "anything at all",
      finalize_ctx: { qa_benchmark: { name: "BugBox", tagline: "Mini issue tracker" } },
    });
    expect(id.name).toBe("BugBox");
  });

  it("never returns an empty name", () => {
    for (const title of ["", "   ", "· retry 3", "Web app — "]) {
      expect(t(title).name.length).toBeGreaterThan(0);
    }
  });
});

describe("a document type is never a product name", () => {
  // The card read "📄 Product Requirements Document". BOILERPLATE matches that phrase, but it
  // is anchored at ^ and the real title starts with an emoji, so the check never fired and the
  // heading became the app's name — four different apps would read identically.
  it("sees past a leading emoji", () => {
    expect(appIdentity({ title: "📄 Product Requirements Document" }).name)
      .not.toMatch(/product requirements document/i);
  });

  it("sees past other decoration", () => {
    for (const t of [
      "# 📄 Product Requirements Document",
      "• PRD",
      "**Spec**",
      "📝  Specification",
    ]) {
      expect(appIdentity({ title: t }).name).not.toMatch(
        /^(product requirements document|prd|spec|specification)$/i,
      );
    }
  });

  it("says Untitled app rather than naming the artifact", () => {
    expect(appIdentity({ title: "📄 Product Requirements Document" }).name).toBe("Untitled app");
  });

  it("still finds a real name behind the decoration", () => {
    expect(appIdentity({ title: "📄 PRD: Invoice Chaser" }).name).toBe("Invoice Chaser");
    expect(appIdentity({ title: "# Shorty — Product Requirements Document" }).name).toBe("Shorty");
  });
});

describe("a titled PRD is not an Untitled app", () => {
  /**
   * Verbatim titles from shared-server, from cards the operator photographed reading "Untitled
   * app" — with their own names rendered directly above them as the descriptor chip.
   *
   * The head/tail split did its job and the last gate did its job: "PRD" is a document type
   * and must never be an app name. What was missing is that when the head IS boilerplate, the
   * tail is not a subtitle — it is the name. So the name was found and then discarded.
   *
   * The dash matters. "PRD: X" was handled; "PRD — X" was not, because the prefix stripper
   * only accepted a colon. Every app specced through a meeting gets the dash form.
   */
  it.each([
    ["PRD \u2014 Pull\u2010Request Review\u2010Time Tracker (PR\u2010RTT)",
     "Pull-Request Review-Time Tracker (PR-RTT)"],
    ["PRD \u2014 UnitFlip", "UnitFlip"],
    ["PRD: BugBox", "BugBox"],
    ["Product Requirements Document \u2014 CSV\u2010to\u2010Chart Explorer",
     "CSV-to-Chart Explorer"],
  ])("names %s", (title, expected) => {
    expect(appIdentity({ title }).name).toBe(expected);
  });

  it("does not leave the name duplicated in the descriptor beside it", () => {
    // The card showed the name twice: once as the chip, once absent. Promoting the tail must
    // MOVE it, not copy it.
    const id = appIdentity({ title: "PRD \u2014 UnitFlip" });
    expect(id.name).toBe("UnitFlip");
    expect(id.kind ?? "").not.toContain("UnitFlip");
  });

  it("still refuses a title that is ONLY a document type", () => {
    // The gate this fix relaxes is load-bearing: with no tail there is no name to promote,
    // and four different apps all reading "PRD" is what it was added to prevent.
    expect(appIdentity({ title: "Product Requirements Document" }).name).toBe("Untitled app");
    expect(appIdentity({ title: "PRD" }).name).toBe("Untitled app");
  });

  it("normalises the hyphen a model actually types", () => {
    // U+2010 is not the ASCII hyphen and was not in the normalising set, so the name carried a
    // character the operator cannot type into a search box.
    expect(appIdentity({ title: "Invoice\u2010Chaser" }).name).toBe("Invoice-Chaser");
  });
});

describe("an app is named by whichever run knows its name", () => {
  /**
   * A card takes one name for the whole group. It used to take it from members[0], and a
   * follow-up run is titled after the WORK — "Follow-up: deferred review findings — 📄 Product
   * Requirements Document" — with no product name in it. Sorted first, that run named the
   * card, and a shipped app whose other runs say "UnitFlip" rendered as "Untitled app".
   */
  const followup = { title: "Follow-up: deferred review findings \u2014 \ud83d\udcc4 Product Requirements Document" };
  const real = { title: "PRD \u2014 UnitFlip" };

  it("looks past a run that carries no name", () => {
    expect(appIdentity(followup).name).toBe("Untitled app");     // the run really is nameless
    expect(groupIdentity([followup, real]).name).toBe("UnitFlip");
  });

  it("does not go looking when the first run already knows", () => {
    expect(groupIdentity([real, followup]).name).toBe("UnitFlip");
  });

  it("keeps the leading run's label, which says what the newest work is", () => {
    // The name comes from the sibling; "follow-up" describes THIS card's latest run and the
    // sibling cannot tell you that.
    expect(groupIdentity([followup, real]).label).toBe("follow-up");
  });

  it("still says Untitled when no run in the group has a name", () => {
    // The honest answer. Inventing one from the brief would put a name on the card that
    // appears nowhere else in the system.
    expect(groupIdentity([followup, { title: "PRD" }]).name).toBe("Untitled app");
  });

  it("survives an empty group without throwing", () => {
    expect(groupIdentity([]).name).toBe("Untitled app");
  });
});
