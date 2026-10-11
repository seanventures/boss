import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { StackWarning } from "../components/v2/templates/stack-warning";
import { parseStackCheck } from "../lib/templates";
import { codeOnly } from "./_helpers/source";

(globalThis as Record<string, unknown>).React = React;

/**
 * "Someone wants Go — can't we fail faster than the Build stage?" A brief asking for a stack BOSS
 * cannot build is said BEFORE Start (GET /v1/stacks), in the API's words, with a "Start anyway"
 * the API honours as `allow_unsupported_stack` (services/stacks.py). Both start doors ask.
 */
const GO = {
  check: {
    ok: false, code: "stack_unsupported", unsupported: ["Go"], off: [],
    message: "This brief asks for Go, which BOSS cannot build. BOSS builds Python (FastAPI, Flask, Django), Node (Express) and Java (Spring Boot).",
  },
};

function state(over: Partial<{ refused: boolean; allowed: boolean }> = {}) {
  const check = parseStackCheck(GO);
  return { check, refused: true, allowed: false, blocks: true, setAllowed: () => {}, ...over };
}

describe("a brief that asks for a stack BOSS cannot build", () => {
  it("reads the API's verdict", () => {
    expect(parseStackCheck(GO)).toEqual({ ok: false, message: GO.check.message, unsupported: ["Go"] });
    expect(parseStackCheck({ check: { ok: true, message: "" } })?.ok).toBe(true);
    expect(parseStackCheck({})).toBeNull();
  });

  it("is said before Start, with what IS supported and a Start anyway", () => {
    const html = renderToStaticMarkup(<StackWarning state={state()} testId="w" />);
    expect(html).toContain("asks for Go, which BOSS cannot build");
    expect(html).toContain("BOSS builds Python");
    expect(html).toContain('data-testid="w-anyway"');
    expect(html).toContain("Start anyway");
  });

  it("says nothing when the stack is buildable", () => {
    expect(renderToStaticMarkup(<StackWarning state={state({ refused: false })} />)).toBe("");
  });

  it("both start doors ask, block Start, and send the override only when ticked", () => {
    for (const file of ["app/workflows/new-project-composer.tsx", "components/v2/templates/template-sheet.tsx"]) {
      const src = codeOnly(file);
      expect(src).toContain("useStackCheck(");
      expect(src).toContain("<StackWarning");
      expect(src).toMatch(/stackCheck\.blocks \?/);
      expect(src).toContain("stackCheck.refused && stackCheck.allowed ? { allow_unsupported_stack: true }");
    }
  });
});
