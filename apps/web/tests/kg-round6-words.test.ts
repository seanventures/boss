/**
 * KG round 6: run titles are cut at a word, never inside one, with the whole title as the
 * tooltip; a title the API already cut mid-word is healed. The top bar's New build fits.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { healCut, splitWhen, wordCut } from "../lib/word-cut";
import { provenanceLines } from "../lib/kg-provenance";

describe("wordCut", () => {
  it("keeps whole words and ends with an ellipsis", () => {
    const r = wordCut("Business rules scan: loan repayment schedule transaction processors", 40);
    expect(r.text).toBe("Business rules scan: loan repayment…");
    expect(r.full).toBe("Business rules scan: loan repayment schedule transaction processors");
    expect(r.cut).toBe(true);
  });
  it("leaves a short title alone", () => {
    expect(wordCut("Fix the login", 40)).toEqual({ text: "Fix the login", full: "Fix the login", cut: false });
  });
  it("heals a title the API cut inside a word", () => {
    expect(healCut("Business rules scan: loan repaym… · Oct 25, 02:50")).toBe("Business rules scan: loan… · Oct 25, 02:50");
    expect(healCut("Abstract…")).toBe("Abstract…");
  });
  it("keeps the run's time apart from its name", () => {
    expect(splitWhen("A scan of loans · Oct 25, 02:50")).toEqual(["A scan of loans", " · Oct 25, 02:50"]);
    expect(splitWhen("A scan of loans · Oct 24, 19:50 PDT")).toEqual(["A scan of loans", " · Oct 24, 19:50 PDT"]);
  });
});

describe("the provenance line's run", () => {
  it("is cut at a word, the whole title its tooltip", () => {
    const title = "Business rules scan: loan repayment schedule transaction processors in fineract-loan";
    const lines = provenanceLines({ run_title: title, created_by_kind: "agent" } as never, { layer: "learning" });
    const part = lines.flat().find((p) => p.kind === "text" && p.text.startsWith("“"));
    expect(part && "title" in part ? part.title : null).toBe(title);
    expect(part && part.kind === "text" ? part.text : "").toMatch(/^“[^”]*\b[a-z]+…”$/);
    expect(part && part.kind === "text" ? part.text.length : 99).toBeLessThanOrEqual(63);
  });
});

describe("the top bar fits at 1280-1440", () => {
  it("New build becomes its + with a name and a tooltip up to 1480 px", () => {
    const shell = readFileSync("app/ConsoleShell.tsx", "utf8");
    expect(shell).toMatch(/data-testid="topbar-new-project"\s+aria-label="New build"\s+title=\{NEW_BUILD_TIP\}/);
    const css = readFileSync("app/globals.css", "utf8");
    expect(css).toMatch(/@media \(max-width: 1480px\) \{[^@]*\.bx-new-project \.bx-new-label \{ display: none; \}/);
  });
});
