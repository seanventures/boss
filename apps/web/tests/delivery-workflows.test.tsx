import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DeliveryWorkflows } from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";

/**
 * Delivery workflows in the console: pick a path, watch it, build your own.
 *
 * The screen must never invent a capability, a risk tier or an approval rule. Everything it
 * draws comes from the engine's registry, so the palette, the validation and the gates are the
 * same ones the runner enforces — a builder that disagreed with the runtime would let a
 * customer save a workflow that cannot run, and find out at 2am.
 */
(globalThis as Record<string, unknown>).React = React;

const UI = codeOnly("app/delivery-workflows-ui.tsx");
const ROUTE = codeOnly("app/api/delivery-workflows/route.ts");
const SHELL = codeOnly("app/ConsoleShell.tsx");
const EDITION = codeOnly("lib/edition.ts");
const CSS = codeOnly("app/globals.css");

describe("the screen never claims an empty project", () => {
  it("says it is loading before it has asked", () => {
    const html = renderToStaticMarkup(
      <DeliveryWorkflows projectId="boss-dogfood" actor="ceo" />);
    expect(html).toContain("Loading delivery workflows");
  });

  it("has three states, not two", () => {
    expect(UI).toContain('useState<"loading" | "ready" | "failed">');
  });

  it("shows the reason it failed", () => {
    expect(UI).toContain("Could not load delivery workflows — {error}");
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
    expect(ROUTE).toContain("const detail = (parsed as { detail?: string })?.detail;");
    expect(UI).toContain("result.error");
  });
});

describe("the risk floor is visible before a step is placed", () => {
  it("marks forced-manual capabilities in the palette", () => {
    // Finding out at save time that a step can never be automatic is finding out too late.
    expect(UI).toContain("c.approval_forced_manual");
    expect(UI).toContain("always approved");
  });

  it("locks the approval control on a step that cannot be automatic", () => {
    // A control block has no approval of its own either — only capability steps do.
    expect(UI).toContain('disabled={step.kind !== "capability"');
    expect(UI).toContain("|| capability?.approval_forced_manual}");
  });

  it("states the rule in words", () => {
    expect(UI).toContain("always requires a person. That is not");
  });
});

describe("the builder cannot produce something the runtime will refuse", () => {
  it("gives every dropped step a unique id", () => {
    // Adding "build" twice with one id is rejected by the runtime — at save time, with a
    // message about duplicates, after the customer has laid out a whole workflow.
    // One id generator for both capabilities and control blocks, and it checks what is
    // already taken rather than counting — counting collides after a delete.
    expect(UI).toContain("const nextId = (current: Draft[], stem: string)");
    expect(UI).toContain("while (taken.has(`${stem}_${n}`)) n += 1;");
  });

  it("wires each step to the next one", () => {
    expect(UI).toContain("const next = index + 1 < draft.length ? [draft[index + 1].id] : [];");
  });

  it("reads required inputs from the run's input instead of inventing values", () => {
    // A workflow that hard-codes the thing being built only works once.
    expect(UI).toContain("{ from_input: name }");
  });

  it("only offers a customer's own workflows for editing", () => {
    // Offering a catalogue workflow and refusing on save is a control that lies about what it
    // does.
    expect(UI).toContain("installed.filter((i) => !i.is_catalogue)");
  });
});

describe("reordering is reachable without a pointer", () => {
  it("supports drag", () => {
    expect(UI).toContain("onDragStart");
    expect(UI).toContain("onDrop");
  });

  it("and buttons, because a drag-only builder excludes half its operators", () => {
    expect(UI).toContain("Move ${step.id} earlier");
    expect(UI).toContain("Move ${step.id} later");
  });
});

describe("a run says what it is waiting for", () => {
  it("names the approver, not just 'waiting'", () => {
    // "Waiting" alone sends an operator to ask three people who it is waiting on.
    expect(UI).toContain("waiting on {run.waiting_on}");
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

  it("stops polling when nothing is in flight", () => {
    // An idle console should be idle.
    expect(UI).toContain("if (!anyLive) return;");
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
    expect(SHELL).toContain('id: "delivery_workflows"');
    expect(SHELL).toContain('label: "Workflows"');
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

describe("an action's outcome reaches the person who caused it", () => {
  it("counts only standard workflows against the catalogue total", () => {
    // Counting the customer's own forks too read "14 of 12 installed in this project" — a
    // number that cannot be true, which makes a reader doubt everything else on the card.
    expect(UI).toContain("installed.filter((i) => i.is_catalogue)");
  });

  it("gives every fork a key that is not already taken", () => {
    // `${key}_custom` collided the second time anyone forked the same workflow, and a 409
    // whose notice is scrolled off the top of a long page is indistinguishable from a button
    // that does nothing.
    expect(UI).toContain("for (let n = 2; taken.has(candidate); n += 1)");
  });

  it("lands the operator in the editor, because the button says 'fork to edit'", () => {
    // On success the only visible change was one line of text at the top of a page the
    // operator had scrolled away from; the new workflow appeared in a dropdown on a tab they
    // were not looking at.
    expect(UI).toContain('setTab("builder")');
    expect(UI).toContain("setBuilderTarget(created.id)");
  });

  it("keeps the notice on screen while the page scrolls", () => {
    const rule = CSS.slice(CSS.indexOf(".dw-notice {"), CSS.indexOf(".dw-empty"));
    expect(rule).toContain("position: sticky");
  });
});

describe("the builder opens with what is already there", () => {
  it("loads the selected workflow's current steps", () => {
    // It opened empty, so changing one step of a ten-step workflow meant rebuilding the other
    // nine from memory — and saving would have replaced the workflow with the partial thing.
    expect(UI).toContain('view: "spec"');
    expect(UI).toContain("setDraft(toDraft(body.spec.steps))");
  });

  it("distinguishes 'still reading' from 'genuinely empty'", () => {
    expect(UI).toContain('data-testid="dw-canvas-loading"');
    expect(UI).toContain("This workflow has no steps yet");
  });

  it("keeps configuration it does not understand", () => {
    // wait_seconds, workflow_version_id. Dropping them on load would silently reset one step
    // while the customer edited another.
    expect(UI).toContain('!["id", "kind", "binding", "approval", "next"].includes(k)');
  });
});

describe("resetting to the standard", () => {
  it("is offered only when there is something to reset to", () => {
    // A fork records what it came from; a workflow written from scratch has no original, and
    // the control would be a lie on it.
    expect(UI).toContain("loaded?.forked_from ? (");
    expect(UI).toContain('data-testid="dw-reset"');
  });

  it("restores the original's steps rather than clearing the canvas", () => {
    expect(UI).toContain("setDraft(toDraft(loaded.forked_from.spec.steps))");
  });
});

describe("the flow blocks the runtime always had", () => {
  it("offers the ones that work in a linear canvas", () => {
    expect(UI).toContain('data-testid={`dw-block-${b.kind}`}');
    expect(UI).toContain("blocks.filter((b) => !b.canvas_required)");
  });

  it("does not offer ones that would produce a spec the runtime rejects", () => {
    // A palette entry that cannot be saved is a promise the product cannot keep.
    expect(UI).toContain("blocks.filter((b) => b.canvas_required)");
    expect(UI).toContain("once the canvas can draw");
  });

  it("emits a control block in the shape the runtime reads", () => {
    // wait_seconds and workflow_version_id live at the top level of the step, not in binding.
    // Asserted WITHOUT the empty-object literals: `codeOnly` collapses `{}` to a space when
    // it strips leftover JSX-comment braces, so a string containing one never matches.
    expect(UI).toContain("return { id: step.id, kind: step.kind, binding:");
    expect(UI).toContain("...(step.config ?? ");
  });
});

describe("the canvas reads as a sequence", () => {
  it("joins each step to the next", () => {
    // Evenly-spaced identical boxes read as an unordered list, which is the wrong mental
    // model for a thing whose order is its meaning.
    const rule = CSS.slice(CSS.indexOf(".dw-draft-step:not(:last-child)::after"));
    expect(rule.slice(0, 200)).toContain("height: 14px");
    expect(CSS).toContain(".dw-draft-step:not(:last-child)::before");
  });

  it("shows where a dragged step will land", () => {
    expect(UI).toContain('data-drop={dragging !== null && dragging !== index');
    expect(CSS).toContain('.dw-draft-step[data-drop="true"]');
  });
});
