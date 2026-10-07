import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { AREA_IDS, isAreaId, toAreaId } from "../lib/area-ids";
import { resolveLegacyArea } from "../app/legacy-areas";
import { codeOnly } from "./_helpers/source";

/**
 * Every in-console link names an area the console has.
 *
 * Review of milestone/console-b, 2026-10-02: Command Center's "Open captain" linked to
 * ?area=metatron and "See your apps" to ?area=apps. Neither id was in the nav any more, the shell
 * drops a navigate event for an unknown id, and so both clicks did nothing; the area tests only
 * checked the nav against itself. AreaLink now takes an AreaId (a compile error for a literal),
 * and this enumerates every literal target in the console's source: AreaLink area="...", and
 * every ?area= / &area= in an href.
 */
const ID = "[a-z][a-z0-9_-]*";
const SHELL = codeOnly("app/ConsoleShell.tsx");

function navAreas(): Set<string> {
  return new Set([...SHELL.matchAll(new RegExp(`\\{\\s*id: "(${ID})",\\s*label:`, "g"))].map((m) => m[1]));
}

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (name === "node_modules" || name.startsWith(".")) continue;
    if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
    else if (/\.(tsx?|jsx?)$/.test(name)) out.push(path);
  }
  return out;
}

function linkTargets(): Array<{ file: string; area: string }> {
  const found: Array<{ file: string; area: string }> = [];
  for (const file of [...sourceFiles("app"), ...sourceFiles("components"), ...sourceFiles("lib")]) {
    const code = codeOnly(file);
    for (const m of code.matchAll(new RegExp(`<AreaLink[^>]*?\\barea="(${ID})"`, "g"))) {
      found.push({ file, area: m[1] });
    }
    for (const m of code.matchAll(new RegExp(`[?&]area=(${ID})`, "g"))) {
      // An OLD link kept as the input of a redirect check (`from: "?area=monitoring…"`, the
      // Infra/Observability parity table) is a fixture proving it still lands, not a link.
      if (/from:\s*["'`][^"'`]*$/.test(code.slice(Math.max(0, m.index! - 60), m.index!))) continue;
      found.push({ file, area: m[1] });
    }
  }
  return found;
}

describe("every area link goes somewhere", () => {
  it("names only areas that are in the nav", () => {
    const nav = navAreas();
    const targets = linkTargets();
    // 12 at integration: the console-a stream's Command Center replaced the task-era KPI tiles
    // and quick-action links with run rows that link to runs, not areas.
    expect(targets.length, "the scan found too few links to be a real check").toBeGreaterThan(10);
    const dead = targets.filter((t) => !nav.has(t.area)).map((t) => `${t.file}: area=${t.area}`);
    expect(dead, `links to areas the console does not have:\n${dead.join("\n")}`).toEqual([]);
  });

  it("every AreaId is a nav area, or a legacy alias rewritten to one", () => {
    const nav = navAreas();
    const alias = (id: string) => resolveLegacyArea(new URLSearchParams(`area=${id}`))?.get("area") ?? null;
    const lost = AREA_IDS.filter((id) => !nav.has(id) && !(alias(id) && nav.has(alias(id)!)));
    expect(lost).toEqual([]);
  });

  it("an area id from the API that the console does not have falls back instead of dying", () => {
    expect(isAreaId("metatron")).toBe(false);
    expect(toAreaId("metatron", "code_tasks")).toBe("code_tasks");
    expect(toAreaId("meetings", "code_tasks")).toBe("meetings");
    expect(codeOnly("lib/metrics.ts")).toContain('toAreaId(i.area, "code_tasks")');
  });

  it("AreaLink takes an AreaId, not any string", () => {
    expect(codeOnly("app/area-link.tsx")).toMatch(/area: AreaId;/);
  });
});
