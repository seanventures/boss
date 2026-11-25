// @vitest-environment happy-dom
/**
 * The database line says why Postgres was chosen, beside "Postgres for this
 * build ✓", not only in the title and data-tip. Blank reasons are nothing.
 * More than six words is a paragraph. Six or fewer is a span.
 *
 * Breaks if a padded reason stays padded, a blank reason still renders, a
 * seven-word reason is a span, the Postgres sentence disappears, the title
 * stops being the raw why join, or the line stops calling
 * databaseWhySentence(b.database.why).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type DatabaseWhyLineProps = { why: readonly string[]; sentence: string | null };

const outputs = await import("../app/factory/outputs") as {
  databaseWhySentence?: (why: readonly string[] | null | undefined) => string | null;
  DatabaseWhyLine?: (props: DatabaseWhyLineProps) => React.ReactNode;
};
const { databaseWhySentence, DatabaseWhyLine } = outputs;

const SHORT = "needs a table";
const SIX = "one two three four five six";
const LONG = "one two three four five six seven";
const POSTGRES = "Postgres for this build ✓";

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => { root?.unmount(); });
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

function dropLine(): void {
  act(() => { root?.unmount(); });
  host?.remove();
  root = null;
  host = null;
}

async function show(why: readonly string[]): Promise<HTMLElement> {
  dropLine();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const sentence = databaseWhySentence!(why);
  const Line = DatabaseWhyLine!;
  await act(async () => {
    root!.render(<Line why={why} sentence={sentence} />);
  });
  return host;
}

describe("databaseWhySentence", () => {
  it("joins trimmed reasons with a semicolon, or nothing when they are blank", () => {
    expect(databaseWhySentence!([`  ${SHORT}  `])).toBe("needs a table");
    expect(databaseWhySentence!([SHORT])).toBe("needs a table");
    expect(databaseWhySentence!([SIX])).toBe("one two three four five six");
    expect(databaseWhySentence!([LONG])).toBe("one two three four five six seven");
    expect(databaseWhySentence!(["  needs a table  ", "   ", "ships with the build"])).toBe("needs a table; ships with the build");
    expect(databaseWhySentence!(["  a  b  ", "", "  c  "])).toBe("a  b; c");
    expect(databaseWhySentence!(null)).toBeNull();
    expect(databaseWhySentence!(undefined)).toBeNull();
    expect(databaseWhySentence!([])).toBeNull();
    expect(databaseWhySentence!([""])).toBeNull();
    expect(databaseWhySentence!([" \n\t "])).toBeNull();
    expect(databaseWhySentence!(["  ", "\n"])).toBeNull();
  });
});

describe("the database line says why", () => {
  it("shows the trimmed why beside Postgres, as a paragraph past six words", async () => {
    const why = ["  needs a table  ", "ships with the build"];
    const lineHost = await show(why);
    const line = lineHost.querySelector("[data-testid=fx-brief-database]");
    expect(line?.textContent).toContain(POSTGRES);
    expect(line?.querySelector(".fx-out-sub")?.textContent).toBe("Database");
    expect(line?.getAttribute("title")).toBe("  needs a table  ; ships with the build");
    expect(line?.getAttribute("data-tip")).toBe("  needs a table  ; ships with the build");
    const said = line?.querySelector("[data-testid=fx-brief-database-why]");
    expect(said?.tagName).toBe("P");
    expect(said?.textContent).toBe("needs a table; ships with the build");
    expect(said?.hasAttribute("data-user-text")).toBe(true);
    expect(said?.parentElement).toBe(line);
    expect(line?.textContent).not.toBe(said?.textContent);

    const seven = await show([LONG]);
    const sevenSaid = seven.querySelector("[data-testid=fx-brief-database-why]");
    expect(seven.querySelector("[data-testid=fx-brief-database]")?.textContent).toContain(POSTGRES);
    expect(seven.querySelector("[data-testid=fx-brief-database]")?.getAttribute("title")).toBe(LONG);
    expect(sevenSaid?.tagName).toBe("P");
    expect(sevenSaid?.textContent).toBe(LONG);
    expect(sevenSaid?.hasAttribute("data-user-text")).toBe(true);
  });

  it("shows six words or fewer as a span and says nothing when every reason is blank", async () => {
    const six = await show([SIX]);
    const sixLine = six.querySelector("[data-testid=fx-brief-database]");
    const sixSaid = sixLine?.querySelector("[data-testid=fx-brief-database-why]");
    expect(sixLine?.textContent).toContain(POSTGRES);
    expect(sixLine?.getAttribute("title")).toBe(SIX);
    expect(sixLine?.getAttribute("data-tip")).toBe(SIX);
    expect(sixSaid?.tagName).toBe("SPAN");
    expect(sixSaid?.textContent).toBe(SIX);
    expect(sixSaid?.hasAttribute("data-user-text")).toBe(true);

    const short = await show([`  ${SHORT}  `]);
    const shortLine = short.querySelector("[data-testid=fx-brief-database]");
    const shortSaid = shortLine?.querySelector("[data-testid=fx-brief-database-why]");
    expect(shortLine?.textContent).toContain(POSTGRES);
    expect(shortLine?.getAttribute("title")).toBe(`  ${SHORT}  `);
    expect(shortSaid?.tagName).toBe("SPAN");
    expect(shortSaid?.textContent).toBe(SHORT);
    expect(shortSaid?.hasAttribute("data-user-text")).toBe(true);

    const blank = await show([" \n\t ", ""]);
    const blankLine = blank.querySelector("[data-testid=fx-brief-database]");
    expect(blankLine?.textContent).toContain(POSTGRES);
    expect(blankLine?.textContent).not.toContain("fx-brief-database-why");
    expect(blankLine?.getAttribute("title")).toBe(" \n\t ; ");
    expect(blankLine?.getAttribute("data-tip")).toBe(" \n\t ; ");
    expect(blankLine?.querySelector("[data-testid=fx-brief-database-why]")).toBeNull();
  });

  it("calls databaseWhySentence(b.database.why) beside the Postgres sentence", () => {
    const src = codeOnly(join(__dirname, "../app/factory/outputs.tsx"));
    const start = src.indexOf("b.database?.postgres");
    expect(start).toBeGreaterThan(-1);
    const end = src.indexOf("b.repo", start);
    expect(end).toBeGreaterThan(start);
    const branch = src.slice(start, end);
    expect(branch).toContain("databaseWhySentence(b.database.why)");
    expect(branch).toContain("why={b.database.why}");
    expect(branch).toContain("<DatabaseWhyLine");
    const componentStart = src.indexOf("function DatabaseWhyLine");
    expect(componentStart).toBeGreaterThan(-1);
    const componentEnd = src.indexOf("function BriefBody", componentStart);
    expect(componentEnd).toBeGreaterThan(componentStart);
    const component = src.slice(componentStart, componentEnd);
    expect(component).toContain(POSTGRES);
    expect(component).toContain('title={why.join("; ")}');
    expect(component).toContain('data-tip={why.join("; ")}');
    expect(component).toContain('data-testid="fx-brief-database"');
    expect(component).toContain('data-testid="fx-brief-database-why"');
    expect(src).toContain("previewErrorSentence(p.error)");
    expect(src).toContain("function PreviewErrorLine");
  });
});
