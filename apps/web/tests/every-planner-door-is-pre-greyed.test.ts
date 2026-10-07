/**
 * EVERY DOOR TO THE PLANNER IS PRE-GREYED (round 3 role walk). Planning starts a meeting
 * (meeting:convene); the Agents live board's "Plan it with Juno" was a plain button, so a Viewer
 * pressed it and the API refused POST /api/conversations 403. Every control whose press calls
 * openPlan( is a GatedButton on meeting:convene.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..");
function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    if (n === "node_modules" || n.startsWith(".")) return [];
    return statSync(p).isDirectory() ? files(p) : p.endsWith(".tsx") ? [p] : [];
  });
}

describe("planner doors", () => {
  it("are GatedButtons on meeting:convene", () => {
    const bad: string[] = [];
    for (const f of [...files(join(ROOT, "app")), ...files(join(ROOT, "components"))]) {
      const s = readFileSync(f, "utf8");
      for (const m of s.matchAll(/onClick=\{\(\) => openPlan\(/g)) {
        const open = s.lastIndexOf("<", m.index!);
        const tag = s.slice(open, m.index!);
        if (!/^<GatedButton\b[^]*permission="meeting:convene"/.test(tag)) {
          bad.push(`${f.slice(ROOT.length + 1)}:${s.slice(0, m.index!).split("\n").length}`);
        }
      }
    }
    expect(bad).toEqual([]);
  });

  it("every control LABELLED “Plan it…” is a GatedButton (the planner's, or the org plan's agent:update)", () => {
    // The final role walk: the Factory's empty-state "Plan it with Juno" called an onPlan prop, so
    // the openPlan( rule above never saw it. The label is what a person presses; gate by it.
    const bad: string[] = [];
    for (const f of [...files(join(ROOT, "app")), ...files(join(ROOT, "components"))]) {
      const s = readFileSync(f, "utf8");
      for (const m of s.matchAll(/>\s*Plan it( with [A-Z][a-z]+)?\s*</g)) {
        const before = s.slice(0, m.index! + 1);
        const open = Math.max(before.lastIndexOf("<button"), before.lastIndexOf("<GatedButton"));
        if (open < 0) continue;
        const tag = before.slice(open);
        if (!/^<GatedButton\b[^]*permission="[a-z_]+:[a-z_]+"/.test(tag)) {
          bad.push(`${f.slice(ROOT.length + 1)}:${before.split("\n").length}`);
        }
      }
    }
    expect(bad).toEqual([]);
  });
});
