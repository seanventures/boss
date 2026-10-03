import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DeliveryWorkflows, readAreaUrl } from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";

/**
 * The Workflows area: Runs and Library, and the editor full width on `&wf=`.
 *
 * The screen must never invent a capability, a risk tier or an approval rule. Everything it
 * draws comes from the engine's registry, so the gates are the ones the runner enforces.
 *
 * REWRITTEN for Workflow Studio. The builder (BuilderView), the plan panel, the Yours and
 * Catalogue tabs and the Design tab are gone; what this file pinned about them now lives with
 * their replacements — the store round-trip (tests/workflow-editor-store.test.ts), the lane and
 * palette (tests/flow-lane.test.tsx, tests/workflow-editor.test.tsx), the inspector
 * (tests/inspector.test.tsx) and the Library (tests/workflow-library.test.tsx). What is kept
 * here is the area itself: its states, its runs, its tabs, and the custom-step form.
 */
(globalThis as Record<string, unknown>).React = React;

const UI = codeOnly("app/delivery-workflows-ui.tsx");
const LIBRARY = codeOnly("app/workflows/library.tsx");
/** The define/edit step form, moved out of the workflow screen into its own file. */
const FORM = codeOnly("app/workflows/define-step-form.tsx");
const ROUTE = codeOnly("app/api/delivery-workflows/route.ts");
const SHELL = codeOnly("app/ConsoleShell.tsx");
const EDITION = codeOnly("lib/edition.ts");
const CSS = codeOnly("app/globals.css");

describe("the screen never claims an empty project", () => {
  it("says it is loading before it has asked", () => {
    const html = renderToStaticMarkup(
      <DeliveryWorkflows projectId="boss-dogfood" actor="ceo" />);
    expect(html).toContain("Loading workflows");
  });

  it("has three states, not two", () => {
    expect(UI).toContain('useState<"loading" | "ready" | "failed">');
  });

  it("shows the reason it failed", () => {
    expect(UI).toContain("Could not load workflows — {error}");
  });
});

describe("two tabs, synced to the URL, and the editor is not a tab", () => {
  it("reads Runs unless the URL says Library", () => {
    expect(readAreaUrl("")).toEqual({ tab: "runs", wf: null });
    expect(readAreaUrl("?area=delivery_workflows&tab=library")).toEqual({ tab: "library", wf: null });
    // A retired tab id is rewritten before this reads it (app/legacy-areas.ts); on its own it
    // is not a tab.
    expect(readAreaUrl("?tab=design")).toEqual({ tab: "runs", wf: null });
  });

  it("opens the editor on &wf=, including a new one", () => {
    expect(readAreaUrl("?tab=library&wf=video_short")).toEqual({ tab: "library", wf: "video_short" });
    expect(readAreaUrl("?wf=new")).toEqual({ tab: "runs", wf: "new" });
    expect(readAreaUrl("?wf=%20")).toEqual({ tab: "runs", wf: null });
  });

  it("has no builder left in it", () => {
    for (const gone of ["function BuilderView", "function WorkflowPlanPanel", "function YoursView",
                        "function CatalogueView", "Design a workflow", "Fork to edit", "setBuilderTarget"]) {
      expect(UI).not.toContain(gone);
    }
  });

  it("stays small enough to read", () => {
    expect(UI.split("\n").length).toBeLessThan(4000);
  });
});

describe("nothing is invented", () => {
  it("takes the capability palette from the engine", () => {
    // Not a hard-coded list in the UI. A palette that drifts from the registry offers steps
    // that cannot be saved.
    expect(UI).toContain('get<{ capabilities: Capability[]');
    expect(ROUTE).toContain("/v1/delivery-workflows/capabilities");
  });

  it("takes the risk floor from the engine rather than assuming 2", () => {
    expect(UI).toContain("manual_approval_risk_floor");
  });

  it("forwards the engine's own validation message", () => {
    // "step 'd' is risk tier 2 and always requires a person" is the whole value of the
    // validation; replacing it with "request failed" throws it away.
    // REWRITTEN (review of the readiness change): the route forwarded `detail` by casting it to
    // a string, which forwarded the readiness OBJECT as `error` too. It now goes through the
    // shared helper, which keeps a sentence verbatim (and only a sentence) as `error`.
    expect(ROUTE).toContain("refusalFrom(r.status, parsed)");
    expect(codeOnly("lib/start-refusal.ts")).toContain("if (flat) return { error: flat };");
    // AND IN WORDS: the helper reads the detail through apiErrorText's reader (lib/api-error.ts),
    // so a validation array is "field: message", never "[object Object]" (console-b stream).
    expect(codeOnly("lib/start-refusal.ts")).toContain('import { detailText } from "./api-error";');
    expect(UI).toContain("result.error");
  });
});

describe("a run says what it is waiting for", () => {
  it("names the approver, not just 'waiting'", () => {
    // "Waiting" alone sends an operator to ask three people who it is waiting on.
    // REWRITTEN: by name ("Release Manager"), not the raw role key the line used to print.
    expect(UI).toContain("waiting on {/^[a-z0-9_]+$/.test(run.waiting_on)");
    expect(UI).toContain("roleLabel(run.waiting_on) : run.waiting_on}");
  });

  it("shows what each step produced", () => {
    // A run view showing only status makes an operator open five other screens to find the PR
    // it opened.
    expect(UI).toContain("dw-step-out");
  });

  it("never renders a nested object as [object Object]", () => {
    // The least useful string in software.
    expect(UI).toContain("function summarise");
    expect(UI).not.toMatch(/\$\{String\(v\)\.slice/);
  });

  it("shows progress as a count as well as a bar", () => {
    expect(UI).toContain("{run.steps_done} of {run.step_count}");
  });

  it("keeps asking even when nothing is running", () => {
    // This replaces a test that asserted the opposite — "an idle console should be idle" — which
    // is what broke it. A console cannot know work has STARTED without asking: a project begun
    // from the Command Center, the Factory form, or a PRD approval creates its run elsewhere.
    // Anyone holding this page open when the only run was blocked saw a frozen list and no sign
    // of the project they had just started.
    expect(UI).not.toContain("if (!anyLive) return;");
    expect(UI).toContain("anyLive ? 8000 : 15000");
  });

  it("opens the run the operator was sent here to see", () => {
    // Creating a project redirects here with `?wfr=<id>`. Expanding it on arrival is the
    // difference between "your project is somewhere in this list" and showing them the steps.
    expect(UI).toContain('get("wfr")');
    expect(UI).toContain("setOpenRun(wanted)");
  });
});

describe("status is never colour alone", () => {
  it("renders the word in the badge", () => {
    expect(UI).toContain("{run.status}</span>");
    expect(CSS).toContain('.dw-badge[data-tone="wait"]');
  });
});

describe("where it lives", () => {
  it("is a first-class area in the console", () => {
    // One entry, "Workflows": built, run and watched in one area (Workflow Studio spec 2.1).
    expect(SHELL).toContain('{ id: "delivery_workflows", label: "Workflows"');
  });

  it("is available to self-hosted operators", () => {
    // Someone who cannot choose or see the path their work takes is running the product blind.
    const set = EDITION.slice(EDITION.indexOf("V1_COMMUNITY_PRIMARY_NAV = new Set("));
    expect(set.slice(0, set.indexOf("]);"))).toContain('"delivery_workflows"');
  });
});

describe("the route handler refuses what it does not know", () => {
  it("rejects an unknown view rather than guessing", () => {
    expect(ROUTE).toContain("unknown view");
  });

  it("rejects an unknown action rather than guessing", () => {
    expect(ROUTE).toContain("unknown action");
  });
});

describe("a customer can define their own step", () => {
  it("offers the form from the Library's Steps view", () => {
    expect(LIBRARY).toContain('data-testid="wf-lib-new-step"');
    expect(LIBRARY).toContain("<DefineStepModal");
    expect(FORM).toContain('data-testid="dw-define-form"');
  });

  it("NEVER offers a risk field", () => {
    // The whole design rests on this. A customer who could type their own tier would have a
    // documented way past every approval gate in the product, so the form must not have the
    // control at all — not a disabled one, not a hidden one.
    const form = FORM;
    expect(form).toContain("export function DefineStepForm(");
    expect(form).not.toContain("setRiskTier");
    expect(form).not.toContain('name="risk_tier"');
    expect(form).not.toContain("risk_tier:");
  });

  it("says what the binding costs before the step is created", () => {
    expect(FORM).toContain('data-testid="dw-define-risk"');
    expect(FORM).toContain("This step will always stop for a person.");
  });

  it("names an unclassified skill as unclassified rather than as tier 0", () => {
    expect(FORM).toContain("nobody has classified this skill");
  });

  it("warns that an untrusted binding will not publish", () => {
    expect(FORM).toContain('data-testid="dw-define-blocked"');
  });

  it("says so when there is nothing to bind to, counting CHOICES not servers", () => {
    // A project with three registered MCP servers that are all unreachable has three empty
    // groups in the dropdown and nothing to pick. Counting servers would call that "fine".
    expect(FORM).toContain("Nothing to bind a step to yet.");
    expect(FORM).toContain("bindable.mcp_servers.reduce((n, server) => n + server.tools.length, 0)");
    expect(FORM).toContain("|| choices === 0}");
  });

  it("names the unreachable servers rather than leaving an empty dropdown unexplained", () => {
    expect(FORM).toContain("registered but not answering");
  });
});

describe("the custom-step endpoints exist on both sides", () => {
  it("the route proxies them", () => {
    expect(ROUTE).toContain("custom_steps:");
    expect(ROUTE).toContain("define_step:");
    expect(ROUTE).toContain("export async function DELETE");
  });

  it("the API resolves risk rather than accepting it", () => {
    const api = codeOnly("../../apps/api/app/main.py");
    expect(api).toContain('@app.get("/v1/delivery-workflows/custom-steps")');
    expect(api).toContain('@app.post("/v1/delivery-workflows/custom-steps")');
    // The create endpoint must not read a risk tier off the payload.
    const create = api.slice(
      api.indexOf("def create_delivery_custom_step"),
      api.indexOf("def delete_delivery_custom_step"),
    );
    expect(create).not.toContain('payload.get("risk_tier")');
  });
});
