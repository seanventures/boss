/**
 * Every in-console link names an area the console has.
 *
 * Two Command Center buttons did nothing at all: "See your apps" (area "apps", which was never
 * an area) and "Open captain" (area "metatron", removed). AreaLink prevents the default and
 * dispatches boss:navigate, and the shell ignores an id it does not know — so the click was
 * swallowed silently. This enumerates every literal area target in the console and the areas
 * the engine names in its guidance, against the shell's own list.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else if (/\.(ts|tsx)$/.test(name)) out.push(path);
  }
  return out;
}

const SHELL = readFileSync("app/ConsoleShell.tsx", "utf8");
const AREAS = new Set(
  [...SHELL.slice(SHELL.indexOf("const AREAS: AreaDef[] = ["),
                  SHELL.indexOf("];", SHELL.indexOf("const AREAS: AreaDef[] = [")))
    .matchAll(/\bid: "([a-z_-]+)"/g)].map((m) => m[1]));

const TARGETS = [
  /<AreaLink\b[^>]*?\barea="([^"]+)"/g,
  /[?&]area=([a-z_-]+)/g,
  /areaHref\("([a-z_-]+)"/g,
  /"boss:navigate",\s*\{\s*detail:\s*"([a-z_-]+)"/g,
];

describe("in-console links", () => {
  it("the shell's area list is read (the scan works)", () => {
    expect(AREAS.size).toBeGreaterThan(30);
    expect(AREAS.has("delivery_workflows")).toBe(true);
  });

  it("every literal area target is an area the shell knows", () => {
    const bad: string[] = [];
    let seen = 0;
    for (const file of [...walk("app"), ...walk("lib"), ...walk("components")]) {
      const src = readFileSync(file, "utf8");
      for (const pattern of TARGETS) {
        for (const m of src.matchAll(pattern)) {
          seen += 1;
          if (!AREAS.has(m[1])) {
            bad.push(`${relative(".", file)}:${src.slice(0, m.index).split("\n").length} → ${m[1]}`);
          }
        }
      }
    }
    expect(seen).toBeGreaterThan(15);
    expect(bad).toEqual([]);
  });

  it("the areas the engine sends people to are areas too", () => {
    // decision_actions.guidance() and command_center name where a refusal or a setup gap is
    // fixed; the console renders them as links.
    const engine = ["decision_actions.py", "command_center.py"].map((f) =>
      readFileSync(join("..", "..", "engine", "boss_engine", "services", f), "utf8")).join("\n");
    const named = [...engine.matchAll(/"(?:area|fix_area)":\s*"([a-z_-]+)"/g)].map((m) => m[1]);
    expect(named.length).toBeGreaterThan(2);
    for (const area of named) expect(AREAS.has(area), area).toBe(true);
  });

  it("the Metatron leftovers are gone from both pages", () => {
    // The Community tree has one page: the private repo's page-community.tsx IS this page.tsx
  // (the export's page_export_rewrite), so the console-a stream's two-page loop reads one here.
  for (const page of ["app/page.tsx"]) {
      const src = readFileSync(page, "utf8");
      expect(src).not.toContain("Open captain");
      expect(src).not.toContain("Scoped status query");
    }
  });
});
