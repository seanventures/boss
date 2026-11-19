import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync("app/projects/px.css", "utf8");

function declarations(selector: string) {
  const start = css.indexOf(`${selector} {`);
  expect(start).toBeGreaterThanOrEqual(0);
  return css.slice(start, css.indexOf("}", start));
}

describe("failed project alerts keep their message readable", () => {
  it("reserves a full text column independent of action widths", () => {
    const card = declarations(".px-ny.px-ny-failed");
    expect(card).toContain("display: grid");
    expect(card).toContain("grid-template-columns: 22px minmax(0, 1fr)");
    expect(card).toContain("align-items: start");
    expect(declarations(".px-ny-failed > .px-row")).toContain("grid-column: 2");
  });

  it("wraps long messages and actions within narrow cards", () => {
    expect(declarations(".px-ny-failed > .px-ny-b")).toContain("overflow-wrap: anywhere");
    const actions = declarations(".px-ny-failed > .px-row > *");
    expect(actions).toContain("max-width: 100%");
    expect(actions).toContain("white-space: normal");
    expect(actions).toContain("overflow-wrap: anywhere");
  });
});
