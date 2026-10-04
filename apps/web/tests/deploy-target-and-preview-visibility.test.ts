/** The controls an operator needs must survive the fields the API leaves out.
 *
 *  Three separate reports of "I added the host and the workflow still says there is none" and
 *  "the preview is open in another tab and the step says nothing is serving it" came down to
 *  two absent fields — a response key read under the wrong name, and a `capability` that is
 *  null on any step that has not run yet. Nothing here needed a browser to catch. It needed
 *  somebody to assert the shape.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import {
  verifiedHostsFrom, isDeployStep, isPreviewStep, laneStateOf, type RunStep,
} from "../app/delivery-workflows-ui";

type Step = Parameters<typeof isPreviewStep>[0];

/** A step as the API actually sends it, including the fields it omits. */
function step(over: Partial<Step>): Step {
  return {
    step_id: "deploy", kind: "capability", status: "pending",
    capability: null, output: {}, evidence: [], error: null, decision_id: null,
    approval: {}, attempt: 1, started_at: null, completed_at: null,
    ...over,
  } as Step;
}

describe("hosts come back under `hosts`, not `items`", () => {
  const verified = { id: "dh_1", name: "hetzner-ci-01", status: "verified" };

  it("reads the key the deployment-hosts endpoint actually uses", () => {
    // THE BUG. Verbatim shape of GET /v1/deployment-hosts. Reading `items` here returned []
    // and the deploy step said "this project has no verified host" about a VERIFIED host.
    const body = { hosts: [verified], preconditions: {} };
    expect(verifiedHostsFrom(body).map((h) => h.name)).toEqual(["hetzner-ci-01"]);
  });

  it("still reads `items`, so a unified shape would not break it", () => {
    expect(verifiedHostsFrom({ items: [verified] })).toHaveLength(1);
  });

  it("offers no host that cannot be shipped to, and never throws on junk", () => {
    expect(verifiedHostsFrom({ hosts: [{ id: "d", status: "pending" }] })).toEqual([]);
    expect(verifiedHostsFrom({})).toEqual([]);
    expect(verifiedHostsFrom(null)).toEqual([]);
    expect(verifiedHostsFrom({ hosts: "not-a-list" })).toEqual([]);
  });
});

describe("a step is recognised before it has run", () => {
  // WHY THIS IS THE WHOLE POINT. `capability` is written into a step's OUTPUT by the handler,
  // so a PENDING step carries null — and pending is exactly when an operator still needs to
  // bind a host or start a preview. Gating those controls on capability alone hid them at the
  // only moment they were useful.
  it("finds the deploy step with capability null", () => {
    expect(isDeployStep(step({ step_id: "deploy", capability: null }))).toBe(true);
  });

  it("finds the preview step with capability null", () => {
    expect(isPreviewStep(step({ step_id: "preview", capability: null }))).toBe(true);
  });

  it("still honours capability when the step id is something else", () => {
    expect(isDeployStep(step({ step_id: "ship_it", capability: "deploy" }))).toBe(true);
    expect(isPreviewStep(step({ step_id: "try_it", capability: "preview" }))).toBe(true);
  });

  it("does not claim unrelated steps", () => {
    expect(isDeployStep(step({ step_id: "build", capability: "build" }))).toBe(false);
    expect(isPreviewStep(step({ step_id: "build", capability: "build" }))).toBe(false);
  });
});


describe("the deploy step binds an app to a host, not just picks one", () => {
  // Asked for many times, and what shipped each time was a bare dropdown that fired on
  // change — which chooses the container name and the port on the operator's behalf and
  // gives them nothing to confirm. The project card has always had the real control.
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("offers the host, the app name and the port", () => {
    expect(SOURCE).toContain('data-testid="dw-deploy-host"');
    expect(SOURCE).toContain('data-testid="dw-deploy-appname"');
    expect(SOURCE).toContain('data-testid="dw-deploy-port"');
  });

  it("waits for an explicit Set target rather than acting on change", () => {
    expect(SOURCE).toContain('data-testid="dw-deploy-settarget"');
    expect(SOURCE).toContain("Set target");
  });

  it("posts to the same endpoint the project card uses, so the two cannot drift", () => {
    expect(SOURCE).toContain('"/api/app-deploy"');
    expect(SOURCE).toContain('op: "set-target"');
  });

  it("binds the BUILD, since that is what has an app to place", () => {
    expect(SOURCE).toContain("buildRunId={buildRunId ?? \"\"}");
  });
});

describe("an action always says it is running", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("keeps the busy flag set through the reload, not just the request", () => {
    // `setBusy(null)` ran BEFORE `load()`, so every action ended with the button snapping back
    // to normal while the console spent seconds re-fetching with nothing on screen. From the
    // operator's side that is a click that did nothing.
    expect(SOURCE).toContain("if (result.ok) await load();");
    expect(SOURCE).toMatch(/finally\s*\{[^}]*setBusy\(null\)/);
  });

  it("shows one indicator that every action gets for free", () => {
    expect(SOURCE).toContain('data-testid="dw-working"');
    expect(SOURCE).toContain('role="status"');
  });

  it("refreshes the view once a binding lands", () => {
    // The write succeeded and the step went on showing the same empty form.
    expect(SOURCE).toContain("if (r.ok) onBound();");
  });
});

describe("re-running does not duplicate a live workflow", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("stops the live one first rather than refusing", () => {
    // The rule is unchanged — a press must never leave two workflows on one project — but
    // refusing was the wrong way to keep it: the button was disabled, its explanation said
    // "cancel this run first", and no cancel existed anywhere. Three runs piled up. It now
    // stops and starts, in that order, and does not start at all if the stop failed.
    // Offered on a live run by the engine's rule (workflow_run_actions.py), rendered here.
    expect(SOURCE).toContain("stop_and_rerun: onStopAndRerun");
    expect(SOURCE).toContain('testId: "dw-stop-and-rerun"');
    // REWRITTEN (review of the readiness change): this pinned the browser's stop-then-start.
    // When the start was refused after the stop had landed, the operator had no run at all.
    // The press is one start naming the run it replaces; the server checks, stops and starts
    // in one transaction, so it still never leaves two runs — and never leaves none.
    expect(SOURCE).toContain("replaces_run: run.id");
  });

  it("and stopping is available on its own", () => {
    expect(SOURCE).toContain('testId: "dw-stop"');
  });

  it("points at the control that actually retries", () => {
    expect(SOURCE).toMatch(/Try … again|Try \{capability/);
  });

  it("still allows a re-run once the run has stopped", () => {
    expect(SOURCE).toContain('testId: "dw-rerun"');
  });
});

describe("the agent is reachable from the step", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("opens the console's existing dock rather than a second chat surface", () => {
    expect(SOURCE).toContain("askBossAbout");
    expect(SOURCE).toContain("openDockAbout({ subject, question");
  });

  it("opens ABOUT this step, with the operator's question prefilled — not an empty box", () => {
    // boss-1: the step's words used to be pasted into the question, cut at 400 characters.
    // The step itself travels now, and the server reads its whole record.
    expect(SOURCE).toContain('{ kind: "workflow_step", id: workflowRunId, step: step.step_id }');
    expect(SOURCE).toContain("stepQuestion(step, capability?.label)");
    expect(SOURCE).toContain("onAsk={ask}");
  });
});

describe("a succeeded deploy shows the app, not an old failure", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("does not dress a succeeded step's history as a current problem", () => {
    // A Deploy marked SUCCEEDED, whose app was answering 200, displayed "no preview is
    // running for this app right now … Start a fresh preview, then deploy" in red — an
    // instruction to redo work that was already done.
    expect(SOURCE).toContain('step.error && step.status !== "succeeded"');
    // The "Earlier:" prefix is gone because the note itself is gone from the page on a
    // succeeded step — it is history, so it lives on the explain dot. The RULE this test
    // protects is unchanged and now stronger: a succeeded step shows no note as prose at all,
    // and a step that did NOT succeed still shows its reason where it cannot be missed.
    // On a succeeded step the note is the Output's Note (stepOutput), never a red line here.
    expect(SOURCE).toContain('step.status === "succeeded" ? null : (');
    const at = SOURCE.indexOf('data-testid="dw-step-note"');
    expect(SOURCE.slice(at - 260, at)).toContain('phase === "failed_on" ? "bad"');
  });

  it("links to the app once it has actually shipped", () => {
    expect(SOURCE).toContain('data-testid="dw-open-app"');
    expect(SOURCE).toContain("const liveAppUrl");
  });

  it("links only on a succeeded DEPLOYMENT, and only from the bound target", () => {
    // Linking on anything less repeats the mistake this view keeps making: reporting a state
    // the system is not in. The condition moved from the step's status to the deployment's,
    // because the step is precisely what cannot be trusted — see the suite below.
    expect(SOURCE).toContain('lastDeploy?.status !== "succeeded"');
    expect(SOURCE).toContain("shippedTo?.domains");
  });
});

describe("a step's output shows what it says, not which keys it has", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("renders scalars as key=value", () => {
    // A deploy's result showed as "{health, status, applied}" under a heading reading DETAIL:
    // three facts exist and none of what they say.
    expect(SOURCE).toContain("scalars.join(\" · \")");
  });

  it("keeps the key list only when there is nothing short to say", () => {
    expect(SOURCE).toContain("return keys.length ? `{${keys.slice(0, 3).join(\", \")}}` : \"{}\"");
  });
});

describe("a bound app can request a gated deployment from the deploy step", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("offers a deployment plan once a target is bound", () => {
    // Binding says where a deployment would go; planning creates the reviewable ChangeSet.
    expect(SOURCE).toContain('data-testid="dw-ship-now"');
    expect(SOURCE).toContain("Plan deployment");
  });

  it("uses the console's existing deploy op rather than a new path", () => {
    expect(SOURCE).toContain('op: "deploy"');
  });

  it("shows the ChangeSet review step without claiming execution", () => {
    expect(SOURCE).toContain("plannedDeploymentFrom(d)");
    expect(SOURCE).toContain("Nothing has deployed. ChangeSet");
    expect(SOURCE).toContain("approve it, then apply it");
  });

  it("does not poll a plan as if a deployment had started", () => {
    const shipNow = SOURCE.slice(SOURCE.indexOf("function ShipNow("), SOURCE.indexOf("function stepCaptain("));
    expect(shipNow).not.toContain("/api/run-deployments");
  });
});

describe("a skipped step says why, instead of reading as a fault", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("does not label a deliberate skip 'not run'", () => {
    // Research shows this on most runs, and an operator who has watched it run for four
    // minutes on another run reasonably reads it as broken. It is not: the step returns
    // status: not_applicable with a sentence saying why, and the chain threw that sentence
    // away and printed two words that sound like a defect.
    expect(SOURCE).not.toContain('phase === "skipped" ? "not run"');
    // On the run's lane it is `skipped` (laneStateOf), never a failure.
    const skipped = { step_id: "research", kind: "capability", status: "skipped", capability: "research",
                      output: { status: "not_applicable", note: "No research phase" } } as unknown as RunStep;
    expect(laneStateOf(skipped).phase).toBe("skipped");
    expect(laneStateOf(skipped).meta).toBe("skipped");
  });

  it("carries the recorded reason", () => {
    // The lane node's tooltip, and the opened step, both show the sentence the capability
    // recorded when it stood aside.
    const skipped = { step_id: "research", kind: "capability", status: "skipped", capability: "research",
                      output: { status: "not_applicable", note: "No research phase" } } as unknown as RunStep;
    expect(laneStateOf(skipped).tip).toBe("No research phase");
    expect(SOURCE).toContain('data-testid="dw-step-note"');
    expect(SOURCE).toContain("{String(step.output.note)}");
  });
});

describe("the live link follows the deployment, not the step", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("does not decide 'shipped' from the step's own status", () => {
    // HookWatch's deploy step reads SUCCEEDED because of a blocked run recorded before
    // `blocked` stopped counting as success. Trusting it produced "Open the app →
    // 192.0.2.202:8000" for an address with no container on it.
    expect(SOURCE).toContain('lastDeploy?.status !== "succeeded"');
    expect(SOURCE).not.toContain('if (!isDeployStep(step, capability) || step.status !== "succeeded") return ""');
  });

  it("asks the deployment history for that build", () => {
    expect(SOURCE).toContain("const [lastDeploy, setLastDeploy]");
    expect(SOURCE).toContain("body?.runs?.[buildRunId]?.deployments");
  });
});

describe("the page opens on what needs you", () => {
  const SOURCE = readFileSync(
    new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("collapses Start a project behind one line", () => {
    // It occupied the whole top of the page, pushing what is actually running below the fold.
    expect(SOURCE).toContain('data-testid="dw-composer-open"');
    expect(SOURCE).toContain("composerOpen || runs.length === 0");
  });

  it("still opens the composer when there is nothing else to look at", () => {
    // An empty page with a closed box is a dead end.
    expect(SOURCE).toContain("runs.length === 0");
  });

  it("opens a run automatically, once", () => {
    // Re-deciding on every poll would reopen a run the operator had just closed.
    expect(SOURCE).toContain("openedOnce.current");
    expect(SOURCE).toContain("setOpenRun((current) => current ?? runToOpen(runs))");
  });
});
