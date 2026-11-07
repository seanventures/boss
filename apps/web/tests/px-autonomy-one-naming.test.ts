/**
 * 13-20: ONE naming for the autonomy levels — A0 Ask, A1 Propose, A2 Act, A3 Ship — in Project ›
 * Settings, Govern › Autonomy, badges and tooltips. The console's words (lib/autonomy-levels.ts)
 * are the engine's (task_classes.LEVEL_NAMES); the old second names never come back.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { LEVEL_MEANINGS, LEVEL_WORDS, levelLabel } from "../lib/autonomy-levels";
import { LEVEL_WORDS as FROM_AMBIENT } from "../lib/ambient";
import { codeOnly } from "./_helpers/source";

describe("autonomy levels have one name each", () => {
  it("the wireframe's words, the same in the console and the engine", () => {
    expect(LEVEL_WORDS).toEqual({ A0: "Ask", A1: "Propose", A2: "Act", A3: "Ship" });
    expect(FROM_AMBIENT).toBe(LEVEL_WORDS);
    expect(levelLabel("A1")).toBe("A1 Propose");
    expect(Object.keys(LEVEL_MEANINGS)).toEqual(["A0", "A1", "A2", "A3"]);
    const engine = readFileSync("../../engine/boss_engine/services/ambient/task_classes.py", "utf8");
    expect(engine).toContain('LEVEL_NAMES = {"A0": "Ask", "A1": "Propose", "A2": "Act", "A3": "Ship"}');
  });

  it("no surface keeps the old names", () => {
    for (const f of ["lib/ambient.ts", "lib/ambient-demo.ts", "app/agent-ops/autonomy-panel.tsx", "app/agent-ops/badges.tsx",
                     "app/agent-ops/drawer.tsx", "app/projects/tab-settings.tsx"]) {
      expect(codeOnly(f), f).not.toMatch(/Act to draft|Act and report|"Observe"/);
    }
  });
});
