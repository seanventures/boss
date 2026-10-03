import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { cssOnly } from "./_helpers/source";

/**
 * The workflow and agent screens' stylesheets hold only rules something still wears.
 *
 * Workflow Studio retired the builder, the Yours and Catalogue cards, the step rail and Agent
 * Studio. Their rules — `.dw-builder`, `.dw-draft*`, `.dw-rail`, `.as-flow-*`, the Studio
 * layout — stayed in globals.css after the markup went, ~1,500 lines nobody could tell were
 * dead, and one of them (`.dw-rail`) was already fighting a newer rule of the same name. A
 * test that pinned a rule (`.dw-new-box`) kept passing after the class left the markup.
 *
 * So, as a class: every class in these families that globals.css styles is worn by some
 * console source file; and Workflow Studio's own `wf-` classes never reuse a name another
 * screen already styles (the Task workflow screen owns `.wf-chip`, `.wf-steps`, `.wf-step*`).
 */
const FAMILIES = /^(dw-|as-|aw-|studio-)/;

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (name === "node_modules" || name.startsWith(".")) continue;
    if (statSync(path).isDirectory()) out.push(...sources(path));
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\./.test(name)) out.push(path);
  }
  return out;
}

const SOURCE = ["app", "components", "lib"].flatMap(sources)
  .map((path) => readFileSync(path, "utf8")).join("\n");
const GLOBALS = cssOnly("app/globals.css");
const classesIn = (css: string) => new Set([...css.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map((m) => m[1]));
const worn = (cls: string) => new RegExp(`(?<![\\w-])${cls.replace(/[-]/g, "\\-")}(?![\\w-])`).test(SOURCE);

describe("no dead workflow or agent CSS", () => {
  it("every dw-, as-, aw- and studio- class globals.css styles is worn somewhere", () => {
    const styled = [...classesIn(GLOBALS)].filter((c) => FAMILIES.test(c));
    expect(styled.length).toBeGreaterThan(50);
    expect(styled.filter((c) => !worn(c))).toEqual([]);
  });

  it("the retired builder, rail and Studio layout are gone", () => {
    for (const gone of [".dw-builder", ".dw-draft {", ".dw-rail {", ".as-flow-lane", ".as-flow-node",
                        ".as-inspector", ".dw-plan", ".dw-design-way"]) {
      expect(GLOBALS).not.toContain(gone);
    }
  });

  it("Workflow Studio's wf- classes never reuse a name another screen styles", () => {
    const studio = readdirSync("app/workflows").filter((f) => f.endsWith(".css"))
      .flatMap((f) => [...classesIn(cssOnly(`app/workflows/${f}`))].filter((c) => c.startsWith("wf-")));
    expect(studio.length).toBeGreaterThan(20);
    const taken = [...classesIn(GLOBALS)].filter((c) => c.startsWith("wf-"));
    expect(studio.filter((c) => taken.includes(c))).toEqual([]);
  });
});
