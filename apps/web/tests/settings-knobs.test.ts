import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * How hard BOSS runs lived entirely in environment variables, so changing a number meant an
 * ops action and a container restart. In practice nobody changed them and the defaults became
 * the product.
 */
const KNOBS = codeOnly("app/settings-knobs-ui.tsx");
const SHELL = codeOnly("app/ConsoleShell.tsx");
const STORE = codeOnly("../../engine/boss_engine/services/project_settings.py");
const CSS = codeOnly("app/globals.css");

describe("the area is called Settings", () => {
  it("is labelled for what it governs, not just spend", () => {
    expect(SHELL).toContain('id: "budget", label: "Settings"');
  });

  it("keeps the area id, because ids are routes and bookmarks", () => {
    expect(SHELL).toContain('id: "budget"');
  });
});

describe("a knob says where its value came from", () => {
  it("shows the source beside every knob", () => {
    // "runs three agents" and "nobody chose, so it runs the deployment default" are different
    // facts, and only one tells you whether turning this dial does anything.
    expect(KNOBS).toContain('data-source={k.source}');
    expect(CSS).toContain('.settings-source[data-source="project"]');
  });

  it("offers a way back to the default", () => {
    expect(KNOBS).toContain("Use the default");
  });

  it("hands the knob back rather than storing the current default", () => {
    // Storing it would freeze the project against a later change to the deployment.
    expect(STORE).toContain("def clear_one");
    expect(STORE).toContain("DELETE FROM project_settings");
  });
});

describe("a knob cannot be set to something harmful", () => {
  it("refuses a key it does not know", () => {
    // A settings table that accepts anything is a place where typos live forever.
    expect(STORE).toContain("class UnknownKnob");
    expect(STORE).toContain("is not a setting BOSS knows about");
  });

  it("bounds every value, in words", () => {
    // "between 1 and 16" is actionable; "invalid" is not. A concurrency of 400 is an outage.
    expect(STORE).toContain("has to be between");
    expect(STORE).toContain('"min": 1, "max": 16');
  });
});

describe("the knob is read by the thing it governs", () => {
  it("concurrency is looked up per project at run time", () => {
    // A knob that changes nothing is worse than no knob.
    const GOV = codeOnly("../../engine/boss_engine/services/governed_code_task_service.py");
    expect(GOV).toContain('_effective_setting(');
    expect(GOV).toContain('"code_agent_max_concurrency"');
  });

  it("falls back to the environment when the project has said nothing", () => {
    // The environment was the only answer until this existed, and remains a complete one.
    expect(STORE).toContain("def effective");
    expect(STORE).toContain("return fallback if row is None else row");
  });
});

describe("a step can carry what it needs to KNOW", () => {
  const DW = codeOnly("app/delivery-workflows-ui.tsx");
  const SVC = codeOnly("../../engine/boss_engine/services/workflow_custom_steps.py");

  it("asks for it when the step is defined", () => {
    // A binding says WHICH skill or tool runs; it says nothing about the standard the step is
    // meant to hold to — house style, the clauses that matter, the doc explaining the system
    // it touches. That lived in the head of whoever defined the step.
    expect(DW).toContain('data-testid="dw-step-knowledge"');
    expect(DW).toContain("knowledge: knowledge.trim()");
  });

  it("hands it to the thing that actually runs", () => {
    // Stored and unused would be worse than not asking.
    expect(SVC).toContain('inputs = {**inputs, "knowledge": knowledge.strip()}');
  });

  it("passes nothing when a step has none", () => {
    // A step defined before this existed must behave exactly as it did.
    expect(SVC).toContain("if knowledge.strip():");
  });
});
