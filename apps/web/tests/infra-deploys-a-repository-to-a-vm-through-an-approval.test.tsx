// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DeploysTab } from "../app/infra/deploys";
import { StackDeploymentView } from "../app/stack-deploy-ui";
import { mockInfraFetch, type Route } from "./_helpers/infra-fixtures";
import { TabHarness } from "./_helpers/infra-tab-render";
import {
  canHostStack, deployFormProblem, deploymentActions, isStackTarget, targetFormProblem,
  targetManifestPath, targetPublicUrl, targetRepository, type StackDeployment,
} from "../lib/stack-deploy";

/**
 * Infra deploys a repository to a VM through an approval — in the console, with no custom steps.
 *
 * The operator's flow (2026-10-03): add a VM in Infra, authorise BOSS's key on it, verify; register
 * a repository; then deploy that repository at a ref onto the VM and watch it — "enable BOSS to
 * deploy in the VM from the repo". These pin each state of that flow as the operator sees it:
 * which targets and deployments are listed, what a plan shows (steps, env NAMES, health), which
 * buttons each status offers, that Approve/Reject go through the decisions API with the record's
 * decision_id, that a moving deployment is followed until it settles, and that a failure says
 * what happened and what to do.
 */

type Call = { url: string; method: string; body?: Record<string, unknown> };

let host: HTMLDivElement;
let root: Root;
let calls: Call[];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const VM = {
  id: "host_1", name: "boss-vm", address: "203.0.113.7", ssh_user: "ubuntu", ssh_port: 22,
  status: "verified",
};
const UNREACHED = { ...VM, id: "host_2", name: "fresh", status: "registered", last_preflight: null };

const TARGET = {
  id: "deptgt_1", app_name: "boss", environment: "production", repo: "seanventures/boss",
  host_id: "host_1", target_kind: "stack", target_ref: "ubuntu@203.0.113.7:22", status: "active",
  manifest_path: "boss.deploy.yaml", public_url: "https://boss.example.com",
  required_resources: [{ kind: "repo_owner", name: "seanventures" }],
};
const PER_APP_TARGET = { id: "deptgt_2", app_name: "todo", environment: "staging", repo: "todo",
  target_kind: "ssh", status: "active", required_resources: [] };

function deployment(over: Partial<StackDeployment> = {}): StackDeployment {
  return {
    id: "sdep_1", project_id: "project_1", target_id: "deptgt_1",
    repository: "seanventures/boss", ref: "main", commit: "0123456789abcdef0123",
    manifest: {
      name: "boss", kind: "commands",
      steps: ["install: ./scripts/install/install.sh --yes", { name: "migrate", command: "make migrate" }],
      env_keys: ["POSTGRES_PASSWORD", "BOSS_PUBLIC_URL"],
      health: [{ url: "http://127.0.0.1:8000/health", expect: 200, timeout_seconds: 600 }],
    },
    status: "awaiting_approval", decision_id: "dec_1", changeset_id: "cs_1",
    steps: [], log_tail: null, open_url: null, error: null,
    created_at: "2026-10-03T10:00:00Z", updated_at: "2026-10-03T10:00:00Z",
    // What the API offers a plan waiting on its decision; every other record says its own.
    actions: over.status && over.status !== "awaiting_approval" ? [] : ["approve", "reject"],
    ...over,
  };
}

/**
 * What POST /v1/deployments/stack/targets STORES and returns, mirrored from
 * engine/boss_engine/services/stack_deploy.py `create_stack_target`: the canonical target row,
 * `target_kind: "stack"`, `repo` as owner/name, `manifest_path` and `public_url` columns, and the
 * address derived from the host. The list then answers with what was stored — never with the
 * console's own request echoed back.
 */
function storedTarget(body: Record<string, unknown>, hosts: Array<typeof VM>) {
  const repository = String(body.repository);
  const h = hosts.find((x) => x.id === body.host_id)!;
  return {
    id: `deptgt_${String(body.app_name)}`, project_id: body.project_id, app_name: body.app_name,
    environment: body.environment, repo: repository, target_kind: "stack", status: "active",
    host_id: h.id, target_ref: `${h.ssh_user}@${h.address}:${h.ssh_port}`,
    manifest_path: body.manifest_path ?? null, public_url: body.public_url ?? null,
    required_resources: [{ kind: "repo_owner", name: repository.split("/")[0] }],
  };
}

type Script = {
  deployments?: StackDeployment[];
  hosts?: unknown[];
  targets?: unknown[];
  repositories?: unknown[];
  /** Successive answers to GET /v1/deployments/stack/{id}. The last one repeats. */
  reads?: StackDeployment[];
  /** Hold each answer until the test calls `nextRead()` (the first is answered at once). Without
   *  it a 5 ms poll under a busy combined run passed `running` before the test looked, and the
   *  test failed with "running did not happen". */
  held?: boolean;
  plan?: { status: number; body: unknown };
};

let nextRead: () => void = () => {};

function installFetch(script: Script = {}) {
  calls = [];
  const reads = [...(script.reads ?? [])];
  let cursor = 0;
  nextRead = () => { cursor += 1; };
  const hosts = (script.hosts ?? [VM, UNREACHED]) as Array<typeof VM>;
  const targets = [...(script.targets ?? [TARGET, PER_APP_TARGET])] as Array<Record<string, unknown>>;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : undefined;
    calls.push({ url, method, body });
    if (method === "GET" && url.startsWith("/api/deployment-hosts")) {
      return json({ hosts });
    }
    if (method === "GET" && url.startsWith("/v1/deployment-targets")) {
      return json({ project_id: "project_1", items: targets, limit: 200, offset: 0 });
    }
    if (method === "GET" && url.startsWith("/v1/projects/project_1/repositories")) {
      return json({ repositories: script.repositories ?? [
        { full_name: "seanventures/boss", role: "primary" },
        { full_name: "seanventures/other", role: "secondary" },
      ], suggested: "seanventures/boss" });
    }
    if (method === "GET" && url.startsWith("/v1/deployments/stack?")) {
      return json({ items: script.deployments ?? [] });
    }
    if (method === "GET" && url.startsWith("/v1/deployments/stack/")) {
      const next = reads[Math.min(script.held ? cursor : cursor++, reads.length - 1)];
      return next ? json({ deployment: next }) : json({ detail: "no such deployment" }, 404);
    }
    if (method === "POST" && url === "/v1/deployments/stack/targets") {
      const stored = storedTarget(body ?? {}, hosts);
      targets.push(stored);
      return json(stored, 201);
    }
    if (method === "POST" && url === "/v1/deployments/stack/plan") {
      const plan = script.plan ?? { status: 200, body: { deployment: deployment() } };
      return json(plan.body, plan.status);
    }
    if (method === "POST" && /\/v1\/decisions\/[^/]+\/(approve|reject)$/.test(url)) {
      return json({ id: "dec_1", status: url.endsWith("approve") ? "approved" : "rejected" });
    }
    if (method === "POST" && url.endsWith("/rollback")) {
      return json({ deployment: deployment({ id: "sdep_2", ref: "0000aaaa", commit: "0000aaaa1111" }) });
    }
    return json({ detail: `unexpected ${method} ${url}` }, 500);
  }));
}

async function settle(until: () => boolean, what: string) {
  // A deadline, not a count of ticks: forty zero-delay ticks are a few milliseconds alone and
  // far less than a busy combined run needs for one poll to land.
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) {
    if (until()) return;
    await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  }
  throw new Error(`${what} did not happen: ${host.textContent}`);
}

function button(label: string, scope: ParentNode = host): HTMLButtonElement {
  const found = [...scope.querySelectorAll("button")].find((el) => el.textContent?.trim() === label);
  if (!found) throw new Error(`missing button ${label}: ${host.textContent}`);
  return found as HTMLButtonElement;
}

function buttons(scope: ParentNode = host): string[] {
  return [...scope.querySelectorAll("button")].map((b) => b.textContent?.trim() ?? "");
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("a deployment offers exactly the buttons the API names in `actions`", () => {
  it("renders the API's actions and decides none itself", () => {
    expect(deploymentActions({ actions: ["approve", "reject"] })).toEqual(["approve", "reject"]);
    expect(deploymentActions({ actions: ["rollback", "open"] })).toEqual(["rollback", "open"]);
  });
  it("a record without actions offers nothing, whatever its status", () => {
    expect(deploymentActions({ actions: [] })).toEqual([]);
    expect(deploymentActions({})).toEqual([]);
    expect(deploymentActions({ actions: null })).toEqual([]);
  });
  it("ignores an action it does not know how to render", () => {
    expect(deploymentActions({ actions: ["approve", "teardown"] })).toEqual(["approve"]);
  });
  it("an awaiting record with no approve in actions shows no Approve", async () => {
    installFetch();
    await act(async () => {
      root.render(<StackDeploymentView deploymentId="sdep_1" actor="ceo"
        initial={deployment({ status: "awaiting_approval", actions: [] })} />);
    });
    expect(buttons()).not.toContain("Approve");
    expect(buttons()).not.toContain("Reject");
  });
});

describe("a stack target is read from the shape the API stores", () => {
  it("the canonical target row: target_kind, manifest_path, public_url", () => {
    expect(isStackTarget(TARGET)).toBe(true);
    expect(isStackTarget(PER_APP_TARGET)).toBe(false);
    expect(targetManifestPath(TARGET)).toBe("boss.deploy.yaml");
    expect(targetManifestPath({ id: "a", app_name: "a", environment: "p" })).toBe("boss.deploy.yaml");
    expect(targetManifestPath({ ...TARGET, manifest_path: null })).toBe("boss.deploy.yaml");
    // The console's old required_resources spelling is not a stack target any more.
    expect(isStackTarget({ id: "x", app_name: "x", environment: "p", target_kind: "ssh",
      required_resources: [{ kind: "deploy_kind", name: "stack" }] })).toBe(false);
    expect(targetPublicUrl(TARGET)).toBe("https://boss.example.com");
    expect(targetRepository(TARGET)).toBe("seanventures/boss");
  });
  it("a VM can take a stack once BOSS has reached it, docker or not", () => {
    expect(canHostStack(VM)).toBe(true);
    expect(canHostStack({ ...VM, status: "registered", last_preflight: { reachable: true } })).toBe(true);
    expect(canHostStack(UNREACHED)).toBe(false);
    expect(canHostStack({ ...VM, status: "retired" })).toBe(false);
  });
});

describe("the forms say what is wrong before anything is sent", () => {
  const ok = { hostId: "host_1", name: "boss", environment: "production", repository: "o/r",
               publicUrl: "", manifestPath: "boss.deploy.yaml" };
  it("target form", () => {
    expect(targetFormProblem(ok)).toBeNull();
    expect(targetFormProblem({ ...ok, hostId: "" })).toMatch(/Choose the VM/);
    expect(targetFormProblem({ ...ok, name: "Boss App" })).toMatch(/lowercase/);
    expect(targetFormProblem({ ...ok, publicUrl: "boss.example.com" })).toMatch(/http/);
    expect(targetFormProblem({ ...ok, manifestPath: "../etc/passwd" })).toMatch(/inside the repository/);
    expect(targetFormProblem({ ...ok, repository: "" })).toMatch(/Repo and model/);
  });
  it("deploy form", () => {
    expect(deployFormProblem({ repository: "o/r", ref: "main", targetId: "t" })).toBeNull();
    expect(deployFormProblem({ repository: "o/r", ref: "two words", targetId: "t" })).toMatch(/one ref/);
    expect(deployFormProblem({ repository: "o/r", ref: "main", targetId: "" })).toMatch(/stack target/);
  });
});

/**
 * The same journey on Infra › Deploys, the one place a deploy is planned now (spec §4.6): `+
 * Deploy` (Repository, Ref, Target → `Plan`, the routed POST /v1/infra/targets/{id}/deploy),
 * the toast `Deploy planned`, the new deploy's drawer with its plan, and Approve through the
 * decisions API. Stack targets are added in Fleet's `App target` sheet.
 */
describe("Infra › Deploys: + Deploy → plan → approve in the drawer", () => {
  let infra: { writes: () => { path: string; body: unknown }[]; gets: (re: RegExp) => unknown[] } | null = null;
  let uninstall: (() => void) | null = null;
  afterEach(() => { uninstall?.(); uninstall = null; infra = null; });

  function setValue(el: Element | null, value: string) {
    if (!el) throw new Error("missing field");
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
      : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  const q = (id: string) => document.querySelector<HTMLElement>(`[data-testid="${id}"]`);

  async function openDeploys(routes: Route[] = [], search = "?area=infra&view=deploys") {
    const f = mockInfraFetch({ routes });
    infra = f;
    uninstall = f.install();
    window.history.replaceState(null, "", `/${search}`);
    await act(async () => { root.render(<TabHarness Tab={DeploysTab} />); });
    await settle(() => !!q("inf-deploys") && document.querySelectorAll('[data-testid="inf-deploy-row"]').length > 0, "deploys");
    return f;
  }
  async function openSheet() {
    await act(async () => { q("inf-deploy-new")!.click(); });
    await settle(() => (q("inf-deploy-repo") as HTMLSelectElement | null)?.value === "acme/invoice"
      && (q("inf-deploy-target") as HTMLSelectElement | null)?.value === "t_live", "sheet loaded");
  }

  it("plans the chosen ref onto the chosen target through the routed deploy, then opens its plan", async () => {
    const f = await openDeploys([{ method: "POST", match: /^\/v1\/infra\/targets\/[^/]+\/deploy$/, reply: () => ({ ref: "stack:sd_wait" }) }]);
    const toasts: string[] = [];
    const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
    window.addEventListener("boss:toast", onToast);
    try {
      await openSheet();
      // The repository narrows the targets to its own.
      expect([...(q("inf-deploy-target") as HTMLSelectElement).options].map((o) => o.value)).toEqual(["t_live"]);
      await act(async () => { setValue(q("inf-deploy-target"), "t_live"); setValue(q("inf-deploy-ref"), "feature/deploy"); });
      await act(async () => { q("inf-deploy-plan-btn")!.click(); });
      await settle(() => f.writes().some((w) => w.path === "/v1/infra/targets/t_live/deploy"), "plan");
      expect(f.writes().find((w) => w.path === "/v1/infra/targets/t_live/deploy")?.body)
        .toEqual({ project_id: "project_1", actor: "ceo", ref: "feature/deploy" });
      await settle(() => !!q("inf-deploy-drawer") && !!q("inf-deploy-plan"), "drawer");
      expect(toasts).toContain("Deploy planned");
      expect(new URLSearchParams(window.location.search).get("deploy")).toBe("stack:sd_wait");
      const plan = q("inf-deploy-plan")!;
      expect(plan.textContent).toContain("docker build -t invoice .");
      expect(plan.querySelector('[data-testid="inf-deploy-env-keys"]')?.textContent).toContain("DATABASE_URL");
      expect(q("inf-deploy-rollback-to")).toBeNull();

      await act(async () => { q("inf-deploy-approve")!.click(); });
      await settle(() => f.writes().some((w) => w.path === "/v1/decisions/dec_wait/approve"), "approve");
      await settle(() => toasts.includes("Deploy approved"), "approve toast");
    } finally {
      window.removeEventListener("boss:toast", onToast);
    }
  });

  it("shows the API's refusal of a plan in words", async () => {
    await openDeploys([{ method: "POST", match: /^\/v1\/infra\/targets\/[^/]+\/deploy$/, reply: () => new Response(
      JSON.stringify({ detail: "boss.deploy.yaml at abc123: health[0].url is required" }), { status: 422 }) }]);
    await openSheet();
    await act(async () => { q("inf-deploy-plan-btn")!.click(); });
    await settle(() => !!q("inf-deploy-sheet-error"), "error");
    expect(q("inf-deploy-sheet-error")?.textContent).toBe("boss.deploy.yaml at abc123: health[0].url is required");
    expect(q("inf-deploy-sheet")).not.toBeNull();
  });

  it("More takes cost in dollars and refuses a release packet that is not a JSON object", async () => {
    const f = await openDeploys();
    await openSheet();
    await act(async () => { q("inf-deploy-more")!.click(); });
    await act(async () => { setValue(q("inf-deploy-repo"), "acme/billing"); });
    await settle(() => (q("inf-deploy-target") as HTMLSelectElement).value === "t_cloud", "billing target");
    await act(async () => { setValue(q("inf-deploy-cost"), "1.50"); setValue(q("inf-deploy-packet"), "{\"ci\": "); });
    expect(q("inf-deploy-packet-error")?.textContent).toBeTruthy();
    expect((q("inf-deploy-plan-btn") as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { setValue(q("inf-deploy-packet"), "{\"ci_status\": \"passed\"}"); });
    expect(q("inf-deploy-packet-error")).toBeNull();
    await act(async () => { q("inf-deploy-plan-btn")!.click(); });
    await settle(() => f.writes().some((w) => w.path === "/v1/infra/targets/t_cloud/deploy"), "plan");
    expect(f.writes().find((w) => w.path === "/v1/infra/targets/t_cloud/deploy")?.body).toMatchObject({
      expected_cost_usd: 1.5, release_packet: { ci_status: "passed" } });
  });

  it("a live deploy's drawer: steps, log, Open app, Redeploy of the same commit", async () => {
    const f = await openDeploys([], "?area=infra&view=deploys&deploy=stack%3Asd_live");
    await settle(() => !!q("inf-deploy-log"), "drawer");
    expect(q("inf-deploy-steps")?.querySelectorAll("li")).toHaveLength(6);
    expect(q("inf-deploy-log")?.textContent).toContain("Health check passed");
    expect(q("inf-deploy-open-app")?.getAttribute("href")).toBe("https://invoice.example.com");
    expect(q("inf-deploy-full-log")?.getAttribute("href")).toBe("?area=activity&source=deployment&subject=stack%3Asd_live");
    await act(async () => { q("inf-deploy-redeploy")!.click(); });
    await settle(() => f.writes().some((w) => w.path === "/v1/infra/targets/t_live/deploy"), "redeploy");
    expect(f.writes().find((w) => w.path === "/v1/infra/targets/t_live/deploy")?.body).toMatchObject({ ref: "9f8e7d6" });
  });

  it("a failed deploy's drawer says why in a line and shows the raw error on request", async () => {
    await openDeploys([], "?area=infra&view=deploys&deploy=stack%3Asd_failed");
    await settle(() => !!q("inf-deploy-failed"), "drawer");
    expect(q("inf-deploy-failed")?.textContent).toContain("Migration failed");
    expect(q("inf-deploy-error-raw")).toBeNull();
    await act(async () => { q("inf-deploy-show-error")!.click(); });
    expect(q("inf-deploy-error-raw")?.textContent).toContain("already exists");
    expect(q("inf-deploy-rollback-to")).not.toBeNull();
  });

  it("a registry deploy's drawer: health check, promotion, and its change in Cloud", async () => {
    const f = await openDeploys([], "?area=infra&view=deploys&deploy=registry%3Adr_1");
    await settle(() => !!q("inf-deploy-promotion"), "drawer");
    expect([...q("inf-deploy-promotion")!.querySelectorAll("li")].map((l) => l.textContent))
      .toEqual(["Pipeline verdict", "Staging", "Smoke", "Production", "Rollout"]);
    expect(q("inf-deploy-open-change")?.getAttribute("href")).toBe("?area=infra&view=cloud&cloud=changes&changeset=cs_1");
    await act(async () => { q("inf-deploy-run-check")!.click(); });
    await settle(() => f.writes().some((w) => w.path === "/v1/deployment-runs/dr_1/health-check"), "check");
  });

  it("⋯ holds Rules, CI config and Secrets", async () => {
    const f = await openDeploys();
    await act(async () => { q("inf-deploys-more")!.click(); });
    await act(async () => { q("inf-deploys-rules")!.click(); });
    await settle(() => !!q("inf-rules-list")?.textContent?.includes("Production allowed"), "rules");
    await act(async () => { q("inf-rules-save")!.click(); });
    await settle(() => f.writes().some((w) => w.path === "/v1/deployment-environment-policies"), "save rule");
    await act(async () => { document.querySelector<HTMLButtonElement>('[data-testid="inf-rules-sheet"] .ag-act-close')!.click(); });

    await act(async () => { q("inf-deploys-more")!.click(); });
    await act(async () => { q("inf-deploys-ci")!.click(); });
    await settle(() => !!q("inf-ci-vendor-github_actions") && !!q("inf-ci-runs")?.textContent?.includes("acme/invoice"), "ci");
    expect(q("inf-ci-api")?.textContent).toBe("API reachable");
    await act(async () => { q("inf-ci-generate")!.click(); });
    await settle(() => (q("inf-ci-yaml") as HTMLTextAreaElement).value.includes("jobs:"), "generated");
    await act(async () => { q("inf-ci-validate")!.click(); });
    await settle(() => !!q("inf-ci-verdict")?.textContent?.includes("Valid"), "validated");
    await act(async () => { document.querySelector<HTMLButtonElement>('[data-testid="inf-ci-sheet"] .ag-act-close')!.click(); });

    await act(async () => { q("inf-deploys-more")!.click(); });
    await act(async () => { q("inf-deploys-secrets")!.click(); });
    expect(q("inf-secrets-sheet")).not.toBeNull();
  });
});

describe("a deployment view follows the record", () => {
  async function renderView(initial: StackDeployment, pollMs = 5) {
    await act(async () => {
      root.render(<StackDeploymentView deploymentId={initial.id} actor="ceo" initial={initial} pollMs={pollMs} />);
    });
  }

  it("approves through the decisions API with the record's decision, then follows it to healthy", async () => {
    installFetch({ held: true, reads: [
      deployment({ status: "running", steps: [{ name: "fetch", status: "succeeded" }, { name: "install", status: "running" }],
                   log_tail: "Step 3/9: building api" }),
      deployment({ status: "healthy", open_url: "https://boss.example.com",
                   actions: ["rollback", "open"], rollback_commit: "fedcba9876543210",
                   steps: [{ name: "fetch", status: "succeeded" }, { name: "install", status: "succeeded" },
                           { name: "health", status: "succeeded" }] }),
    ] });
    await renderView(deployment());
    await act(async () => { button("Approve").click(); });
    await settle(() => calls.some((c) => c.url === "/v1/decisions/dec_1/approve"), "approve");
    expect(calls.find((c) => c.url === "/v1/decisions/dec_1/approve")?.body).toMatchObject({ actor: "ceo" });

    await settle(() => host.querySelector('[data-testid="stack-deployment"]')?.getAttribute("data-status") === "running", "running");
    expect(host.querySelector('[data-testid="stack-progress"]')?.textContent).toContain("install");
    expect(host.querySelector('[data-testid="stack-log"]')?.textContent).toContain("building api");
    expect(buttons()).not.toContain("Approve");
    expect(buttons()).not.toContain("Roll back");

    nextRead(); // and then it is healthy
    await settle(() => host.querySelector('[data-testid="stack-deployment"]')?.getAttribute("data-status") === "healthy", "healthy");
    const open = host.querySelector('[data-testid="stack-open"]') as HTMLAnchorElement;
    expect(open.getAttribute("href")).toBe("https://boss.example.com");
    const rollback = host.querySelector('[data-testid="stack-rollback"]') as HTMLButtonElement;
    expect(rollback.textContent).toBe("Roll back to fedcba987654");
    expect(rollback.title).toContain("fedcba987654");

    // Settled: no more reads.
    const reads = calls.filter((c) => c.method === "GET").length;
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    expect(calls.filter((c) => c.method === "GET").length).toBe(reads);
  });

  it("rejects through the decisions API", async () => {
    installFetch({ reads: [deployment({ status: "rejected" })] });
    await renderView(deployment());
    await act(async () => { button("Reject").click(); });
    await settle(() => calls.some((c) => c.url === "/v1/decisions/dec_1/reject"), "reject");
    await settle(() => host.querySelector('[data-testid="stack-deployment"]')?.getAttribute("data-status") === "rejected", "rejected");
    expect(buttons()).not.toContain("Approve");
  });

  it("a failure that never reached the host says what to do and offers no rollback", async () => {
    installFetch();
    await act(async () => {
      root.render(<StackDeploymentView deploymentId="sdep_1" actor="ceo"
        initial={deployment({ status: "failed", actions: [],
          error: { headline: "docker is not installed on boss-vm.", action: "Install docker, then plan again." } })} />);
    });
    expect(host.querySelector('[data-testid="stack-failure"]')?.textContent).toContain("plan again");
    expect(host.querySelector('[data-testid="stack-rollback"]')).toBeNull();
  });

  it("a failure says what happened and what to do, and offers a rollback", async () => {
    installFetch();
    const opened: StackDeployment[] = [];
    await act(async () => {
      root.render(<StackDeploymentView deploymentId="sdep_1" actor="ceo" onOpened={(d) => opened.push(d)}
        initial={deployment({ status: "failed", log_tail: "ERROR: port 443 already in use",
          actions: ["rollback"], rollback_commit: "0000aaaa1111",
          error: { headline: "The health check never answered 200.", action: "Free port 443 on the VM, then deploy again." } })} />);
    });
    const failure = host.querySelector('[data-testid="stack-failure"]')!;
    expect(failure.textContent).toContain("The health check never answered 200.");
    expect(failure.textContent).toContain("Free port 443 on the VM");
    expect(host.querySelector('[data-testid="stack-log"]')?.textContent).toContain("port 443");
    expect(host.querySelector('[data-testid="stack-open"]')).toBeNull();

    await act(async () => { button("Roll back to 0000aaaa1111").click(); });
    await settle(() => opened.length === 1, "rollback opened");
    expect(calls.find((c) => c.url === "/v1/deployments/stack/sdep_1/rollback")?.body).toEqual({ actor: "ceo" });
    expect(opened[0].id).toBe("sdep_2");
  });
});
