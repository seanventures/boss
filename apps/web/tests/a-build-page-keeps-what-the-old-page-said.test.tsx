// @vitest-environment happy-dom
/**
 * A build's page keeps what its old page said (the Factory redesign parity audit).
 *
 * "Lots of features are broken especially the view of each run - every run will give different
 * PR and steps and details.. they are all missing." The old per-run page showed, for ONE build:
 * the brief's criteria and checks, the repo, the workflow and its version, the engines that built
 * and reviewed it, every check WITH its reason, each test command's time, the diff's size, every
 * model call (stage, agent, model, error), the merge decision, Approve & ship, and the run list of
 * the app with each run's PR and cost. The fixture is boss-1's freshness-board build (attempt 2,
 * PR #219), sanitised; the server's shape is engine/boss_engine/services/factory_view.py.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, $$, click, mount, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { at, cockpitPrOpen } from "./_helpers/factory-fixtures";

const deployRuns: unknown[] = [];
const convened: FormData[] = [];
vi.mock("../app/factory/legacy-build-parts", async (orig) => ({
  ...(await orig<object>()),
  DeploymentLine: ({ run }: { run: unknown }) => { deployRuns.push(run); return <span data-testid="deploy-line" />; },
  QaBenchmarkEvidence: ({ benchmarkName }: { benchmarkName: string }) => <span data-testid="qa-evidence">{benchmarkName}</span>,
}));
vi.mock("../app/actions", async (original) => ({
  ...(await original<typeof import("../app/actions")>()),
  conveneMeetingAction: async (form: FormData) => { convened.push(form); return { ok: true }; },
  startPreviewAction: async () => previewReply.value,
}));
const previewReply: { value: Record<string, unknown> } = { value: {} };
vi.mock("../app/deploy-ui", () => ({ LiveDeployment: () => <span data-testid="live-deploy" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Cockpit } = await import("../app/factory/cockpit");

type W = Record<string, unknown>;

/** boss-1's attempt 2 of the freshness board, as the parity API answers it. */
function freshnessBoard(): W {
  const base = cockpitPrOpen();
  const outputs = base.outputs as W;
  return {
    ...base,
    run: { ...(base.run as W), id: "crun_fresh2", title: "Build a documentation freshness board.",
           pull_request_url: "https://github.com/acme/sandbox/pull/219", pull_request_number: 219, decision_id: "dec_219",
           attempt: 2, root_run_id: "crun_fresh1" },
    app: "Documentation freshness board",
    // The merge decision is not in the attention list (already decided) — the PR row must
    // still reach it.
    attention: [],
    metered: true,
    lineage: [
      { run_id: "crun_follow", attempt: 1, state: "pr_open", word: "PR open", at: at(30), kind: "Follow-up",
        title: "Follow-up: deferred review findings", pr_number: 223, cost_usd: 1.2, same_lineage: false },
      { run_id: "crun_fresh2", attempt: 2, state: "pr_open", word: "PR open", at: at(0), kind: "Attempt 2",
        title: "Build a documentation freshness board.", pr_number: 219, cost_usd: 5.54, same_lineage: true },
      { run_id: "crun_fresh1", attempt: 1, state: "pr_open", word: "PR open", at: at(-1400), kind: "First build",
        title: "Build a documentation freshness board.", pr_number: 219, cost_usd: 11.97, same_lineage: true },
    ],
    brief: {
      repo: "acme/sandbox", branch: "boss/code/crun_fresh1", base_branch: "main",
      criteria: ["Each doc shows its last review date", "Stale docs sort first"],
      test_commands: ["npm run build && npm run test:e2e"],
      workflow: { name: "Standard Product Delivery", version: 2, default: false },
      engine: "claude_code", model: "opus", review_engine: "codex", review_model: "gpt-6-sol", cross_model: true,
      turns: 56, built_on: "API host", profile: "lean", risk_tier: 2,
      reviewer_independent: true, test_author_independent: true, qa_benchmark: null,
      deferred_findings: 0, advisory_failing: 4, mutation_caught: false, not_run: [], redelivered: 0, error: null,
    },
    outputs: {
      ...outputs,
      pr: { number: 219, url: "https://github.com/acme/sandbox/pull/219", files_url: "https://github.com/acme/sandbox/pull/219/files",
            decision_id: "dec_219", decision_status: "approved", made_by: 4, word: "#219 · Open", role_key: "software_engineer" },
      checks: { word: "4 failed · shipped anyway", advisory: true, gates: [
        { name: "written_tests_pass", label: "New tests pass", ok: true, role_key: "qa", station: "check", evidence: "exit 0", waivable: true },
        { name: "code_review_passed", label: "Review passed", ok: false, role_key: "adversarial_reviewer", station: "review", evidence: "refuted", waivable: true },
        { name: "security_clean", label: "Security clean", ok: false, role_key: "security_reviewer", station: "review", evidence: "issues", waivable: true },
        { name: "tests_strong_enough", label: "Tests catch bugs", ok: false, role_key: "qa", station: "check", evidence: "mutation missed", waivable: true },
        { name: "consistent", label: "Consistent", ok: false, role_key: "adversarial_reviewer", station: "review", evidence: "drift", waivable: true },
      ] },
      tests: { word: "1 of 1 command passed", role_key: "qa", output_tail: "ok",
               commands: [{ command: "npm run build && npm run test:e2e", exit_code: 0, ok: true, timed_out: false, duration_ms: 210000 }] },
      changes: { word: "6 files · +312 −40", tests: [{ path: "tests/board.spec.ts" }], code: [{ path: "src/board.tsx" }],
                 files_changed: 6, additions: 312, deletions: 40, tests_role_key: "qa", code_role_key: "software_engineer",
                 diff_url: "https://github.com/acme/sandbox/pull/219/files" },
      preview: null,
    },
  };
}

const CALLS = { calls: [
  { stage: "write_tests", engine: "claude_code", model: "claude-sonnet-5", role: "qa", turns: 37, cost_usd_micros: 1201403,
    latency_ms: 291498, retries: 0, ok: true, error: null, included_in_subscription: false },
  { stage: "review_code", engine: "codex", model: "gpt-6-sol", role: "adversarial_reviewer", turns: 4, cost_usd_micros: 90000,
    latency_ms: 50000, retries: 1, ok: false, error: "ran out of turns", included_in_subscription: false },
] };

const onOpenBuild = vi.fn();
const view = () => <Cockpit projectId="p1" runId="crun_fresh2" actor="ceo" onClose={() => {}} onOpenBuild={onOpenBuild} />;
const text = (sel: string) => ($(sel)?.textContent ?? "").replace(/\s+/g, " ").trim();
const open = async (key: string) => {
  const head = $(`[data-testid="fx-out-head-${key}"]`);
  if (head?.getAttribute("aria-expanded") !== "true") await click(head);
};

beforeEach(() => {
  deployRuns.length = 0;
  convened.length = 0;
  window.history.replaceState(null, "", "/?org=org_acme&area=code_tasks&build=crun_fresh2");
  stubFetch(freshnessBoard(), (url) => (url.includes("/activity?") ? CALLS : undefined));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("a build's page keeps what its old page said", () => {
  it("Brief: the criteria, the checks, the repo and branch, the workflow and its version, the engines", async () => {
    await mount(view());
    expect(text('[data-testid="fx-out-value-brief"]')).toBe("2 criteria");
    await open("brief");
    const brief = text('[data-testid="fx-brief"]');
    for (const fact of ["Each doc shows its last review date", "Stale docs sort first", "npm run build && npm run test:e2e",
      "acme/sandbox · boss/code/crun_fresh1", "Standard Product Delivery v2", "claude_code · opus", "codex · gpt-6-sol",
      "cross-model", "lean · risk tier 2", "reviewer yes · tests yes", "API host · 56 turns"]) {
      expect(brief).toContain(fact);
    }
    expect($('[data-testid="fx-brief"] a[href="https://github.com/acme/sandbox/tree/boss%2Fcode%2Fcrun_fresh1"]')).not.toBeNull();
  });

  it("Checks: every check says why, inline — refuted, issues, mutation missed, drift", async () => {
    await mount(view());
    await open("checks");
    expect(text('[data-testid="fx-gate-why-code_review_passed"]')).toBe("refuted");
    expect(text('[data-testid="fx-gate-why-security_clean"]')).toBe("issues");
    expect(text('[data-testid="fx-gate-why-tests_strong_enough"]')).toBe("mutation missed");
    expect(text('[data-testid="fx-gate-why-consistent"]')).toBe("drift");
  });

  it("Tests: how long each command took; Changes: the diff's size", async () => {
    await mount(view());
    await open("tests");
    expect(text('[data-testid="fx-out-body-tests"]')).toContain("exit 0 · ok · 3m");
    await open("changes");
    expect(text('[data-testid="fx-diffstat"]')).toBe("6 files +312 −40");
  });

  it("Pull request: the merge decision is reachable after it left the attention list, keeping the org", async () => {
    await mount(view());
    await open("pr");
    const review = $('[data-testid="fx-pr-review"]') as HTMLAnchorElement | null;
    expect(review).not.toBeNull();
    expect(review?.getAttribute("href")).toBe("?org=org_acme&area=decisions&decision=dec_219");
  });

  it("Cost: every model call, its agent, model and error (the old Steps table)", async () => {
    await mount(view());
    await open("cost");
    await click($('[data-testid="fx-cost-calls"]'));
    expect(text('[data-testid="fx-call-write_tests"]')).toContain("claude-sonnet-5");
    expect(text('[data-testid="fx-call-write_tests"]')).toContain("37 turns");
    expect(text('[data-testid="fx-call-review_code"]')).toContain("ran out of turns");
    expect($('[data-testid="fx-call-review_code"]')?.dataset.ok).toBe("false");
  });

  it("the build list holds every build of the app with its PR and cost, and opens any of them", async () => {
    await mount(view());
    expect(text('[data-testid="fx-attempts"]')).toBe("3 builds ▾");
    await click($('[data-testid="fx-attempts"]'));
    expect(text('[data-testid="fx-attempt-crun_follow"]')).toMatch(/^Follow-up · PR open · PR #223 · \$1\.20/);
    expect(text('[data-testid="fx-attempt-crun_fresh1"]')).toMatch(/^Attempt 1 · PR open · PR #219 · \$12 · /);
    await click($('[data-testid="fx-attempt-crun_follow"]'));
    expect(onOpenBuild).toHaveBeenCalledWith("crun_follow");
  });

  it("Deploy looks the app up by its name, never as Untitled app", async () => {
    await mount(view());
    await open("deploy");
    expect(deployRuns.at(-1)).toMatchObject({ id: "crun_fresh2", title: "Build a documentation freshness board." });
  });

  it("Activity log keeps the org; Ask BOSS leaves this build's own starters for the dock", async () => {
    await mount(view());
    await click($('[data-testid="fx-more"]'));
    const log = $$('[data-testid="fx-ck-menu"] a').find((a) => a.textContent?.startsWith("Activity log"));
    expect(log?.getAttribute("href")).toBe("?org=org_acme&area=activity&run=crun_fresh2");
    await click($('[data-testid="fx-menu-ask"]'));
    const starters = JSON.parse(window.sessionStorage.getItem("boss.agent.suggests.crun_fresh2") ?? "[]") as string[];
    expect(starters).toContain("4 checks failed but shipped as advisory — which should block next time?");
    expect(starters).toContain("The tests missed an injected bug — where are they weakest?");
    expect(starters).toContain("Which of the 2 acceptance criteria are not actually covered by a test?");
  });

  it("＋ Add a feature is on the header, and opens New project on this app (with its live preview)", async () => {
    await mount(view());
    const add = $('[data-testid="fx-add-feature"]') as HTMLAnchorElement;
    expect(add?.textContent).toBe("＋ Add a feature");
    const href = new URLSearchParams(add.getAttribute("href")!.replace(/^\?/, ""));
    expect(href.get("area")).toBe("delivery_workflows");
    expect(href.get("compose")).toBe("add");
    expect(href.get("app")).toBe("crun_fresh2");
  });

  it("Change it can spec the change first with a panel, onto this app's branch", async () => {
    await mount(view());
    await click($('[data-testid="fx-more"]'));
    await click($('[data-testid="fx-menu-change"]'));
    const what = $('[data-testid="fx-change-what"]') as HTMLTextAreaElement;
    const { act } = await import("react");
    const set = (el: HTMLTextAreaElement | HTMLSelectElement, v: string) => act(async () => {
      const proto = Object.getPrototypeOf(el);
      Object.getOwnPropertyDescriptor(proto, "value")?.set?.call(el, v);
      el.dispatchEvent(new Event(el.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
    });
    await set(what, "Bulk-mark docs as reviewed");
    await set($('[data-testid="fx-change-through"]') as HTMLSelectElement, "@panel-feature");
    expect(text('[data-testid="fx-change-propose"]')).toBe("Convene panel");
    await click($('[data-testid="fx-change-propose"]'));
    expect(convened).toHaveLength(1);
    expect(convened[0].get("meeting_type")).toBe("prd_panel");
    expect(String(convened[0].get("directive"))).toContain("[boss:extend_run=crun_fresh2]");
    expect(String(convened[0].get("directive"))).toContain("Bulk-mark docs as reviewed");
  });
});

describe("the preview says where it runs, how far it got, and what a press really did", () => {
  const withPreview = (preview: W | null, over: W = {}) => {
    const c = freshnessBoard();
    return { ...c, ...over, outputs: { ...(c.outputs as W), preview } };
  };

  it("a starting preview lists its steps and its substrate", async () => {
    stubFetch(withPreview({ id: "prev_9", status: "starting", word: "Starting…", steps_done: 1, steps_total: 3, substrate: "docker",
      steps: [{ label: "Install", status: "ok", duration_ms: 41000 }, { label: "Build", status: "failed", log: "vite: not found" },
              { label: "Start", status: "pending" }] }));
    await mount(view());
    await open("preview");
    expect(text('[data-testid="fx-out-body-preview"]')).toContain("docker");
    await click($('[data-testid="fx-preview-steps"]'));
    const ledger = text('[data-testid="fx-preview-ledger"]');
    expect(ledger).toContain("Install · 41s");
    expect(ledger).toContain("Build · vite: not found");
  });

  it("a library build's press says its tests passed; a console timeout is not a failure", async () => {
    stubFetch(withPreview(null));
    await mount(view());
    await open("preview");
    previewReply.value = { note: "Ran 12 tests\nOK" };
    await click($('[data-testid="fx-preview-run"]'));
    expect(text('[data-testid="fx-preview-note"]')).toContain("Tests passed · no live app");
    previewReply.value = { error: "The API did not answer within 120s. It may still be working." };
    await click($('[data-testid="fx-preview-run"]'));
    expect(text('[data-testid="fx-out-body-preview"]')).not.toContain("Couldn't start");
    expect($('[data-testid="fx-preview-starting"]')).not.toBeNull();
  });
});

describe("links into a build land on that build", () => {
  it("run= names a build only when it is one; a stale build= never beats the strip's run=", async () => {
    const { readFactoryUrl } = await import("../app/code-tasks-ui");
    const { searchForProject } = await import("../app/url-intent");
    expect(readFactoryUrl("?area=code_tasks&run=wfr_123").build).toBeNull();
    expect(readFactoryUrl("?area=code_tasks&run=crun_abc").build).toBe("crun_abc");
    const next = searchForProject("?org=o&area=code_tasks&build=crun_old", "crun_new");
    expect(readFactoryUrl(next).build).toBe("crun_new");
  });
});

describe("an app opens on the build that works", () => {
  it("the row opens its newest built attempt, not a newer failed one; the row says its PR, spend and that the last one failed", async () => {
    const { AppsTab } = await import("../app/factory/apps-tab");
    const { buildRow } = await import("./_helpers/factory-fixtures");
    const failed = { ...buildRow({ run_id: "crun_b3", attempt: 3, state: "failed", word: "Failed · Build" }), at: at(0) };
    const built = { ...buildRow({ run_id: "crun_b2", attempt: 2, state: "pr_open", word: "PR open" }), at: at(-60) };
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ apps: [{ key: "crun_b3", app: "Team inbox", run_id: "crun_b3",
      builders: ["qa"], state: "failed", word: "Failed · Build", preview: null, deploys: [], attempts: [failed, built],
      pr_number: 218, pr_url: "https://github.com/acme/sandbox/pull/218", cost_usd: 13.93, repo: "acme/sandbox" }] })));
    const opened: string[] = [];
    const { settle } = await import("./_helpers/live-render");
    await mount(<AppsTab projectId="p1" runs={[]} previews={[]} query="" onOpen={(id) => opened.push(id)} onNew={() => {}} />);
    await settle();
    expect(text(".fx-app .fx-row-now")).toBe("PR open · last failed");
    expect(text('[data-testid="fx-app-pr-crun_b3"]')).toBe("#218");
    expect(text(".fx-app-spend")).toBe("$14");
    await click($(".fx-app-row .fx-c-state"));
    expect(opened).toEqual(["crun_b2"]);
  });

  it("an app with a newer build on the line says shipped · building", async () => {
    const { AppsTab } = await import("../app/factory/apps-tab");
    const { buildRow } = await import("./_helpers/factory-fixtures");
    const { settle } = await import("./_helpers/live-render");
    const live = { ...buildRow({ run_id: "crun_c3", attempt: 1, state: "building", word: "Reviewing" }), at: at(0) };
    const built = { ...buildRow({ run_id: "crun_c2", attempt: 1, state: "pr_open", word: "PR open" }), at: at(-60) };
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ apps: [{ key: "crun_c3", app: "Team inbox", run_id: "crun_c3",
      builders: [], state: "building", word: "Reviewing", preview: null, deploys: [], attempts: [live, built] }] })));
    await mount(<AppsTab projectId="p1" runs={[]} previews={[]} query="" onOpen={() => {}} onNew={() => {}} />);
    await settle();
    expect(text(".fx-app .fx-row-now")).toBe("PR open · building");
  });
});

describe("the Factory says what it is a window onto", () => {
  it("the newest of how many, where PRs go, and that previews could not be listed", async () => {
    const { CodeTasksHome } = await import("../app/code-tasks-ui");
    const { floor } = await import("./_helpers/factory-fixtures");
    const { settle } = await import("./_helpers/live-render");
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      String(url).startsWith("/v1/factory/floor") ? Response.json(floor()) : new Response("{}", { status: 404 })));
    window.history.replaceState(null, "", "/?area=code_tasks");
    const runs = [{ id: "crun_1", title: "Invoice export", status: "completed" }] as never[];
    await mount(<CodeTasksHome projectId="p1" codeTasks={runs} codeTaskTotal={244} apiReachable actor="ceo"
                               previewBlockedReason="the previews API answered 502"
                               githubPlane="mission_control_binding" githubFullName="acme/sandbox" />);
    await settle();
    expect(text('[data-testid="fx-window"]')).toBe("Newest 1 of 244");
    expect(text('[data-testid="fx-github-plane"]')).toBe("GitHub · acme/sandbox");
    expect($('[data-testid="fx-github-plane"]')?.title).toBe("Project GitHub binding for acme/sandbox. Check GitHub access in Connect before starting a build.");
    expect($('[data-testid="fx-preview-list-error"]')?.title).toBe("the previews API answered 502");
  });
});

describe("the small things the old page said before a press", () => {
  it("Retry says what it reuses", async () => {
    const { retryTip } = await import("../app/factory/cockpit");
    const { parseCockpit } = await import("../lib/cockpit");
    expect(retryTip(parseCockpit(freshnessBoard()))).toBe("Resumes on the same branch, reusing the plan");
    const reviewOnly = freshnessBoard();
    (reviewOnly.outputs as W).checks = { word: "1 failed", advisory: false, gates: [
      { name: "code_review_passed", label: "Review passed", ok: false, role_key: null, station: "review", waivable: true }] };
    expect(retryTip(parseCockpit(reviewOnly))).toBe("Resumes on the same branch, reusing the plan and the tests");
  });

  it("a benchmark says what it must do and how it is checked", async () => {
    const { BenchmarkSheetBody } = await import("../app/factory/benchmark-sheet");
    const bench = { id: "bugbox", name: "BugBox", tagline: "Issue tracker", summary: "A small issue tracker.", focus: ["auth"],
      acceptance_scenarios: ["A signed-out visitor cannot see issues"], completion_contract: [],
      verification_commands: ["npm run test:e2e"], scoring_dimensions: [], score_baselines: {} };
    await mount(<BenchmarkSheetBody benchmarks={[bench] as never[]} builtIds={new Set()} projectId="p1" actor="ceo"
                                    launched={{}} onLaunched={() => {}} onOpen={() => {}} />);
    await click($('[data-testid="fx-bench-details-bugbox"]'));
    expect($$('[data-testid="fx-bench-body-bugbox"] > span').map((x) => x.textContent)).toEqual(
      ["Done when", "A signed-out visitor cannot see issues", "Verified by", "npm run test:e2e"]);
  });
});

describe("where a build came from is a link that lands", () => {
  for (const [kind, id, href] of [
    ["workflow_run", "wfr_1", "?org=org_acme&area=delivery_workflows&wfr=wfr_1"],
    ["meeting", "mrun_1", "?org=org_acme&area=meetings&meeting=mrun_1"],
    // The console's research area is `discovery`, and it reads the dossier from `run=`.
    ["discovery", "drun_1", "?org=org_acme&area=discovery&run=drun_1"],
  ] as const) {
    it(`${kind} → ${href}`, async () => {
      stubFetch({ ...freshnessBoard(), origin: { kind, id, label: "x" } });
      await mount(view());
      expect($('[data-testid="fx-origin"]')?.getAttribute("href")).toBe(href);
    });
  }
});
