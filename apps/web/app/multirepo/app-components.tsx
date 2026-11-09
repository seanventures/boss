"use client";

/**
 * FACTORY › APPS › APP › COMPONENTS (multi-repo screen 4): what an app is built from, its
 * components left to right in dependency order (build order; deploys go the same way), each with
 * its repository, language, folder, live version, where it ships and what it needs; the deploy
 * config it reads; and its recent changes, one row per change set however many repositories the
 * change touched.
 *
 * "Edit components" maps components to the project's repositories and PUTs the whole list; the
 * API refuses a cycle, a read-only build target, a repository not in the project (its message is
 * shown as is). Owner/Admin write (repository:manage, step-up); everyone may read.
 * `?area=code_tasks&view=components&app=<app key or any of its builds>`.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowRight, FileCode, LockKey, PencilSimple, Plus, Warning } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import {
  CHANGE_SET_PLACES, MULTIREPO_PLACES, agoWords, buildOrder, changeTone, changeWord, listWords, multirepo, refusalCode, refusalMessage,
  shipsTo, stateWords, type AppComponent, type AppComponents, type ComponentStatus, type ProjectRepo,
} from "../../lib/multirepo";
import { withConsent } from "../../lib/identity/consent";
import { Sheet } from "../../components/identity/kit";
import { NEW_BUILD_TIP } from "../../lib/compose-links";
import { useNow } from "../agents/live-util";
import { areaHref } from "../factory/cockpit-util";
import { showToast } from "../client-forms";
import { ComponentRows, Lang, RepoName, TipQ, rowKey, rowsToInput, type MapRow } from "./parts";

/** Code components in build order (the server's `order`, else a topological sort here). */
export function inBuildOrder(cs: AppComponent[]): AppComponent[] {
  const order = buildOrder(cs.map((c) => ({ name: c.name, kind: "code", depends_on: c.depends_on ?? [] })));
  const at = (c: AppComponent) => c.order ?? order.get(c.name) ?? Number.MAX_SAFE_INTEGER;
  return [...cs].sort((a, b) => at(a) - at(b));
}

function toRows(data: AppComponents): MapRow[] {
  return [...inBuildOrder(data.components), ...data.deploy_config].map((c) => ({
    key: rowKey(), name: c.name, binding_id: c.binding_id, path: c.path ?? "/", kind: c.kind ?? "code",
    depends_on: c.depends_on ?? [], ship_target: c.ship_target ?? undefined,
  }));
}

export function AppComponentsView({ projectId, appKey, onBack }: { projectId: string; appKey: string; onBack: () => void }) {
  const [data, setData] = useState<AppComponents | null>(null);
  const [repos, setRepos] = useState<ProjectRepo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<MapRow[] | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [asking, setAsking] = useState(false);
  const guarded = useGuardedCall();
  const now = useNow(60_000);

  const load = useCallback(async () => {
    try {
      const [c, p] = await Promise.all([
        multirepo.components(projectId, appKey),
        multirepo.projectRepos(projectId).catch(() => null),
      ]);
      setData(c);
      setRepos(p?.items ?? []);
      setError(null);
    } catch (e) { setError(refusalMessage(e)); }
  }, [projectId, appKey]);
  useEffect(() => { void load(); }, [load]);

  const byBinding = useMemo(() => new Map(repos.map((r) => [r.binding_id, r])), [repos]);

  const back = (
    <button type="button" className="mr-ui-link" onClick={onBack} data-testid="mr-cmp-back">
      <ArrowLeft size={12} aria-hidden /> Apps
    </button>
  );
  if (error) {
    return (
      <div className="mr-cmp" data-testid="mr-components">
        <div className="mr-cmp-crumb">{back}</div>
        <div className="mr-ui-row" role="alert">
          <span className="mr-ui-error" data-testid="mr-cmp-error">{error}</span>
          <button type="button" className="ag-btn" onClick={() => { setError(null); void load(); }}>Retry</button>
        </div>
      </div>
    );
  }
  if (!data) return <div className="mr-cmp" data-testid="mr-components"><div className="mr-cmp-crumb">{back}</div><p className="mr-ui-empty">Loading…</p></div>;

  const code = inBuildOrder(data.components);
  const deploy = data.deploy_config;
  const writeRepos = [...new Set(code.map((c) => c.repository).filter((r): r is string => !!r))];
  const readRepos = [...new Set(deploy.map((c) => c.repository).filter((r): r is string => !!r && !writeRepos.includes(r)))];
  const changes = data.change_sets ?? [];
  const app = data.app_name || appKey;

  const save = async () => {
    if (!editing) return;
    setBusy(true);
    setSaveError(null);
    try {
      const out = await guarded(`Change what ${app} is built from.`, () =>
        multirepo.putComponents(projectId, data.app_key || appKey, rowsToInput(editing), data.app_name || undefined));
      if (out && Array.isArray(out.components)) setData({ ...out, deploy_config: out.deploy_config ?? [], change_sets: data.change_sets });
      else void load();
      setEditing(null);
      showToast("success", "Components saved");
    } catch (e) {
      if ((e as { code?: string }).code !== "step_up_cancelled") setSaveError(refusalMessage(e));
    } finally { setBusy(false); }
  };
  const startEdit = () => {
    setSaveError(null);
    const rows = toRows(data);
    setEditing(rows.length ? rows : [{ key: rowKey(), name: "", binding_id: "", path: "/", kind: "code", depends_on: [] }]);
  };

  return (
    <div className="mr-cmp" data-testid="mr-components">
      <div className="mr-cmp-head">
        <div style={{ minWidth: 0 }}>
          <div className="mr-cmp-crumb">{back}<span aria-hidden>›</span><b data-user-text>{app}</b><span aria-hidden>›</span>Components</div>
          <p className="mr-ui-lead" data-testid="mr-cmp-lead">
            {code.length > 1
              ? <>{app} is built from {writeRepos.length === 1 ? "one repository" : `${writeRepos.length} repositories`}. Builds go left to right; deploys too.</>
              : code.length === 1 ? <>{app} builds in one repository.</>
              : <>{app} has no components yet: it builds in the project&apos;s repository. Map its parts to repositories to build them together.</>}
          </p>
        </div>
        <span className="mr-ui-spacer" />
        {editing ? null : (
          <GatedButton permission="repository:manage" className="ag-btn" data-testid="mr-cmp-edit"
                       title="Map this app's components to the project's repositories" onClick={startEdit}>
            {code.length || deploy.length ? <PencilSimple size={13} aria-hidden /> : <Plus size={13} aria-hidden />}
            {code.length || deploy.length ? " Edit components" : " Add component"}
          </GatedButton>
        )}
        {code.length > 1 ? (
          <GatedButton permission="code_task:create" className="ag-btn ag-btn-primary" data-testid="mr-cmp-new"
                       title={`One change across ${app}'s repositories: planned, built and reviewed as one set`}
                       onClick={() => setAsking(true)}>
            <Plus size={13} aria-hidden /> New change
          </GatedButton>
        ) : (
          <GatedButton permission="workflow_run:start" className="ag-btn ag-btn-primary" data-testid="mr-cmp-new"
                       title={NEW_BUILD_TIP}
                       onClick={() => { window.location.assign(areaHref({ area: "delivery_workflows", compose: "add", app: appKey })); }}>
            New change
          </GatedButton>
        )}
      </div>

      {editing ? (
        <div className="mr-ui-card mr-cmp-edit" data-testid="mr-cmp-editor">
          <div className="mr-ui-card-h">
            <h3>Edit components</h3>
            <span className="mr-ui-sub">Saved as one list: order follows what each component needs</span>
          </div>
          {repos.length ? (
            <ComponentRows rows={editing} repos={repos} onChange={setEditing} disabled={busy} withPath withNeeds testPrefix="mr-cmp" />
          ) : (
            <p className="mr-ui-sub">This project has no repositories to map to. Add them in <a className="mr-ui-link" href={areaHref(MULTIREPO_PLACES.projectRepos)}>Repositories</a>.</p>
          )}
          <div className="mr-ui-row">
            <button type="button" className="mr-ui-link" data-testid="mr-cmp-add-row" disabled={busy}
                    onClick={() => setEditing([...editing, { key: rowKey(), name: "", binding_id: "", path: "/", kind: "code", depends_on: [] }])}>
              <Plus size={12} aria-hidden /> Add a component
            </button>
          </div>
          {saveError ? (
            <div className="mr-ui-note" data-tone="bad" role="alert" data-testid="mr-cmp-save-error">
              <Warning size={15} weight="bold" aria-hidden /><div>{saveError}</div>
            </div>
          ) : null}
          <div className="mr-ui-foot">
            <span className="mr-ui-sub">Deploy config is read only: BOSS reads the deploy plan there and never builds it.</span>
            <button type="button" className="ag-btn" onClick={() => { setEditing(null); setSaveError(null); }} disabled={busy}>Cancel</button>
            <GatedButton permission="repository:manage" className="ag-btn ag-btn-primary" data-testid="mr-cmp-save" disabled={busy}
                         onClick={() => void save()}>{busy ? "Saving…" : "Save components"}</GatedButton>
          </div>
        </div>
      ) : code.length ? (
        <div className="mr-cmp-flow" data-testid="mr-cmp-flow">
          {code.map((c, i) => {
            const repo = byBinding.get(c.binding_id);
            const n = i + 1;
            const st = c.repository_active === false ? stateWords("unreachable") : null;
            return (
              <React.Fragment key={c.id ?? c.name}>
                {i ? <div className="mr-cmp-arrow" title="Depends on" aria-hidden><ArrowRight size={16} /></div> : null}
                <div className="mr-cmp-card" data-testid={`mr-cmp-card-${c.name}`} data-inactive={c.repository_active === false || undefined}>
                  <h4>
                    <span className="mr-cmp-ord" aria-label={`Builds ${n === 1 ? "first" : `#${n}`}`}>{n}</span>
                    <span data-user-text>{c.name}</span>
                    <span className="mr-ui-spacer" />
                    {st ? <span className="mr-ui-pill" data-tone={st.tone} title="The repository is no longer active in this project">Inactive</span>
                      : <HealthPill status={c.status} testId={`mr-cmp-health-${c.name}`} />}
                  </h4>
                  <RepoName name={c.repository || repo?.full_name || "—"} sub={<Lang lang={repo?.language} />} />
                  <dl>
                    <dt>Path <TipQ tip="Folder inside the repository. Lets a monorepo hold several components." /></dt>
                    <dd className="mr-ui-mono">{c.path || "/"}</dd>
                    <dt>Live</dt>
                    <dd className="mr-ui-mono" data-testid={`mr-cmp-live-${c.name}`}
                        title={c.status?.live ? "The version running now" : "Nothing deployed or published yet"}>{c.status?.live || "—"}</dd>
                    <dt>Ships to</dt><dd title={c.status?.ships_to || shipsTo(c.ship_target)}>{c.status?.ships_to || shipsTo(c.ship_target)}</dd>
                    <dt>Needs</dt><dd>{c.depends_on?.length ? c.depends_on.join(", ") : "—"}</dd>
                  </dl>
                </div>
              </React.Fragment>
            );
          })}
        </div>
      ) : (
        <div className="mr-ui-card mr-ui-empty" data-testid="mr-cmp-none">
          No components mapped. Every build of {app} goes to the project&apos;s repository.
        </div>
      )}

      {!editing && deploy.map((d) => (
        <div key={d.id ?? d.name} className="mr-ui-card mr-cmp-deploy" data-testid={`mr-cmp-deploy-${d.name}`} style={{ padding: "12px 16px" }}>
          <span className="mr-ui-gh muted" aria-hidden><FileCode size={16} /></span>
          <div style={{ minWidth: 0 }}>
            <b>{d.name || "Deploy config"}</b>{" "}
            <span className="mr-ui-mono mr-ui-dim" data-user-text>{d.repository}{d.path && d.path !== "/" ? `, ${d.path}` : ""}</span>
            <div className="mr-ui-sub">Read only, BOSS reads the deploy plan here, it does not change it</div>
          </div>
          <span className="mr-ui-spacer" />
          <span className="mr-ui-pill" data-tone="warn">Read only</span>
        </div>
      ))}

      <div className="mr-ui-grid2">
        <div className="mr-ui-card">
          <div className="mr-ui-card-h">
            <h3>Recent changes</h3>
            <span className="mr-ui-sub">One row per change, however many repositories it touched</span>
          </div>
          {changes.length ? (
            <div className="mr-ui-table-wrap">
              <table className="mr-ui-table" data-testid="mr-cmp-changes">
                <thead><tr><th>Change</th><th>Repositories</th><th>State</th><th>When</th></tr></thead>
                <tbody>
                  {changes.map((cs) => {
                    const prs = cs.prs ?? 0;
                    return (
                      <tr key={cs.id} data-testid={`mr-cmp-change-${cs.id}`}>
                        <td>
                          <a className="mr-ui-link" href={areaHref(CHANGE_SET_PLACES.cockpit(cs.id))} data-testid={`mr-cmp-open-change-${cs.id}`}
                             title="Open this change: its lanes and pull requests"><b data-user-text>{cs.title || cs.id}</b></a>
                          <span className="mr-ui-when">{prs ? `${prs} ${prs === 1 ? "PR" : "linked PRs"}` : "No PR yet"}</span>
                        </td>
                        <td>
                          <span className="mr-ui-row wrap" style={{ gap: 4 }}>
                            {(cs.repos ?? []).map((r) => <span key={r} className="mr-ui-chip static" data-user-text>{r}</span>)}
                          </span>
                        </td>
                        <td><span className="mr-ui-pill" data-tone={changeTone(cs.state)}>{changeWord(cs.state)}</span></td>
                        <td className="mr-ui-dim nw">{agoWords(cs.created_at, now) || "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="mr-ui-empty" data-testid="mr-cmp-no-changes">
              No changes yet. A change that touches several repositories shows here as one row.
            </p>
          )}
        </div>
        <div className="mr-ui-note" data-testid="mr-cmp-scope">
          <LockKey size={15} aria-hidden />
          <div>
            {writeRepos.length || readRepos.length ? (
              <>
                <b data-user-text>Runs on {app}</b> may touch {listWords(writeRepos) || "no repository"}
                {readRepos.length ? <>, and read {listWords(readRepos)}</> : null}. Nothing else in the organization.
              </>
            ) : <><b data-user-text>Runs on {app}</b> may touch the project&apos;s repository only.</>}
          </div>
        </div>
      </div>
      {asking ? (
        <NewChangeSheet projectId={projectId} appKey={data.app_key || appKey} app={app}
                        repos={writeRepos} onClose={() => setAsking(false)} />
      ) : null}
    </div>
  );
}


const HEALTH: Record<string, { label: string; tone: "ok" | "bad" | "info"; tip: string }> = {
  healthy: { label: "Healthy", tone: "ok", tip: "The last deploy passed its health check" },
  failing: { label: "Failing", tone: "bad", tip: "The last deploy failed or its health check failed" },
  deploying: { label: "Deploying", tone: "info", tip: "A deploy is running now" },
  published: { label: "Published", tone: "ok", tip: "Released by the merge (a package or no host)" },
};
function HealthPill({ status, testId }: { status?: ComponentStatus | null; testId: string }) {
  const h = status?.health ? HEALTH[status.health] : null;
  if (!h) return null;
  return <span className="mr-ui-pill" data-tone={h.tone} title={h.tip} data-testid={testId}>{h.label}</span>;
}

/**
 * "New change" on a multi-repo app: what should change, sent as one change set
 * (POST /v1/apps/{app}/change-sets). A first change asks consent for exactly the app's
 * repositories (409 consent_required: the consent sheet), then is sent again unchanged.
 */
function NewChangeSheet({ projectId, appKey, app, repos, onClose }: {
  projectId: string; appKey: string; app: string; repos: string[]; onClose: () => void;
}) {
  const [title, setTitle] = useState("");
  const [instructions, setInstructions] = useState("");
  const [remember, setRemember] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const start = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const body = { title: title.trim(), ...(instructions.trim() ? { instructions: instructions.trim() } : {}), remember };
      const cs = await withConsent(() => guarded(`Start a change on ${app}.`, () => multirepo.startChangeSet(projectId, appKey, body)));
      showToast("success", "Change started");
      onClose();
      window.location.assign(areaHref(CHANGE_SET_PLACES.cockpit(cs.id)));
    } catch (err) {
      if ((err as { code?: string }).code !== "step_up_cancelled") {
        setError(refusalCode(err) === "consent_required"
          ? "Not started: the agents need your consent for these repositories first."
          : refusalMessage(err));
      }
      setBusy(false);
    }
  };
  return (
    <Sheet title={`New change, ${app}`} onClose={onClose} testId="mr-new-change">
      <form className="mr-cs-body" onSubmit={(e) => void start(e)}>
        <p className="mr-cs-subtitle" style={{ margin: 0 }}>
          BOSS plans it across {repos.length ? listWords(repos) : "the app's repositories"}, builds each in order and opens
          linked pull requests you approve once.
        </p>
        <label className="mr-ui-col" style={{ gap: 6 }}>
          <span className="mr-ui-sub"><b>Title</b></span>
          <input className="mr-ui-input" value={title} onChange={(e) => setTitle(e.target.value)} data-autofocus
                 placeholder="Add saved carts" data-testid="mr-new-change-title" maxLength={160} />
        </label>
        <label className="mr-ui-col" style={{ gap: 6 }}>
          <span className="mr-ui-sub"><b>What should change</b> (optional)</span>
          <textarea className="mr-ui-input" style={{ height: 96, padding: 10, resize: "vertical" }} value={instructions}
                    onChange={(e) => setInstructions(e.target.value)} data-testid="mr-new-change-what"
                    placeholder="Shoppers can save a cart for later and share it with a link." />
        </label>
        <label className="mr-cs-remember" title="Later changes to this app do not ask for the repositories again">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} data-testid="mr-new-change-remember" />
          Remember for <span data-user-text>{app}</span>
        </label>
        {error ? (
          <div className="mr-ui-note" data-tone="bad" role="alert" data-testid="mr-new-change-error">
            <Warning size={15} weight="bold" aria-hidden /><div>{error}</div>
          </div>
        ) : null}
        <div className="mr-cs-foot">
          <span className="mr-ui-spacer" />
          <button type="button" className="idn-btn" onClick={onClose} disabled={busy}>Cancel</button>
          <GatedButton permission="code_task:create" type="submit" className="idn-btn primary" disabled={busy || !title.trim()}
                       data-testid="mr-new-change-start">{busy ? "Starting…" : "Start change"}</GatedButton>
        </div>
      </form>
    </Sheet>
  );
}
