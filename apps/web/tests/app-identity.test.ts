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
import { appIdentity } from "../app/app-identity";

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
