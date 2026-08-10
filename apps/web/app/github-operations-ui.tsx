"use client";

import React, { useMemo, useState } from "react";

import {
  approveGitHubResolutionPlanAction,
  authorizeGitHubMergeAction,
  captureGitHubBranchSnapshotAction,
  createGitHubBranchPolicyAction,
  createGitHubConnectionAction,
  createGitHubRepositoryBindingAction,
  enqueueGitHubMergeAction,
  planGitHubCodeSyncAction,
  repairGitHubReconciliationAction,
  scheduleGitHubReconciliationAction,
  setGitHubConnectionStateAction,
  testGitHubConnectionAction,
} from "./actions";
import type { GitHubOperationsFeed } from "../lib/github-operations";
import { SubmitButton } from "./submit-button";

type GitHubOperationsProps = {
  feed: GitHubOperationsFeed;
  actor: string;
  canManage: boolean;
};

type Tone = "good" | "warn" | "bad" | "muted" | "accent";

function statusTone(status?: string): Tone {
  const value = (status ?? "").toLowerCase();
  if (
    [
      "active",
      "passed",
      "succeeded",
      "resolved",
      "applied",
      "merged",
      "equal",
      "authorized",
    ].includes(value)
  ) {
    return "good";
  }
  if (
    [
      "failed",
      "deadlettered",
      "blocked",
      "error",
      "revoked",
      "conflicted",
      "protected_blocked",
    ].includes(value)
  ) {
    return "bad";
  }
  if (
    [
      "pending",
      "pending_approval",
      "queued",
      "running",
      "applying",
      "diverged",
      "behind",
      "open",
      "resolution_proposed",
    ].includes(value)
  ) {
    return "warn";
  }
  return value ? "accent" : "muted";
}

function label(value?: string | null): string {
  return value?.replaceAll("_", " ") ?? "unknown";
}

function shortSha(value?: string | null): string {
  return value ? value.slice(0, 8) : "—";
}

function shortId(value?: string | null): string {
  return value ? value.slice(0, 12) : "—";
}

function formatTime(value?: string | null): string {
  if (!value) return "not observed";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}

function Status({ value }: { value?: string | null }) {
  return (
    <span className={`ghx-status ${statusTone(value ?? undefined)}`}>
      {label(value)}
    </span>
  );
}

function HiddenScope({
  projectId,
  actor,
}: {
  projectId: string;
  actor: string;
}) {
  return (
    <>
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="actor" value={actor} />
    </>
  );
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return <div className="ghx-empty">{children}</div>;
}

export function GitHubOperations({
  feed,
  actor,
  canManage,
}: GitHubOperationsProps) {
  const [bindingId, setBindingId] = useState(
    feed.bindings.find((item) => item.status === "active")?.id ??
      feed.bindings[0]?.id ??
      "",
  );
  const selectedBinding =
    feed.bindings.find((item) => item.id === bindingId) ?? feed.bindings[0];
  const connection = feed.connections.find(
    (item) => item.id === selectedBinding?.connection_id,
  );
  const bindingRefs = useMemo(
    () =>
      feed.refStates
        .filter((item) => item.repository_binding_id === selectedBinding?.id)
        .sort((left, right) => {
          const defaultRef = selectedBinding?.default_branch;
          if (left.ref_name === defaultRef) return -1;
          if (right.ref_name === defaultRef) return 1;
          return left.ref_name.localeCompare(right.ref_name);
        }),
    [feed.refStates, selectedBinding],
  );
  const [requestedRef, setRequestedRef] = useState("");
  const selectedRef =
    bindingRefs.find((item) => item.ref_name === requestedRef) ??
    bindingRefs[0];
  const policies = feed.policies.filter(
    (item) => item.repository_binding_id === selectedBinding?.id,
  );
  const policy = policies.find((item) => item.status === "active") ?? policies[0];
  const snapshots = feed.snapshots
    .filter((item) => item.repository_binding_id === selectedBinding?.id)
    .sort(
      (left, right) =>
        Date.parse(right.observed_at) - Date.parse(left.observed_at),
    );
  const leases = feed.leases.filter(
    (item) => item.repository_binding_id === selectedBinding?.id,
  );
  const syncRuns = feed.syncRuns.filter(
    (item) => item.repository_binding_id === selectedBinding?.id,
  );
  const conflicts = feed.conflicts.filter(
    (item) => item.repository_binding_id === selectedBinding?.id,
  );
  const conflictIds = new Set(conflicts.map((item) => item.id));
  const resolutionPlans = feed.resolutionPlans.filter((item) =>
    conflictIds.has(item.conflict_set_id),
  );
  const mergeAuthorizations = feed.mergeAuthorizations.filter(
    (item) => item.repository_binding_id === selectedBinding?.id,
  );
  const reconciliationRuns = feed.reconciliationRuns.filter(
    (item) => item.repository_binding_id === selectedBinding?.id,
  );
  const signals = feed.signals.filter(
    (item) => item.repository_binding_id === selectedBinding?.id,
  );
  const refSnapshot = snapshots.find(
    (item) =>
      item.source_ref === selectedRef?.ref_name ||
      item.target_ref === selectedRef?.ref_name,
  );
  const refLease = leases.find(
    (item) =>
      item.ref_name === selectedRef?.ref_name && item.status === "active",
  );
  const refSync = syncRuns.find(
    (item) =>
      item.source_ref === selectedRef?.ref_name ||
      item.target_ref === selectedRef?.ref_name,
  );
  const refMerge = mergeAuthorizations.find(
    (item) =>
      item.source_ref === selectedRef?.ref_name ||
      item.target_ref === selectedRef?.ref_name,
  );
  const latestSnapshot = snapshots[0];
  const activeConnectionCount = feed.summary.activeConnections;
  const connectionHealth =
    connection?.last_test_status ?? connection?.status ?? "unconfigured";

  function chooseBinding(nextBindingId: string) {
    setBindingId(nextBindingId);
    setRequestedRef("");
  }

  return (
    <section
      className="ghx-control"
      data-area="github"
      aria-label="GitHub mission control"
    >
      <header className="ghx-titlebar">
        <div>
          <span className="eyebrow">GitHub Mission Control</span>
          <h2>Repository truth, governed from project to merge.</h2>
          <p>
            Observe every ref, serialize branch writers, reconcile drift, and
            require evidence before changes reach GitHub.
          </p>
        </div>
        <div className="ghx-health" aria-label="GitHub integration health">
          <Status value={feed.apiReachable ? connectionHealth : "error"} />
          <span>{activeConnectionCount} active connection{activeConnectionCount === 1 ? "" : "s"}</span>
          <span>last truth {formatTime(feed.summary.lastObservedAt)}</span>
        </div>
      </header>

      {!feed.apiReachable ? (
        <div className="ghx-alert bad" role="alert">
          <strong>GitHub control plane unavailable.</strong>
          <span>{feed.blockedReason ?? "No API evidence could be loaded."}</span>
        </div>
      ) : null}
      {feed.warnings.length > 0 ? (
        <details className="ghx-alert warn">
          <summary>{feed.warnings.length} evidence stream warning{feed.warnings.length === 1 ? "" : "s"}</summary>
          <ul>
            {feed.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </details>
      ) : null}

      <div className="ghx-metrics" aria-label="GitHub project summary">
        <div><strong>{feed.summary.activeRepositories}</strong><span>repositories</span></div>
        <div><strong>{feed.summary.protectedRefs}</strong><span>protected refs</span></div>
        <div><strong>{feed.summary.activeLeases}</strong><span>active leases</span></div>
        <div className={feed.summary.divergentSnapshots ? "attention" : ""}>
          <strong>{feed.summary.divergentSnapshots}</strong><span>divergent</span>
        </div>
        <div className={feed.summary.openConflicts ? "attention" : ""}>
          <strong>{feed.summary.openConflicts}</strong><span>open conflicts</span>
        </div>
        <div className={feed.summary.reconciliationAttention ? "danger" : ""}>
          <strong>{feed.summary.reconciliationAttention}</strong><span>reconcile failures</span>
        </div>
      </div>

      <div className="ghx-repo-bar">
        <label>
          <span>Project repository</span>
          <select
            value={selectedBinding?.id ?? ""}
            onChange={(event) => chooseBinding(event.target.value)}
            aria-label="Project repository"
          >
            {feed.bindings.length === 0 ? (
              <option value="">No repositories connected</option>
            ) : null}
            {feed.bindings.map((binding) => (
              <option key={binding.id} value={binding.id}>
                {binding.full_name} · {binding.role}
              </option>
            ))}
          </select>
        </label>
        {selectedBinding ? (
          <div className="ghx-repo-meta">
            <strong>{selectedBinding.full_name}</strong>
            <span>{selectedBinding.default_branch ?? "default branch unknown"} · {selectedBinding.visibility ?? "visibility unknown"}</span>
            <Status value={selectedBinding.status} />
          </div>
        ) : (
          <span className="ghx-repo-meta">Connect a repository to establish branch truth.</span>
        )}
        {canManage && selectedBinding ? (
          <div className="ghx-inline-actions">
            <form action={testGitHubConnectionAction}>
              <HiddenScope projectId={feed.projectId} actor={actor} />
              <input type="hidden" name="connection_id" value={selectedBinding.connection_id} />
              <input type="hidden" name="repository_binding_id" value={selectedBinding.id} />
              <SubmitButton className="button secondary" type="submit">Test access</SubmitButton>
            </form>
            <form action={scheduleGitHubReconciliationAction}>
              <HiddenScope projectId={feed.projectId} actor={actor} />
              <input type="hidden" name="repository_binding_id" value={selectedBinding.id} />
              <SubmitButton className="button" type="submit">Reconcile now</SubmitButton>
            </form>
          </div>
        ) : null}
      </div>

      {!canManage ? (
        <p className="ghx-permission">
          Read-only view. Actor <span className="mono">{actor}</span> does not
          hold <span className="mono">github_manage</span>; mutation controls
          are withheld.
        </p>
      ) : null}

      <div className="ghx-workspace">
        <div className="ghx-topology">
          <div className="ghx-section-label">
            <div>
              <span>Live branch topology</span>
              <small>{bindingRefs.length} current refs from immutable observations</small>
            </div>
            <Status value={policy ? `policy v${policy.version}` : "policy missing"} />
          </div>
          <div className="ghx-branch-head" aria-hidden="true">
            <span>Ref</span><span>Commit</span><span>Protection</span><span>Observed</span>
          </div>
          <div className="ghx-branch-list">
            {bindingRefs.length === 0 ? (
              <EmptyState>
                No ref observations yet. Reconcile the repository to establish
                the first canonical branch state.
              </EmptyState>
            ) : (
              bindingRefs.map((ref) => {
                const snapshot = snapshots.find(
                  (item) =>
                    item.source_ref === ref.ref_name ||
                    item.target_ref === ref.ref_name,
                );
                const lease = leases.find(
                  (item) => item.ref_name === ref.ref_name && item.status === "active",
                );
                const isSelected = ref.id === selectedRef?.id;
                return (
                  <button
                    className={`ghx-branch-row${isSelected ? " selected" : ""}`}
                    key={ref.id}
                    onClick={() => setRequestedRef(ref.ref_name)}
                    type="button"
                    aria-pressed={isSelected}
                  >
                    <span>
                      <b>{ref.ref_name}</b>
                      {ref.ref_name === selectedBinding?.default_branch ? <em>default</em> : null}
                      {lease ? <em className="lease">leased</em> : null}
                    </span>
                    <code>{shortSha(ref.commit_sha)}</code>
                    <span><Status value={ref.protected ? "protected" : "open"} /></span>
                    <span>
                      <time>{formatTime(ref.observed_at)}</time>
                      {snapshot ? <small>{label(snapshot.classification)}</small> : null}
                    </span>
                  </button>
                );
              })
            )}
          </div>

          {canManage && selectedBinding ? (
            <details className="ghx-operation">
              <summary>Compare branches and plan code sync</summary>
              <form action={captureGitHubBranchSnapshotAction} className="ghx-form-grid">
                <HiddenScope projectId={feed.projectId} actor={actor} />
                <input type="hidden" name="repository_binding_id" value={selectedBinding.id} />
                <label>
                  Source ref
                  <input name="source_ref" placeholder="feature/my-change" required />
                </label>
                <label>
                  Target ref
                  <input name="target_ref" defaultValue={selectedBinding.default_branch} placeholder="main" required />
                </label>
                <SubmitButton className="button secondary" type="submit">Capture immutable snapshot</SubmitButton>
              </form>
              {latestSnapshot ? (
                <form action={planGitHubCodeSyncAction} className="ghx-plan-line">
                  <HiddenScope projectId={feed.projectId} actor={actor} />
                  <input type="hidden" name="repository_binding_id" value={selectedBinding.id} />
                  <input type="hidden" name="snapshot_id" value={latestSnapshot.id} />
                  <span>
                    Latest: <b>{latestSnapshot.source_ref}</b> → <b>{latestSnapshot.target_ref}</b>
                    {" · "}{label(latestSnapshot.classification)}
                  </span>
                  <label>
                    <span className="sr-only">Resolution branch</span>
                    <input name="resolution_branch" placeholder="ai/codex/resolve-…" />
                  </label>
                  <SubmitButton className="button" type="submit">Plan from snapshot</SubmitButton>
                </form>
              ) : null}
            </details>
          ) : null}
        </div>

        <aside className="ghx-inspector" aria-label="Selected branch evidence">
          <div className="ghx-section-label">
            <div>
              <span>Evidence inspector</span>
              <small>{selectedRef?.ref_name ?? "Select a branch"}</small>
            </div>
          </div>
          {selectedRef ? (
            <>
              <dl className="ghx-kv">
                <div><dt>Commit</dt><dd><code>{selectedRef.commit_sha ?? "deleted"}</code></dd></div>
                <div><dt>Tree</dt><dd><code>{selectedRef.tree_sha ?? "—"}</code></dd></div>
                <div><dt>Receipt</dt><dd><code>{shortId(selectedRef.latest_receipt_id)}</code></dd></div>
                <div><dt>Observation</dt><dd><code>{shortId(selectedRef.latest_observation_id)}</code></dd></div>
                <div><dt>Exists</dt><dd>{selectedRef.exists ? "yes" : "no"}</dd></div>
                <div><dt>Protected</dt><dd>{selectedRef.protected ? "yes" : "no"}</dd></div>
              </dl>

              <div className="ghx-inspector-block">
                <h3>Divergence</h3>
                {refSnapshot ? (
                  <>
                    <div className="ghx-divergence">
                      <span><b>{refSnapshot.ahead_by}</b> ahead</span>
                      <span><b>{refSnapshot.behind_by}</b> behind</span>
                      <Status value={refSnapshot.classification} />
                    </div>
                    <p>merge base <code>{shortSha(refSnapshot.merge_base_commit_sha)}</code> · receipt <code>{shortId(refSnapshot.receipt_id)}</code></p>
                  </>
                ) : <p>No comparison snapshot includes this ref.</p>}
              </div>

              <div className="ghx-inspector-block">
                <h3>Writer lease</h3>
                {refLease ? (
                  <dl className="ghx-kv compact">
                    <div><dt>Owner</dt><dd>{refLease.owner_kind} / {refLease.owner_id}</dd></div>
                    <div><dt>Fence</dt><dd>{refLease.fencing_token}</dd></div>
                    <div><dt>Expires</dt><dd>{formatTime(refLease.lease_expires_at)}</dd></div>
                  </dl>
                ) : <p>No active writer. The next mutation must acquire a fenced lease.</p>}
              </div>

              <div className="ghx-inspector-block">
                <h3>Governed work</h3>
                <div className="ghx-work-links">
                  <span>Sync <Status value={refSync?.status} /></span>
                  <span>Merge <Status value={refMerge?.status} /></span>
                </div>
                {refSync?.rollback_ref ? (
                  <p>Rollback <code>{refSync.rollback_ref}</code> at <code>{shortSha(refSync.rollback_sha)}</code></p>
                ) : <p>Rollback evidence appears after a write plan is authorized.</p>}
              </div>
            </>
          ) : (
            <EmptyState>Select a branch to inspect its exact evidence chain.</EmptyState>
          )}
        </aside>
      </div>

      <div className="ghx-flow" aria-label="GitHub governed workflow">
        <div className="ghx-flow-head">
          <div>
            <span>Governed delivery flow</span>
            <small>Every provider write is fenced, receipted, and replay-safe.</small>
          </div>
          <span>{syncRuns.length + conflicts.length + mergeAuthorizations.length} tracked work items</span>
        </div>

        <div className="ghx-flow-lane">
          <div className="ghx-flow-stage">
            <b>01</b><span>Code sync</span><small>snapshot → deterministic plan</small>
          </div>
          <div className="ghx-flow-items">
            {syncRuns.length === 0 ? <span className="ghx-muted">No sync runs</span> : syncRuns.slice(0, 6).map((run) => (
              <div className="ghx-flow-item" key={run.id}>
                <span><b>{run.source_ref}</b> → <b>{run.target_ref}</b></span>
                <Status value={run.status} />
                <span>{label(run.action)}</span>
                <code>{shortId(run.id)}</code>
              </div>
            ))}
          </div>
        </div>

        <div className="ghx-flow-lane">
          <div className="ghx-flow-stage">
            <b>02</b><span>Conflict resolution</span><small>red → green → independent review</small>
          </div>
          <div className="ghx-flow-items">
            {conflicts.length === 0 ? <span className="ghx-muted">No conflicts</span> : conflicts.slice(0, 6).map((conflict) => {
              const plan = resolutionPlans.find((item) => item.conflict_set_id === conflict.id);
              return (
                <div className="ghx-flow-item expanded" key={conflict.id}>
                  <span><b>{conflict.resolution_branch}</b> · {conflict.conflict_count} file conflict{conflict.conflict_count === 1 ? "" : "s"}</span>
                  <Status value={plan?.status ?? conflict.status} />
                  <span>{plan ? `plan v${plan.version}` : "validated plan required"}</span>
                  {canManage && plan?.status === "proposed" ? (
                    <form action={approveGitHubResolutionPlanAction} className="ghx-review-form">
                      <HiddenScope projectId={feed.projectId} actor={actor} />
                      <input type="hidden" name="resolution_plan_id" value={plan.id} />
                      <input name="review_summary" placeholder="Independent review evidence" required />
                      <SubmitButton className="button secondary" type="submit">Approve plan</SubmitButton>
                    </form>
                  ) : (
                    <code>{shortId(plan?.id ?? conflict.id)}</code>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div className="ghx-flow-lane">
          <div className="ghx-flow-stage">
            <b>03</b><span>Merge authorization</span><small>checks + reviews + expiring approval</small>
          </div>
          <div className="ghx-flow-items">
            {mergeAuthorizations.length === 0 ? <span className="ghx-muted">No merge authorizations</span> : mergeAuthorizations.slice(0, 6).map((authorization) => (
              <div className="ghx-flow-item expanded" key={authorization.id}>
                <a href={authorization.pull_request_url} target="_blank" rel="noreferrer">
                  PR #{authorization.pull_request_number}
                </a>
                <Status value={authorization.status} />
                <span>{authorization.observed_checks.length}/{authorization.required_checks.length} checks · {authorization.observed_reviews.length}/{authorization.required_approvals} reviews</span>
                {canManage && authorization.status === "pending_approval" ? (
                  <form action={authorizeGitHubMergeAction} className="ghx-review-form">
                    <HiddenScope projectId={feed.projectId} actor={actor} />
                    <input type="hidden" name="merge_authorization_id" value={authorization.id} />
                    <input name="review_summary" placeholder="Authorization evidence" required />
                    <SubmitButton className="button secondary" type="submit">Authorize</SubmitButton>
                  </form>
                ) : canManage && authorization.status === "authorized" ? (
                  <form action={enqueueGitHubMergeAction}>
                    <HiddenScope projectId={feed.projectId} actor={actor} />
                    <input type="hidden" name="merge_authorization_id" value={authorization.id} />
                    <SubmitButton className="button secondary" type="submit">Enqueue merge</SubmitButton>
                  </form>
                ) : (
                  <code>{shortId(authorization.changeset_id)}</code>
                )}
              </div>
            ))}
          </div>
        </div>

        <div className="ghx-flow-lane">
          <div className="ghx-flow-stage">
            <b>04</b><span>Reconciliation</span><small>webhook + poll + repair converge</small>
          </div>
          <div className="ghx-flow-items">
            {reconciliationRuns.length === 0 ? <span className="ghx-muted">No reconciliation runs</span> : reconciliationRuns.slice(0, 6).map((run) => (
              <div className="ghx-flow-item expanded" key={run.id}>
                <span><b>{label(run.source)}</b> · {run.observation_ids.length} observations</span>
                <Status value={run.status} />
                <span>{run.attempt_count}/{run.max_attempts} attempts · {run.receipt_ids.length} receipts</span>
                {canManage && ["failed", "deadlettered"].includes(run.status) ? (
                  <form action={repairGitHubReconciliationAction}>
                    <HiddenScope projectId={feed.projectId} actor={actor} />
                    <input type="hidden" name="reconciliation_run_id" value={run.id} />
                    <SubmitButton className="button secondary" type="submit">Repair</SubmitButton>
                  </form>
                ) : <code>{shortId(run.id)}</code>}
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="ghx-audit-strip">
        <span>Inbound signals <b>{signals.length}</b></span>
        <span>Snapshots <b>{snapshots.length}</b></span>
        <span>Receipts linked <b>{
          syncRuns.reduce((sum, run) => sum + run.receipt_ids.length, 0) +
          mergeAuthorizations.reduce((sum, run) => sum + run.receipt_ids.length, 0) +
          reconciliationRuns.reduce((sum, run) => sum + run.receipt_ids.length, 0)
        }</b></span>
        <span>Rollback plans <b>{mergeAuthorizations.filter((item) => Object.keys(item.rollback_plan).length > 0).length}</b></span>
      </div>

      {canManage ? (
        <details className="ghx-admin">
          <summary>Connection, repository, and policy setup</summary>
          <div className="ghx-admin-grid">
            <form action={createGitHubConnectionAction} className="ghx-admin-form">
              <HiddenScope projectId={feed.projectId} actor={actor} />
              <h3>Connect GitHub</h3>
              <label>Label<input name="connection_label" placeholder="Production GitHub App" required /></label>
              <label>Authentication
                <select name="auth_method" defaultValue="github_app">
                  <option value="github_app">GitHub App</option>
                  <option value="personal_access_token">Fine-grained token</option>
                </select>
              </label>
              <label>Credential reference<input name="credential_ref" placeholder="secret://github/app-key" required /></label>
              <label>App ID<input name="app_id" placeholder="optional" /></label>
              <label>Installation ID<input name="installation_id" placeholder="optional" /></label>
              <label>Required permissions<textarea name="required_permissions" defaultValue={"contents:read\npull_requests:write\nchecks:read"} /></label>
              <SubmitButton className="button" type="submit">Create connection</SubmitButton>
            </form>

            <form action={createGitHubRepositoryBindingAction} className="ghx-admin-form">
              <HiddenScope projectId={feed.projectId} actor={actor} />
              <h3>Bind repository</h3>
              <label>Connection
                <select name="connection_id" required>
                  <option value="">Choose connection</option>
                  {feed.connections.map((item) => <option key={item.id} value={item.id}>{item.connection_label}</option>)}
                </select>
              </label>
              <label>Repository<input name="repository" placeholder="owner/repository" required /></label>
              <label>Role
                <select name="role" defaultValue="primary">
                  <option value="primary">Primary</option>
                  <option value="source">Source</option>
                  <option value="upstream">Upstream</option>
                  <option value="mirror">Mirror</option>
                </select>
              </label>
              <label>Default branch<input name="default_branch" placeholder="main" /></label>
              <SubmitButton className="button" type="submit">Bind to project</SubmitButton>
            </form>

            <form action={createGitHubBranchPolicyAction} className="ghx-admin-form">
              <HiddenScope projectId={feed.projectId} actor={actor} />
              <h3>Set branch policy</h3>
              <label>Repository
                <select name="repository_binding_id" required>
                  <option value="">Choose repository</option>
                  {feed.bindings.map((item) => <option key={item.id} value={item.id}>{item.full_name}</option>)}
                </select>
              </label>
              <label>Protected refs<textarea name="protected_ref_patterns" defaultValue={"main\nrelease/*"} /></label>
              <label>Automation prefixes<textarea name="allowed_branch_prefixes" defaultValue={"ai/codex/\nai/grok/"} /></label>
              <label>Required checks<textarea name="required_checks" placeholder={"test\nlint\nbuild"} /></label>
              <label>Required approvals<input name="required_approvals" type="number" min="0" defaultValue="1" /></label>
              <SubmitButton className="button" type="submit">Create immutable policy version</SubmitButton>
            </form>
          </div>

          {connection ? (
            <div className="ghx-connection-control">
              <span>
                <b>{connection.connection_label}</b> · credential {connection.credential_configured ? "configured" : "missing"} · {connection.missing_permissions.length} missing permissions
              </span>
              <form action={setGitHubConnectionStateAction}>
                <HiddenScope projectId={feed.projectId} actor={actor} />
                <input type="hidden" name="connection_id" value={connection.id} />
                <input type="hidden" name="operation" value={connection.status === "disabled" ? "enable" : "disable"} />
                <SubmitButton className="button ghost" type="submit">
                  {connection.status === "disabled" ? "Enable" : "Disable"} connection
                </SubmitButton>
              </form>
            </div>
          ) : null}
        </details>
      ) : null}
    </section>
  );
}
