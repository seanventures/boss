"use client";

/**
 * Infra → Deploy a repository: a whole application stack, from a registered repository at a ref,
 * onto a VM BOSS onboarded — planned, approved, run and health-gated by BOSS.
 *
 * The repository says HOW it is deployed (its `boss.deploy.yaml`: install/update steps, env,
 * health); this panel only says WHAT (repository + ref) and WHERE (a stack target on a VM). That
 * split is the point: the console never learns how BOSS, or anything as large, is built — so any
 * application that ships a manifest deploys through the same four clicks.
 *
 * Every state the operator can see is the API's record, re-read while it moves. Buttons are the
 * record's own `actions`, decided by the API — never a guess from status or what was last pressed.
 */
import { GatedButton } from "../components/gated";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { onChanged } from "./changed";

import { browserHref } from "../lib/browser-links";
import { formatDateTime } from "../lib/datetime";
import {
  DEFAULT_MANIFEST_PATH, DEFAULT_REF, canHostStack, createStackTarget, decideStackDeploy, deploymentActions,
  deployFormProblem, getStackDeployment, isAttached, isLive, listStackDeployments, loadHosts, loadRepositories, loadStackTargets,
  planStackDeploy, rollbackStackDeploy, shortCommit, statusLabel, statusTone,
  stepText, targetFormProblem, targetManifestPath, targetPublicUrl, targetRepository,
  type DeployHost, type Repository, type StackDeployment, type StackTarget,
} from "../lib/stack-deploy";
import { useOnScreen } from "../lib/area-shown";

/** How often a moving deployment is re-read. A deploy takes minutes; a step takes seconds. */
export const POLL_MS = 3000;

function when(iso?: string | null): string {
  return iso ? formatDateTime(iso) : "";
}

export function StackDeployPanel({ projectId, actor }: { projectId: string; actor: string }) {
  const [hosts, setHosts] = useState<DeployHost[]>([]);
  const [targets, setTargets] = useState<StackTarget[]>([]);
  const [repos, setRepos] = useState<Repository[]>([]);
  const [suggested, setSuggested] = useState("");
  const [deployments, setDeployments] = useState<StackDeployment[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [openId, setOpenId] = useState<string | null>(null);
  const [addingTarget, setAddingTarget] = useState(false);
  const [deployTo, setDeployTo] = useState<string>("");

  const reload = useCallback(async () => {
    const [h, t, r, d] = await Promise.all([
      loadHosts(projectId), loadStackTargets(projectId), loadRepositories(projectId),
      listStackDeployments(projectId),
    ]);
    const problems: string[] = [];
    if (h.ok) setHosts(h.data); else problems.push(h.error);
    if (t.ok) setTargets(t.data); else problems.push(t.error);
    if (r.ok) { setRepos(r.data.repositories); setSuggested(r.data.suggested); } else problems.push(r.error);
    if (d.ok) setDeployments(d.data); else problems.push(d.error);
    setLoadError(problems.length ? problems.join(" ") : null);
  }, [projectId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void reload().finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [reload]);

  // A host added or verified in the Hosts card, a repository connected in Setup, a decision
  // approved elsewhere: this panel fetches for itself, so it must hear about them.
  useEffect(() => onChanged(() => { void reload(); }, "stack-deploy"), [reload]);

  const verified = hosts.filter(canHostStack);
  const hostName = (id?: string | null) => hosts.find((h) => h.id === id)?.name ?? "";
  const byTarget = useMemo(() => {
    const out: Record<string, StackDeployment[]> = {};
    for (const d of deployments) (out[d.target_id] ??= []).push(d);
    for (const list of Object.values(out)) {
      list.sort((a, b) => String(b.created_at ?? "").localeCompare(String(a.created_at ?? "")));
    }
    return out;
  }, [deployments]);

  function opened(d: StackDeployment) {
    setDeployments((prev) => [d, ...prev.filter((x) => x.id !== d.id)]);
    setOpenId(d.id);
  }

  return (
    <section className="ct-block sd-panel" data-testid="stack-deploy-panel" aria-label="Deploy a repository">
      <div className="ct-block-title">Deploy a repository</div>
      <p className="detail" style={{ margin: 0 }}>
        BOSS deploys a registered repository at a branch, tag or commit onto a verified VM. The
        repository says how in its <code>{DEFAULT_MANIFEST_PATH}</code>; every deployment is
        planned first and runs only after you approve it.
      </p>

      {loading ? <p className="detail">Loading stack targets…</p> : null}
      {loadError ? <p className="detail" role="alert" data-testid="stack-load-error">{loadError}</p> : null}

      {!loading ? (
        <>
          <div className="sd-targets" data-testid="stack-targets">
            {targets.length === 0 ? (
              <p className="detail" data-testid="stack-targets-empty" style={{ margin: 0 }}>
                {verified.length
                  ? "No stack targets yet. Add one to say where a repository deploys."
                  : "No stack targets yet. Add and verify a VM under Hosts first — a stack target names the VM it runs on."}
              </p>
            ) : targets.map((t) => {
              const recent = (byTarget[t.id] ?? []).slice(0, 5);
              const url = targetPublicUrl(t);
              return (
                <article key={t.id} className="ct-outcome sd-target" data-testid="stack-target"
                         data-target-id={t.id}>
                  <div className="sd-row">
                    <strong>{t.app_name}</strong>
                    <span className="cap-badge">{t.environment}</span>
                    <span className="detail">
                      {isAttached(t) ? `on ${hostName(t.host_id) || t.target_ref}` : "not on a VM yet"}
                      {" · "}<code>{targetRepository(t)}</code>
                      {" · "}<code>{targetManifestPath(t)}</code>
                    </span>
                    {url ? <a className="detail" href={browserHref(url)} target="_blank" rel="noreferrer">{url}</a> : null}
                    {isAttached(t) ? (
                      <GatedButton permission="deployment:create" type="button" className="cap-btn primary" data-testid="stack-target-deploy"
                              onClick={() => setDeployTo(t.id)}>
                        Deploy
                      </GatedButton>
                    ) : null}
                  </div>
                  {isAttached(t) ? null : (
                    <p className="detail" data-testid="stack-target-unattached">
                      Not on a VM, so it cannot deploy. Add the stack target again on a verified VM.
                    </p>
                  )}
                  {recent.length ? (
                    <ul className="sd-recent" data-testid="stack-recent">
                      {recent.map((d) => (
                        <li key={d.id}>
                          <button type="button" className="sd-link" onClick={() => setOpenId(openId === d.id ? null : d.id)}
                                  aria-expanded={openId === d.id}>
                            <span className={`cap-badge tone-${statusTone(String(d.status))}`}>{statusLabel(String(d.status))}</span>
                            {" "}<code>{d.ref}</code>
                            {d.commit ? <> · <code>{shortCommit(d.commit)}</code></> : null}
                            <span className="detail"> · {when(d.created_at)}</span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : <span className="detail">Nothing deployed here yet.</span>}
                </article>
              );
            })}
          </div>

          <div className="sd-row">
            <button type="button" className="cap-btn" aria-expanded={addingTarget}
                    data-testid="stack-add-target-toggle"
                    onClick={() => setAddingTarget((v) => !v)}>
              {addingTarget ? "Cancel" : "Add a stack target"}
            </button>
          </div>

          {addingTarget ? (
            <AddStackTarget projectId={projectId} actor={actor} hosts={verified} repos={repos}
                            suggested={suggested}
                            onAdded={async (t) => { setAddingTarget(false); setDeployTo(t.id); await reload(); }} />
          ) : null}

          <DeployRepository projectId={projectId} actor={actor} repos={repos} suggested={suggested}
                            targets={targets.filter(isAttached)} targetId={deployTo} onTarget={setDeployTo}
                            onPlanned={opened} />

          {openId ? (
            <StackDeploymentView key={openId} deploymentId={openId} actor={actor}
                                 initial={deployments.find((d) => d.id === openId) ?? null}
                                 onChanged={(d) => setDeployments((prev) => prev.map((x) => (x.id === d.id ? d : x)))}
                                 onOpened={opened}
                                 onClose={() => setOpenId(null)} />
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function AddStackTarget({ projectId, actor, hosts, repos, suggested, onAdded }: {
  projectId: string; actor: string; hosts: DeployHost[]; repos: Repository[]; suggested: string;
  onAdded: (t: StackTarget) => void | Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const value = (k: string) => String(form.get(k) ?? "").trim();
    const input = {
      hostId: value("host_id"), name: value("name"), environment: value("environment"),
      repository: value("repository"), publicUrl: value("public_url"),
      manifestPath: value("manifest_path") || DEFAULT_MANIFEST_PATH,
    };
    const problem = targetFormProblem(input);
    if (problem) { setError(problem); return; }
    const host = hosts.find((h) => h.id === input.hostId);
    if (!host) { setError("That VM is no longer reachable — verify it under Hosts."); return; }
    setBusy(true); setError(null);
    try {
      const r = await createStackTarget(projectId, actor, { ...input, host });
      if (!r.ok) { setError(r.error); return; }
      await onAdded(r.data);
    } finally { setBusy(false); }
  }

  if (!hosts.length) {
    return (
      <p className="detail" data-testid="stack-add-target-no-host">
        A stack target runs on a verified VM. Add one under Hosts above, put its key on the VM,
        and press Verify — then come back here.
      </p>
    );
  }
  return (
    <form className="cap-add sd-form" data-testid="stack-add-target" onSubmit={submit} noValidate>
      <div className="cap-add-row">
        <label>
          VM
          <select name="host_id" defaultValue={hosts[0]?.id}>
            {hosts.map((h) => (
              <option key={h.id} value={h.id}>{h.name} · {h.ssh_user || "root"}@{h.address}</option>
            ))}
          </select>
        </label>
        <label>
          Stack name
          <input name="name" placeholder="boss" autoComplete="off" spellCheck={false} />
        </label>
        <label>
          Environment
          <input name="environment" defaultValue="production" autoComplete="off" />
        </label>
      </div>
      <div className="cap-add-row">
        <RepoSelect repos={repos} suggested={suggested} />
        <label>
          Public URL (optional)
          <input name="public_url" placeholder="https://boss.example.com" autoComplete="off" spellCheck={false} />
        </label>
        <label>
          Manifest path
          <input name="manifest_path" defaultValue={DEFAULT_MANIFEST_PATH} autoComplete="off" spellCheck={false} />
        </label>
      </div>
      {error ? <p className="detail" role="alert" data-testid="stack-add-target-error">{error}</p> : null}
      <div>
        <GatedButton permission="deployment_target:manage" className="cap-btn primary" type="submit" disabled={busy}>
          {busy ? "Adding…" : "Add stack target"}
        </GatedButton>
      </div>
    </form>
  );
}

function RepoSelect({ repos, suggested, value, onChange }: {
  repos: Repository[]; suggested: string; value?: string; onChange?: (v: string) => void;
}) {
  if (!repos.length) {
    return (
      <label>
        Repository
        <span className="detail" data-testid="stack-no-repos">
          No repository is registered — connect one under Repo and models.
        </span>
        <input type="hidden" name="repository" value="" />
      </label>
    );
  }
  const initial = repos.some((r) => r.full_name === suggested) ? suggested : repos[0].full_name;
  return (
    <label>
      Repository
      <select name="repository"
              {...(onChange ? { value: value || initial, onChange: (e: React.ChangeEvent<HTMLSelectElement>) => onChange(e.target.value) }
                : { defaultValue: initial })}>
        {repos.map((r) => (
          <option key={r.full_name} value={r.full_name}>
            {r.full_name}{r.role === "primary" ? " (primary)" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

function DeployRepository({ projectId, actor, repos, suggested, targets, targetId, onTarget, onPlanned }: {
  projectId: string; actor: string; repos: Repository[]; suggested: string; targets: StackTarget[];
  targetId: string; onTarget: (id: string) => void; onPlanned: (d: StackDeployment) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [repository, setRepository] = useState("");
  const selected = targets.find((t) => t.id === targetId) ?? targets[0];

  // The target names its repository; choosing a target picks it, and the operator can still
  // choose another registered one (a fork, a second app in the same stack).
  useEffect(() => {
    const own = selected ? targetRepository(selected) : "";
    if (own && repos.some((r) => r.full_name === own)) setRepository(own);
  }, [selected, repos]);

  if (!targets.length) return null;

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const input = {
      repository: String(form.get("repository") ?? "").trim(),
      ref: String(form.get("ref") ?? "").trim(),
      targetId: String(form.get("target_id") ?? "").trim(),
    };
    const problem = deployFormProblem(input);
    if (problem) { setError(problem); return; }
    setBusy(true); setError(null);
    try {
      const r = await planStackDeploy(projectId, actor, input);
      if (!r.ok) { setError(r.error); return; }
      onPlanned(r.data);
    } finally { setBusy(false); }
  }

  return (
    <form className="cap-add sd-form" data-testid="stack-deploy-form" onSubmit={submit} noValidate>
      <div className="cap-add-row">
        <RepoSelect repos={repos} suggested={suggested} value={repository} onChange={setRepository} />
        <label>
          Ref
          <input name="ref" defaultValue={DEFAULT_REF} autoComplete="off" spellCheck={false}
                 placeholder="main, a tag, or a commit SHA" />
        </label>
        <label>
          Deploy to
          <select name="target_id" value={selected?.id ?? ""} onChange={(e) => onTarget(e.target.value)}>
            {targets.map((t) => (
              <option key={t.id} value={t.id}>{t.app_name} · {t.environment}</option>
            ))}
          </select>
        </label>
      </div>
      <p className="detail" style={{ margin: 0 }}>
        Planning reads the manifest at that commit and checks the VM without changing it. Nothing
        runs on the VM until you approve the plan.
      </p>
      {error ? <p className="detail" role="alert" data-testid="stack-deploy-error">{error}</p> : null}
      <div>
        <GatedButton permission="deployment:create" className="cap-btn primary" type="submit" disabled={busy || !repos.length}>
          {busy ? "Planning…" : "Plan deployment"}
        </GatedButton>
      </div>
    </form>
  );
}

export function StackDeploymentView({ deploymentId, actor, initial, onChanged, onOpened, onClose, pollMs = POLL_MS }: {
  deploymentId: string; actor: string; initial: StackDeployment | null;
  onChanged?: (d: StackDeployment) => void; onOpened?: (d: StackDeployment) => void;
  onClose?: () => void; pollMs?: number;
}) {
  const [d, setD] = useState<StackDeployment | null>(initial);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const r = await getStackDeployment(deploymentId);
    if (r.ok) { setD(r.data); onChanged?.(r.data); setError(null); }
    else setError(r.error);
    return r.ok ? r.data : null;
    // onChanged is a parent callback; re-creating the poll for it would restart the timer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deploymentId]);

  useEffect(() => { if (!initial) void refresh(); }, [initial, refresh]);

  const live = isLive(d?.status);
  const onScreen = useOnScreen();   // off screen, nothing is asked (lib/area-shown.ts)
  useEffect(() => {
    if (!live || !onScreen) return;
    const timer = window.setInterval(() => { void refresh(); }, pollMs);
    return () => window.clearInterval(timer);
  }, [live, refresh, pollMs, onScreen]);

  if (!d) {
    return (
      <div className="ct-outcome" data-testid="stack-deployment">
        {error ? <p className="detail" role="alert">{error}</p> : <p className="detail">Loading deployment…</p>}
      </div>
    );
  }

  const actions = deploymentActions(d);
  const status = String(d.status);
  const manifest = d.manifest ?? {};

  async function decide(verdict: "approve" | "reject") {
    if (!d?.decision_id) return;
    setBusy(true); setError(null);
    try {
      const r = await decideStackDeploy(d.decision_id, actor, verdict);
      if (!r.ok) setError(r.error);
      await refresh();
    } finally { setBusy(false); }
  }

  async function rollback() {
    if (!d) return;
    setBusy(true); setError(null);
    try {
      const r = await rollbackStackDeploy(d.id, actor);
      if (!r.ok) { setError(r.error); return; }
      onOpened?.(r.data);
    } finally { setBusy(false); }
  }

  return (
    <article className="ct-outcome sd-deployment" data-testid="stack-deployment" data-status={status}>
      <div className="sd-row">
        <strong>{manifest.name || d.repository}</strong>
        <span className={`cap-badge tone-${statusTone(status)}`} data-testid="stack-status">{statusLabel(status)}</span>
        <span className="detail">
          <code>{d.repository}</code> @ <code>{d.ref}</code>
          {d.commit ? <> → <code>{shortCommit(d.commit)}</code></> : null}
        </span>
        {onClose ? <button type="button" className="cap-btn" onClick={onClose}>Close</button> : null}
      </div>

      {status === "failed" || d.error?.headline ? (
        <div className="sd-failure" role="alert" data-testid="stack-failure">
          <strong>{d.error?.headline || "The deployment failed."}</strong>
          {d.error?.action ? <p className="detail" style={{ margin: 0 }}>{d.error?.action}</p> : null}
        </div>
      ) : null}

      <details className="sd-manifest" open={status === "awaiting_approval"} data-testid="stack-manifest">
        <summary className="detail">
          What the manifest will do{manifest.kind ? ` (${manifest.kind})` : ""}
        </summary>
        {planSteps(manifest.steps).length ? (
          <ol className="sd-steps">
            {planSteps(manifest.steps).map((s, i) => <li key={i}><code>{s}</code></li>)}
          </ol>
        ) : null}
        {(manifest.env_keys ?? []).length ? (
          <p className="detail" data-testid="stack-env-keys" style={{ margin: 0 }}>
            Environment (names only):{" "}
            {(manifest.env_keys ?? []).map((k) => <code key={k} className="sd-chip">{k}</code>)}
          </p>
        ) : null}
        {(manifest.health ?? []).length ? (
          <ul className="detail sd-health" style={{ margin: 0 }}>
            {(manifest.health ?? []).map((h, i) => (
              <li key={i}>Health: <code>{h.url}</code> answers {h.expect ?? 200}
                {h.timeout_seconds ? ` within ${h.timeout_seconds}s` : ""}</li>
            ))}
          </ul>
        ) : null}
      </details>

      {(d.steps ?? []).length ? (
        <ol className="fleet-steps sd-progress" aria-label="Deployment progress" data-testid="stack-progress">
          {(d.steps ?? []).map((s, i) => (
            <li key={`${s.name}-${i}`} className="fleet-step" data-state={
              s.status === "succeeded" || s.status === "done" || s.status === "healthy" ? "done"
                : s.status === "running" ? "current" : s.status === "failed" ? "failed" : "todo"}>
              <span className="fleet-step-dot">{s.status === "succeeded" || s.status === "done" ? "✓" : i + 1}</span>
              <span>{s.name}</span>
              <span className="detail"> {s.status}</span>
            </li>
          ))}
        </ol>
      ) : null}

      {d.log_tail ? (
        <details open={live || status === "failed"}>
          <summary className="detail">Log</summary>
          <pre className="hw-command sd-log" data-testid="stack-log">{d.log_tail}</pre>
        </details>
      ) : null}

      {status === "awaiting_approval" && !d.decision_id ? (
        <p className="detail">Waiting for the approval to be raised…</p>
      ) : null}
      {status === "running" ? <p className="detail">Deploying — this view follows it.</p> : null}

      {error ? <p className="detail" role="alert" data-testid="stack-action-error">{error}</p> : null}

      {actions.length ? (
        <div className="sd-row" data-testid="stack-actions">
          {actions.includes("approve") ? (
            <GatedButton permission="decision:approve" type="button" className="cap-btn primary" disabled={busy} onClick={() => void decide("approve")}>
              Approve
            </GatedButton>
          ) : null}
          {actions.includes("reject") ? (
            <GatedButton permission="decision:approve" type="button" className="cap-btn" disabled={busy} onClick={() => void decide("reject")}>
              Reject
            </GatedButton>
          ) : null}
          {actions.includes("open") && d.open_url ? (
            <a className="cap-btn primary" data-testid="stack-open" href={browserHref(d.open_url)}
               target="_blank" rel="noreferrer">
              Open app
            </a>
          ) : null}
          {actions.includes("rollback") ? (
            <GatedButton permission="deployment:rollback" type="button" className="cap-btn" disabled={busy} onClick={() => void rollback()}
                    data-testid="stack-rollback"
                    title={`Plans a deployment of ${shortCommit(d.rollback_commit) || "the previous healthy commit"}, through the same approval`}>
              Roll back{d.rollback_commit ? ` to ${shortCommit(d.rollback_commit)}` : ""}
            </GatedButton>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

/** The plan's steps as lines. The engine groups them by phase — {install: [...], update: [...]}
 *  — and this read them as a flat list, so every plan said "No steps recorded". */
export function planSteps(steps: unknown): string[] {
  if (Array.isArray(steps)) return steps.map((s) => stepText(s as never));
  if (steps && typeof steps === "object") {
    return Object.entries(steps as Record<string, unknown>).flatMap(([phase, list]) =>
      (Array.isArray(list) ? list : []).map((s) => `${phase}: ${stepText(s as never)}`));
  }
  return [];
}
