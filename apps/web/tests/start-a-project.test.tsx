// @vitest-environment happy-dom
/**
 * START A PROJECT (BOSSAPP-810): the three screens on the API's real shapes.
 *
 *   1 New project     three doors (tag · time, title, line, action) and the presets grid; a preset
 *                     makes its project (POST /v1/projects, a taken name gets " 2"); a banking
 *                     preset is a knowledge-graph project opened on its Documents tab;
 *                     `/?new_project=sample|import|idea|1` opens the flow at that choice
 *   the sample        "Hello World" + POST /v1/templates/hello-world/apply (new app, node, Minor
 *                     Feature); refused for what the install lacks → the checklist and Start anyway
 *   2 Import a repo   accounts from the registry, the account's allowed repos, BOSS reads it
 *                     (GET /v1/github/repos/{id}/facts), the name prefilled → POST /v1/projects
 *                     then PUT /v1/projects/{id}/repos/{repo}
 *   3 the project     GET /v1/projects/{id}/start: repo · branch · code mapped ✓, chips, estimate,
 *                     Start → POST /api/delivery-workflows (the project's feature workflow on the
 *                     repo), five tiles, each piece of work with its state and one action
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectStartCard } from "../app/start-project/project-start";
import { StartProjectSheet } from "../app/start-project/start-sheet";
import { ProjectMenu } from "../app/project-menu";
import {
  accountsOf, estimateLine, nameFromIdea, newProjectHref, startBody, stepFromParam, type RepoFacts, type StartView,
} from "../lib/start-project";
import {
  $$, choose, click, mountAs, registryFixture, stubFetch, tid, type, unmount,
} from "./_helpers/multirepo-fixtures";

afterEach(() => { unmount(); window.history.replaceState(null, "", "/"); });

/** GET /v1/github/repos/{id}/facts, as services/start_project.repo_facts answers it. */
const FACTS: RepoFacts = {
  repo_id: "ghr_api", full_name: "acme/api", branch: "develop", description: "Orders and carts service",
  private: true, facts: { language: "Java", framework: "Spring", build: "Gradle", tests: true, runs_as: "Web API" },
  read_from: "github", mapping: { state: "not_started", percent: 0, done: false }, in_project: false,
  suggested_name: "Api",
};

/** GET /v1/projects/{id}/start, as services/start_project.view answers it. */
function view(extra: Partial<StartView> = {}): StartView {
  return {
    project_id: "order-service", name: "Order service", description: null, project_type: "app", archived: false,
    repo: { full_name: "acme/order-service", branch: "develop", mapping: { state: "done", percent: 100, done: true } },
    start: { mode: "feature", work_class: null, workflow_key: "minor_feature", workflow_name: "Minor Feature",
             resolved_key: "minor_feature", minutes: 106, minutes_to_pr: 86, minutes_low: 45, minutes_high: 110, cost: { runs: 4, low_usd: 2, high_usd: 4, metered: true } },
    examples: ["Add an API endpoint", "Fix a bug", "Add tests", "Upgrade a library"],
    next: [
      { key: "plan", label: "Plan", who: "You approve", you: true },
      { key: "build", label: "Build", who: "BOSS", you: false },
      { key: "review", label: "Review & test", who: "BOSS", you: false },
      { key: "pr", label: "Pull request", who: "You merge", you: true },
      { key: "preview", label: "Preview", who: "BOSS", you: false },
    ],
    work: [
      { run_id: "wfr_plan", title: "Orders by customer", workflow: "Minor Feature", workflow_key: "minor_feature",
        status: "waiting", state: "Waiting for your plan approval", tone: "amber",
        action: { kind: "approve", label: "Review plan" }, pull_request_url: null, created_at: "2026-11-04T10:00:00Z" },
      { run_id: "wfr_pr", title: "Add tests", workflow: "Minor Feature", workflow_key: "minor_feature",
        status: "running", state: "Pull request ready", tone: "green",
        action: { kind: "pr", label: "Open PR", url: "https://github.com/acme/order-service/pull/7" },
        pull_request_url: "https://github.com/acme/order-service/pull/7", created_at: "2026-11-03T10:00:00Z" },
    ],
    sample: { template: "hello-world", name: "Hello World", stack: "node", workflow_key: "minor_feature" },
    ...extra,
  };
}

const READINESS = {
  ready: false,
  checked: [{ key: "deployment_target", label: "Somewhere to deploy", fix: "Add a host.", area: "infra", ok: false,
              detail: "No host", needed_by: [{ step_id: "deploy", label: "Deploy" }], first_step_label: "Deploy",
              after_a_person: true }],
  missing: [{ key: "deployment_target", label: "Somewhere to deploy", fix: "Add a host.", area: "infra", ok: false,
              detail: "No host", needed_by: [{ step_id: "deploy", label: "Deploy" }], first_step_label: "Deploy",
              after_a_person: true }],
};

describe("the URL opens the flow at a choice", () => {
  it("reads ?new_project= and makes the Welcome screen's links", () => {
    expect(stepFromParam(null)).toBeNull();
    expect(stepFromParam("1")).toBe("choose");
    expect(["sample", "import", "idea"].map(stepFromParam)).toEqual(["sample", "import", "idea"]);
    expect([newProjectHref("sample"), newProjectHref("import"), newProjectHref("idea"), newProjectHref()])
      .toEqual(["/?new_project=sample", "/?new_project=import", "/?new_project=idea", "/?new_project=1"]);
  });

  it("the project menu opens Import a repo from /?new_project=import, and closing drops the param", async () => {
    window.history.replaceState(null, "", "/?org=org_1&new_project=import");
    stubFetch((c) => (c.path === "/v1/github/repos" ? { body: registryFixture() } : undefined));
    await mountAs(<ProjectMenu projects={[{ id: "p1", name: "Acme Shop" }]} activeId="p1" homeId="p1" orgId="org_1" />);
    expect(tid("start-project")!.getAttribute("data-step")).toBe("import");
    expect(tid("import-repo")).not.toBeNull();
    await click(tid("start-close"));
    expect(tid("start-project")).toBeNull();
    expect(new URL(window.location.href).searchParams.has("new_project")).toBe(false);
    expect(new URL(window.location.href).searchParams.get("org")).toBe("org_1");
  });

  it("an in-console link to /?new_project=sample opens the sample", async () => {
    window.history.replaceState(null, "", "/?org=org_1");
    stubFetch(() => undefined);
    await mountAs(<ProjectMenu projects={[{ id: "p1", name: "Acme Shop" }]} activeId="p1" homeId="p1" />);
    expect(tid("start-project")).toBeNull();
    window.history.pushState(null, "", "/?org=org_1&new_project=sample");
    await act(async () => { window.dispatchEvent(new PopStateEvent("popstate")); });
    expect(tid("start-project")!.getAttribute("data-step")).toBe("sample");
  });

  it("/?new_project=idea opens one sentence box; the full form is one press away, with Back", async () => {
    window.history.replaceState(null, "", "/?new_project=idea");
    stubFetch(() => undefined);
    await mountAs(<ProjectMenu projects={[{ id: "p1", name: "Acme Shop" }]} activeId="p1" homeId="p1" />);
    expect(tid("start-idea-step")!.querySelector("label span")!.textContent).toBe("Describe it in a sentence");
    expect(tid("new-project-form")).toBeNull();
    await click(tid("idea-form"));
    // Both project types are still offered: an app, or a knowledge graph of an existing codebase.
    expect($$('[data-testid="new-project-type"] input').map((i) => (i as HTMLInputElement).value)).toEqual(["app", "knowledge_graph"]);
    await click(tid("new-project-back"));
    expect(tid("start-idea-step")).not.toBeNull();
  });
});

describe("New app from an idea", () => {
  it("names it from the sentence, makes the project and starts its new-app workflow with the sentence", async () => {
    const calls = stubFetch((c) => {
      if (c.method === "POST" && c.path === "/v1/projects") return { status: 201, body: { id: "shift-swap-board", name: "Shift-swap board" } };
      if (c.method === "POST" && c.path === "/api/delivery-workflows") return { body: { run: { id: "wfr_1" } } };
      return undefined;
    });
    const go = vi.fn();
    await mountAs(<StartProjectSheet step="idea" onStep={() => {}} onClose={() => {}} projectId="p1" homeId="p1" go={go} />);
    expect((tid("idea-go") as HTMLButtonElement).disabled).toBe(true);
    const sentence = "Build a shift-swap board where staff offer shifts and a manager approves";
    await type(tid("idea-text"), sentence);
    expect((tid("idea-name") as HTMLInputElement).value).toBe("Shift-swap board");
    await click(tid("idea-go"));
    expect(calls.find((c) => c.path === "/v1/projects")!.body).toEqual({ name: "Shift-swap board", description: sentence });
    expect(calls.find((c) => c.path === "/api/delivery-workflows")!.body).toEqual({
      action: "start", project_id: "shift-swap-board", work_class: "project.new",
      input: { directive: sentence, title: "Shift-swap board" } });
    expect(new URLSearchParams(String(go.mock.calls[0][0]).split("?")[1]).get("project_id")).toBe("shift-swap-board");
  });

  it("a name typed by the person is kept; refused for what is missing, Start anyway reuses the project", async () => {
    let starts = 0;
    const calls = stubFetch((c) => {
      if (c.method === "POST" && c.path === "/v1/projects") return { status: 201, body: { id: "rota", name: "Rota" } };
      if (c.method === "POST" && c.path === "/api/delivery-workflows") {
        starts += 1;
        return starts === 1 ? { status: 409, body: { error: "Not ready here yet", readiness: READINESS } } : { body: { run: { id: "wfr_1" } } };
      }
      return undefined;
    });
    const go = vi.fn();
    await mountAs(<StartProjectSheet step="idea" onStep={() => {}} onClose={() => {}} projectId="p1" homeId="p1" go={go} />);
    await type(tid("idea-text"), "A fair on-call rota for a team of six");
    await type(tid("idea-name"), "Rota");
    await type(tid("idea-text"), "A fair on-call rota for a team of six people");
    expect((tid("idea-name") as HTMLInputElement).value).toBe("Rota");
    await click(tid("idea-go"));
    expect(tid("start-idea-readiness")).not.toBeNull();
    await click(tid("dw-readiness-start-anyway"));
    await click(tid("idea-go"));
    expect(calls.filter((c) => c.path === "/v1/projects").length).toBe(1);
    expect((calls.filter((c) => c.path === "/api/delivery-workflows")[1].body as { start_anyway: string[] }).start_anyway).toEqual(["deployment_target"]);
    expect(go).toHaveBeenCalledTimes(1);
  });

  it("derives a short name from a sentence", () => {
    expect(nameFromIdea("Build an incident timeline from alert logs.")).toBe("Incident timeline");
    expect(nameFromIdea("A tool that tracks expiring access")).toBe("Tool");
    expect(nameFromIdea("")).toBe("");
  });
});

describe("screen 1 — New project", () => {
  it("offers three doors, each with its tag and time, and the presets with See all", async () => {
    stubFetch(() => undefined);
    await mountAs(<StartProjectSheet step="choose" onStep={() => {}} onClose={() => {}} projectId="p1" homeId="p1" />);
    const doors = ["sample", "import", "idea"].map((k) => tid(`start-${k}`)!);
    expect(doors.map((d) => d.querySelector(".spj-tag")!.textContent)).toEqual(["8 min", "2 min", "1 min"]);
    expect(doors.map((d) => d.querySelector("b")!.textContent)).toEqual(["Try a sample", "Import a repo", "New app from an idea"]);
    expect(doors.map((d) => d.querySelector(".spj-cta")!.textContent)).toEqual(["Start", "Choose repo", "Describe it"]);
    expect(doors[0].getAttribute("data-highlight")).toBe("true");
    const presets = $$('[data-testid="start-presets"] .spj-preset:not(.spj-preset-all)');
    expect(presets.map((p) => p.textContent)).toContain("Bug triage boardWeb app");
    for (const kg of ["AML alert investigation", "Regulatory change", "Data quality RCA"]) {
      expect(presets.map((p) => p.textContent)).toContain(`${kg}Banking, knowledge graph`);
    }
    expect(tid("start-presets-all")!.textContent).toBe("See all 19");
    await click(tid("start-presets-all"));
    expect($$('[data-testid="start-presets"] .spj-preset').length).toBe(19);
  });

  it("a door moves to its screen", async () => {
    stubFetch(() => undefined);
    const onStep = vi.fn();
    await mountAs(<StartProjectSheet step="choose" onStep={onStep} onClose={() => {}} projectId="p1" homeId="p1" />);
    await click(tid("start-import"));
    await click(tid("start-idea"));
    await click(tid("start-sample"));
    expect(onStep.mock.calls.map((c) => c[0])).toEqual(["import", "idea", "sample"]);
  });

  it("a banking preset makes a knowledge-graph project and opens its Documents", async () => {
    const calls = stubFetch((c) => (c.method === "POST" && c.path === "/v1/projects"
      ? { status: 201, body: { id: "aml-alert-investigation", name: "AML alert investigation", project_type: "knowledge_graph" } }
      : undefined));
    const go = vi.fn();
    await mountAs(<StartProjectSheet step="choose" onStep={() => {}} onClose={() => {}} projectId="p1" homeId="p1" go={go} />);
    await click(tid("preset-aml"));
    expect(calls.find((c) => c.path === "/v1/projects")!.body).toEqual({ name: "AML alert investigation", project_type: "knowledge_graph" });
    const href = new URLSearchParams(String(go.mock.calls[0][0]).split("?")[1]);
    expect([href.get("project_id"), href.get("area"), href.get("kg_tab")]).toEqual(["aml-alert-investigation", "knowledge", "documents"]);
  });

  it("an app preset whose name is taken makes the next one and opens its page with the brief", async () => {
    const calls = stubFetch((c) => {
      if (c.method !== "POST" || c.path !== "/v1/projects") return undefined;
      const name = (c.body as { name: string }).name;
      return name === "Bug triage board"
        ? { status: 409, body: { detail: { error: "project_exists", slug: "bug-triage-board", project_id: "bug-triage-board" } } }
        : { status: 201, body: { id: "bug-triage-board-2", name } };
    });
    const go = vi.fn();
    await mountAs(<StartProjectSheet step="choose" onStep={() => {}} onClose={() => {}} projectId="p1" homeId="p1" go={go} />);
    await click(tid("preset-bug-triage-board"));
    expect(calls.filter((c) => c.path === "/v1/projects").map((c) => (c.body as { name: string }).name))
      .toEqual(["Bug triage board", "Bug triage board 2"]);
    const href = new URLSearchParams(String(go.mock.calls[0][0]).split("?")[1]);
    expect([href.get("project_id"), href.get("area"), href.get("start_preset")]).toEqual(["bug-triage-board-2", "overview", "bug-triage-board"]);
  });
});

describe("the sample", () => {
  it("makes Hello World and applies the hello-world template as its first app, then opens it", async () => {
    const calls = stubFetch((c) => {
      if (c.method === "POST" && c.path === "/v1/projects") return { status: 201, body: { id: "hello-world", name: "Hello World" } };
      if (c.method === "POST" && c.path === "/v1/templates/hello-world/apply") {
        return { status: 201, body: { workflow_run_id: "wfr_1", app_key: "hello-world", project_id: "hello-world" } };
      }
      return undefined;
    });
    const go = vi.fn();
    await mountAs(<StartProjectSheet step="sample" onStep={() => {}} onClose={() => {}} projectId="p1" homeId="p1" go={go} />);
    expect($$(".spj-flow li b").map((b) => b.textContent)).toEqual(["Plan", "Build", "Review", "Pull request", "Preview"]);
    await click(tid("start-sample-go"));
    expect(calls.find((c) => c.path === "/v1/projects")!.body).toEqual({ name: "Hello World" });
    expect(calls.find((c) => c.path.endsWith("/apply"))!.body).toEqual({
      project_id: "hello-world", new_app: { name: "Hello World" }, stack: "node", workflow_key: "minor_feature",
      dedupe_key: "sample:hello-world" });
    expect(new URLSearchParams(String(go.mock.calls[0][0]).split("?")[1]).get("project_id")).toBe("hello-world");
  });

  it("refused for what the install lacks: the checklist, then Start anyway sends it once more", async () => {
    let applies = 0;
    const calls = stubFetch((c) => {
      if (c.method === "POST" && c.path === "/v1/projects") return { status: 201, body: { id: "hello-world", name: "Hello World" } };
      if (c.path === "/v1/templates/hello-world/apply") {
        applies += 1;
        return applies === 1 ? { status: 409, body: { detail: { message: "Not ready", readiness: READINESS } } }
          : { status: 201, body: { workflow_run_id: "wfr_1" } };
      }
      return undefined;
    });
    const go = vi.fn();
    await mountAs(<StartProjectSheet step="sample" onStep={() => {}} onClose={() => {}} projectId="p1" homeId="p1" go={go} />);
    await click(tid("start-sample-go"));
    expect(tid("start-sample-readiness")).not.toBeNull();
    expect(go).not.toHaveBeenCalled();
    expect(tid("start-sample-go")!.textContent).toBe("Start anyway");
    await click(tid("dw-readiness-start-anyway"));
    await click(tid("start-sample-go"));
    // One project, two applies: the second says what it starts without.
    expect(calls.filter((c) => c.path === "/v1/projects").length).toBe(1);
    expect((calls.filter((c) => c.path.endsWith("/apply"))[1].body as { start_anyway: string[] }).start_anyway).toEqual(["deployment_target"]);
    expect(go).toHaveBeenCalledTimes(1);
  });
});

describe("screen 2 — Import a repo", () => {
  function api() {
    const reg = registryFixture();
    reg.items.push({ ...reg.items[0], id: "ghr_billing", full_name: "globex/billing", language: "Python" });
    return stubFetch((c) => {
      if (c.path === "/v1/github/repos") return { body: reg };
      if (c.path === "/v1/github/repos/ghr_api/facts") return { body: FACTS };
      if (c.method === "POST" && c.path === "/v1/projects") return { status: 201, body: { id: "orders", name: (c.body as { name: string }).name } };
      if (c.method === "PUT" && c.path === "/v1/projects/orders/repos/ghr_api") {
        return { body: { project_id: "orders", repo_mode: "single", items: [{ binding_id: "b1", full_name: "acme/api" }], addable: [] } };
      }
      return undefined;
    });
  }

  it("picks an account's allowed repo, reads it, names it, and creates the project with the repo", async () => {
    const calls = api();
    const go = vi.fn();
    await mountAs(<StartProjectSheet step="import" onStep={() => {}} onClose={() => {}} projectId="p1" homeId="p1" go={go} />);
    // Every account the registry reaches; allowed, live repos only (not mobile, not the archived one).
    expect($$('[data-testid="import-account"] option').map((o) => o.textContent)).toEqual(["acme", "globex"]);
    expect($$('[data-testid="import-repos"] .spj-repo .spj-mono').map((r) => r.textContent)).toEqual(["api", "contracts", "infra", "web"]);
    expect(tid("import-add-account")!.textContent).toBe("Repo not listed? Add a GitHub account");
    expect(new URLSearchParams(tid("import-add-account")!.getAttribute("href")!.split("?")[1]).get("area")).toBe("setup");
    expect((tid("import-create") as HTMLButtonElement).disabled).toBe(true);

    await click(tid("import-repo-ghr_api"));
    expect(tid("import-repo-ghr_api")!.getAttribute("aria-selected")).toBe("true");
    expect($$('[data-testid="import-facts"] div').map((d) => d.textContent)).toEqual([
      "LanguageJava · Spring", "Build & testGradle ✓", "Runs asWeb API", "Branchdevelop"]);
    expect(tid("import-mapping")!.textContent).toContain("Mapping starts when you create it");
    expect((tid("import-name") as HTMLInputElement).value).toBe("Api");

    await type(tid("import-name"), "Orders");
    await click(tid("import-create"));
    expect(calls.find((c) => c.method === "POST" && c.path === "/v1/projects")!.body).toEqual({ name: "Orders" });
    expect(calls.some((c) => c.method === "PUT" && c.path === "/v1/projects/orders/repos/ghr_api")).toBe(true);
    const href = new URLSearchParams(String(go.mock.calls[0][0]).split("?")[1]);
    expect([href.get("project_id"), href.get("area")]).toEqual(["orders", "project"]);
  });

  it("another account lists its own repos; Map business rules too makes a knowledge-graph project", async () => {
    const calls = api();
    await mountAs(<StartProjectSheet step="import" onStep={() => {}} onClose={() => {}} projectId="p1" homeId="p1" go={() => {}} />);
    await choose(tid("import-account"), "globex");
    expect($$('[data-testid="import-repos"] .spj-repo .spj-mono').map((r) => r.textContent)).toEqual(["billing"]);
    await choose(tid("import-account"), "acme");
    await click(tid("import-repo-ghr_api"));
    await click(tid("import-rules"));
    await click(tid("import-create"));
    expect(calls.find((c) => c.method === "POST" && c.path === "/v1/projects")!.body).toEqual({ name: "Api", project_type: "knowledge_graph" });
  });

  it("names its accounts from the registry, the connection's first", () => {
    expect(accountsOf(registryFixture())).toEqual(["acme"]);
    expect(accountsOf(null)).toEqual([]);
  });
});

describe("screen 3 — What should we add?", () => {
  function api(o: { refuse?: boolean } = {}) {
    let starts = 0;
    return stubFetch((c) => {
      if (c.path === "/v1/projects/order-service/start") return { body: view() };
      if (c.method === "POST" && c.path === "/api/delivery-workflows") {
        starts += 1;
        if (o.refuse && starts === 1) return { status: 409, body: { error: "Not ready here yet", readiness: READINESS } };
        return { body: { run: { id: "wfr_new" } } };
      }
      return undefined;
    });
  }

  it("shows the repo, the branch and the mapped code; chips fill the box; Start runs the project's feature workflow on the repo", async () => {
    const calls = api();
    await mountAs(<ProjectStartCard projectId="order-service" orgId="org_1" />);
    expect(tid("project-start")!.querySelector("h2")!.textContent).toBe("Order service");
    expect(tid("project-start-repo")!.textContent).toBe("acme/order-service, develop, code mapped ✓");
    expect(tid("project-start-ask")!.querySelector("label")!.textContent).toBe("What should we add?");
    expect($$('[data-testid="project-start-chips"] button').map((b) => b.textContent))
      .toEqual(["Add an API endpoint", "Fix a bug", "Add tests", "Upgrade a library"]);
    expect(tid("project-start-estimate")!.textContent).toBe("Estimate: 45–110 min to a pull request · ~$3 or included in subscription");
    expect((tid("project-start-go") as HTMLButtonElement).disabled).toBe(true);
    await click($$('[data-testid="project-start-chips"] button')[2]);
    expect((tid("project-start-text") as HTMLTextAreaElement).value).toBe("Add tests");
    await click(tid("project-start-go"));
    const start = calls.find((c) => c.method === "POST" && c.path === "/api/delivery-workflows")!;
    expect(start.body).toEqual({ action: "start", project_id: "order-service", workflow_key: "minor_feature",
                                 input: { directive: "Add tests", title: "Add tests", repository: "acme/order-service" } });
    expect(tid("project-start-started")).not.toBeNull();
  });

  it("says what happens next, and each piece of work with its state and one action", async () => {
    api();
    await mountAs(<ProjectStartCard projectId="order-service" orgId="org_1" />);
    expect($$('[data-testid="project-start-next"] li').map((l) => l.textContent))
      .toEqual(["PlanYou approve", "BuildBOSS", "Review & testBOSS", "Pull requestYou merge", "PreviewBOSS"]);
    expect($$('[data-testid="project-start-next"] li[data-you]').length).toBe(2);
    const plan = tid("project-start-work-wfr_plan")!;
    expect(plan.getAttribute("data-tone")).toBe("amber");
    expect(plan.querySelector(".spj-work-state")!.textContent).toBe("Waiting for your plan approval");
    const review = plan.querySelector("a")!;
    expect(review.textContent).toBe("Review plan");
    expect(new URLSearchParams(review.getAttribute("href")!.split("?")[1]).get("wfr")).toBe("wfr_plan");
    const pr = tid("project-start-work-wfr_pr")!.querySelector("a")!;
    expect([pr.textContent, pr.getAttribute("href"), pr.getAttribute("target")])
      .toEqual(["Open PR", "https://github.com/acme/order-service/pull/7", "_blank"]);
  });

  it("refused for what the project lacks: the checklist, and Start anyway", async () => {
    const calls = api({ refuse: true });
    await mountAs(<ProjectStartCard projectId="order-service" />);
    await type(tid("project-start-text"), "Add an endpoint that lists orders by customer");
    await click(tid("project-start-go"));
    expect(tid("project-start-readiness")).not.toBeNull();
    expect((tid("project-start-go") as HTMLButtonElement).disabled).toBe(true);
    await click(tid("dw-readiness-start-anyway"));
    await click(tid("project-start-go"));
    const starts = calls.filter((c) => c.method === "POST" && c.path === "/api/delivery-workflows");
    expect(starts.length).toBe(2);
    expect((starts[1].body as { start_anyway: string[] }).start_anyway).toEqual(["deployment_target"]);
  });

  it("a project with no repo builds a new app; its box starts empty", async () => {
    stubFetch((c) => (c.path === "/v1/projects/idea/start" ? { body: view({
      project_id: "idea", name: "Shift swap", description: "A shift-swap board for a small team", repo: null, work: [],
      start: { mode: "new_app", work_class: "project.new", workflow_key: null, workflow_name: "Standard Product Delivery", minutes: 60, cost: null } }) } : undefined));
    await mountAs(<ProjectStartCard projectId="idea" />);
    expect(tid("project-start-ask")!.querySelector("label")!.textContent).toBe("What should we build?");
    // The description is not typed for the person: the box is empty and Start waits for words.
    expect((tid("project-start-text") as HTMLTextAreaElement).value).toBe("");
    expect((tid("project-start-text") as HTMLTextAreaElement).placeholder).toMatch(/^e\.g\. /);
    expect((tid("project-start-go") as HTMLButtonElement).disabled).toBe(true);
    expect(tid("project-start-chips")).toBeNull();
    expect(tid("project-start-estimate")!.textContent).toBe("Estimate: ~60 min · included in subscription or pay per use");
  });

  it("builds the start door's body and the estimate line from the view", () => {
    expect(startBody(view(), "Fix a bug.\nIt crashes on empty carts")).toEqual({
      project_id: "order-service", workflow_key: "minor_feature",
      input: { directive: "Fix a bug.\nIt crashes on empty carts", title: "Fix a bug", repository: "acme/order-service" } });
    expect(startBody(view({ repo: null, start: { mode: "new_app", work_class: "project.new", workflow_key: null } }), "A board"))
      .toEqual({ project_id: "order-service", work_class: "project.new", input: { directive: "A board", title: "A board" } });
    expect(estimateLine({ mode: "feature", work_class: null, workflow_key: "x", minutes: null, cost: { runs: 1, low_usd: 0.2, high_usd: 0.4 } }))
      .toBe("Estimate: ~$0.30 or included in subscription");
  });
});

describe("one task box per project page (walk 806)", () => {
  it("the Command Center drops its own task box where the project page's card is", async () => {
    const { readFileSync } = await import("node:fs");
    const page = readFileSync("app/page.tsx", "utf8");
    const cc = readFileSync("app/command-center-ui.tsx", "utf8");
    expect(page).toMatch(/\{showsStartCard \? \(\s*<ProjectStartCard/);
    expect(page).toContain("startCard={showsStartCard}");
    expect(cc).toContain('{startCard && action.kind === "empty" ? null : <div className="cc-next"');
    expect(cc).toContain('open={!startCard && action.kind === "empty"}');
  });
});
