import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { prdDigest, prdParts } from "../lib/research-panels";

/**
 * "Build it" from a panel hands the composer the agreed PRD — and the re-check of
 * rp/research-panels (2026-10-05) found it listing Non-Goals under "Goals:": a keyword match on
 * /goal/ caught "Non-Goals", and the real Goals, in a table, were skipped. Sections are read by
 * their ROLE and tables row by row. Checked against all 22 PRDs the shared server's panels wrote
 * (tests/_helpers/real-prds.json, cut after the requirements).
 */
const PRDS: string[] = JSON.parse(readFileSync("tests/_helpers/real-prds.json", "utf8"));

/** The Non-Goals as the PRD wrote them, read independently of prdParts: every line after a line
 *  naming Non-Goals, up to the next heading or rule. */
function nonGoalLines(md: string): string[] {
  const out: string[] = [];
  let inside = false;
  for (const raw of md.split("\n")) {
    const line = raw.trim();
    // A heading or bold line that names Non-Goals on their own — not "Goals & Non-Goals".
    if (/non[‐‑–-]?\s*goals?|out of scope/i.test(line) && /^(#|\*\*|__)/.test(line)
        && !/goals?\s*(?:&|and)\s*non/i.test(line)) { inside = true; continue; }
    if (inside && (/^#/.test(line) || /^-{3,}$/.test(line))) inside = false;
    if (inside && line) out.push(line.replace(/^[-*+•]\s+|\d+[.)]\s+/, "").replace(/[*_`|]/g, "").replace(/\s+/g, " ").trim());
  }
  return out.filter((l) => l.length > 6);
}

describe("Build it reads a PRD by section", () => {
  it("is checked against every real PRD", () => {
    expect(PRDS).toHaveLength(22);
  });

  it.each(PRDS.map((md, i) => [i, md] as const))("PRD %i: no Non-Goal is ever a Goal", (_i, md) => {
    const parts = prdParts(md);
    const non = nonGoalLines(md);
    for (const g of parts.goals) {
      expect(non.some((n) => n.includes(g.slice(0, 40)) || g.includes(n.slice(0, 40)))).toBe(false);
      expect(g).not.toMatch(/out of scope|non[‐‑–-]?goal/i);
    }
  });

  it("finds the goals, whether a table or a list, in nearly every real PRD", () => {
    const withGoals = PRDS.filter((md) => prdParts(md).goals.length > 0).length;
    expect(withGoals).toBeGreaterThanOrEqual(20);
  });

  it("reads a goals table by its Goal column and requirements by name, not ID", () => {
    const md = "## 3. Goals & Non‑Goals\n\n| Goal | Success Metric |\n|---|---|\n| **Surface overdue flags first** | 80 % cleared |\n\n"
      + "**Non‑Goals**\n\n* Automated PR generation\n\n---\n\n## 5.1 Functional\n\n| ID | Requirement | Description |\n|---|---|---|\n"
      + "| **F‑1** | Source‑agnostic ingestion | Adapters |\n\n### 5.2 Non‑functional\n\n- p95 under 200 ms\n";
    expect(prdParts(md)).toEqual({ summary: [], goals: ["Surface overdue flags first"],
      nonGoals: ["Automated PR generation"], requirements: ["Source‑agnostic ingestion"] });
    expect(prdDigest(md, "Flags", null)).toBe("Flags\n\nGoals:\n- Surface overdue flags first\n\nWhat it must do:\n- Source‑agnostic ingestion");
  });
});
