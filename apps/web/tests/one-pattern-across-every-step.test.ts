import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * "Not just deploy — review all the workflow steps and follow this consistent pattern."
 *
 * One rule, applied to every step kind:
 *
 *   an EXPLANATION goes on the dot beside the thing it explains;
 *   PROGRESS stays visible, because a minute of silence after a click reads as a click that
 *     did nothing;
 *   a WARNING stays visible, because it is news and not elaboration.
 *
 * That third distinction is the one worth guarding. "Everything grey becomes a tooltip" would
 * have hidden "starting a preview, this takes up to a minute" and "this link opens a LATER
 * build", which are the two sentences on this screen an operator cannot afford to miss.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");
const CSS = codeOnly("app/globals.css");

describe("every step follows the same rule", () => {
  it("retrying a failed step explains itself on the dot", () => {
    const at = UI.indexOf('data-testid="dw-step-retry"');
    expect(UI.slice(at, at + 400)).toContain("<Explain");
    expect(UI).toContain("Runs this step only; everything before it stays done.");
  });

  it("a decided gate keeps its headline and moves the elaboration", () => {
    // "Approved — starting." is news. The paragraph after it is not.
    expect(UI).toContain("<strong>Approved — starting.</strong>");
    const at = UI.indexOf("<strong>Approved — starting.</strong>");
    expect(UI.slice(at, at + 300)).toContain("<Explain");
  });

  it("an expired gate does too, and keeps its way out", () => {
    const at = UI.indexOf("The gate expired before anyone answered it.");
    const block = UI.slice(at, at + 700);
    expect(block).toContain("<Explain");
    expect(block).toContain('data-testid="dw-step-reopen"');
  });

  it("and the deploy step it started from is unchanged", () => {
    expect(UI).toContain('label="How setting a target works"');
    expect(UI).toContain('label="What approving does"');
  });
});

describe("progress and warnings are not explanations", () => {
  it("a preview that is starting says so in the layout, not behind a click", () => {
    const at = UI.indexOf("{starting ? (");
    const block = UI.slice(at, at + 400);
    expect(block).toContain('data-testid="dw-preview-hint"');
    expect(block).toContain("this takes up to a minute");
    expect(block).not.toContain("<Explain");
  });

  it("a preview of the WRONG build is a warning and stays visible", () => {
    // Visible AND SHORT. I argued it had to stay on the page — right — then left it as two
    // lines of prose, which is the thing being complained about. Six words carry it.
    expect(UI).toContain("Preview is from a later build");
    const at = UI.indexOf("Preview is from a later build");
    expect(UI.slice(at - 200, at)).toContain('className="dw-preview-warn"');
    // The explanation goes where every other explanation went.
    expect(UI.slice(at, at + 400)).toContain('label="What that means"');
  });

  it("but what a preview IS goes on the dot", () => {
    expect(UI).toContain('label="What this preview is"');
  });
});

describe("an open run is a different object, not a thicker stripe", () => {
  it("the card says whether it is open", () => {
    expect(UI).toContain('data-open={open ? "true" : undefined}');
  });

  it("and open changes border, surface and elevation — not just one of them", () => {
    const at = CSS.indexOf('.dw-run[data-open="true"] {');
    const block = CSS.slice(at, at + 400);
    expect(block).toContain("border-color");
    expect(block).toContain("background");
    expect(block).toContain("box-shadow");
  });

  it("its header is separated from the body it opened", () => {
    expect(CSS).toContain('.dw-run[data-open="true"] > .dw-run-head {');
  });

  it("status still reads down the left edge, independently of open", () => {
    // Two different questions — "which run is this" and "this is the one I opened" — so two
    // different signals.
    expect(CSS).toContain('.dw-runs .dw-run[data-status="waiting"]      { border-left-color:');
    expect(CSS).toContain(".dw-runs .dw-run { border-left-width: 4px; }");
  });

  it("and there is real air between cards", () => {
    expect(CSS).toContain(".dw-runs { display: grid; gap: 16px; }");
  });
});

describe("the step chrome stops being prose", () => {
  it("what a step IS lives on a dot, not a permanent line", () => {
    // "The whole inner pipeline: implement, adversarial review, security check, run the tests,
    // open a gated pull request. Succeeds when a PR exists." — under every step header, always.
    expect(UI).not.toContain('<p className="dw-detail-what">');
    expect(UI).toContain("{capability.summary}");
    const at = UI.indexOf("{capability.summary}");
    expect(UI.slice(at - 300, at)).toContain("<Explain");
  });

  it("and a succeeded step's history does too", () => {
    expect(UI).toContain('label="Why this step recorded no work"');
  });
});

describe("machine fields are evidence, not content", () => {
  it("they are filed behind a disclosure", () => {
    expect(UI).toContain('data-testid="dw-step-out-wrap"');
    expect(UI).toContain("What this step recorded ·");
  });

  it("the same id is not printed under two headings", () => {
    // AWAIT and RUN ID held the identical crun_ on every build step.
    expect(UI).toContain("all.findIndex(([, w]) => w === v) === i");
  });

  it("and an empty field is not a row", () => {
    // BINDING ID rendered as "—".
    expect(UI).toContain('!(v === null || v === undefined || v === "")');
  });
});

describe("one action has one control", () => {
  it("Ask BOSS is a button, not also a link of the same name", () => {
    expect(UI).not.toContain('label: "Ask BOSS",');
    expect(UI).toContain('data-testid="dw-ask-boss"');
  });

  it("and the preview link is not both a button and a link", () => {
    expect(UI).toContain('const visibleLinks = links.filter((l) => l.label !== "open the preview")');
    expect(UI).toContain('data-testid="dw-preview-open"');
  });
});

describe("the rail keeps the timing and drops the noise", () => {
  it("a duration that is not 0s is what the node says", () => {
    expect(UI).toContain("export function railMeta(step: RunStep): string {");
    const at = UI.indexOf("export function railMeta");
    const fn = UI.slice(at, at + 500);
    expect(fn).toContain('if (took && took !== "0s") return took;');
  });

  it("and a terminal state the tick already shows says nothing", () => {
    const at = UI.indexOf("export function railMeta");
    expect(UI.slice(at, at + 500))
      .toContain('["succeeded", "completed"].includes(step.status) ? ""');
  });

  it("but a state that is NOT terminal still shows", () => {
    // "blocked", "waiting", "running" are the ones worth reading on a ten-node rail.
    const at = UI.indexOf("export function railMeta");
    expect(UI.slice(at, at + 500)).toContain(": step.status;");
  });

  it("the node uses it", () => {
    expect(UI).toContain(") : railMeta(step)}");
  });
});

describe("the workflow agent is reachable from the workflow page", () => {
  it("there is a way in, where workflows are managed", () => {
    // The tools to create, edit and delete a workflow in conversation shipped with no entry
    // point — so the feature existed and nobody could find it, which is the same as not having
    // built it.
    expect(UI).toContain('data-testid="dw-workflow-agent"');
    expect(UI).toContain("Design one with BOSS");
  });

  it("and it opens addressed to a role, with an opener that says what it can do", () => {
    const at = UI.indexOf('data-testid="dw-workflow-agent"');
    const block = UI.slice(at, at + 900);
    expect(block).toContain('"program_manager"');
    expect(block).toContain("draft a new one");
    expect(block).toContain("retire one");
  });
});
