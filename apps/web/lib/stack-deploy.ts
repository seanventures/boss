/**
 * The console's half of a governed STACK deployment: a repository with a `boss.deploy.yaml`,
 * deployed at a ref onto a VM BOSS onboarded, through a Resource Plane ChangeSet and an approval.
 *
 * This is the path that lets BOSS deploy something as big as BOSS. The older per-app path ships
 * one container with fixed env and a single health check, and it stays paused in Community; a
 * stack deployment instead runs the repository's OWN install/update steps from its manifest, so
 * the console never needs to know how an application is built — only where it goes and at which
 * commit.
 *
 * Every call goes to the API through the console's own origin (app/v1/[...path]/route.ts adds
 * the key on the server), and every write goes through `apiWrite`, so a double click is one
 * plan, one approval, one rollback.
 *
 * THE ACTOR IS ALWAYS PASSED IN, never defaulted: a literal actor 403s on every authed install.
 */
import { apiErrorText } from "./api-error";
import { apiWrite } from "./api-write";

export type StackStatus =
  | "planned" | "awaiting_approval" | "running" | "healthy" | "failed" | "rejected";

export type StackStep = {
  name: string;
  status: string;
  started_at?: string | null;
  ended_at?: string | null;
};

/** A manifest step as the API renders it: the command text, or a named object. */
export type ManifestStep = string | { name?: string; command?: string; phase?: string };

export type StackDeployment = {
  id: string;
  project_id: string;
  target_id: string;
  repository: string;
  ref: string;
  commit?: string | null;
  manifest?: {
    name?: string;
    kind?: string;
    steps?: ManifestStep[];
    /** NAMES only. Values — generated, secret, or literal — never leave the API. */
    env_keys?: string[];
    health?: Array<{ url?: string; expect?: number; timeout_seconds?: number }>;
  } | null;
  status: StackStatus | string;
  decision_id?: string | null;
  changeset_id?: string | null;
  steps?: StackStep[];
  log_tail?: string | null;
  open_url?: string | null;
  error?: { headline?: string; action?: string } | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type StackTarget = {
  id: string;
  app_name: string;
  environment: string;
  repo?: string | null;
  repo_owner?: string | null;
  host_id?: string | null;
  target_ref?: string | null;
  status?: string | null;
  deploy_kind?: string | null;
  manifest_path?: string | null;
  public_url?: string | null;
  required_resources?: Array<{ kind?: string; name?: string }> | null;
};

export type Repository = {
  full_name: string;
  role?: string | null;
  default_branch?: string | null;
  status?: string | null;
  archived?: boolean | null;
};

export type DeployHost = {
  id: string;
  name: string;
  address: string;
  ssh_user?: string | null;
  ssh_port?: number | null;
  status: string;
  last_preflight?: { reachable?: boolean } | null;
};

/**
 * A VM a stack may be placed on: one BOSS has actually opened a session to.
 *
 * Not only `verified`. Host verification also demands docker, because the per-app path ships
 * containers; a stack brings its own requirements in its manifest (`requires: {docker, sudo}`)
 * and the plan's read-only preflight checks exactly those. Requiring docker here would refuse a
 * VM whose manifest installs docker itself.
 */
export function canHostStack(h: DeployHost): boolean {
  return h.status !== "retired" && (h.status === "verified" || h.last_preflight?.reachable === true);
}

export type Result<T> = { ok: true; data: T } | { ok: false; error: string };

export const DEFAULT_MANIFEST_PATH = "boss.deploy.yaml";
export const DEFAULT_REF = "main";

/** Still moving: the view keeps asking until the deployment reaches one of the other three. */
export const LIVE_STATUSES: ReadonlySet<string> = new Set(["planned", "awaiting_approval", "running"]);

export function isLive(status: string | null | undefined): boolean {
  return LIVE_STATUSES.has(String(status ?? ""));
}

/**
 * The buttons a deployment offers, decided by its status and nothing else.
 *
 * Approve/Reject exist only while a decision is waiting AND the record names it — a button that
 * cannot name what it approves would approve nothing. Rollback is offered once the deployment
 * has finished either way: it plans a NEW deployment of the previous healthy commit, through the
 * same approval, so it is never offered while this one is still in flight.
 */
export type StackAction = "approve" | "reject" | "rollback" | "open";

export function stackActions(d: Pick<StackDeployment, "status" | "decision_id" | "open_url">): StackAction[] {
  const out: StackAction[] = [];
  if (d.status === "awaiting_approval" && d.decision_id) out.push("approve", "reject");
  if (d.status === "healthy" && d.open_url) out.push("open");
  if (d.status === "healthy" || d.status === "failed") out.push("rollback");
  return out;
}

export function statusLabel(status: string): string {
  const named: Record<string, string> = {
    planned: "Planned",
    awaiting_approval: "Waiting for your approval",
    running: "Deploying",
    healthy: "Healthy",
    failed: "Failed",
    rejected: "Rejected",
  };
  return named[status] ?? status.replace(/_/g, " ");
}

export function statusTone(status: string): "green" | "amber" | "red" | "blue" {
  if (status === "healthy") return "green";
  if (status === "failed" || status === "rejected") return "red";
  if (status === "running") return "blue";
  return "amber";
}

/** What one manifest step reads as in the summary. */
export function stepText(step: ManifestStep): string {
  if (typeof step === "string") return step;
  const name = (step?.name ?? "").trim();
  const command = (step?.command ?? "").trim();
  if (name && command) return `${name}: ${command}`;
  return name || command || "";
}

export function shortCommit(commit: string | null | undefined): string {
  return (commit ?? "").slice(0, 12);
}

/**
 * A stack target, read from whichever shape the target record carries it in.
 *
 * Canonical: top-level `deploy_kind: "stack"`. A target store that keeps per-target facts in
 * `required_resources` (as app_port and app_entry are) is read the same way, so the console does
 * not depend on which of the two the API settled on.
 */
function resource(t: StackTarget, kind: string): string | null {
  const hit = (t.required_resources ?? []).find((r) => r?.kind === kind);
  return hit?.name ? String(hit.name) : null;
}

export function isStackTarget(t: StackTarget): boolean {
  return (t.deploy_kind ?? resource(t, "deploy_kind")) === "stack";
}

export function targetManifestPath(t: StackTarget): string {
  return t.manifest_path || resource(t, "manifest_path") || DEFAULT_MANIFEST_PATH;
}

export function targetPublicUrl(t: StackTarget): string | null {
  return t.public_url || resource(t, "public_url") || null;
}

/** `owner/name`, however the target recorded it. */
export function targetRepository(t: StackTarget): string {
  const repo = (t.repo ?? "").trim();
  if (!repo || repo.includes("/")) return repo;
  const owner = t.repo_owner || resource(t, "repo_owner");
  return owner ? `${owner}/${repo}` : repo;
}

/** What is wrong with a stack-target form, in the operator's words, or null. */
export function targetFormProblem(input: {
  hostId: string; name: string; environment: string; repository: string;
  publicUrl: string; manifestPath: string;
}): string | null {
  if (!input.hostId) return "Choose the VM this stack runs on — add and verify one above first.";
  if (!/^[a-z0-9][a-z0-9-]*$/.test(input.name)) {
    return "Name the stack in lowercase letters, digits and dashes (for example boss).";
  }
  if (!input.environment.trim()) return "Name the environment (for example production).";
  if (!/^[^/\s]+\/[^/\s]+$/.test(input.repository)) {
    return "Choose a repository registered under Repo and model.";
  }
  if (input.publicUrl && !/^https?:\/\/\S+$/i.test(input.publicUrl)) {
    return "The public URL must start with http:// or https://.";
  }
  const path = input.manifestPath.trim();
  if (!path || path.startsWith("/") || path.split("/").includes("..")) {
    return "The manifest path is a file inside the repository, such as boss.deploy.yaml.";
  }
  return null;
}

/** What is wrong with a deploy form, or null. */
export function deployFormProblem(input: { repository: string; ref: string; targetId: string }): string | null {
  if (!/^[^/\s]+\/[^/\s]+$/.test(input.repository)) {
    return "Choose a repository registered under Repo and model.";
  }
  if (!input.ref.trim() || /\s/.test(input.ref.trim())) {
    return "Give one ref: a branch, a tag, or a commit SHA.";
  }
  if (!input.targetId) return "Choose where it deploys — add a stack target first.";
  return null;
}

const enc = encodeURIComponent;

async function read<T>(path: string, what: string): Promise<Result<T>> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    const body = await r.json().catch(() => null);
    if (!r.ok) return { ok: false, error: apiErrorText(body, r.status) };
    return { ok: true, data: body as T };
  } catch (e) {
    return { ok: false, error: `Could not read ${what}: ${String((e as Error)?.message ?? e)}` };
  }
}

async function send<T>(path: string, body: Record<string, unknown>,
                       press: { scope: string; label: string }): Promise<Result<T>> {
  try {
    const r = await apiWrite(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, { press });
    const data = await r.json().catch(() => null);
    if (!r.ok) return { ok: false, error: apiErrorText(data, r.status) };
    return { ok: true, data: data as T };
  } catch (e) {
    return {
      ok: false,
      error: `${press.label} did not reach the control plane: ${String((e as Error)?.message ?? e)}`,
    };
  }
}

/** The deployment record, from either `{deployment}` or a bare record. */
function deploymentOf(body: unknown): StackDeployment | null {
  const b = body as { deployment?: StackDeployment } & Partial<StackDeployment> | null;
  if (!b) return null;
  if (b.deployment && typeof b.deployment === "object") return b.deployment;
  return typeof b.id === "string" ? (b as StackDeployment) : null;
}

function asDeployment(r: Result<unknown>): Result<StackDeployment> {
  if (!r.ok) return r;
  const d = deploymentOf(r.data);
  return d ? { ok: true, data: d } : { ok: false, error: "The API answered without a deployment record." };
}

export async function loadRepositories(projectId: string): Promise<Result<{ repositories: Repository[]; suggested: string }>> {
  const r = await read<{ repositories?: Repository[]; suggested?: string }>(
    `/v1/projects/${enc(projectId)}/repositories`, "the registered repositories");
  if (!r.ok) return r;
  return {
    ok: true,
    data: {
      repositories: (r.data?.repositories ?? []).filter((repo) => repo?.full_name && !repo.archived),
      suggested: r.data?.suggested ?? "",
    },
  };
}

/** Hosts through the route handler that strips the key reference — the browser never needs it. */
export async function loadHosts(projectId: string): Promise<Result<DeployHost[]>> {
  const r = await read<{ hosts?: DeployHost[] }>(
    `/api/deployment-hosts?project_id=${enc(projectId)}`, "the VMs");
  return r.ok ? { ok: true, data: r.data?.hosts ?? [] } : r;
}

export async function loadStackTargets(projectId: string): Promise<Result<StackTarget[]>> {
  const r = await read<{ items?: StackTarget[]; targets?: StackTarget[] }>(
    `/v1/deployment-targets?project_id=${enc(projectId)}&limit=200`, "the deployment targets");
  if (!r.ok) return r;
  const all = r.data?.items ?? r.data?.targets ?? [];
  return { ok: true, data: all.filter((t) => isStackTarget(t) && t.status !== "retired") };
}

export async function listStackDeployments(projectId: string, targetId?: string): Promise<Result<StackDeployment[]>> {
  const q = new URLSearchParams({ project_id: projectId });
  if (targetId) q.set("target_id", targetId);
  const r = await read<{ items?: StackDeployment[] }>(
    `/v1/deployments/stack?${q.toString()}`, "the stack deployments");
  return r.ok ? { ok: true, data: r.data?.items ?? [] } : r;
}

export async function getStackDeployment(id: string): Promise<Result<StackDeployment>> {
  return asDeployment(await read(`/v1/deployments/stack/${enc(id)}`, "the deployment"));
}

/**
 * Register a stack target and point it at its VM.
 *
 * Two calls because they are two facts: what deploys (name, environment, repository, manifest)
 * and where (the host). `attach` derives the target's address from the host row, so the two can
 * never drift — the target is created with that same projection, never a typed address.
 */
export async function createStackTarget(projectId: string, actor: string, input: {
  host: DeployHost; name: string; environment: string; repository: string;
  publicUrl: string; manifestPath: string;
}): Promise<Result<StackTarget>> {
  const user = (input.host.ssh_user || "root").trim();
  const port = Number(input.host.ssh_port || 22);
  const created = await send<StackTarget>("/v1/deployment-targets", {
    project_id: projectId,
    actor,
    app_name: input.name,
    environment: input.environment.trim(),
    repo: input.repository,
    target_kind: "ssh",
    target_ref: `${user}@${input.host.address}:${port}`,
    deploy_kind: "stack",
    manifest_path: input.manifestPath.trim() || DEFAULT_MANIFEST_PATH,
    public_url: input.publicUrl.trim() || null,
    host_id: input.host.id,
  }, { scope: "stack-target-create", label: "Add stack target" });
  if (!created.ok) return created;
  const attached = await send<StackTarget>(
    `/v1/deployment-hosts/${enc(input.host.id)}/attach`,
    { target_id: created.data.id, actor },
    { scope: `stack-target-attach-${created.data.id}`, label: "Point the target at its VM" });
  if (!attached.ok) {
    return { ok: false, error: `The target was added but not pointed at ${input.host.name}: ${attached.error}` };
  }
  return { ok: true, data: { ...created.data, host_id: input.host.id } };
}

export async function planStackDeploy(projectId: string, actor: string, input: {
  targetId: string; repository: string; ref: string;
}): Promise<Result<StackDeployment>> {
  return asDeployment(await send("/v1/deployments/stack/plan", {
    project_id: projectId,
    target_id: input.targetId,
    repository: input.repository,
    ref: input.ref.trim(),
    actor,
  }, { scope: `stack-plan-${input.targetId}`, label: "Plan deployment" }));
}

/** Approve or reject through the decisions API — the same decision the Approvals area shows. */
export async function decideStackDeploy(decisionId: string, actor: string,
                                        verdict: "approve" | "reject"): Promise<Result<unknown>> {
  return send(`/v1/decisions/${enc(decisionId)}/${verdict}`, {
    actor,
    note: verdict === "approve" ? "Approved from Infra: deploy this stack." : "Rejected from Infra.",
  }, { scope: `stack-decision-${decisionId}`, label: verdict === "approve" ? "Approve" : "Reject" });
}

export async function rollbackStackDeploy(deploymentId: string, actor: string): Promise<Result<StackDeployment>> {
  return asDeployment(await send(`/v1/deployments/stack/${enc(deploymentId)}/rollback`, { actor },
    { scope: `stack-rollback-${deploymentId}`, label: "Roll back" }));
}
