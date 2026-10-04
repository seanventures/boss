import { describe, expect, it } from "vitest";

import { canAsk, defaultAdd, firstRun, nextStep, readParams, rowMode, stepNumber } from "../lib/setup-stepper";
import { board, everyRed, firstRun as fresh, repoDone, tryStep } from "./_helpers/setup-fixtures";

describe("the stepper follows the server's next step", () => {
  it("a fresh install is a stepper on Repository", () => {
    const view = fresh();
    expect(firstRun(view)).toBe(true);
    expect(nextStep(view)).toBe("repository");
    expect(rowMode(view, "repository")).toBe("current");
    expect(rowMode(view, "models")).toBe("waiting");
    expect(rowMode(view, "try")).toBe("waiting");
    expect(rowMode(view, "jobs")).toBe("collapsed");
    expect(rowMode(view, "budget")).toBe("collapsed");
    expect([stepNumber(view, "repository"), stepNumber(view, "models"), stepNumber(view, "try")])
      .toEqual([1, 2, 3]);
    expect(stepNumber(view, "jobs")).toBeNull();
  });

  it("a finished step collapses and the next one opens", () => {
    expect(rowMode(repoDone(), "repository")).toBe("done");
    expect(rowMode(repoDone(), "models")).toBe("current");
    expect(rowMode(tryStep(), "models")).toBe("done");
    expect(rowMode(tryStep(), "try")).toBe("current");
  });

  it("after the first PR it is a board with no numbers", () => {
    const view = board();
    expect(firstRun(view)).toBe(false);
    for (const row of ["repository", "models", "jobs", "budget", "try"] as const) {
      expect(rowMode(view, row)).toBe("open");
      expect(stepNumber(view, row)).toBeNull();
    }
  });
});

describe("deep links", () => {
  it("reads focus, add and source", () => {
    expect(readParams("?area=setup&focus=budget")).toEqual({ focus: "budget", add: null, source: null });
    expect(readParams("?area=setup&add=openai")).toEqual({ focus: "models", add: "openai", source: null });
    expect(readParams("?area=setup&source=setup-openrouter").source).toBe("setup-openrouter");
  });

  it("maps the legacy step names", () => {
    expect(readParams("?area=setup&step=github").focus).toBe("repository");
    expect(readParams("?area=setup&step=model").focus).toBe("models");
    expect(readParams("?area=setup&focus=nonsense").focus).toBeNull();
  });
});

describe("first-run choices", () => {
  it("opens Add a model on a subscription signed in on the server, else a found local model", () => {
    const view = fresh();
    expect(defaultAdd(view)).toEqual({ tile: "local", provider: "ollama" });
    view.detected = view.detected.map((d) => d.provider === "codex" ? { ...d, found: "signed_in" } : d);
    expect(defaultAdd(view)).toEqual({ tile: "subscription", provider: "codex" });
    view.detected = view.detected.map((d) => ({ ...d, found: "not_on_server" }));
    expect(defaultAdd(view)).toEqual({ tile: "subscription", provider: "claude_code" });
  });

  it("Ask BOSS only when some model works", () => {
    expect(canAsk(fresh())).toBe(false);
    expect(canAsk(board())).toBe(true);
    const red = everyRed();
    red.models = red.models.map((m) => ({ ...m, pill: { ...m.pill, tone: "red" as const } }));
    expect(canAsk(red)).toBe(false);
  });
});
