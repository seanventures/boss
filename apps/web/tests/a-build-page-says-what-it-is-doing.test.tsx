// @vitest-environment happy-dom
/**
 * A build's page says what the build is doing, what each tool call did, and lets you search it.
 *
 * The operator's page for attempt 5 of HelpDesk said "Skipped" under Plan and Tests (they were
 * kept from attempt 4's saved workspace), "Not yet / Not run" down the Outputs, and "Bash · ok"
 * fifty-two times, with no way to find the Traceback. The server's shapes are
 * engine/boss_engine/services/build_detail.py (cockpit.summary and GET …/tool-log).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildIndex, errorLine, groupTools, highlight, missingWord, nowFacts, parseSummary, parseToolLog, searchLines, step,
  testsWord, isStackLine,
} from "../lib/build-detail";
import { $, $$, click, mount, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { at, cockpit, stations } from "./_helpers/factory-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { Cockpit } = await import("../app/factory/cockpit");

type W = Record<string, unknown>;

const VERIFY = [
  "collected 11 items",
  "Traceback (most recent call last):",
  '  File "/work/tests/test_auth.py", line 41, in test_google_login_redirect',
  "AssertionError: assert 500 == 302",
  "    at Object.<anonymous> (/work/web/src/login.test.ts:14:5)",
  "FAILED tests/test_auth.py::test_boss_login - KeyError: 'BOSS_CLIENT_ID'",
  "✘ web/src/login.test.ts > login > redirects",
  "========== 7 failed, 4 passed in 3.21s ==========",
].join("\n");

const SUMMARY: W = {
  phase: "repair", stage: { step: "repair", title: "Repair", started_at: at(6), role_key: "software_engineer" },
  time_limit_s: 2700, attempt: 5,
  seeded: { from_run: "crun_four", attempt: 4, files: 75, tests: 11, at: at(1), missing: false },
  note: "Use the BOSS client id from settings",
  files: ["app/auth.py", "app/settings.py"], files_count: 2, files_final: false,
  verify: { run_id: "crun_four", attempt: 4, own: false, at: "2026-10-04T08:40:00.000Z", ok: false, passed: 4, failed: 7,
            total: 11, failing: ["tests/test_auth.py::test_google_login_redirect", "tests/test_auth.py::test_boss_login"] },
  gates: { run_id: "crun_four", attempt: 4, own: false, passed: ["Code written", "Old tests pass"], failed: ["New tests pass"] },
};

const LOG: W = {
  items: [
    { act_id: "act_BUILD0000000001", tool: "Read", arg: "app/auth.py", ok: true, ms: 120, output: "import os" },
    { act_id: "act_BUILD0000000001", tool: "Read", arg: "app/settings.py", ok: true, ms: 90, output: "X = 1" },
    { act_id: "act_BUILD0000000001", tool: "Read", arg: "app/routes.py", ok: true, ms: 80, output: "routes" },
    { act_id: "act_BUILD0000000001", tool: "Bash", arg: "pytest -q tests/test_auth.py", ok: false, ms: 6600, exit_code: 1,
      error: "Exit code 1\n" + VERIFY },
    { act_id: "act_BUILD0000000001", tool: "Edit", arg: "app/auth.py", ok: true, ms: 300, output: "updated" },
  ],
  total: 5, more: false,
  verify: { run_id: "crun_four", attempt: 4, own: false, output: VERIFY, clipped: false },
};

function seededCockpit(): W {
  return cockpit({
    run: { id: "crun_five", title: "HelpDesk: add login · retry 5", status: "running", phase: "repair", created_at: at(0),
           updated_at: at(9), attempt: 5, root_run_id: "crun_root" },
    lineage: [{ run_id: "crun_five", attempt: 5, state: "building", word: "Coding", at: at(0) },
              { run_id: "crun_four", attempt: 4, state: "failed", word: "Failed", at: at(-60) }],
    stations: stations("build", "working", [
      { key: "plan", status: "done", word: "Kept from #4", tip: "Carried from #4's saved workspace, not re-run" },
      { key: "tests", status: "done", word: "Kept from #4", tip: "Carried from #4's saved workspace, not re-run" },
    ]),
    outputs: { ...(cockpit().outputs as W), tests: { word: "Not run", role_key: null, commands: [] }, changes: null,
               cost: { word: "Not yet", metered_usd: 0, subscription_calls: 0, by_agent: [], by_station: {} } },
    cost_usd: 0, metered: false,
    summary: SUMMARY,
  });
}

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the Now summary", () => {
  const input = (over: Partial<Parameters<typeof nowFacts>[0]> = {}) => ({
    summary: parseSummary(SUMMARY), state: { word: "Coding", state: "building", station: "build" },
    stations: [{ key: "build", label: "Build" }], live: true, startedAt: at(0), updatedAt: at(9),
    lineage: [{ attempt: 5, sameLineage: true }, { attempt: 4, sameLineage: true }], costUsd: 1.87, metered: true, costWord: "",
    ...over,
  });

  it("says the stage against its time limit, the attempt, the seed, the note, files, tests, gates and cost", () => {
    const facts = nowFacts(input(), Date.parse(at(15)));
    const by = Object.fromEntries(facts.map((f) => [f.key, f]));
    expect(by.stage.value).toBe("Build · Repair · 9m of 45m");
    expect(by.attempt.value).toBe("5 of 5");
    expect(by.seeded.value).toBe("From #4 · 75 files · 11 tests");
    expect(by.note.value).toBe("“Use the BOSS client id from settings”");
    expect(by.files.value).toBe("2 so far");
    expect(by.tests.value).toMatch(/^7\/11 failed · #4 · at \d\d:\d\d$/);
    expect(by.tests.detail).toEqual(["tests/test_auth.py::test_google_login_redirect", "tests/test_auth.py::test_boss_login"]);
    expect(by.gates.value).toBe("1 failed · 2 passed · #4");
    expect(by.cost.value).toBe("$1.87");
  });

  it("warns as the stage nears its limit, and says nothing it was not told", () => {
    const late = nowFacts(input(), Date.parse(at(6 + 40)));
    expect(late[0].tone).toBe("warn");
    const bare = nowFacts(input({ summary: parseSummary({ attempt: 1 }), lineage: [] }), Date.parse(at(15)));
    expect(bare.map((f) => f.key)).toEqual(["stage", "attempt", "files", "tests", "cost"]);
    expect(bare.find((f) => f.key === "tests")?.value).toBe("Not run yet");
    expect(parseSummary(undefined)).toBeNull();
  });

  it("counts tests only from what the runner printed", () => {
    expect(testsWord({ passed: 4, failed: 7, total: 11, ok: false })).toBe("7/11 failed");
    expect(testsWord({ passed: 11, failed: 0, total: 11, ok: true })).toBe("11/11 passed");
    expect(testsWord({ passed: null, failed: null, total: null, ok: false })).toBe("Failed");
  });
});

describe("the Outputs say why something is missing", () => {
  const base = (over: W = {}) => ({
    summary: parseSummary(SUMMARY), state: { word: "Coding", state: "building", station: "build" }, stations: [], live: true,
    lineage: [], costUsd: 0, metered: false, costWord: "Not yet",
    outputs: { pr: null, tests: { word: "Not run", commands: [] }, changes: null, preview: null, cost: { word: "Not yet" } },
    ...over,
  });
  it("a running build holds the PR, shows the last tests with whose they were, and files so far", () => {
    expect(missingWord("pr", base())?.word).toBe("Held until the gates pass");
    expect(missingWord("tests", base())?.word).toMatch(/^7\/11 failed at \d\d:\d\d · #4$/);
    expect(missingWord("changes", base())?.word).toBe("2 files so far");
    expect(missingWord("preview", base())?.word).toBe("After the PR");
    expect(missingWord("cost", base())?.word).toBe("None metered yet");
  });
  it("a failed build says the PR was not opened, and a real word is kept", () => {
    const failed = base({ live: false, state: { word: "Failed", state: "failed", station: "build" } });
    expect(missingWord("pr", failed)?.word).toBe("Not opened · gates failed");
    expect(missingWord("pr", base({ outputs: { ...base().outputs, pr: { word: "#842 · Open" } } }))).toBeNull();
    const none = base({ summary: parseSummary({ attempt: 1 }) });
    expect(missingWord("tests", none)?.word).toBe("After the build");
  });
});

describe("tool calls", () => {
  it("fold consecutive reads, keep failures alone, and say why a call failed", () => {
    const log = parseToolLog(LOG);
    const groups = groupTools(log.items);
    expect(groups.map((g) => (g.kind === "reads" ? `Read×${g.calls.length}` : g.call.tool))).toEqual(["Read×3", "Bash", "Edit"]);
    expect(errorLine(log.items[3].error ?? "")).toBe("7 failed, 4 passed in 3.21s");
    expect(errorLine("<tool_use_error>Permission denied: /etc/x</tool_use_error>")).toBe("Permission denied: /etc/x");
  });
});

describe("search", () => {
  const log = parseToolLog(LOG);
  const story = [{ id: "a1", type: "act", verb: "Coding", title: "Repair", station: "build", said: "Fixed the redirect" }];
  const index = buildIndex(story, log, (k) => (k === "build" ? "Build" : ""), () => "Repair");

  it("reads the story, each call's command, path and output, and the verify output", () => {
    const hits = searchLines(index, "boss_client_id", null);
    expect(hits.map((h) => h.source)).toEqual(["tool", "verify"]);
    expect(searchLines(index, "app/settings.py", null)[0].text).toBe("Read · app/settings.py");
    expect(searchLines(index, "redirect", null).some((h) => h.source === "story")).toBe(true);
    expect(searchLines(index, "", null)).toEqual([]);
  });

  it("quick filters find errors and stack traces", () => {
    const stack = searchLines(index, "", "stack").map((h) => h.text.trim());
    expect(stack).toContain("Traceback (most recent call last):");
    expect(stack).toContain("at Object.<anonymous> (/work/web/src/login.test.ts:14:5)");
    expect(stack).toContain("✘ web/src/login.test.ts > login > redirects");
    expect(stack).not.toContain("collected 11 items");
    const errors = searchLines(index, "", "errors");
    expect(errors.some((h) => h.text.startsWith("Bash · pytest"))).toBe(true);
    expect(errors.some((h) => h.text === "import os")).toBe(false);
    expect(isStackLine('  File "/a.py", line 3, in f')).toBe(true);
  });

  it("highlights each match and jumps round", () => {
    expect(highlight("KeyError: 'BOSS' and boss", "boss")).toEqual([["KeyError: '", false], ["BOSS", true], ["' and ", false], ["boss", true]]);
    expect(step(-1, 3, 1)).toBe(0);
    expect(step(2, 3, 1)).toBe(0);
    expect(step(0, 3, -1)).toBe(2);
    expect(step(0, 0, 1)).toBe(-1);
  });
});

describe("on the page", () => {
  const route = (url: string) => (url.includes("/tool-log") ? LOG : undefined);

  it("shows Now, Kept from #4, each call in a line with its failure open, and searches with /", async () => {
    stubFetch(seededCockpit(), route);
    await mount(<Cockpit projectId="boss-dogfood" runId="crun_five" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
    expect($('[data-testid="fx-nowband"]')?.textContent).toContain("From #4 · 75 files · 11 tests");
    expect($('[data-testid="fx-now-detail-tests"]')?.textContent).toContain("tests/test_auth.py::test_boss_login");
    expect(document.body.textContent).toContain("Kept from #4");
    expect(document.body.textContent).not.toContain("Skipped");
    expect($('[data-testid="fx-out-value-pr"]')?.textContent).toBe("Held until the gates pass");

    await click($('[data-testid="fx-tools-act_BUILD0000000001"]'));
    expect($('[data-testid="fx-tools-act_BUILD0000000001"]')?.textContent).toContain("1 failed");
    expect($('[data-testid="fx-reads-0"]')?.textContent).toContain("Read ×3");
    const bash = $('[data-testid="fx-call-3"]');
    expect(bash?.textContent).toContain("pytest -q tests/test_auth.py");
    expect(bash?.textContent).toContain("6.6s");
    expect(bash?.textContent).toContain("exit 1 · 7 failed, 4 passed in 3.21s");
    expect($('[data-testid="fx-call-err-3"]')?.textContent).toContain("Traceback");   // a failure opens by itself
    await click($('[data-testid="fx-tl-failures"]'));
    expect($$('[data-testid^="fx-call-"]').filter((e) => /^fx-call-\d+$/.test(e.dataset.testid ?? "")).length).toBe(1);

    const { act } = await import("react");
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "/", bubbles: true })); });
    const input = $('[data-testid="fx-search-input"]') as HTMLInputElement;
    expect(document.activeElement).toBe(input);
    await click($('[data-testid="fx-search-stack"]'));
    expect($('[data-testid="fx-search-count"]')?.textContent).toMatch(/^1\/\d+$/);
    expect($('[data-testid="fx-hits"]')?.textContent).toContain("Traceback (most recent call last):");
    await click($('[data-testid="fx-search-next"]'));
    expect($('[data-testid="fx-hit-1"]')?.dataset.current).toBe("true");
  });
});
