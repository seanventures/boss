"use client";

/**
 * REPO AND MODELS › REPOSITORIES (multi-repo screen 1): every repository the GitHub App can
 * reach, as GitHub lists it. A tick is "BOSS may use it"; projects then choose from the ticked
 * ones. Ticks are kept here until "Save · N allowed" sends the whole set (PUT
 * /v1/github/repos/allowed), and the server runs the three checks (Read · Write · PRs) on each
 * newly allowed row — nothing says Working until all three pass. Archived repositories cannot be
 * ticked. Unticking removes a repository from every project; running work keeps its grant until
 * it ends.
 *
 * Owner/Admin write (repository:register, step-up); everyone else sees the same table with the
 * controls disabled and the reason on them. `?area=setup&focus=repositories` lands here.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowClockwise, ArrowSquareOut, Check, GithubLogo, MagnifyingGlass, Warning } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import {
  agoWords, matchesFilter, multirepo, refusalMessage, registryCounts,
  type Registry, type RegistryFilter, type RegistryRepo,
} from "../../lib/multirepo";
import { useNow } from "../agents/live-util";
import { showToast } from "../client-forms";
import { Checks, Lang, RepoName, StatePill, TipQ } from "./parts";

const FILTERS: { key: RegistryFilter; label: string; tip: string }[] = [
  { key: "all", label: "All", tip: "Every repository the GitHub App reaches" },
  { key: "allowed", label: "Allowed", tip: "Ticked: projects may choose them" },
  { key: "needs_fix", label: "Needs a fix", tip: "Allowed, but a check failed or it is read only" },
  { key: "not_allowed", label: "Not allowed", tip: "Not ticked: no project can use them" },
];

const TICK_TIP = "Allow BOSS to use this repository. Ticking runs the three checks first.";

function wantsFocus(): boolean {
  try { return new URLSearchParams(window.location.search).get("focus") === "repositories"; } catch { return false; }
}

export function RepositoryRegistry({ projectId }: { projectId: string }) {
  const [reg, setReg] = useState<Registry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<RegistryFilter>("all");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<null | "save" | "recheck" | "refresh" | string>(null);
  const [focused, setFocused] = useState(false);
  const ref = useRef<HTMLElement>(null);
  const gate = useGate("repository:register");
  const guarded = useGuardedCall();
  const now = useNow(30_000);

  const take = useCallback((next: Registry) => {
    setReg(next);
    setTicked(new Set(next.items.filter((r) => r.allowed).map((r) => r.id)));
  }, []);

  const load = useCallback(async () => {
    try {
      take(await multirepo.registry(projectId));
      setError(null);
    } catch (e) {
      // An API without the registry (older server) is not an error to show on this board.
      if ((e as { status?: number }).status === 404) setMissing(true);
      else setError(refusalMessage(e));
    }
  }, [projectId, take]);
  useEffect(() => { void load(); }, [load]);

  // `focus=repositories` lands here once the table has drawn.
  useEffect(() => {
    if (!reg || !wantsFocus()) return;
    setFocused(true);
    const el = ref.current;
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "start" });
  }, [reg]);

  const server = useMemo(() => new Set(reg?.items.filter((r) => r.allowed).map((r) => r.id) ?? []), [reg]);
  const changed = useMemo(() => {
    if (!reg) return false;
    if (server.size !== ticked.size) return true;
    for (const id of ticked) if (!server.has(id)) return true;
    return false;
  }, [reg, server, ticked]);

  if (missing) return null;

  const write = async (what: string, label: string, call: () => Promise<Registry>) => {
    setBusy(what);
    try {
      take(await guarded(label, call));
      return true;
    } catch (e) {
      const code = (e as { code?: string }).code;
      if (code !== "step_up_cancelled") showToast("error", refusalMessage(e));
      return false;
    } finally { setBusy(null); }
  };

  const save = async () => {
    if (!reg) return;
    // EXACTLY THE TICKED SET, in the list's order: the server allows these and no others.
    const ids = reg.items.filter((r) => ticked.has(r.id)).map((r) => r.id);
    if (await write("save", "Change which repositories BOSS may use.", () => multirepo.saveAllowed(projectId, ids))) {
      showToast("success", `${ids.length} allowed`);
    }
  };
  const recheckOne = async (repo: RegistryRepo) => {
    setBusy(repo.id);
    try {
      const row = await guarded("Check this repository again.", () => multirepo.check(projectId, repo.id));
      setReg((r) => (r ? { ...r, items: r.items.map((x) => (x.id === row.id ? row : x)) } : r));
    } catch (e) {
      if ((e as { code?: string }).code !== "step_up_cancelled") showToast("error", refusalMessage(e));
    } finally { setBusy(null); }
  };

  const conn = reg?.connection ?? null;
  const items = reg?.items ?? [];
  const counts = reg ? registryCounts(reg) : null;
  const q = query.trim().toLowerCase();
  const shown = items.filter((r) => matchesFilter(r, filter)
    && (!q || r.full_name.toLowerCase().includes(q) || (r.description ?? "").toLowerCase().includes(q)));
  const readOnly = items.filter((r) => r.allowed && r.state === "read_only");
  const broken = items.filter((r) => r.allowed && (r.state === "needs_fix" || r.state === "unreachable"));
  const account = conn?.account_login || reg?.org_id || "";
  const kindWord = conn?.kind === "token" ? "GitHub token" : "GitHub App";
  const showRefresh = !reg?.in_use || !items.length;

  return (
    <section ref={ref} className="mr-reg" id="su-repositories" data-testid="mr-registry" data-focused={focused || undefined}
             aria-label="Repositories">
      <div>
        <div className="mr-ui-eyebrow">Repositories</div>
        <p className="mr-ui-lead">Every repository the GitHub App can reach. Tick the ones BOSS may use; projects then choose from these.</p>
      </div>
      <div className="mr-ui-card">
        <div className="mr-ui-card-h">
          <span className="mr-ui-gh" aria-hidden><GithubLogo size={18} weight="bold" /></span>
          <div>
            <h3 data-testid="mr-reg-title">{account ? `${kindWord} · ${account}` : kindWord}</h3>
            <div className="mr-ui-sub" data-testid="mr-reg-sub">
              {account ? <>Installed on <b data-user-text>{account}</b>{conn?.account_type === "User" ? "" : " organization"} · </> : null}
              reaches {items.length} {items.length === 1 ? "repository" : "repositories"}
              <TipQ tip="The list comes from GitHub, not from BOSS. To reach more repositories, change the installation in GitHub, then Refresh." />
            </div>
          </div>
          <span className="mr-ui-spacer" />
          {conn?.change_url ? (
            <a className="ag-btn" href={conn.change_url} target="_blank" rel="noreferrer" data-testid="mr-reg-change"
               title="Choose which repositories the App reaches, in GitHub">
              <GithubLogo size={13} aria-hidden /> Change in GitHub
            </a>
          ) : null}
          {showRefresh ? (
            <GatedButton permission="repository:register" className="ag-btn" data-testid="mr-reg-refresh"
                         disabled={!!busy} title="Read the repository list from GitHub again"
                         onClick={() => void write("refresh", "Read the repository list from GitHub.", () => multirepo.refresh(projectId))}>
              <ArrowClockwise size={13} aria-hidden className={busy === "refresh" ? "su-spin" : undefined} /> Refresh from GitHub
            </GatedButton>
          ) : null}
          <GatedButton permission="repository:register" className="ag-btn" data-testid="mr-reg-recheck"
                       disabled={!!busy || !items.some((r) => r.allowed)}
                       title="Run Read, Write and PRs again on every allowed repository"
                       onClick={() => void write("recheck", "Check every allowed repository again.", () => multirepo.recheckAll(projectId))}>
            <ArrowClockwise size={13} aria-hidden className={busy === "recheck" ? "su-spin" : undefined} /> Recheck all
          </GatedButton>
        </div>

        {error ? (
          <div className="mr-ui-row" role="alert" data-testid="mr-reg-error">
            <span className="mr-ui-error">{error}</span>
            <button type="button" className="ag-btn" onClick={() => { setError(null); void load(); }}>Retry</button>
          </div>
        ) : !reg ? <p className="mr-ui-empty">Loading…</p> : (
          <>
            <div className="mr-reg-tools">
              <div className="mr-reg-filters" role="group" aria-label="Show">
                {FILTERS.map((f) => (
                  <button key={f.key} type="button" className="mr-ui-chip" aria-pressed={filter === f.key}
                          data-tone={f.key === "needs_fix" && counts?.needs_fix ? "warn" : undefined}
                          data-testid={`mr-reg-filter-${f.key}`} title={f.tip} onClick={() => setFilter(f.key)}>
                    {f.label} {counts?.[f.key] ?? 0}
                  </button>
                ))}
              </div>
              <span className="mr-ui-spacer" />
              <label className="mr-ui-search">
                <MagnifyingGlass size={13} aria-hidden />
                <span className="ag-sr">Find a repository</span>
                <input type="search" placeholder="Find a repository" value={query} data-testid="mr-reg-find"
                       onChange={(e) => setQuery(e.target.value)} />
              </label>
            </div>
            {items.length === 0 ? (
              <p className="mr-ui-empty" data-testid="mr-reg-empty">
                GitHub lists no repositories for this connection yet. Refresh from GitHub, or change the installation there.
              </p>
            ) : (
              <div className="mr-ui-table-wrap">
                <table className="mr-ui-table" data-testid="mr-reg-table">
                  <thead>
                    <tr>
                      <th><span className="ag-sr">Allowed</span></th>
                      <th>Repository</th>
                      <th>Language</th>
                      <th className="mr-hide-narrow">Branch</th>
                      <th>Checks <TipQ tip="Run against GitHub on tick and every hour. Nothing says Working until all three pass." /></th>
                      <th className="mr-hide-narrow">Used by</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shown.map((r) => {
                      const on = ticked.has(r.id);
                      const archived = r.archived || r.state === "archived";
                      const off = archived || !gate.allowed || !!busy;
                      const why = archived ? "Archived on GitHub. It cannot be allowed." : !gate.allowed ? gate.reason : TICK_TIP;
                      return (
                        <tr key={r.id} data-testid={`mr-reg-row-${r.id}`} data-changed={on !== r.allowed || undefined}>
                          <td>
                            <button type="button" role="checkbox" className="mr-ui-cb" aria-checked={on}
                                    aria-disabled={off || undefined} aria-label={`Allow ${r.full_name}`} title={why}
                                    data-testid={`mr-reg-tick-${r.id}`}
                                    onClick={() => {
                                      if (off) return;
                                      setTicked((s) => { const n = new Set(s); if (n.has(r.id)) n.delete(r.id); else n.add(r.id); return n; });
                                    }}>
                              {on ? <Check size={12} weight="bold" aria-hidden /> : null}
                            </button>
                          </td>
                          <td><RepoName name={r.full_name} muted={!r.allowed}
                                        sub={archived ? "Archived on GitHub" : r.description || (r.private ? "Private" : undefined)} /></td>
                          <td><Lang lang={r.language} /></td>
                          <td className="mr-ui-mono mr-ui-dim mr-hide-narrow">{r.default_branch || "—"}</td>
                          <td className="nw"><Checks repo={r} /></td>
                          <td className="mr-ui-mut mr-hide-narrow" data-user-text>{r.used_by?.length ? r.used_by.map((u) => u.name).join(", ") : "—"}</td>
                          <td className="nw">
                            <span className="mr-ui-row" style={{ gap: 6 }}>
                              <StatePill state={archived ? "archived" : r.allowed ? r.state : "not_allowed"} testId={`mr-reg-state-${r.id}`} />
                              {r.allowed && !archived ? (
                                <GatedButton permission="repository:register" className="ag-icon-btn" disabled={!!busy}
                                             aria-label={`Check ${r.full_name} again`} title="Check again"
                                             data-testid={`mr-reg-check-${r.id}`} onClick={() => void recheckOne(r)}>
                                  <ArrowClockwise size={12} aria-hidden className={busy === r.id ? "su-spin" : undefined} />
                                </GatedButton>
                              ) : null}
                            </span>
                            <span className="mr-ui-when">
                              {archived ? "—" : r.checked_at ? agoWords(r.checked_at, now) : "not checked"}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                    {!shown.length ? (
                      <tr><td colSpan={7} className="mr-ui-empty" data-testid="mr-reg-none">Nothing matches.</td></tr>
                    ) : null}
                  </tbody>
                </table>
              </div>
            )}
            <div className="mr-ui-foot">
              <span className="mr-ui-sub" data-testid="mr-reg-foot">
                {ticked.size} allowed · ticking runs the three checks first · unticking removes it from every project;
                work already running keeps its grant until it ends.
              </span>
              {changed ? (
                <button type="button" className="ag-btn" data-testid="mr-reg-undo" disabled={!!busy}
                        onClick={() => setTicked(new Set(server))}>Undo</button>
              ) : null}
              <GatedButton permission="repository:register" className="ag-btn ag-btn-primary" data-testid="mr-reg-save"
                           disabled={!changed || !!busy} title={changed ? "Save which repositories BOSS may use" : "Nothing changed"}
                           onClick={() => void save()}>
                {busy === "save" ? "Saving…" : `Save · ${ticked.size} allowed`}
              </GatedButton>
            </div>
          </>
        )}
      </div>
      {readOnly.map((r) => (
        <div key={r.id} className="mr-ui-note" data-tone="warn" data-testid={`mr-reg-readonly-${r.id}`}>
          <Warning size={15} weight="bold" aria-hidden />
          <div>
            <b data-user-text>{r.full_name} is read only.</b> The GitHub App was granted Contents: read on this repository,
            so BOSS can plan deploys from it but cannot open pull requests there.{" "}
            {conn?.change_url ? (
              <a className="mr-ui-link" href={conn.change_url} target="_blank" rel="noreferrer">
                Grant write in GitHub <ArrowSquareOut size={11} aria-hidden />
              </a>
            ) : "Grant write in GitHub"}{" "}
            or keep it read only (deploy config only).
          </div>
        </div>
      ))}
      {broken.length ? (
        <div className="mr-ui-note" data-tone="bad" data-testid="mr-reg-broken">
          <Warning size={15} weight="bold" aria-hidden />
          <div>
            <b data-user-text>{broken.map((r) => r.full_name).join(", ")}</b> {broken.length === 1 ? "needs" : "need"} a fix:
            a check failed against GitHub. Fix the App&apos;s access there, then Recheck all.
          </div>
        </div>
      ) : null}
    </section>
  );
}
