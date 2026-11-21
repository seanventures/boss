import { readdirSync, readFileSync } from "node:fs";
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

describe("every failed card with actions takes the same layout", () => {
  it("each px-ny card holding FailedActions is px-ny-failed, its text in px-ny-b", () => {
    const dir = "app/projects";
    for (const f of readdirSync(dir).filter((n) => n.endsWith(".tsx"))) {
      const src = readFileSync(`${dir}/${f}`, "utf8");
      let at = src.indexOf("<FailedActions");
      while (at >= 0) {
        // The card that holds it: the nearest `<div className="px-ny…">` (not -ico / -b) above.
        const before = src.slice(0, at);
        const cards = [...before.matchAll(/<div className="([^"]*)"/g)]
          .filter((m) => m[1].split(/\s+/)[0] === "px-ny");
        const last = cards[cards.length - 1];
        if (last && before.lastIndexOf("footer=") < (last.index ?? 0)) {
          const card = src.slice(last.index, at);
          expect(last[1], `${f}: a failed card with actions`).toContain("px-ny-failed");
          expect(card, `${f}: its text column`).toContain('className="px-ny-b"');
        }
        at = src.indexOf("<FailedActions", at + 1);
      }
    }
  });
});
