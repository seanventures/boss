import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  STOPPED_STEP_STATUSES, bucketOf, laneStateOf, runNeedsYou, runToOpen, skippedBy, stoppedAnswers,
  stoppedStep, type RunAction, type RunStep,
} from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";
import { standingOf } from "./_helpers/run-standing";

/**
 * A run that has stopped at a failed step is waiting for the operator — and the console did
 * not say so (review of the failed-step change, 2026-10-02). It landed under "Stopped" beside
 * runs somebody cancelled, the page did not open on it, and its card showed no reason, because
 * the runtime wrote `error` and the card rendered only `blocked_reason`. The Try again and Skip
 * buttons were two clicks deep. A non-technical operator read "something broke", not "research
 * failed — decide: try again or skip".
 *
 * The rule enforced here, for EVERY state a step can stop a run in: the run is "Needs you", the
 * page opens on it, and its card carries the sentence and both answers.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");
const ENGINE = readFileSync("../../engine/boss_engine/services/workflows.py", "utf8");

type Run = Parameters<typeof runToOpen>[0][number];

function step(status: string, over: Partial<RunStep> = {}): RunStep {
  return { step_id: "research", kind: "capability", status, capability: "research",
           output: {}, decision_id: null, ...over } as unknown as RunStep;
}

// REWRITTEN (2026-10-02): a run carries `standing` from the server, which is what the console
// now files it by; the fixture takes it from the engine's own sets rather than a list here.
function run(status: string, steps: RunStep[], id = "wfr_stopped"): Run {
  return { id, status, standing: standingOf(status), steps, workflow_key: "k",
           workflow_name: "W", input: {},
           created_at: "2026-10-02T00:00:00Z", updated_at: "2026-10-02T00:00:00Z" } as never;
}

describe("a run stopped at a failed step needs you", () => {
  it("names exactly the engine's stopped states", () => {
    // One list on each side of the wire, held equal, so a new stopped state cannot be
    // understood by the runtime and missed by the console.
    const engine = /STOPPED_STEP_STATES = \(([^)]*)\)/.exec(ENGINE);
    expect(engine).not.toBeNull();
    const names = [...engine![1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
    expect([...STOPPED_STEP_STATUSES].sort()).toEqual(names.sort());
  });

  for (const status of STOPPED_STEP_STATUSES) {
    for (const runStatus of ["failed", "deadlettered", "running", "waiting"]) {
      it(`is "Needs you" when a step is ${status} and the run is ${runStatus}`, () => {
        const r = run(runStatus, [step("succeeded", { step_id: "a" }), step(status)]);
        expect(bucketOf(r)).toBe("needs_you");
        expect(runNeedsYou(r)).toBe(true);
        expect(stoppedStep(r)?.step_id).toBe("research");
      });
    }

    it(`opens first, over a newer running run, when a step is ${status}`, () => {
      const runs = [run("running", [step("running")], "wfr_newer"),
                    run("failed", [step(status)], "wfr_stopped")];
      expect(runToOpen(runs)).toBe("wfr_stopped");
    });

    it(`is history, not "Needs you", once the run with a ${status} step is cancelled`, () => {
      expect(bucketOf(run("cancelled", [step(status)]))).toBe("blocked");
      expect(bucketOf(run("completed", [step(status)]))).toBe("done");
    });

    it(`does not count a refused approval (${status}) as a stop to decide`, () => {
      // That one is a person's no, already answered; its card says so in its own words.
      const refused = step(status, { decision_id: "dec_1", step_id: "approve" });
      expect(stoppedStep(run("blocked", [refused]))).toBeNull();
      expect(bucketOf(run("blocked", [refused]))).toBe("blocked");
    });
  }

  it("puts the reason and both answers on the run card", () => {
    const at = UI.indexOf('data-testid="dw-run-stopped"');
    expect(at).toBeGreaterThan(-1);
    const card = UI.slice(at, at + 1200);
    expect(card).toContain("run.blocked_reason");
    // REWRITTEN: the sentence named both answers as a literal, whatever the server offered —
    // "skip it" on an approval, where there is no Skip. It now names exactly the answers the
    // run's actions carry for that step (see the test below).
    expect(card).toContain("stoppedAnswers(stepActions(run, stopped.step_id))");
    expect(card).toContain("<FailedStepActions key={stopped.step_id} step={stopped}");
    expect(card).toContain('idPrefix="dw-run-stopped"');
    expect(card).toContain("onSkipStep(run.id, stopped.step_id, reason)");
    expect(card).toContain("onRetryStep(run.id, stopped.step_id)");
  });
});

describe("the stopped card names only the answers the server offers", () => {
  const on = (key: RunAction["key"]): RunAction =>
    ({ key, label: key, enabled: true, reason: null, step_id: "research" });
  it("both, one, or none — never an answer that is not there", () => {
    expect(stoppedAnswers({ retry: on("retry_step"), skip: on("skip_step") }))
      .toBe(" Try it again, or skip it and carry on.");
    expect(stoppedAnswers({ retry: on("retry_step") })).toBe(" Try it again.");
    expect(stoppedAnswers({ skip: on("skip_step") })).toBe(" Skip it and carry on.");
    expect(stoppedAnswers({})).toBe("");
    // Offered but not right now (a run undoing itself) is not an answer to tell anyone to take.
    expect(stoppedAnswers({ retry: { ...on("retry_step"), enabled: false, reason: "undoing" } }))
      .toBe("");
  });
});

describe("an operator's skip reads as one", () => {
  it("says who skipped it and why, unlike a branch never taken", () => {
    const byOperator = step("skipped", { output: {
      skipped_by_operator: true, skipped_by: "ceo", skip_reason: "out of budget" } });
    expect(skippedBy(byOperator)).toBe("skipped by ceo: out of budget");
    expect(skippedBy(step("skipped", { output: { reason: "path_not_reached" } }))).toBe("");
    // On the run lane the node says it in words (REWRITTEN for the shared lane).
    expect(laneStateOf(byOperator).meta).toBe("skipped by ceo: out of budget");
    expect(laneStateOf(step("skipped", { output: { reason: "path_not_reached" } })).meta).toBe("skipped");
  });

  it("gives every step its own detail, so a half-typed reason does not follow you", () => {
    expect(UI).toContain("key={current.step_id}");
  });
});
