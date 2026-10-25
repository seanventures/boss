/**
 * The Graph's scan copy names BOSS's real agent, never a placeholder (brec_aeaa99f4fedf4823a4e4):
 * it said "Scout reads it", a name no BOSS agent has; the scan's captain is the Chief Architect.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { SCAN_CAPTAIN_ROLE, scanReader, sentenceStart } from "../lib/kg-scan";

describe("the scan's reader", () => {
  it("is the agent the engine staffs the scan step with", () => {
    const catalogue = readFileSync("../../engine/boss_engine/services/workflow_catalogue.py", "utf8");
    expect(catalogue).toMatch(new RegExp(`"scan_business_rules":\\s*"${SCAN_CAPTAIN_ROLE}"`));
    expect(scanReader()).toBe("the Chief Architect");
    expect(sentenceStart(scanReader())).toBe("The Chief Architect");
  });

  it("no knowledge-area copy names a placeholder agent", () => {
    const dir = "app/knowledge";
    const offenders: string[] = [];
    for (const f of readdirSync(dir).filter((n) => /\.(tsx?)$/.test(n))) {
      const text = readFileSync(join(dir, f), "utf8");
      if (/\b(Scout|Agent [XYZ])\b/.test(text)) offenders.push(f);
    }
    expect(offenders).toEqual([]);
  });
});
