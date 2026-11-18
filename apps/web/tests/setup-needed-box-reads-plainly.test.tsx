/**
 * The sample's "Setup needed" box reads plainly (stranger test r2, 2026-11-17):
 *   · a deploy host the run stops for you before is said as "later, before deploy", not as needed
 *     now while the note under it says the PR does not need one;
 *   · "Needed by …" and the detail are two proper sentences ("…Set up monitoring. no model is
 *     connected" was a lower-case join);
 *   · no stray "Open setup : A connected model" text: the link's name is its aria-label.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ReadinessPanel, laterWords, neededByWords, type Readiness, type ReadinessNeed } from "../app/workflow-readiness-ui";

const need = (over: Partial<ReadinessNeed>): ReadinessNeed => ({
  key: "model", label: "A connected model", fix: "Connect a model in Setup.", area: "setup",
  href: "/?area=setup&focus=models", ok: false, detail: "no model is connected to this project",
  needed_by: [{ step_id: "panel", label: "Convene a panel" }, { step_id: "mon", label: "Set up monitoring" }],
  first_step_label: "Convene a panel", after_a_person: false, ...over });

const R: Readiness = { ready: false, checked: [need({})], missing: [
  need({}),
  need({ key: "deployment_target", label: "Somewhere to deploy", detail: "no verified deployment host.",
         needed_by: [{ step_id: "deploy", label: "Deploy" }], first_step_label: "Deploy to staging", after_a_person: true }),
] };

describe("Setup needed", () => {
  it("joins the sentences properly", () => {
    expect(neededByWords(need({}))).toBe("Needed by Convene a panel, Set up monitoring. No model is connected to this project.");
    expect(neededByWords(need({ detail: "x is missing." }))).toBe("Needed by Convene a panel, Set up monitoring. X is missing.");
    expect(neededByWords(need({ needed_by: [], detail: "" }))).toBe("");
  });

  it("says a later need as later, before deploy", () => {
    expect(laterWords({ key: "deployment_target", first_step_label: "Deploy to staging" })).toBe("later, before deploy");
    expect(laterWords({ key: "email", first_step_label: "Email the newsletter" })).toBe("later, before Email the newsletter");
    const html = renderToStaticMarkup(<ReadinessPanel readiness={R} accepted={[]} onAccept={() => {}} />);
    expect(html).toContain('data-testid="dw-readiness-later-deployment_target"');
    expect(html).toContain("(later, before deploy)");
    expect(html).not.toContain('data-testid="dw-readiness-later-model"');
  });

  it("has no stray link text, and no lower-case sentence join", () => {
    const html = renderToStaticMarkup(<ReadinessPanel readiness={R} accepted={[]} onAccept={() => {}} />);
    expect(html).not.toContain("sr-only");
    expect(html).not.toMatch(/Open setup<[^>]*>:/);
    expect(html).toContain('aria-label="Open setup for A connected model"');
    expect(html).not.toMatch(/\. no model/);
  });
});
