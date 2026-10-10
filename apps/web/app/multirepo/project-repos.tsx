"use client";

/**
 * FACTORY › REPOSITORIES (multi-repo screen 2): which of the org's allowed repositories this
 * project uses, each with a role hint for planning (Contracts · Backend · Frontend · Deploy
 * config …), and the project's mode — One repo (every app builds in one repository, today's
 * behaviour) or Several repos (each app maps its components to repositories).
 *
 * Remove is refused while an app uses the repository (409 in_use: its message names the apps);
 * Several → One is refused while an app has several components (409 apps_use_several). The add
 * picker lists only repositories allowed in Repo and models, and says how many more are not.
 * Owner/Admin write (repository:manage, step-up). `?area=code_tasks&view=repositories`.
 */
import React, { useCallback, useEffect, useState } from "react";
import { GitBranch, Info, LockKey, Warning } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import {
  MULTIREPO_PLACES, ROLE_HINTS, multirepo, refusalMessage, roleLabel,
  type ProjectRepos, type RoleHint,
} from "../../lib/multirepo";
import { areaHref } from "../factory/cockpit-util";
import { showToast } from "../client-forms";
import { GhMark, Lang, RepoName, StatePill, TipQ } from "./parts";

const MODE_TIP = "One repo: every app builds in one repository. Several: each app maps its components to repositories.";

export function ProjectRepositories({ projectId }: { projectId: string }) {
  const [view, setView] = useState<ProjectRepos | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** A refused write, said where it happened (in_use, apps_use_several, …). */
  const [refusal, setRefusal] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const gate = useGate("repository:manage");
  const guarded = useGuardedCall();

  const load = useCallback(async () => {
    try { setView(await multirepo.projectRepos(projectId)); setError(null); }
    catch (e) { setError(refusalMessage(e)); }
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const write = async (what: string, label: string, call: () => Promise<ProjectRepos>, done?: string) => {
    setBusy(what);
    setRefusal(null);
    try {
      setView(await guarded(label, call));
      if (done) showToast("success", done);
    } catch (e) {
      if ((e as { code?: string }).code !== "step_up_cancelled") setRefusal(refusalMessage(e));
    } finally { setBusy(null); }
  };

  if (error) {
    return (
      <div className="mr-prj" data-testid="mr-project-repos">
        <div className="mr-ui-row" role="alert">
          <span className="mr-ui-error" data-testid="mr-prj-error">{error}</span>
          <button type="button" className="ag-btn" onClick={() => { setError(null); void load(); }}>Retry</button>
        </div>
      </div>
    );
  }
  if (!view) return <div className="mr-prj" data-testid="mr-project-repos"><p className="mr-ui-empty">Loading…</p></div>;

  const multi = view.repo_mode === "multi";
  const notAllowed = view.not_allowed_count ?? 0;
  const apps = view.apps ?? [];
  const registryLink = areaHref(MULTIREPO_PLACES.registry);

  return (
    <div className="mr-prj" data-testid="mr-project-repos">
      <div className="mr-ui-grid2">
        <div className="mr-ui-card">
          <div className="mr-ui-card-h">
            <h3>Repositories</h3>
            <span className="mr-ui-sub">Which of the allowed repositories this project uses</span>
            <span className="mr-ui-spacer" />
            <span className="mr-ui-seg" role="radiogroup" aria-label="Repo mode" title={MODE_TIP} data-testid="mr-prj-mode">
              {(["single", "multi"] as const).map((m) => {
                const on = view.repo_mode === m;
                return (
                  <button key={m} type="button" role="radio" aria-checked={on} data-testid={`mr-prj-mode-${m}`}
                          disabled={!!busy} {...gate.props}
                          title={gate.allowed ? (m === "single" ? "Every app builds in one repository" : "Each app maps its components to repositories") : gate.reason}
                          onClick={gate.guard(() => {
                            if (on) return;
                            void write("mode", "Change how this project's apps use repositories.",
                                       () => multirepo.setRepoMode(projectId, m), m === "multi" ? "Several repos" : "One repo");
                          })}>
                    {m === "single" ? "One repo" : "Several repos"}{gate.sr}
                  </button>
                );
              })}
            </span>
          </div>

          {refusal ? (
            <div className="mr-ui-note" data-tone="bad" role="alert" data-testid="mr-prj-refusal">
              <Warning size={15} weight="bold" aria-hidden /><div>{refusal}</div>
            </div>
          ) : null}

          {view.items.length ? (
            <div className="mr-ui-table-wrap">
              <table className="mr-ui-table" data-testid="mr-prj-table">
                <thead>
                  <tr>
                    <th>Repository</th>
                    <th>Role <TipQ tip="A hint for planning and suggestions. The app decides which component lives where." /></th>
                    <th className="mr-hide-narrow">Used by apps</th>
                    <th className="mr-hide-narrow">Base</th>
                    <th>Status</th>
                    <th><span className="ag-sr">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  {view.items.map((r) => {
                    const used = r.used_by_apps ?? [];
                    return (
                      <tr key={r.binding_id} data-testid={`mr-prj-row-${r.binding_id}`}>
                        <td><RepoName name={r.full_name} sub={<Lang lang={r.language} />} /></td>
                        <td>
                          {gate.allowed ? (
                            <select className="mr-ui-select" value={r.role_hint ?? ""} disabled={!!busy}
                                    aria-label={`Role of ${r.full_name}`} data-testid={`mr-prj-role-${r.binding_id}`}
                                    onChange={(e) => void write(`role:${r.binding_id}`, "Change this repository's role.",
                                      () => multirepo.putProjectRepo(projectId, r.binding_id,
                                                                     { role_hint: (e.target.value || null) as RoleHint | null }))}>
                              <option value="">No role</option>
                              {ROLE_HINTS.map((h) => <option key={h.key} value={h.key}>{h.label}</option>)}
                            </select>
                          ) : (
                            <span className="mr-ui-chip static" title={gate.reason} data-testid={`mr-prj-role-${r.binding_id}`}>
                              {roleLabel(r.role_hint) || "No role"}
                            </span>
                          )}
                        </td>
                        <td className="mr-ui-mut mr-hide-narrow" data-user-text>{used.length ? used.map((a) => a.app_name).join(", ") : "—"}</td>
                        <td className="mr-ui-mono mr-ui-dim mr-hide-narrow">
                          {r.base_branch || "main"}{r.is_default ? <span className="mr-ui-dim" title="The project's default repository"> · default</span> : null}
                        </td>
                        <td><StatePill state={r.state} /></td>
                        <td className="end">
                          <GatedButton permission="repository:manage" className="ag-btn" disabled={!!busy}
                                       title={used.length ? `Used by ${used.map((a) => a.app_name).join(", ")}: remove it from those apps first`
                                         : "Remove from this project. Blocked while an app uses it."}
                                       data-testid={`mr-prj-remove-${r.binding_id}`}
                                       onClick={() => void write(`remove:${r.binding_id}`, "Remove a repository from this project.",
                                         () => multirepo.removeProjectRepo(projectId, r.binding_id), `Removed ${r.full_name}`)}>
                            Remove
                          </GatedButton>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="mr-ui-empty" data-testid="mr-prj-empty">This project uses no repository yet. Add one below.</p>
          )}

          <div className="mr-ui-foot mr-prj-add">
            <select className="mr-ui-select mono" value="" disabled={!!busy || !view.addable.length} {...gate.props}
                    aria-label="Add a repository" data-testid="mr-prj-add"
                    onChange={(e) => {
                      const id = e.target.value;
                      if (!id || !gate.allowed) return;
                      const name = view.addable.find((a) => a.id === id)?.full_name ?? "it";
                      void write(`add:${id}`, "Add a repository to this project.", () => multirepo.putProjectRepo(projectId, id, {}), `Added ${name}`);
                    }}>
              <option value="">{view.addable.length ? "Add a repository…" : "Nothing more to add"}</option>
              {view.addable.map((a) => (
                <option key={a.id} value={a.id}>{a.full_name}{a.language ? ` · ${a.language}` : ""}</option>
              ))}
            </select>
            <span className="mr-ui-sub" data-testid="mr-prj-add-tip">
              Lists only repositories allowed in <a className="mr-ui-link" href={registryLink}>Repo and models</a>
              {notAllowed ? <> · {notAllowed} more there {notAllowed === 1 ? "is" : "are"} not allowed.</> : "."}
            </span>
          </div>
          {view.registry_in_use === false ? (
            <div className="mr-ui-note" data-testid="mr-prj-no-registry" style={{ marginTop: 12 }}>
              <Info size={15} aria-hidden />
              <div>No repository is allowed for BOSS yet. Tick them in <a className="mr-ui-link" href={registryLink}>Repo and models › Repositories</a>.</div>
            </div>
          ) : null}
        </div>

        <div className="mr-ui-col">
          <div className="mr-ui-note">
            <LockKey size={15} aria-hidden />
            <div><b>Access follows the app.</b> A run gets a GitHub grant for exactly its app&apos;s repositories, never the whole project or organization.</div>
          </div>
          <div className="mr-ui-card" style={{ padding: 14 }}>
            <div className="mr-ui-eyebrow" style={{ marginBottom: 6 }}>Apps in this project</div>
            {apps.length ? (
              <div data-testid="mr-prj-apps">
                {apps.map((a) => (
                  <div key={a.app_key} className="mr-ui-xrow" data-testid={`mr-prj-app-${a.app_key}`}>
                    <GhMark size="sm" />
                    <div>
                      <b data-user-text>{a.app_name}</b>
                      <small>{[a.chain || (multi ? "No components yet" : "One repository"),
                               a.deploys_with ? `deploys with ${a.deploys_with}` : ""].filter(Boolean).join(" · ")}</small>
                    </div>
                    <a className="mr-ui-link" href={areaHref(MULTIREPO_PLACES.components(a.app_key))}
                       title={`${a.app_name}'s components`}>Open</a>
                  </div>
                ))}
              </div>
            ) : <p className="mr-ui-sub">No app has components yet.</p>}
          </div>
          <div className="mr-ui-note">
            <GitBranch size={15} aria-hidden />
            <div>BOSS works on <span className="mr-ui-code">boss/*</span> branches and merges through pull requests. It never pushes to a base branch.</div>
          </div>
        </div>
      </div>
    </div>
  );
}
