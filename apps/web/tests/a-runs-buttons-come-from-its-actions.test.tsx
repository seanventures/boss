import React from "react";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import {
  FailedStepActions, RUN_ACTION_KEYS, RunActionsBar, RunsView, STEP_ACTION_KEYS, stepActions,
  type RunAction, type RunStep,
} from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";
import { schemaRunStatuses, standingOf } from "./_helpers/run-standing";
import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";

/**
 * A run's buttons are the server's list, rendered — never decided here.
 *
 * The incident: a cancelled Standard Product Delivery run whose Research step had FAILED still
 * offered "Build on this research →". The product owner: "Does it make sense to have Build on
 * this research? these small bugs and experience give quality." The button existed because this
 * file decided it with its own condition ("a succeeded step with a discovery id"), and an old
 * research step that had failed was recorded as exactly that. Stop, Run again, Remove, Re-check,
 * Try again and Skip were each decided the same way, from `run.status` checks that disagreed
 * with what the engine would actually do.
 *
 * The engine now sends `actions` with every run (workflow_run_actions.py) and this file renders
 * it. These tests hold the console to that: no status checks where buttons are chosen, every
 * engine key has its words here, and a rendered run shows exactly what it was sent.
 *
 * THE FIRST VERSION OF THIS FILE COULD NOT SEE THE INCIDENT COME BACK. It looked inside the three
 * functions that render actions and nowhere else; a reviewer put
 * `{run.status === "cancelled" && ... ? <button onClick={onBuildOnThis}>Build on this research →</button> : null}`
 * into StepPath — exactly where the original button lived — and every test stayed green. So the
 * checks below ENUMERATE: every reference anywhere in the file to a handler that performs a run
 * action must sit in one of the places that renders the server's list, and the whole run view is
 * rendered with an empty list to prove nothing appears that the server did not send.
 */
(globalThis as Record<string, unknown>).React = React;
// The lane is not what this file is about; it gets a stand-in that draws one node per step.
vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));

const UI = codeOnly("app/delivery-workflows-ui.tsx");
const ENGINE = readFileSync("../../engine/boss_engine/services/workflow_run_actions.py", "utf8");

/** The body of a top-level function or const in the UI source, up to the next top-level one. */
function body(start: string): string {
  const at = UI.indexOf(start);
  expect(at, `${start} is missing`).toBeGreaterThan(-1);
  const next = UI.slice(at + start.length).search(/\n(export )?(function|const|type) /);
  return UI.slice(at, next === -1 ? undefined : at + start.length + next);
}

function pythonTuple(name: string): string[] {
  const match = ENGINE.match(new RegExp(`${name} = \\(([^)]*)\\)`));
  expect(match, `${name} not found in workflow_run_actions.py`).toBeTruthy();
  return [...match![1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
}

/** Every prop that performs a run action when called. */
const RUN_ACTION_HANDLERS = [
  "onStop", "onStopAndRerun", "onRerun", "onDelete", "onBuildOnThis", "onRecheck",
  "onRetryStep", "onSkipStep", "onRetry", "onSkip",
];
/** The components that only hand those props on (or, FailedStepActions, render the list). */
const PASS_THROUGH = ["RunsView", "StepPath", "StepDetail", "FailedStepActions"];

function lineOf(index: number): number {
  return UI.slice(0, index).split("\n").length;
}

/** The index of the bracket closing the one at `open`. */
function closing(open: number): number {
  const pair: Record<string, string> = { "{": "}", "(": ")" };
  const [o, c] = [UI[open], pair[UI[open]]];
  let depth = 0;
  for (let i = open; i < UI.length; i++) {
    if (UI[i] === o) depth++;
    else if (UI[i] === c && --depth === 0) return i;
  }
  throw new Error(`unbalanced ${o} at line ${lineOf(open)}`);
}

/** [start, end) of every handler-named attribute on every <Tag …> element. */
function attributeSpans(tag: string, names: string[] = RUN_ACTION_HANDLERS): Array<[number, number]> {
  const spans: Array<[number, number]> = [];
  for (const el of UI.matchAll(new RegExp(`<${tag}\\b`, "g"))) {
    let depth = 0;
    for (let i = el.index! + tag.length + 1; i < UI.length; i++) {
      const ch = UI[i];
      if (ch === "{") depth++;
      else if (ch === "}") depth--;
      else if (ch === ">" && depth === 0) break;
      if (depth === 0) {
        const attr = UI.slice(i).match(/^\s([A-Za-z]+)=\{/);
        if (attr && names.includes(attr[1])) {
          const open = i + attr[0].length - 1;
          spans.push([i, closing(open) + 1]);
        }
      }
    }
  }
  return spans;
}

/** Where a run-action handler may be referenced. See the test that reads it. */
const ALLOWED: Array<[number, number]> = (() => {
  const spans: Array<[number, number]> = [];
  for (const fn of PASS_THROUGH) {
    const at = UI.indexOf(`function ${fn}(`);
    expect(at, `${fn} is missing`).toBeGreaterThan(-1);
    const sigEnd = closing(at + `function ${fn}`.length);
    spans.push([at, sigEnd + 1]);
    if (fn === "FailedStepActions") spans.push([sigEnd, closing(UI.indexOf("{", sigEnd)) + 1]);
    spans.push(...attributeSpans(fn));
  }
  for (const bar of UI.matchAll(/<RunActionsBar\b[^>]*?handlers=\{/g)) {
    const open = bar.index! + bar[0].length - 1;
    spans.push([open, closing(open) + 1]);
  }
  return spans;
})();

const noop = () => {};
const handlers = {
  recheck: noop, stop: noop, stop_and_rerun: noop, rerun: noop, remove: noop,
  build_on_research: noop,
};

function run(actions: RunAction[]) {
  return {
    id: "wfr_1", status: "cancelled", workflow_key: "standard_product_delivery",
    workflow_name: "Standard Product Delivery", version: 1, steps: [], step_count: 0,
    steps_done: 0, current_step: null, waiting_on: null, blocked_reason: null, error: null,
    updated_at: null, actions,
  };
}

const ok = (key: RunAction["key"], label: string, step_id?: string): RunAction =>
  ({ key, label, enabled: true, reason: null, ...(step_id ? { step_id } : {}) });

describe("which buttons a run has is the server's decision", () => {
  it("the console knows exactly the action keys the engine can send", () => {
    expect([...RUN_ACTION_KEYS]).toEqual(pythonTuple("RUN_ACTION_KEYS"));
    expect([...STEP_ACTION_KEYS]).toEqual(pythonTuple("STEP_ACTION_KEYS"));
  });

  it("and has the words for every run-level one — a new action cannot arrive unexplained", () => {
    const view = body("const RUN_ACTION_VIEW");
    for (const key of RUN_ACTION_KEYS) {
      expect(view, `${key} has no view`).toMatch(new RegExp(`\\n  ${key}: \\{`));
    }
    expect((view.match(/\n    explain:/g) ?? []).length).toBe(RUN_ACTION_KEYS.length);
  });

  it("the places that render run actions read no run or step status", () => {
    // Every place a workflow-management button is chosen. A `run.status` or `step.status`
    // check here is a private copy of an engine rule — the shape the incident came from.
    for (const fn of ["export function RunActionsBar(", "export function FailedStepActions(",
                      "const RUN_ACTION_VIEW"]) {
      const code = body(fn);
      expect(code, fn).not.toMatch(/\brun\.status\b/);
      expect(code, fn).not.toMatch(/\bstep\.status\b/);
      expect(code, fn).not.toContain("ACTIVE_RUN_STATES");
      expect(code, fn).not.toContain("STOPPED_STEP_STATUSES");
    }
    const path = body("function StepPath(");
    expect(path).toContain("<RunActionsBar run={run}");
  });

  it("every handler that performs a run action is reached only through the server's list", () => {
    // EVERY reference to every one of these, anywhere in the file, must be inside one of:
    //   - a component's own signature (declaring the prop),
    //   - a handler-named attribute on a component that only passes it down,
    //   - the `handlers={{…}}` map handed to <RunActionsBar> (which renders the run's list),
    //   - the body of FailedStepActions (which renders the step's list).
    // A <button> wired to one anywhere else — the incident's shape — is a reference outside
    // all of them, and fails here naming its line.
    const stray: string[] = [];
    for (const name of RUN_ACTION_HANDLERS) {
      for (const m of UI.matchAll(new RegExp(`\\b${name}\\b`, "g"))) {
        if (!ALLOWED.some(([a, b]) => m.index! >= a && m.index! < b)) {
          stray.push(`${name} at line ${lineOf(m.index!)}: ${UI.slice(m.index! - 60, m.index! + 40)
            .replace(/\s+/g, " ")}`);
        }
      }
    }
    expect(stray, "a run action reached without the server's list").toEqual([]);
  });

  it("no pass-through component renames, defaults or spreads a run-action handler", () => {
    // THE REVIEWER'S ESCAPE (review of 0fd6c45): `onBuildOnThis: buildIt` in StepPath's
    // destructuring sat inside the allowed signature span, so a status-gated
    // `<button onClick={() => buildIt()}>` never mentioned a handler name and passed every
    // check. A pass-through takes each handler under its own name, or the scan cannot follow it.
    const escapes: string[] = [];
    for (const fn of PASS_THROUGH) {
      const at = UI.indexOf(`function ${fn}(`);
      const open = UI.indexOf("{", at + `function ${fn}(`.length - 1);
      const pattern = UI.slice(open, closing(open) + 1);
      if (/\.\.\./.test(pattern)) escapes.push(`${fn} spreads its props (...)`);
      for (const name of RUN_ACTION_HANDLERS) {
        if (new RegExp(`\\b${name}\\s*[:=]`).test(pattern)) {
          escapes.push(`${fn} renames or defaults ${name}`);
        }
      }
    }
    expect(escapes).toEqual([]);
  });

  it("every request that performs a run action is made in the one handler set", () => {
    // The other way round: the requests themselves (retry, skip, re-check, stop, build on a
    // run) are issued only from the handler attributes on <RunsView>, which reach the screen
    // only through RunActionsBar and FailedStepActions. A second place issuing one is a second
    // place deciding when it may be issued.
    const handlerSet = attributeSpans("RunsView");
    expect(handlerSet.length).toBeGreaterThan(0);
    const requests = [
      ...UI.matchAll(/\bact\("[^"]*",\s*"(retry_step|skip_step|recheck|stop)"/g),
      ...UI.matchAll(/\bfrom_run:/g),
      // REWRITTEN (review of the readiness change): Stop and run again is no longer a browser
      // `stop` then `start` — a refused start after a landed stop left no run at all. It is one
      // start naming `replaces_run`, which the server checks, stops and starts in one
      // transaction; Re-run names `rerun_of`. Both are run-action requests and are held here.
      ...UI.matchAll(/\breplaces_run:|\brerun_of:/g),
    ];
    expect(requests.length).toBeGreaterThanOrEqual(6);
    for (const m of requests) {
      expect(handlerSet.some(([a, b]) => m.index! >= a && m.index! < b),
             `${m[0]} at line ${lineOf(m.index!)} is outside the run-action handlers`).toBe(true);
    }
  });

  it("and no OTHER file in the console makes one on its own say-so", () => {
    // THE SECOND ENTRY POINT (review of 0fd6c45). The run report's "Build from this" POSTed
    // `from_run` whenever it had next steps, deciding for itself, and the server refused it on
    // every live run and every run without research. The scan above read one file, so it could
    // not see it. Every source file under app/, components/ and lib/ is read here: a run-action
    // request key outside the handler set is refused, and `from_run` anywhere else must be
    // sent only when the run's own `build_on_research` action is enabled.
    const roots = ["app", "components", "lib"];
    const files: string[] = [];
    const walk = (dir: string) => {
      let names: string[] = [];
      try { names = readdirSync(dir); } catch { return; }
      for (const name of names) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) {
          if (name !== "node_modules" && !(dir === "app" && name === "api")) walk(path);
        } else if (/\.(tsx?|jsx?)$/.test(name) && !/\.test\./.test(name)) {
          files.push(path);
        }
      }
    };
    roots.forEach(walk);
    expect(files.length).toBeGreaterThan(50);
    const stray: string[] = [];
    for (const file of files) {
      if (file === join("app", "delivery-workflows-ui.tsx")) continue;
      const code = codeOnly(file);
      for (const m of code.matchAll(/\baction:\s*"(retry_step|skip_step|recheck)"|\bact\("[^"]*",\s*"(retry_step|skip_step|recheck|stop)"/g)) {
        stray.push(`${file}: ${m[0]}`);
      }
      for (const m of code.matchAll(/\bfrom_run:/g)) {
        const before = code.slice(Math.max(0, m.index! - 40), m.index!);
        const gated = /carriesResearch \? \{ $/.test(before)
          && /const carriesResearch = researchAction\?\.key === "build_on_research"\s*&& researchAction\.enabled === true/
            .test(code);
        if (!gated) stray.push(`${file}: from_run sent without the run's build_on_research action`);
      }
    }
    expect(stray).toEqual([]);
  });

  it("the console's old private rules are gone", () => {
    // `researched` was the Build-on-this-research rule; `canSkip` was Skip's. Both now live in
    // the engine, once, shared with the endpoints that act on them.
    expect(UI).not.toContain("const researched");
    expect(UI).not.toContain("canSkip(");
    // A step's Try again/Skip render whenever its server-sent actions exist, not on a
    // hand-kept list of statuses.
    expect(UI).not.toContain('["failed", "deadlettered", "cancelled"].includes(step.status)');
  });

  it("every action button's test id exists once, in the view table, not as a literal button", () => {
    for (const id of ["dw-recheck", "dw-stop", "dw-stop-and-rerun", "dw-rerun", "dw-delete-run",
                      "dw-build-on-this"]) {
      expect(UI).not.toContain(`data-testid="${id}"`);
      expect(UI.split(`testId: "${id}"`).length - 1).toBe(1);
    }
  });
});

describe("a run renders exactly the actions it was sent", () => {
  it("THE INCIDENT: a cancelled run whose research failed shows no Build on this research", () => {
    // What the engine sends for it (see the engine table's INCIDENT rows).
    const html = renderToStaticMarkup(
      <RunActionsBar run={run([ok("rerun", "Run this workflow again"),
                               ok("remove", "Remove this run")])}
                     busy={null} handlers={handlers} />);
    expect(html).toContain("Run this workflow again");
    expect(html).toContain("Remove this run");
    expect(html).not.toContain("dw-build-on-this");
    expect(html).not.toContain('data-testid="dw-stop"');
  });

  it("a live run shows Stop and Stop and run again, and not Remove", () => {
    const html = renderToStaticMarkup(
      <RunActionsBar run={run([ok("stop", "Stop this run"), ok("stop_and_rerun", "Stop and run again")])}
                     busy={null} handlers={handlers} />);
    expect(html).toContain('data-testid="dw-stop"');
    expect(html).toContain('data-testid="dw-stop-and-rerun"');
    expect(html).not.toContain("dw-delete-run");
    expect(html).not.toContain("dw-rerun\"");
  });

  it("a disabled action is shown disabled with its reason as the button's help", () => {
    const html = renderToStaticMarkup(
      <RunActionsBar run={run([{ key: "build_on_research", label: "Build on this research →",
                                 enabled: false,
                                 reason: "the research is still being written up" }])}
                     busy={null} handlers={handlers} />);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*title="the research is still being written up"/);
    expect(html).toContain('data-enabled="false"');
  });

  it("a run with no actions renders no management row at all", () => {
    expect(renderToStaticMarkup(
      <RunActionsBar run={run([])} busy={null} handlers={handlers} />)).toBe("");
  });

  it("a step's Try again and Skip come from the run's list for THAT step", () => {
    const step = { step_id: "research", kind: "capability", status: "failed",
                   capability: "research" } as unknown as RunStep;
    const sent = run([
      { key: "retry_step", label: "Try Research again", enabled: false, step_id: "research",
        reason: "it is undoing what it did — wait for that to finish" },
      { key: "skip_step", label: "Skip and proceed", enabled: false, step_id: "research",
        reason: "it is undoing what it did — wait for that to finish" },
      ok("retry_step", "Try Panel again", "panel"),
    ]);
    const html = renderToStaticMarkup(
      <FailedStepActions step={step} actions={stepActions(sent, "research")} busy={null}
                         onRetry={noop} onSkip={noop} />);
    expect(html).toContain("Try Research again");
    expect(html).not.toContain("Try Panel again");
    expect(html).toMatch(/data-testid="dw-step-retry"/);
    expect(html).toMatch(/disabled=""[^>]*title="it is undoing what it did/);
    // Nothing sent for a step, nothing rendered — whatever its status says.
    expect(renderToStaticMarkup(
      <FailedStepActions step={step} actions={stepActions(sent, "deploy")} busy={null}
                         onRetry={noop} onSkip={noop} />)).toBe("");
  });

  it("Skip is absent when the server did not send it — an approval gate", () => {
    const gate = { step_id: "approve", kind: "capability", status: "failed",
                   capability: "spec_review" } as unknown as RunStep;
    const html = renderToStaticMarkup(
      <FailedStepActions step={gate} busy={null} onRetry={noop} onSkip={noop}
                         actions={stepActions(run([ok("retry_step", "Try Spec review again", "approve")]),
                                              "approve")} />);
    expect(html).toContain("Try Spec review again");
    expect(html).not.toContain("Skip and proceed");
  });
});


describe("the whole run view shows nothing the server did not send", () => {
  /** THE INCIDENT'S RUN, as the API lists it: cancelled, research SUCCEEDED with a dossier,
   *  the panel failed. Every hand-written rule the console ever had would have offered
   *  something here — Build on this research, Try again, Skip, Re-check. */
  function incident(actions: RunAction[], status = "cancelled") {
    const s = (step_id: string, st: string, capability: string, output = {}) => ({
      step_id, kind: "capability", status: st, capability, output, evidence: [], error: st ===
        "failed" ? "the panel never convened" : null, approval: {}, due_at: null,
      timeout_applied: null, attempt: 1, started_at: null, completed_at: null, decision_id: null,
    }) as unknown as RunStep;
    return {
      ...run(actions), status, standing: standingOf(status), finished: status !== "running",
      steps: [s("research", "succeeded", "research", { discovery_id: "drun_1" }),
              s("panel", "failed", "meeting")],
      step_count: 2, steps_done: 1, input: { title: "Lead triage" },
    };
  }

  function view(r: ReturnType<typeof incident>, opened = true) {
    return renderToStaticMarkup(
      <RunsView runs={[r] as never} openRun={opened ? r.id : null} setOpenRun={noop}
                onAdvance={noop} busy={null} capabilities={[]} onRetryStep={noop}
                onSkipStep={noop} onRerun={noop} onStop={noop} onDelete={noop}
                onStopAndRerun={noop} onBuildOnThis={noop} onRecheck={noop} onSetTarget={noop}
                onPrepareTarget={noop} onRunPreview={noop} onBound={noop} projectId="p"
                actor="ceo" roles={[]} targets={[]} hosts={[]} />);
  }

  const ACTION_TEST_IDS = ["dw-recheck", "dw-stop", "dw-stop-and-rerun", "dw-rerun",
                           "dw-delete-run", "dw-build-on-this"];

  // EVERY status the database allows, not three. The reviewer's mutation gated its button on
  // `run.status === "completed"`, a status this loop never rendered.
  for (const status of schemaRunStatuses()) {
    it(`a ${status} run sent no actions renders no workflow-management button, no Try again, no Skip`, () => {
      for (const opened of [true, false]) {
        const html = view(incident([], status), opened);
        for (const id of ACTION_TEST_IDS) expect(html).not.toContain(`data-testid="${id}"`);
        expect(html).not.toMatch(/data-testid="dw-[a-z-]*-(retry|skip)"/);
        expect(html).not.toContain("Build on this research");
        expect(html).not.toContain("dw-run-actions");
      }
    });
  }

  it("and the same run renders exactly what it IS sent — the check above is not vacuous", () => {
    const html = view(incident([
      ok("rerun", "Run this workflow again"), ok("remove", "Remove this run"),
      ok("build_on_research", "Build on this research →"),
      ok("retry_step", "Try Panel again", "panel"), ok("skip_step", "Skip and proceed", "panel"),
    ], "failed"));
    for (const id of ["dw-rerun", "dw-delete-run", "dw-build-on-this"]) {
      expect(html).toContain(`data-testid="${id}"`);
    }
    expect(html).not.toContain('data-testid="dw-stop"');
    expect(html).toContain('data-testid="dw-run-stopped-retry"');
    expect(html).toContain('data-testid="dw-run-stopped-skip"');
  });
});
