"use client";

/**
 * What Infra's buttons post (infra-observability spec §6.5). Same-origin `/v1/…` through the
 * browser proxy (app/v1/[...path]/route.ts adds the key on the server), so the browser never
 * holds a credential; every write goes through `apiWrite`, so a double press is one request
 * (tests/every-write-goes-through-api-write.test.ts). Each call answers the API's own sentence
 * on refusal; nothing is retried behind the operator's back. After a write the caller says
 * `notifyChanged("infra")` and refreshes its poll (see `afterWrite`).
 */
import { detailText } from "../../lib/api-error";
import { apiWrite, type Press } from "../../lib/api-write";
import { notifyChanged } from "../changed";
import { declareAction } from "../../lib/identity/guarded-action";

export type Result<T = Record<string, unknown>> = { ok: true; data: T } | { ok: false; error: string; status?: number };

const enc = encodeURIComponent;

/** The API's sentence for a refusal: `detail` (string or `{message}`), else the status. */
export function apiSentence(body: unknown, status: number): string {
  const b = (body ?? {}) as { detail?: unknown; error?: unknown; message?: unknown };
  // ONE READER (lib/api-error detailText): {headline, action}, {message}, validation lists —
  // always words, never the refusal's JSON.
  return detailText(b.detail ?? b.error ?? b.message) || `The API answered ${status}.`;
}

/** One keyed write. `press` names the button (a second, different press in its scope is refused
 *  and said); leave it out for background writes. */
export async function send<T = Record<string, unknown>>(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string,
                                                         body: Record<string, unknown> | null, press?: Press): Promise<Result<T>> {
  try {
    const r = await apiWrite(path, {
      method, cache: "no-store",
      ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
    }, press ? { press } : {});
    const data = await r.json().catch(() => null);
    if (!r.ok) return { ok: false, error: apiSentence(data, r.status), status: r.status };
    return { ok: true, data: (data ?? {}) as T };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "The API did not answer." };
  }
}

/** A read the drawers make on demand (refs, preflight, secrets, repositories). */
export async function read<T = Record<string, unknown>>(path: string): Promise<Result<T>> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    const data = await r.json().catch(() => null);
    if (!r.ok) return { ok: false, error: apiSentence(data, r.status), status: r.status };
    return { ok: true, data: (data ?? {}) as T };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "The API did not answer." };
  }
}

/** Every Infra write ends here: the other panels re-poll, and this one now. */
export function afterWrite(...refreshers: Array<(() => void) | undefined>): void {
  notifyChanged("infra");
  for (const r of refreshers) r?.();
}

const P = (scope: string, label: string): Press => ({ scope, label });

// ── hosts (§4.4; the routes the old FleetPanel's server actions posted) ─────────────────────

export const registerHost = (projectId: string, actor: string, f: { address: string; ssh_user: string; ssh_port: number;
  name: string; arch?: string; admin_identity_file?: string }) =>
  send<{ id?: string }>("POST", "/v1/deployment-hosts", { project_id: projectId, actor, ...f }, P("infra-host-add", "Add host"));
export const generateHostKey = (hostId: string, actor: string, backend: string) =>
  send<{ public_key?: string; install_command?: string; install_variants?: Record<string, string> }>(
    "POST", `/v1/deployment-hosts/${enc(hostId)}/key`, { actor, backend }, P(`infra-host-${hostId}`, "Generate key"));
export const adoptHostKey = (hostId: string, actor: string, privateKey: string, backend: string) =>
  send("POST", `/v1/deployment-hosts/${enc(hostId)}/adopt-key`, { actor, private_key: privateKey, backend },
       P(`infra-host-${hostId}`, "Adopt key"));
export const verifyHost = (hostId: string, actor: string, label = "Verify") =>
  send<{ status?: string }>("POST", `/v1/deployment-hosts/${enc(hostId)}/verify`, { actor }, P(`infra-host-${hostId}`, label));
export const installHostKey = (hostId: string, actor: string, backend: string) =>
  send("POST", `/v1/deployment-hosts/${enc(hostId)}/install-key`, { actor, backend }, P(`infra-host-${hostId}`, "Install key for me"));
export const provisionHost = (hostId: string, actor: string) =>
  send("POST", `/v1/deployment-hosts/${enc(hostId)}/provision`, { actor }, P(`infra-host-${hostId}`, "Install Docker"));
/** The routed retire (S1): the server checks the typed name too. */
export const retireHost = (projectId: string, hostId: string, actor: string, f: { confirm_name: string;
  revoke_key: boolean; uninstall_docker: boolean; delete_record: boolean; detach_apps: boolean }) =>
  send("POST", `/v1/infra/hosts/${enc(hostId)}/retire`, { project_id: projectId, actor, ...f }, P(`infra-host-${hostId}`, "Retire"));

// ── targets (§4.5) ──────────────────────────────────────────────────────────────────────────

/** One Deploy, routed by target kind server-side; answers the new deploy's ref. */
export const deployTarget = (projectId: string, targetId: string, actor: string, ref: string | null,
                             extras: { expected_cost_usd?: number; budget_reservation_id?: string; release_packet?: Record<string, unknown> } = {}) =>
  send<{ ref?: string }>("POST", `/v1/infra/targets/${enc(targetId)}/deploy`, { project_id: projectId, actor, ref: ref || null, ...extras },
                         P(`infra-target-${targetId}`, "Deploy"));
export const setTargetToggles = (targetId: string, actor: string, patch: { auto_deploy?: boolean; paused?: boolean }) =>
  send("POST", `/v1/deployment-targets/${enc(targetId)}/auto-deploy`, { ...patch, actor }, P(`infra-target-${targetId}`, "Toggle"));
export const moveTarget = (projectId: string, targetId: string, actor: string, hostId: string) =>
  send("POST", `/v1/infra/targets/${enc(targetId)}/move`, { project_id: projectId, actor, host_id: hostId }, P(`infra-target-${targetId}`, "Move"));
export const retireTarget = (targetId: string, actor: string) =>
  send("DELETE", `/v1/deployment-targets/${enc(targetId)}?actor=${enc(actor)}`, null, P(`infra-target-${targetId}`, "Retire"));
/** Remove the app from its host (services/app_removal.py): governed like a deploy — a ChangeSet
 *  approved by a person or the environment's policy, a leased job, a receipt. Data is kept
 *  unless `deleteData`. */
export const removeTargetApp = (targetId: string, actor: string, deleteData: boolean, reason: string) =>
  send<{ id?: string; next_action?: string; decision_id?: string }>("POST", `/v1/deployment-targets/${enc(targetId)}/remove`,
       { actor, delete_data: deleteData, reason }, P(`infra-target-${targetId}`, "Remove app"));
export const bindSecret = (projectId: string, targetId: string, actor: string, name: string, secretRefId: string) =>
  send("POST", `/v1/deployment-targets/${enc(targetId)}/secrets`, { project_id: projectId, actor, name, secret_ref_id: secretRefId },
       P(`infra-target-${targetId}`, "Bind secret"));
export const createStackTarget = (projectId: string, actor: string, f: { host_id: string; app_name: string; environment: string;
  repository: string; public_url: string | null; manifest_path: string; port?: number | null }) =>
  send<{ id?: string }>("POST", "/v1/deployments/stack/targets", { project_id: projectId, actor, ...f }, P("infra-target-add", "Create"));
/** `More` on the App target sheet: port, entry, publishable ports, health check (merged, never
 *  replacing the target's other resources — main.py update_deployment_target_endpoint). */
export const updateTarget = (targetId: string, actor: string, patch: Record<string, unknown>) =>
  send("PATCH", `/v1/deployment-targets/${enc(targetId)}`, { ...patch, actor }, P(`infra-target-${targetId}`, "Save"));
export const createRegistryTarget = (projectId: string, actor: string, f: Record<string, unknown>) =>
  send<{ id?: string }>("POST", "/v1/deployments", { project_id: projectId, actor, ...f }, P("infra-target-add", "Create"));

// ── deploys (§4.6) ──────────────────────────────────────────────────────────────────────────

export const decideDeploy = (decisionId: string, actor: string, verdict: "approve" | "reject") =>
  send("POST", `/v1/decisions/${enc(decisionId)}/${verdict}`, {
    actor, note: verdict === "approve" ? "Approved from Infra." : "Rejected from Infra.",
  }, P(`infra-decision-${decisionId}`, verdict === "approve" ? "Approve" : "Reject"));
export const rollbackDeploy = (projectId: string, deployRef: string, actor: string) =>
  send<{ ref?: string }>("POST", `/v1/infra/deploys/${enc(deployRef)}/rollback`, { project_id: projectId, actor },
                         P(`infra-deploy-${deployRef}`, "Roll back"));
export const redeployDelivery = (deploymentId: string, actor: string) =>
  send("POST", `/v1/delivery/deployments/${enc(deploymentId)}/redeploy`, { actor }, P(`infra-deploy-${deploymentId}`, "Redeploy"));
export const runHealthCheck = (projectId: string, runId: string, actor: string) =>
  send("POST", `/v1/deployment-runs/${enc(runId)}/health-check`, { project_id: projectId, actor }, P(`infra-deploy-${runId}`, "Run check"));
export const saveEnvironmentPolicy = (projectId: string, actor: string, f: Record<string, unknown>) =>
  send("POST", "/v1/deployment-environment-policies", { project_id: projectId, actor, ...f }, P("infra-rules", "Save"));

// ── previews (§4.7) ─────────────────────────────────────────────────────────────────────────

export const stopPreview = (previewId: string, actor: string) =>
  send("POST", `/v1/previews/${enc(previewId)}/stop`, { actor }, P(`infra-preview-${previewId}`, "Stop"));
export const restartPreview = (projectId: string, previewId: string, actor: string) =>
  send("POST", `/v1/infra/previews/${enc(previewId)}/restart`, { project_id: projectId, actor }, P(`infra-preview-${previewId}`, "Restart"));

// ── plugins (Infra › Plugins; install-wide registry) ─────────────────────────────────────────

export const enablePlugin = (pluginId: string) =>
  send("POST", `/v1/infra/plugins/${enc(pluginId)}/enable`, {}, P(`infra-plugin-${pluginId}`, "On"));
export const disablePlugin = (pluginId: string) =>
  send("POST", `/v1/infra/plugins/${enc(pluginId)}/disable`, {}, P(`infra-plugin-${pluginId}`, "Off"));

// ── extensions (boss.extension/v1, BOSSAPP-861; install-wide, enabled per project) ──────────

export const previewExtension = (source: string) =>
  send<{ preview?: unknown }>("POST", "/v1/extensions/preview", { source }, P("infra-extension-install", "Check"));
export const installExtension = (source: string, checksum: string, accept: string[]) =>
  send<{ extension?: unknown }>("POST", "/v1/extensions/install", { source, checksum, accept },
                                P("infra-extension-install", "Install"));
export const enableExtension = (extensionId: string, projectId: string) =>
  send("POST", `/v1/extensions/${enc(extensionId)}/enable`, { project_id: projectId },
       P(`infra-extension-${extensionId}`, "On"));
export const disableExtension = (extensionId: string, projectId: string) =>
  send("POST", `/v1/extensions/${enc(extensionId)}/disable`, { project_id: projectId },
       P(`infra-extension-${extensionId}`, "Off"));
export const uninstallExtension = (extensionId: string) =>
  send("DELETE", `/v1/extensions/${enc(extensionId)}`, null, P(`infra-extension-${extensionId}`, "Remove"));
export type ExtensionWebhook = { name: string; url: string; secret: string; signatureHeader: string; scheme: string };
export const extensionWebhooks = (extensionId: string) =>
  read<{ webhooks?: ExtensionWebhook[] }>(`/v1/extensions/${enc(extensionId)}/webhooks`);

// ── secrets (§4.3 `+ Add › Secret`, Deploys `⋯ › Secrets`, target SECRETS) ──────────────────

export type SecretRow = {
  secret_ref_id: string; name: string; backend: string; secret_kind: string; status: string;
  fingerprint: string | null; last4: string | null; bound_to: string[];
};
export const listSecrets = (projectId: string) => read<{ secrets?: SecretRow[] }>(`/v1/secrets?project_id=${enc(projectId)}`);
export const putSecret = (projectId: string, actor: string, f: { name: string; value: string; secret_kind: string; backend: string }) =>
  send<{ name?: string; fingerprint?: string; last4?: string }>("POST", "/v1/secrets", { project_id: projectId, actor, ...f }, P("infra-secrets", "Upload"));
export const generateKeypair = (projectId: string, actor: string, f: { name: string; backend: string }) =>
  send<{ public_key?: string; fingerprint?: string }>("POST", "/v1/secrets/keypair", { project_id: projectId, actor, ...f }, P("infra-secrets", "Generate keypair"));
export const verifySecret = (secretRefId: string, actor: string) =>
  send<{ resolvable?: boolean; fingerprint_unchanged?: boolean; error?: string }>(
    "POST", `/v1/secrets/${enc(secretRefId)}/verify`, { actor }, P("infra-secrets", "Verify"));

// ── reads the drawers make ──────────────────────────────────────────────────────────────────

export const listRefs = (projectId: string, repo: string) => {
  const [owner, name] = repo.split("/");
  return read<{ branches?: Array<{ name: string; sha?: string }>; default_branch?: string }>(
    `/v1/delivery/repos/${enc(owner ?? "")}/${enc(name ?? "")}/refs?project_id=${enc(projectId)}`);
};
/** deployment_connectors.preflight(): `ready` plus the concrete list of what is missing. */
export type PreflightBody = { ready?: boolean; problems?: string[]; connector?: string; required_secrets?: string[] };
export const preflightTarget = (projectId: string, targetId: string) =>
  read<PreflightBody>(`/v1/delivery/targets/${enc(targetId)}/preflight?project_id=${enc(projectId)}`);
export const listRepositories = (projectId: string) =>
  read<{ repositories?: Array<{ full_name: string; role?: string; archived?: boolean }>; suggested?: string }>(
    `/v1/projects/${enc(projectId)}/repositories`);

// THE PERMISSION EACH ACTION DECLARES (IDENTITY.md §6.5, R4; lib/identity/guarded-action.ts).
// A declaration for the console to mirror — disabled with the reason when not held — never a
// decision: the API checks every call. tests/identity-mirroring.test.ts keeps this table whole.
declareAction(registerHost, "host:manage");
declareAction(generateHostKey, "host:manage");
declareAction(adoptHostKey, "host:manage");
declareAction(verifyHost, "host:manage");
declareAction(installHostKey, "host:manage");
declareAction(provisionHost, "host:manage");
declareAction(retireHost, "host:manage");
declareAction(deployTarget, "deployment:create");
declareAction(setTargetToggles, "deployment_target:manage");
declareAction(moveTarget, "deployment_target:manage");
declareAction(retireTarget, "deployment_target:manage");
declareAction(removeTargetApp, "deployment:create");
declareAction(bindSecret, "deployment_target:manage");
declareAction(createStackTarget, "deployment_target:manage");
declareAction(updateTarget, "deployment_target:manage");
declareAction(createRegistryTarget, "deployment_target:manage");
declareAction(decideDeploy, "decision:approve");
declareAction(rollbackDeploy, "deployment:rollback");
declareAction(redeployDelivery, "deployment:create");
declareAction(runHealthCheck, "deployment:create");
declareAction(saveEnvironmentPolicy, "deployment_target:manage");
declareAction(stopPreview, "preview:manage");
declareAction(enablePlugin, "setup:manage");
declareAction(disablePlugin, "infra:provision");
declareAction(restartPreview, "preview:manage");
declareAction(previewExtension, "setup:manage");
declareAction(installExtension, "setup:manage");
declareAction(enableExtension, "setup:manage");
declareAction(disableExtension, "infra:provision");
declareAction(uninstallExtension, "setup:manage");
declareAction(extensionWebhooks, "setup:manage");
declareAction(listSecrets, "secret:read_metadata");
declareAction(putSecret, "secret:manage");
declareAction(generateKeypair, "secret:manage");
declareAction(verifySecret, "secret:read_metadata");
declareAction(listRefs, "repository:read");
declareAction(preflightTarget, "deployment:read");
declareAction(listRepositories, "repository:read");
