// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StackDeployPanel, StackDeploymentView } from "../app/stack-deploy-ui";
import {
  canHostStack, deployFormProblem, isStackTarget, stackActions, targetFormProblem,
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
  id: "deptgt_1", app_name: "boss", environment: "production", repo: "boss", host_id: "host_1",
  target_ref: "ubuntu@203.0.113.7:22", status: "active",
  required_resources: [
    { kind: "repo_owner", name: "seanventures" },
    { kind: "deploy_kind", name: "stack" },
    { kind: "manifest_path", name: "boss.deploy.yaml" },
    { kind: "public_url", name: "https://boss.example.com" },
  ],
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
    ...over,
  };
}

/**
 * What POST /v1/deployment-targets STORES, mirrored from apps/api/app/main.py and
 * engine/boss_engine/services/stack_targets.py (pinned there by
 * apps/api/tests/test_a_stack_target_is_stored_and_pointed_at_its_vm_in_one_write.py): stack facts
 * become `required_resources` entries, an `owner/name` repo is split, and `host_id` derives the
 * address. The list then answers with what was stored — never with the console's own request
 * echoed back, which is how a dropped field once passed here and failed against the real API.
 */
function storedTarget(body: Record<string, unknown>, hosts: Array<typeof VM>) {
  const resources: Array<{ kind: string; name: string }> = [{ kind: "container_runtime", name: "docker" }];
  const [owner, repo] = String(body.repo).includes("/") ? String(body.repo).split("/") : ["", String(body.repo)];
  if (owner) resources.push({ kind: "repo_owner", name: owner });
  for (const kind of ["deploy_kind", "manifest_path", "public_url"]) {
    const v = body[kind];
    if (typeof v === "string" && v.trim()) resources.push({ kind, name: v.trim() });
  }
  const h = hosts.find((x) => x.id === body.host_id);
  return {
    id: `deptgt_${String(body.app_name)}`, project_id: body.project_id, app_name: body.app_name,
    environment: body.environment, repo, target_kind: "ssh", status: "active",
    host_id: h ? h.id : null,
    target_ref: h ? `${h.ssh_user}@${h.address}:${h.ssh_port}` : body.target_ref,
    required_resources: resources,
  };
}

type Script = {
  deployments?: StackDeployment[];
  hosts?: unknown[];
  targets?: unknown[];
  repositories?: unknown[];
  /** Successive answers to GET /v1/deployments/stack/{id}. The last one repeats. */
  reads?: StackDeployment[];
  plan?: { status: number; body: unknown };
};

function installFetch(script: Script = {}) {
  calls = [];
  const reads = [...(script.reads ?? [])];
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
      const next = reads.length > 1 ? reads.shift()! : reads[0];
      return next ? json({ deployment: next }) : json({ detail: "no such deployment" }, 404);
    }
    if (method === "POST" && url === "/v1/deployment-targets") {
      const stored = storedTarget(body ?? {}, hosts);
      targets.push(stored);
      return json(stored, 201);
    }
    if (method === "POST" && url.endsWith("/attach")) {
      const hostId = url.split("/").at(-2);
      const t = targets.find((x) => x.id === body?.target_id);
      if (t) t.host_id = hostId;
      return json({ target_id: body?.target_id, host_id: hostId });
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
  for (let i = 0; i < 40; i += 1) {
    if (until()) return;
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
  throw new Error(`${what} did not happen: ${host.textContent}`);
}

async function renderPanel() {
  await act(async () => { root.render(<StackDeployPanel projectId="project_1" actor="ceo" />); });
  await settle(() => !host.textContent?.includes("Loading stack targets"), "panel load");
}

function button(label: string, scope: ParentNode = host): HTMLButtonElement {
  const found = [...scope.querySelectorAll("button")].find((el) => el.textContent?.trim() === label);
  if (!found) throw new Error(`missing button ${label}: ${host.textContent}`);
  return found as HTMLButtonElement;
}

function buttons(scope: ParentNode = host): string[] {
  return [...scope.querySelectorAll("button")].map((b) => b.textContent?.trim() ?? "");
}

function setField(form: HTMLFormElement, name: string, value: string) {
  const el = form.querySelector(`[name="${name}"]`) as HTMLInputElement | HTMLSelectElement;
  el.value = value;
}

async function submit(form: HTMLFormElement) {
  await act(async () => { form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
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

describe("a deployment offers only the buttons its status allows", () => {
  it("approve and reject only while a named decision waits", () => {
    expect(stackActions({ status: "awaiting_approval", decision_id: "dec_1" })).toEqual(["approve", "reject"]);
    expect(stackActions({ status: "awaiting_approval", decision_id: null })).toEqual([]);
  });
  it("nothing while it is planned or running — it is still moving", () => {
    expect(stackActions({ status: "planned", decision_id: "dec_1" })).toEqual([]);
    expect(stackActions({ status: "running", decision_id: "dec_1", open_url: "https://x" })).toEqual([]);
  });
  it("open once healthy; roll back only when the API says this record can", () => {
    expect(stackActions({ status: "healthy", open_url: "https://boss.example.com", rollback_available: true }))
      .toEqual(["open", "rollback"]);
    expect(stackActions({ status: "failed", rollback_available: true })).toEqual(["rollback"]);
    expect(stackActions({ status: "rejected", decision_id: "dec_1" })).toEqual([]);
  });
  it("no roll back on an older healthy record, or on a failure that never reached the host", () => {
    // An older record in the recent list: rolling it back would deploy past the live release.
    expect(stackActions({ status: "healthy", open_url: "https://boss.example.com", rollback_available: false }))
      .toEqual(["open"]);
    // Refused at preflight: nothing on the host changed, so there is nothing to roll back.
    expect(stackActions({ status: "failed", rollback_available: false })).toEqual([]);
    // A record from an API that does not say: never guessed from the status.
    expect(stackActions({ status: "failed" })).toEqual([]);
  });
});

describe("a stack target is read from the shape the API stores", () => {
  it("required_resources, and nothing else", () => {
    expect(isStackTarget(TARGET)).toBe(true);
    expect(isStackTarget(PER_APP_TARGET)).toBe(false);
    expect(targetManifestPath(TARGET)).toBe("boss.deploy.yaml");
    expect(targetManifestPath({ id: "a", app_name: "a", environment: "p" })).toBe("boss.deploy.yaml");
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

describe("Infra → Deploy a repository", () => {
  it("lists only stack targets, each with its VM, repository, manifest and recent deployments", async () => {
    installFetch({ deployments: [deployment({ status: "healthy", open_url: "https://boss.example.com" })] });
    await renderPanel();
    const cards = host.querySelectorAll('[data-testid="stack-target"]');
    expect(cards).toHaveLength(1);
    expect(cards[0].textContent).toContain("boss");
    expect(cards[0].textContent).toContain("boss-vm");
    expect(cards[0].textContent).toContain("seanventures/boss");
    expect(cards[0].textContent).toContain("boss.deploy.yaml");
    expect(cards[0].querySelector('[data-testid="stack-recent"]')?.textContent).toContain("Healthy");
    expect(host.textContent).not.toContain("todo");
  });

  it("adds a stack target on a reachable VM in one write, and it is listed with Deploy after reload", async () => {
    installFetch({ targets: [] });
    await renderPanel();
    expect(host.querySelector('[data-testid="stack-targets-empty"]')).not.toBeNull();
    await act(async () => { button("Add a stack target").click(); });
    const form = host.querySelector('[data-testid="stack-add-target"]') as HTMLFormElement;
    // Only VMs BOSS has reached are offered.
    const vmOptions = [...form.querySelectorAll('select[name="host_id"] option')].map((o) => o.getAttribute("value"));
    expect(vmOptions).toEqual(["host_1"]);
    expect((form.querySelector('[name="manifest_path"]') as HTMLInputElement).value).toBe("boss.deploy.yaml");
    setField(form, "name", "boss");
    setField(form, "public_url", "https://boss.example.com");
    await submit(form);
    await settle(() => host.querySelectorAll('[data-testid="stack-target"]').length === 1, "target listed");

    const created = calls.filter((c) => c.method === "POST" && c.url === "/v1/deployment-targets");
    expect(created).toHaveLength(1);
    expect(created[0].body).toEqual({
      project_id: "project_1", actor: "ceo", app_name: "boss", environment: "production",
      repo: "seanventures/boss", host_id: "host_1",
      deploy_kind: "stack", manifest_path: "boss.deploy.yaml", public_url: "https://boss.example.com",
    });
    // Where it runs is part of the same write: no second call can fail and leave an orphan.
    expect(calls.some((c) => c.url.endsWith("/attach"))).toBe(false);
    const card = host.querySelector('[data-testid="stack-target"]')!;
    expect(card.textContent).toContain("boss-vm");
    expect(card.textContent).toContain("https://boss.example.com");
    expect(card.querySelector('[data-testid="stack-target-deploy"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="stack-deploy-form"]')).not.toBeNull();
  });

  it("a stack target with no VM offers Point at a VM, never Deploy", async () => {
    const orphan = { ...TARGET, id: "deptgt_orphan", host_id: null, target_ref: "typed@198.51.100.9:22" };
    installFetch({ targets: [orphan] });
    await renderPanel();
    const card = host.querySelector('[data-testid="stack-target"]')!;
    expect(card.textContent).toContain("not on a VM yet");
    expect(card.textContent).not.toContain("198.51.100.9");
    expect(card.querySelector('[data-testid="stack-target-deploy"]')).toBeNull();
    // Nor can a plan be raised against it from the form.
    expect(host.querySelector('[data-testid="stack-deploy-form"]')).toBeNull();

    await act(async () => { button("Point at a VM", card).click(); });
    await settle(() => !!host.querySelector('[data-testid="stack-target-deploy"]'), "attached");
    const attach = calls.find((c) => c.url.endsWith("/attach"));
    expect(attach?.url).toBe("/v1/deployment-hosts/host_1/attach");
    expect(attach?.body).toEqual({ target_id: "deptgt_orphan", actor: "ceo" });
  });

  it("refuses a bad stack name without sending anything", async () => {
    installFetch({ targets: [] });
    await renderPanel();
    await act(async () => { button("Add a stack target").click(); });
    const form = host.querySelector('[data-testid="stack-add-target"]') as HTMLFormElement;
    setField(form, "name", "My Stack");
    await submit(form);
    expect(host.querySelector('[data-testid="stack-add-target-error"]')?.textContent).toMatch(/lowercase/);
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("says where to start when no VM is reachable yet", async () => {
    installFetch({ targets: [], hosts: [UNREACHED] });
    await renderPanel();
    expect(host.querySelector('[data-testid="stack-targets-empty"]')?.textContent).toMatch(/Add and verify a VM/);
    await act(async () => { button("Add a stack target").click(); });
    expect(host.querySelector('[data-testid="stack-add-target-no-host"]')).not.toBeNull();
  });

  it("plans a deployment of the chosen repository at the ref, and shows the plan for approval", async () => {
    installFetch({ reads: [deployment()] });
    await renderPanel();
    const form = host.querySelector('[data-testid="stack-deploy-form"]') as HTMLFormElement;
    expect((form.querySelector('[name="ref"]') as HTMLInputElement).value).toBe("main");
    expect((form.querySelector('[name="repository"]') as HTMLSelectElement).value).toBe("seanventures/boss");
    setField(form, "ref", "feature/deploy");
    await submit(form);
    await settle(() => !!host.querySelector('[data-testid="stack-deployment"]'), "deployment view");

    const plan = calls.find((c) => c.url === "/v1/deployments/stack/plan");
    expect(plan?.body).toEqual({
      project_id: "project_1", target_id: "deptgt_1", repository: "seanventures/boss",
      ref: "feature/deploy", actor: "ceo",
    });
    const view = host.querySelector('[data-testid="stack-deployment"]')!;
    expect(view.getAttribute("data-status")).toBe("awaiting_approval");
    expect(view.textContent).toContain("Waiting for your approval");
    expect(view.textContent).toContain("0123456789ab");
    const manifest = view.querySelector('[data-testid="stack-manifest"]')!;
    expect(manifest.textContent).toContain("install: ./scripts/install/install.sh --yes");
    expect(manifest.textContent).toContain("migrate: make migrate");
    expect(manifest.textContent).toContain("http://127.0.0.1:8000/health");
    expect(view.querySelector('[data-testid="stack-env-keys"]')?.textContent).toContain("POSTGRES_PASSWORD");
    expect(buttons(view)).toEqual(expect.arrayContaining(["Approve", "Reject"]));
    expect(buttons(view)).not.toContain("Roll back");
  });

  it("shows the API's refusal of a plan in words", async () => {
    installFetch({ plan: { status: 422, body: { detail: "boss.deploy.yaml at abc123: health[0].url is required" } } });
    await renderPanel();
    await submit(host.querySelector('[data-testid="stack-deploy-form"]') as HTMLFormElement);
    await settle(() => !!host.querySelector('[data-testid="stack-deploy-error"]'), "error");
    expect(host.querySelector('[data-testid="stack-deploy-error"]')?.textContent)
      .toBe("boss.deploy.yaml at abc123: health[0].url is required");
  });
});

describe("a deployment view follows the record", () => {
  async function renderView(initial: StackDeployment, pollMs = 5) {
    await act(async () => {
      root.render(<StackDeploymentView deploymentId={initial.id} actor="ceo" initial={initial} pollMs={pollMs} />);
    });
  }

  it("approves through the decisions API with the record's decision, then follows it to healthy", async () => {
    installFetch({ reads: [
      deployment({ status: "running", steps: [{ name: "fetch", status: "succeeded" }, { name: "install", status: "running" }],
                   log_tail: "Step 3/9: building api" }),
      deployment({ status: "healthy", open_url: "https://boss.example.com",
                   rollback_available: true, rollback_commit: "fedcba9876543210",
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
        initial={deployment({ status: "failed", rollback_available: false,
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
          rollback_available: true, rollback_commit: "0000aaaa1111",
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
