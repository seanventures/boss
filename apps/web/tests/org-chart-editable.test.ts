import { describe, expect, it } from "vitest";

import { laneStateOf, type RunStep } from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";

/**
 * The chart was derived entirely from observed handoffs — who passed work to whom in the last
 * week. Honest, useful, and completely unchangeable: nobody could say "the QA lead reports to
 * the captain" or add a seat that has not been filled yet. A measurement nobody can act on is a
 * report, and this one had no effect anywhere else in the product.
 */
const UI = codeOnly("app/workforce-ui.tsx");
const SVC = codeOnly("../../engine/boss_engine/services/org_assignments.py");
const CSS = codeOnly("app/globals.css");

describe("the chart can be changed", () => {
  it("every seat can be dragged onto another", () => {
    expect(UI).toContain("onDragStart={() => setDragging(n.id)}");
    expect(UI).toContain("view === \"planned\" && dragging && dragging !== n.id");
  });

  it("offers the agents nobody has placed yet", () => {
    // Roles are fetched on demand for placement; they are not chart nodes yet.
    expect(UI).toContain('data-testid="org-bench"');
    expect(UI).toContain("Add role to team");
    expect(UI).toContain("/api/agent?view=roles");
    expect(UI).toContain('aria-label={`Reports to for ${displayName(n)}`}');
  });

  it("can make a seat a root, and hand one back", () => {
    expect(UI).toContain("void place(n.id, null)");
    expect(UI).toContain('data-testid={`org-reset-${n.id}`}');
    expect(UI).toContain('data-testid="org-reset-all"');
  });
});

describe("the declared chart never destroys the observed one", () => {
  it("keeps both", () => {
    // Where they disagree is the most interesting thing on the page: an agent meant to report
    // to the captain who in practice hands everything to the architect.
    expect(SVC).toContain("def apply_to");
    expect(SVC).toContain('"declared":');
    expect(SVC).toContain('"has_declared": False');
    expect(SVC).toContain('"has_declared": True');
    expect(SVC).toContain('"observed_tree": chart.get("tree") or []');
  });

  it("marks which seats were decided rather than measured", () => {
    expect(SVC).toContain('node["placed"] = True');
    expect(CSS).toContain('.org-node[data-placed="true"]');
  });

  it("keeps a seat somebody placed even if BOSS has never seen it work", () => {
    // A seat created and not yet filled: dropping it would make the editor appear to forget
    // what it was told.
    expect(SVC).toContain('"status": "UNSEEN"');
  });
});

describe("a chart that cannot be drawn is refused before it is stored", () => {
  it("catches a loop, however many hops", () => {
    // A cycle is not a display bug: the renderer would recurse forever or silently drop
    // somebody.
    expect(SVC).toContain("class OrgCycle");
    expect(SVC).toContain("def _would_cycle");
    expect(SVC).toContain("already sits above");
  });

  it("catches the one-hop case too", () => {
    expect(SVC).toContain("cannot report to itself");
  });

  it("distinguishes 'is a root' from 'nobody has said'", () => {
    // A NULL parent is a real statement; deleting is how a seat goes back to being observed.
    expect(SVC).toContain("def clear_one");
    expect(SVC).toContain("DELETE FROM org_assignments");
  });
});

describe("you can say something to an agent", () => {
  const WF = codeOnly("app/workforce-ui.tsx");
  const CHAT = codeOnly("app/agent-ui.tsx");
  // Agent World: Ask carries the ROLE KEY to the 1:1 talk (lib/agent-world.ts askAgent), which
  // falls back to this same side dock, addressed to that agent, until the talk is there.
  const ASK = codeOnly("lib/agent-world.ts");
  const ROSTER = codeOnly("app/agents/roster-zones.tsx");

  it("every agent card offers it", () => {
    // The page listed what every agent had done and offered no way to say anything to any of
    // them — read-only, on a screen about a workforce.
    expect(ROSTER).toContain('data-testid={`wf-ask-${a.id}`}');
    expect(WF).toContain("function askAgent");
    expect(WF).toContain('talkTo(agent.id, agent.name, { from: "roster" })');
  });

  it("opens the side dock in place rather than leaving the page", () => {
    // It navigated to the Agents AREA, so asking one question cost the operator whatever they
    // were looking at. The dock exists for exactly this and can carry an identity as easily
    // as a run id.
    // Through the one door helper, which edits the current URL and fires the popstate the
    // shell listens for (lib/dock-subject.ts).
    // Agent World: a registered agent opens its own talk sheet in place (by role key, its
    // voice composed from its persona); an unregistered one, which has no voice yet, keeps the
    // dock, addressed by name.
    expect(ASK).toContain("openTalk(roleKey, context)");
    expect(WF).toContain("openDockAbout({ subject: null, role: agent.name");
  });

  it("addresses the question to that agent, not to BOSS in general", () => {
    // The name used to be prose in a prefilled box, so the answer came from BOSS itself with
    // no lens, no remit and no memory of what that role had argued.
    expect(WF).toContain("`@${agent.name}: `");
    expect(CHAT).toContain('fd.set("role", roleName)');
  });

  it("consumes the instruction rather than leaving it in the URL", () => {
    // A one-shot left in the URL is re-delivered by every later navigation, which is how a
    // composer ends up refilling itself behind your typing.
    expect(CHAT).toContain('url.searchParams.delete("ask")');
  });

  it("keeps the operator where they were", () => {
    // No navigation at all now: the url is edited in place, so org/actor/area survive by
    // construction rather than by being copied across a rebuilt query string.
    expect(WF).toContain("new URL(window.location.href)");
    expect(WF).not.toContain("function askHref");
  });});

describe("the Engineering Captain leads the work", () => {
  const CT = codeOnly("../../engine/boss_engine/services/code_task_service.py");
  const PS = codeOnly("../../engine/boss_engine/services/project_settings.py");

  it("is put on a new project, not only a benchmark run", () => {
    // The captain existed, appeared on the org chart, and did nothing: the swarm that puts it
    // in charge was bound only for official runs, so every ordinary project was built by an
    // implementer with nobody above it.
    expect(CT).toContain("bind_captain_swarm");
    expect(CT).toContain("official=False");
  });

  it("is a knob, because leading a project costs turns", () => {
    expect(PS).toContain('"captain_leads_projects"');
    expect(CT).toContain('"captain_leads_projects"');
  });

  it("never stops a build from happening", () => {
    // An unled build is better than no build.
    expect(CT).toContain("could not put a captain on");
  });
});

describe("the composer looks like the console it is in", () => {
  // The Runs composer moved to its own module (app/workflows/new-project-composer.tsx).
  const DW = codeOnly("app/workflows/new-project-composer.tsx");
  const CSS_N = codeOnly("app/globals.css");
  const DS = codeOnly("app/design-system.css");

  it("uses a class the stylesheet actually defines", () => {
    // `mr-input` was invented and defined nowhere, so every field wearing it rendered as a
    // browser-default white box in a dark console. The design system defines .input and
    // .textarea.
    expect(DW).not.toContain('className="mr-input"');
    expect(DS).toContain(".textarea,");
  });

  it("puts the field and its controls on one surface", () => {
    // Three separate elements sharing a row read as a form; the Command Center's composer is
    // the same act from another page and looks like one thing.
    // REWRITTEN: this pinned `.dw-new-box`, a rule no element wore any more — it went with the
    // other dead workflow CSS. What the composer's field wears is what must be styled.
    expect(DW).toContain('className="dw-new-input"');
    expect(CSS_N).toContain(".dw-new-input");
  });

  it("starts on the shortcut every other composer uses", () => {
    expect(DW).toContain("(e.metaKey || e.ctrlKey) && e.key === \"Enter\"");
  });
});

describe("the preview step can be acted on", () => {
  const DW = codeOnly("app/delivery-workflows-ui.tsx");

  it("offers Run preview and Open preview, like Factory", () => {
    // The step reported what it had recorded and offered no way to do anything: no button to
    // start one, and — once the recorded preview expired — no link either.
    expect(DW).toContain('data-testid="dw-preview-run"');
    expect(DW).toContain('data-testid="dw-preview-open"');
    expect(DW).toContain('"start_preview"');
  });

  it("starts one against the build, not the workflow run", () => {
    // A preview is a code-task operation; the endpoint is the one Factory calls.
    expect(DW).toContain("const buildRunId");
    expect(DW).toContain('awaited?.kind === "code_task"');
  });

  it("never prints a loopback address as if it were openable", () => {
    // 127.0.0.1:42543 is where the app listens ON THE HOST. Printing it under a heading
    // reading PREVIEW URL invites somebody to copy it, paste it, and get nothing.
    // The step's Output (app/workflows/step-run.tsx) drops it with the shared rule.
    expect(codeOnly("app/workflows/step-run.tsx")).toContain("isLoopbackPreviewUrl(text)");
  });
});

describe("a step that failed while the run went on", () => {
  const DW = codeOnly("app/delivery-workflows-ui.tsx");
  const CSS = codeOnly("app/globals.css");

  it("reads as failed, not as 'not run'", () => {
    // Saying "not run" about work that ran and died hides something worth fixing.
    expect(DW).toContain("function failedButContinued");
    expect(DW).toContain("failed — went on");
    expect(DW).toContain('"dw-step-failed-on"');
  });

  it("still lets the path continue past it, and says which it was on the lane", () => {
    // It is not a stopped run: the project carried on without it. On the run's lane it is a
    // failure (red) whose line says it went on, and the line to the next step is not held back.
    const base = { kind: "capability", capability: null, evidence: [], error: null, approval: {},
                   due_at: null, timeout_applied: null, attempt: 1, started_at: null,
                   completed_at: null, decision_id: null };
    const research = { ...base, step_id: "research", status: "failed",
                       output: { recorded_as: "failed_continued" } } as RunStep;
    const build = { ...base, step_id: "build", status: "running", output: {} } as RunStep;
    expect(laneStateOf(research)).toMatchObject({ phase: "failed", meta: "failed — went on" });
    expect(laneStateOf(build).phase).toBe("moving");
    expect(codeOnly("app/workflows/flow/flow-lane.tsx"))
      .toContain('previous === "skipped" || previous === "failed"');
  });
});
