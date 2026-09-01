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
    // Both kinds of step: a built-in capability above the floor, and a custom step whose
    // resolved tier is above it. Either one must refuse to be switched back to auto.
    expect(UI).toContain("|| capability?.approval_forced_manual");
    expect(UI).toContain("|| mine?.approval_forced_manual}");
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
    expect(SHELL).toContain('id: "delivery_workflows"');
    // Labelled "Workflow" since the nav reorder; the AREA id is what must not drift.
    expect(SHELL).toContain('id: "delivery_workflows"');
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
    // Reworded when "New workflow" arrived: an empty canvas now belongs to something the
    // customer just created, so it says what to do rather than describing a state.
    expect(UI).toContain("No steps yet — add them from the left");
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

describe("the catalogue card is scannable across twelve of them", () => {
  it("draws the steps as a connected sequence, not a wrapped set of chips", () => {
    // Order is the whole meaning of a workflow. A wrapped list of chips says the steps are an
    // unordered set, which is the one thing they are not.
    expect(UI).toContain('className="dw-rail"');
    expect(UI).toContain('className="dw-rail-link"');
    expect(CSS).toContain(".dw-rail-step[data-gate] .dw-rail-dot");
  });

  it("gives every card the same shape so a grid of them lines up", () => {
    // Ragged action rows are what made the grid read as unfinished.
    const rule = CSS.slice(CSS.indexOf(".dw-card {"), CSS.indexOf(".dw-card::before"));
    expect(rule).toContain("grid-template-rows: auto auto 1fr auto auto auto");
  });

  it("labels the numbers a customer chooses on", () => {
    // "10 · ~112 min · 2" is unreadable.
    expect(UI).toContain("<dt>Approvals</dt>");
    expect(CSS).toContain(".dw-stats div[data-emphasis] dd");
  });

  it("colours the class chip to match the card's stripe", () => {
    // Two readings of the same fact, so one card is never visibly unlabelled beside another.
    for (const cls of ["project.new", "bugfix", "feature.minor", "feature.major"]) {
      expect(CSS).toContain(`.dw-class[data-class="${cls}"]`);
    }
  });

  it("says when a workflow needs nothing set up first", () => {
    // A reason to pick that one, and blank space says nothing.
    expect(UI).toContain("Nothing to set up first");
  });

  it("has one primary action, not three equal ones", () => {
    const actions = UI.slice(UI.indexOf('className="dw-card-actions"'));
    expect(actions.slice(0, 700)).toContain('className="cap-btn primary"');
  });

  it("turns motion off when the reader asked for that", () => {
    const reduced = CSS.slice(CSS.indexOf(".dw-card, .dw-tab { transition: none; }"));
    expect(reduced.slice(0, 160)).toContain(".dw-card:hover { transform: none; }");
  });
});

describe("build your own actually lets you build one", () => {
  it("offers a way to author one, not only to fork", () => {
    // The tab was called "Build your own" and the only way in was to fork something, so a
    // screen that promised authorship offered only editing. It is now the second TAB.
    expect(UI).toContain('data-testid="dw-tab-new"');
    expect(UI).toContain("Create a new one");
  });

  it("makes authoring a mode you are IN, not a button among buttons", () => {
    // Flat and evenly weighted, the row gave "+ New workflow" the same presence as "Reset to
    // the standard" — so the one control that STARTS something read as the least important
    // thing on the line. Tinting it helped; being a tab settles it, because a tab also says
    // which mode you are currently in, which a button never did.
    expect(UI).toContain('role="tablist"');
    expect(UI).toContain("aria-selected={creating}");
    expect(UI).toContain("aria-selected={!creating}");
    expect(CSS).toContain(".dw-mode-tab");
    expect(CSS).toContain('.dw-mode-tab[aria-selected="true"]');
  });

  it("keeps a single solid button, so two do not compete", () => {
    // Accent-tinted, not solid: prominent without turning the header into two primary buttons
    // arguing about which one the operator meant. Asserted on the SHAPE — a mix of accent into
    // transparency — rather than on the exact percentage, which is a taste that will move.
    const cta = CSS.slice(CSS.indexOf(".dw-new-cta {"));
    expect(cta.slice(0, cta.indexOf("}"))).toMatch(
      /background: color-mix\(in srgb, var\(--accent\) \d+%, transparent\)/);
    expect(cta.slice(0, cta.indexOf("}"))).not.toContain("background: var(--accent)");
  });

  it("separates 'edit that one' from 'start a new one'", () => {
    // They are two jobs. Sharing one header, "Editing: X" sat beside "Start from: Y" and the
    // operator had to work out which of the two workflows the controls acted on — which is how
    // a project ends up with 54 definitions named "(copy)" and "(copy 2)".
    expect(UI).toContain('data-testid="dw-tab-edit"');
    expect(UI).toContain('data-testid="dw-tab-new"');
    expect(UI).toContain('<div className="dw-head-left">');
  });

  it("switching tabs uses the mode changes that already existed", () => {
    // beginCreating stashes the canvas, cancelCreating restores it. The tabs must drive THOSE,
    // not a second, parallel notion of which mode the screen is in.
    const tabs = UI.slice(UI.indexOf('role="tablist"'), UI.indexOf('dw-canvas-head'));
    expect(tabs).toContain("cancelCreating()");
    expect(tabs).toContain("beginCreating()");
  });

  it("does not act when you press the tab you are already on", () => {
    // Re-entering create would blank a canvas the operator is part way through.
    const tabs = UI.slice(UI.indexOf('role="tablist"'), UI.indexOf('dw-canvas-head'));
    expect(tabs).toContain("if (creating) cancelCreating()");
    expect(tabs).toContain("if (!creating) beginCreating()");
  });

  it("can start from an existing workflow, including a standard", () => {
    // "Work on top of it" is the common case, so the list is every workflow in the project
    // rather than only the customer's own.
    expect(UI).toContain('data-testid="dw-new-from"');
    expect(UI).toContain('Nothing — an empty workflow');
    expect(UI).toContain('{i.is_catalogue ? " (standard)" : ""}');
  });

  it("still records where imported steps came from", () => {
    // Import no longer forks on the spot, but the lineage it used to create is what makes
    // "Reset to the standard" mean anything — so Create forks when there was a source.
    expect(UI).toContain('act(`Create ${name}`, "fork", { workflow_key: fromKey, new_name: name })');
    expect(UI).toContain('act(`Create ${name}`, "create", { name })');
  });

  it("writes the edited steps, not only the ones it copied", () => {
    // Fork stores the SOURCE's steps. Whatever was changed on the canvas before Create has to
    // be written straight after, or the operator's edits are silently dropped.
    expect(UI).toContain('act(`Save ${name}`, "save", { definition_id: created.id, spec })');
  });

  it("selects what was just created", () => {
    // Creating something and leaving the operator to find it in a dropdown is the failure
    // "fork to edit" already had.
    const create = UI.slice(UI.indexOf("onCreate={async (name, spec, fromKey)"));
    expect(create.slice(0, 1600)).toContain("setBuilderTarget(created.id)");
  });

  it("does not make a customer invent an identifier", () => {
    // The key is derived server-side, in ONE place, for create and import both — two callers
    // deriving keys their own way is how one 409s on a collision the other avoided.
    const api = codeOnly("../../apps/api/app/main.py");
    expect(api).toContain("def _free_workflow_key");
    expect(api).toContain("resolved_key = new_key or _free_workflow_key(");
  });

  it("opens a workflow that has no version yet", () => {
    // A workflow created from scratch has no published version until its first save, and an
    // inner join turned that into a 404 — the builder would refuse to open the very thing the
    // New workflow button had just made.
    const api = codeOnly("../../apps/api/app/main.py");
    expect(api).toContain("LEFT JOIN workflow_versions v ON v.workflow_definition_id = d.id");
  });
});

describe("a customer can define their own step", () => {
  it("offers the form from the builder palette", () => {
    expect(UI).toContain('data-testid="dw-define-step"');
    expect(UI).toContain('data-testid="dw-define-form"');
  });

  it("NEVER offers a risk field", () => {
    // The whole design rests on this. A customer who could type their own tier would have a
    // documented way past every approval gate in the product, so the form must not have the
    // control at all — not a disabled one, not a hidden one.
    const form = UI.slice(UI.indexOf("function DefineStepForm"), UI.indexOf("function BuilderView"));
    expect(form).not.toContain("setRiskTier");
    expect(form).not.toContain('name="risk_tier"');
    expect(form).not.toContain("risk_tier:");
  });

  it("says what the binding costs before the step is created", () => {
    expect(UI).toContain('data-testid="dw-define-risk"');
    expect(UI).toContain("This step will always stop for a person.");
  });

  it("names an unclassified skill as unclassified rather than as tier 0", () => {
    expect(UI).toContain("nobody has classified this skill");
  });

  it("warns that an untrusted binding will not publish", () => {
    expect(UI).toContain('data-testid="dw-define-blocked"');
  });

  it("says so when there is nothing to bind to, counting CHOICES not servers", () => {
    // A project with three registered MCP servers that are all unreachable has three empty
    // groups in the dropdown and nothing to pick. Counting servers would call that "fine".
    expect(UI).toContain("Nothing to bind a step to yet.");
    expect(UI).toContain("bindable.mcp_servers.reduce((n, server) => n + server.tools.length, 0)");
    expect(UI).toContain("|| choices === 0}");
  });

  it("names the unreachable servers rather than leaving an empty dropdown unexplained", () => {
    expect(UI).toContain("registered but not answering");
  });
});

describe("a customer's own steps are first-class in the palette", () => {
  it("lists them beside the built-in ones", () => {
    expect(UI).toContain('dw-palette-custom-${c.step_key}');
    expect(UI).toContain("Your steps");
  });

  it("shows what a step RUNS, not only what it was called", () => {
    // A step called "Cleanup" bound to a tier-3 tool must not read as harmless.
    expect(UI).toContain("runs ${c.risk_source}");
  });

  it("cannot place a step whose binding is gone", () => {
    expect(UI).toContain("disabled={!!busy || !c.usable}");
    expect(UI).toContain("c.unusable_reason");
  });

  it("arrives on the canvas already manual when the tier forces it", () => {
    expect(UI).toContain("approval: step.approval_forced_manual ?");
  });

  it("will not let the approval be switched back to auto", () => {
    expect(UI).toContain("|| mine?.approval_forced_manual");
  });

  it("emits the binding the runtime reads", () => {
    expect(UI).toContain("custom_step: step.customStep");
  });

  it("reads one back off a saved spec, so editing does not drop it", () => {
    expect(UI).toContain("customStep: binding.custom_step");
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

describe("the builder header asks one question at a time", () => {
  it("hides 'Editing' while a new workflow is being named", () => {
    // "Editing: Standard Product Delivery" beside "Start from: Backend Service Setup" asks the
    // operator to hold two workflows in mind, and says nothing about which the buttons act on.
    expect(UI).toContain("{!creating ? (");
    expect(UI).toContain('data-mode={creating ? "creating" : "editing"}');
  });

  it("hides the controls that would act on the OTHER workflow", () => {
    // Reset and Save apply to the one in the dropdown that is no longer on screen. Leaving
    // them visible is not merely noisy — clicking either does something to a workflow the
    // operator is not looking at.
    expect(UI).toContain('<span className="dw-canvas-actions" hidden={creating}>');
    expect(UI).toContain("{!creating && loaded?.forked_from ? (");
  });

  it("labels the name field, because it took the labelled slot", () => {
    // An unlabelled box beside a labelled dropdown reads as part of the dropdown.
    expect(UI).toContain('<span><WfIcon id="edit" /> Name</span>');
    expect(UI).toContain('<span><WfIcon id="importIn" /> Start from</span>');
  });

  it("makes creating LOOK like a different mode", () => {
    // The same flat row with different contents left an operator unsure whether they were
    // editing the workflow in the dropdown or making a new one.
    expect(CSS).toContain('.dw-canvas-head[data-mode="creating"]');
  });
});

describe("importing does not need a name typed first", () => {
  it("falls back to a name derived from what it copies", () => {
    // Import was disabled until a name was typed, and a disabled button that does not say it
    // is disabled reads as broken — which is how it was reported. Copying something has an
    // obvious name available.
    expect(UI).toContain("const name = newName.trim() || defaultImportName;");
    expect(UI).toContain("uniqueWorkflowName(source.name, installed.map((i) => i.name))");
  });

  it("puts no bookkeeping in the name", () => {
    // Forking appended "(yours)", copying appended "(copy)", and doing both produced
    // "Standard Product Delivery (yours) (copy)" — three words of bookkeeping in the one
    // string an operator reads, none of which said anything the row did not already show.
    // The rule itself, including compounding and collisions, is tested in workflow-names.
    expect(UI).not.toContain("(yours)");
    expect(UI).not.toContain("(copy)`");
    expect(UI).toContain("uniqueWorkflowName");
  });

  it("uses the same rule when forking as when importing", () => {
    // Two mint sites with two rules is how the two suffixes came to be combinable.
    const forkAt = UI.indexOf("new_name:");
    expect(UI.slice(forkAt, forkAt + 120)).toContain("uniqueWorkflowName");
  });
});

describe("creating something says so, where the person is looking", () => {
  it("confirms on the canvas that changed", () => {
    // Import created the workflow, copied its steps and selected it — then showed a toolbar
    // identical to the one before the click, so it read as "went back to editing". That is how
    // a working feature got reported as broken.
    expect(UI).toContain('data-testid="dw-made"');
    expect(UI).toContain("You are editing it now");
  });

  it("names what it copied from, and how much came across", () => {
    expect(UI).toContain("step${draft.length === 1 ? \"\" : \"s\"} copied");
  });

  it("only shows it for the workflow actually on screen", () => {
    // Switching to another workflow must not leave a confirmation about a different one.
    expect(UI).toContain("justMade && justMade.id === target ?");
  });

  it("can be dismissed", () => {
    expect(UI).toContain("onClick={onDismissJustMade}");
  });

  it("shows that name before the button is pressed", () => {
    // As the placeholder, so the operator can see what the workflow will be called.
    expect(UI).toContain('placeholder={defaultImportName || "Name it"}');
  });

  it("says why, in the one case it IS still disabled", () => {
    expect(UI).toContain('data-testid="dw-new-hint"');
    expect(UI).toContain("Name it, or pick something to start from.");
  });
});

describe("the header carries a fact, not just controls", () => {
  it("says how big the workflow is and where it came from", () => {
    // The dead gap in the middle of the row is now the one place on the screen that says
    // whether "Reset to the standard" beside it means anything.
    expect(UI).toContain('data-testid="dw-head-meta"');
    expect(UI).toContain("written from scratch");
  });

  it("keeps the two actions together instead of flinging them apart", () => {
    expect(CSS).toContain(".dw-canvas-actions { display: inline-flex; gap: 6px; align-items: center; margin-left: auto; }");
  });
});


describe("Import fills the canvas; only Create writes anything", () => {
  it("does not create a workflow when Import is pressed", () => {
    // Import used to fork server-side on the spot, so a workflow appeared in the project
    // before the operator had decided anything and the screen jumped into editing it.
    expect(UI).toContain('data-testid="dw-import-steps"');
    expect(UI).toContain("const importSteps = useCallback(async () => {");
    // It reads the spec. It does not post.
    const fn = UI.slice(UI.indexOf("const importSteps = useCallback"));
    const body = fn.slice(0, fn.indexOf("}, [importFrom"));
    expect(body).toContain('view: "spec"');
    expect(body).not.toContain("post(");
  });

  it("stays in the new-workflow panel afterwards", () => {
    // The whole point: the operator is not moved somewhere else.
    const fn = UI.slice(UI.indexOf("const importSteps = useCallback"));
    expect(fn.slice(0, fn.indexOf("}, [importFrom"))).not.toContain("setCreating(false)");
  });

  it("says what landed on the canvas, in the panel", () => {
    expect(UI).toContain('data-testid="dw-imported"');
    expect(UI).toContain("nothing is saved until you do");
  });

  it("can be pressed again with a different source", () => {
    // Safe precisely because nothing was written the first time.
    expect(UI).toContain('importedFrom ? "Import again" : "Import steps"');
  });

  it("starts the new workflow on an EMPTY canvas", () => {
    // Otherwise the steps of the workflow being edited look like the new one's.
    expect(UI).toContain("const beginCreating = useCallback(() => {");
    expect(UI).toContain("setStashed(draft);");
  });

  it("does not load the selected workflow's steps over the new ones", () => {
    expect(UI).toContain("if (creating) return;");
  });

  it("puts the previous canvas back on Cancel", () => {
    // Abandoning the idea must leave the operator exactly where they were.
    expect(UI).toContain("if (stashed) setDraft(stashed);");
  });
});


describe("the builder has icons, and they mean something", () => {
  it("draws them inline rather than pulling in an icon package", () => {
    // A whole library for eleven glyphs is a lot of bytes for eleven glyphs, and the nav
    // already settled this question.
    expect(UI).toContain("const WF_ICONS: Record<string, React.ReactNode>");
    expect(UI).toContain('stroke="currentColor"');
  });

  it("uses currentColor so a glyph dims with its label", () => {
    // Otherwise every disabled and hover state needs a second rule for the icon.
    expect(CSS).toContain(".dw-icon");
    expect(UI).not.toContain('stroke="#');
  });

  it("renders nothing for an unknown id", () => {
    // An empty square beside a label reads as a broken image; the label alone reads as a
    // control without an icon, which is what it is.
    expect(UI).toContain("if (!d) return null;");
  });

  it("keeps the words", () => {
    // An icon-only control in a builder is a guessing game.
    expect(UI).toContain('<WfIcon id="check" /> Create');
    expect(UI).toContain('<WfIcon id="x" /> Cancel');
    expect(UI).toContain('<WfIcon id="reset" /> Reset to the standard');
  });

  it("swaps the import glyph once something has been imported", () => {
    // "Import again" is a repeat, not a first import, and the arrow says so.
    expect(UI).toContain('<WfIcon id={importedFrom ? "again" : "importIn"} />');
  });

  it("gives the empty canvas a shape rather than a lone sentence", () => {
    // A centred line on a large blank panel reads as a page that failed to load.
    expect(UI).toContain('data-testid="dw-canvas-empty"');
    expect(CSS).toContain(".dw-empty-mark");
  });
});

describe("the panel's buttons wrap as a group", () => {
  it("keeps Import, Create and Cancel together", () => {
    // Loose, they broke apart at the console's width and Cancel dropped onto a line of its
    // own, which reads as a layout fault rather than a wrap.
    expect(UI).toContain('<span className="dw-new-actions">');
    expect(CSS).toContain(".dw-new-actions { display: inline-flex;");
  });

  it("does not let a label break mid-button", () => {
    expect(CSS).toContain(".dw-canvas-head .cap-btn, .dw-new .cap-btn { white-space: nowrap; }");
  });
});
