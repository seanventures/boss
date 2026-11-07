"use client";

/**
 * ADD A PROJECT › EXISTING · REPO OR FOLDER (BOSSAPP-914 screen 03): three columns, left to right.
 *
 *   1 Pick it        the source: GitHub (the accounts the org's registry reaches — read-only chips,
 *                    `+ account` opens Repo and models, contract §15 — a search box and the allowed
 *                    repos with how long ago they were pushed, or `in use` when a project has one)
 *                    or Folder on host (a verified server and an absolute path, read over ssh,
 *                    read-only: lib/host-folder.ts)
 *   2 Helm reads it  live as soon as something is picked: Access, Stack, Build & test (GET
 *                    /v1/github/repos/{id}/facts, or the folder's read) and Map code with its files
 *                    and a progress bar
 *   3 Name & captain the name (prefilled), the captain (the real candidates; the default captain,
 *                    never Marshal), the default crew's faces, "Map business rules too (Knowledge
 *                    Graph)" and ONE primary: Start onboarding → POST /v1/projects, the repository
 *                    joins it (PUT /v1/projects/{id}/repos/{repo}) or the folder is recorded
 *                    (POST /v1/projects/{id}/host-folders), the captain is set when another was
 *                    chosen, onboarding starts, and the project page opens on it (screen 10).
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  CaretDown, Check, FolderOpen, GitBranch, GithubLogo, HardDrives, MagnifyingGlass, RocketLaunch, User, Warning,
} from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { IdentityError, idGet, idPost } from "../../lib/identity/api";
import { multirepo, refusalMessage, type Registry, type RegistryRepo } from "../../lib/multirepo";
import { accountsOf, importable, startProjectApi, type RepoFacts } from "../../lib/start-project";
import { ageShort, folderRows, hostFolders, type FolderFacts, type FolderHost } from "../../lib/host-folder";
import { consoleHref } from "../console-href";
import { AgentAvatar } from "../agent-avatar";

type Source = "github" | "folder";
type CheckState = "done" | "now" | "todo" | "failed";
type Candidate = { agent_id: string; role_key: string; name: string; role: string };
type CaptainRead = { role_key: string | null; name: string | null; role?: string; candidates: Candidate[];
                     squad: { name: string; role_key: string; role?: string }[] };

/** The checks of column 2 for a picked repo: Access, Stack, Build & test (real values or "at first build"). */
export function repoChecks(repo: RegistryRepo, f: RepoFacts | null): { label: string; sub: string; state: CheckState }[] {
  const c = repo.checks ?? {};
  const asked = [c.read?.ok, c.write?.ok, c.pull_requests?.ok];
  const access: CheckState = asked.every((x) => x === true) ? "done" : asked.some((x) => x === false) ? "failed" : f ? "done" : "now";
  const missing = [c.read?.ok === false ? "read" : "", c.write?.ok === false ? "branch" : "", c.pull_requests?.ok === false ? "PR" : ""].filter(Boolean);
  const stack = f ? [f.facts.language, f.facts.framework].filter(Boolean).join(" · ") : "";
  const build = f?.facts.build ? `${f.facts.build}${f.facts.tests ? " · tests" : ""}` : "";
  return [
    { label: "Access", sub: access === "failed" ? `no ${missing.join(" · ")} access` : "read · branch · PR", state: access },
    { label: "Stack", sub: f ? stack || "at first build" : "reading…", state: f ? (stack ? "done" : "todo") : "now" },
    { label: "Build & test", sub: f ? build || "the first build runs them" : "reading…", state: f ? (build ? "done" : "todo") : "now" },
  ];
}

/** Map code's line and bar: "118 / 214 files", or how many files it will map once created. */
export function mapLine(f: RepoFacts | null): { sub: string; pct: number; state: CheckState } {
  if (!f) return { sub: "reading…", pct: 0, state: "todo" };
  const m = f.mapping;
  const total = m.files ?? f.files_total ?? null;
  if (m.done) return { sub: total ? `${total} / ${total} files` : "mapped", pct: 100, state: "done" };
  if (m.state === "running") return { sub: total ? `${m.parsed ?? 0} / ${total} files` : `${m.percent}%`, pct: m.percent, state: "now" };
  if (m.state === "failed") return { sub: "stopped — Re-check", pct: 0, state: "failed" };
  if (m.state === "queued") return { sub: total ? `0 / ${total} files · starting` : "starting", pct: 2, state: "now" };
  return { sub: total ? `0 / ${total} files · starts on Start` : "starts on Start", pct: 0, state: "todo" };
}

function CheckRow({ label, sub, state, testId }: { label: string; sub: string; state: CheckState; testId?: string }) {
  return (
    <div className="spj-ck" data-s={state} data-testid={testId}>
      <span className="spj-ck-b" aria-hidden>{state === "done" ? <Check size={11} weight="bold" /> : state === "failed" ? <Warning size={11} weight="bold" /> : null}</span>
      <span className="spj-ck-t">{label}<small>{sub}</small></span>
    </div>
  );
}

export function ImportRepo({ projectId, onCreated }: {
  /** The project open now: the org registry and the servers are read through it. */
  projectId: string;
  /** The new project's id, and whether it is a knowledge-graph one. */
  onCreated: (projectId: string, type: "app" | "knowledge_graph") => void;
}) {
  const guarded = useGuardedCall();
  const canCreate = useGate("project:create");
  const [source, setSource] = useState<Source>("github");
  const [registry, setRegistry] = useState<Registry | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [account, setAccount] = useState("");
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string>("");
  const [facts, setFacts] = useState<RepoFacts | null>(null);
  const [factsError, setFactsError] = useState<string | null>(null);
  // Folder on host
  const [hosts, setHosts] = useState<FolderHost[] | null>(null);
  const [hostId, setHostId] = useState("");
  const [path, setPath] = useState("");
  const [folder, setFolder] = useState<FolderFacts | null>(null);
  const [reading, setReading] = useState(false);
  const [folderError, setFolderError] = useState<string | null>(null);
  // Name & captain
  const [name, setName] = useState("");
  const named = useRef(false);
  const [captain, setCaptain] = useState<CaptainRead | null>(null);
  const [chosen, setChosen] = useState<string>("");
  const [picking, setPicking] = useState(false);
  const [rules, setRules] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    multirepo.registry(projectId).then((r) => {
      if (!alive) return;
      setRegistry(r);
      const first = accountsOf(r).find((a) => importable(r, a).length) ?? accountsOf(r)[0] ?? "";
      setAccount(first);
    }).catch((e) => { if (alive) setLoadError(refusalMessage(e)); });
    idGet<CaptainRead>(`/v1/projects/${encodeURIComponent(projectId)}/captain`).then((c) => {
      if (!alive || !c) return;
      const candidates = Array.isArray(c.candidates) ? c.candidates : [];
      setCaptain({ ...c, candidates, squad: Array.isArray(c.squad) ? c.squad : [] });
      const def = candidates.find((x) => x.role_key === "engineering_captain") ?? candidates.find((x) => x.role_key !== "chief_of_staff");
      setChosen(def?.role_key ?? c.role_key ?? "");
    }).catch(() => { /* the default captain still takes it over */ });
    return () => { alive = false; };
  }, [projectId]);

  useEffect(() => {
    if (source !== "folder" || hosts) return undefined;
    let alive = true;
    hostFolders.hosts(projectId).then((h) => {
      if (!alive) return;
      setHosts(h.hosts ?? []);
      setHostId((cur) => cur || h.hosts?.[0]?.id || "");
    }).catch((e) => { if (alive) { setHosts([]); setFolderError(refusalMessage(e)); } });
    return () => { alive = false; };
  }, [source, hosts, projectId]);

  const accounts = useMemo(() => accountsOf(registry), [registry]);
  const repos = useMemo(() => {
    const all = importable(registry, account);
    const q = query.trim().toLowerCase();
    return q ? all.filter((r) => r.full_name.toLowerCase().includes(q)) : all;
  }, [registry, account, query]);
  const repo = (registry?.items ?? []).find((r) => r.id === picked) ?? null;

  useEffect(() => {
    if (!picked) { setFacts(null); return undefined; }
    const ctl = new AbortController();
    setFacts(null); setFactsError(null);
    startProjectApi.facts(projectId, picked, ctl.signal).then((f) => {
      setFacts(f);
      if (!named.current) setName(f.suggested_name);
    }).catch((e) => { if (!ctl.signal.aborted) setFactsError(refusalMessage(e)); });
    return () => ctl.abort();
  }, [picked, projectId]);

  const pick = (id: string) => {
    setPicked(id);
    if (!named.current) {
      const full = repos.find((r) => r.id === id)?.full_name ?? "";
      const tail = full.split("/").pop() ?? "";
      const words = tail.replace(/[-_.]+/g, " ").trim();
      setName(words ? words[0].toUpperCase() + words.slice(1) : "");
    }
  };

  const readFolder = async () => {
    if (!hostId || !path.trim() || reading) return;
    setReading(true); setFolderError(null); setFolder(null);
    try {
      const f = await guarded("Read the folder on the server.", () => hostFolders.probe(projectId, hostId, path.trim()));
      setFolder(f);
      if (!f.exists) setFolderError(`There is no folder ${path.trim()} on ${f.host ?? "that server"}.`);
      else if (!named.current && f.suggested_name) setName(f.suggested_name);
    } catch (e) {
      setFolderError(e instanceof IdentityError && typeof e.body.message === "string" ? e.body.message : refusalMessage(e));
    } finally {
      setReading(false);
    }
  };

  // The folder's GitHub remote, when the registry reaches it: that repository joins the project too.
  const folderRepo = folder?.github ? (registry?.items ?? []).find((r) => r.full_name.toLowerCase() === folder.github!.toLowerCase() && r.allowed) ?? null : null;
  const ready = source === "github" ? Boolean(repo) : Boolean(folder?.exists);

  const create = async () => {
    if (!ready || !name.trim() || busy) return;
    if (!canCreate.allowed) { setError(canCreate.reason); return; }
    setBusy(true); setError(null);
    const type = rules ? "knowledge_graph" : "app";
    let made: { id: string } | null = null;
    try {
      made = await guarded("Create the project.", () => startProjectApi.create(name.trim(), { type }));
      const id = made.id;
      if (source === "github" && repo) {
        await guarded(`Add ${repo.full_name} to the project.`, () => multirepo.putProjectRepo(id, repo.id, {}));
      } else if (folder) {
        await guarded(`Add ${folder.path} to the project.`, () => hostFolders.register(id, hostId, folder.path));
        if (folderRepo) await guarded(`Add ${folderRepo.full_name} to the project.`, () => multirepo.putProjectRepo(id, folderRepo.id, {}));
      }
      if (chosen && chosen !== "engineering_captain") {
        await guarded("Set the captain.", () => idPost(`/v1/projects/${encodeURIComponent(id)}/captain`, { agent_id: chosen })).catch(() => undefined);
      }
      // Onboarding starts on its own when a repository joins; this is the same idempotent door.
      await idPost(`/v1/projects/${encodeURIComponent(id)}/onboarding/start`, {}).catch(() => undefined);
      onCreated(id, type);
    } catch (e) {
      const exists = (e as { exists?: string | null }).exists;
      if (exists !== undefined) setError(`A project called ${name.trim()} exists. Choose another name.`);
      else if (e instanceof IdentityError && e.code === "step_up_cancelled") setError("Not created: you cancelled the check.");
      else setError(made ? `The project was created, but its source was not added: ${refusalMessage(e)}` : refusalMessage(e));
      if (made) onCreated(made.id, type);
    } finally {
      setBusy(false);
    }
  };

  const addAccount = consoleHref({ area: "setup", focus: "repository", new_project: undefined });
  const shownName = source === "github" ? repo?.full_name.split("/")[1] : folder?.path.split("/").pop();
  const live = source === "github" ? Boolean(repo) : Boolean(folder?.exists);
  const checks: { label: string; sub: string; state: CheckState }[] = source === "github"
    ? (repo ? repoChecks(repo, facts) : [])
    : (folder?.exists ? folderRows(folder).map((r) => ({ label: r.label, sub: r.sub, state: (r.ok ? "done" : "todo") as CheckState })) : []);
  const map = source === "github" ? (repo ? mapLine(facts) : null)
    : folder?.exists ? { sub: `0 / ${folder.files ?? 0} files · starts on Start`, pct: 0, state: "todo" as CheckState } : null;
  const cap = captain?.candidates.find((c) => c.role_key === chosen) ?? null;
  const capName = cap?.name ?? captain?.name ?? "Helm";
  const capRole = cap?.role ?? captain?.role ?? "Engineering Captain";
  const crew = (captain?.squad ?? []).filter((m) => m.role_key !== chosen).slice(0, 4);

  return (
    <div className="spj-import" data-testid="import-repo">
      <section className="spj-col" aria-labelledby="spj-col-pick">
        <h3 id="spj-col-pick" className="spj-col-title"><span>1</span> Pick it</h3>
        <div className="spj-seg spj-seg-s" role="tablist" aria-label="Source">
          <button type="button" role="tab" aria-selected={source === "github"} data-testid="import-src-github" onClick={() => setSource("github")}>
            <GithubLogo size={14} aria-hidden /> GitHub</button>
          <button type="button" role="tab" aria-selected={source === "folder"} data-testid="import-src-folder" onClick={() => setSource("folder")}>
            <FolderOpen size={14} aria-hidden /> Folder on host</button>
        </div>
        {source === "github" ? (
          <>
            {loadError ? <p className="bx-np-error" role="alert" data-testid="import-load-error">{loadError}</p> : null}
            <div className="spj-accounts" data-testid="import-accounts">
              {accounts.map((a) => (
                <button key={a} type="button" className="spj-acct" aria-pressed={a === account} data-testid={`import-account-${a}`}
                        title={`Repositories ${a} reaches`} onClick={() => { setAccount(a); setPicked(""); }}>
                  <User size={11} aria-hidden /> {a}</button>
              ))}
              <a className="spj-link spj-small" href={addAccount} data-testid="import-add-account"
                 title="Connect another GitHub account or allow more repositories in Repo and models">+ account</a>
            </div>
            <label className="spj-search">
              <MagnifyingGlass size={14} aria-hidden />
              <input type="search" placeholder="Search repos" aria-label="Search repos" data-testid="import-search"
                     value={query} onChange={(e) => setQuery(e.target.value)} />
            </label>
            <ul className="spj-repos" role="listbox" aria-label="Repositories" data-testid="import-repos" aria-busy={!registry && !loadError}>
              {!registry && !loadError ? [0, 1, 2].map((i) => <li key={i} className="spj-skel" aria-hidden />) : null}
              {repos.map((r) => {
                const usedBy = (r.used_by ?? [])[0];
                return (
                  <li key={r.id}>
                    <button type="button" role="option" aria-selected={r.id === picked} className="spj-repo"
                            data-testid={`import-repo-${r.id}`} onClick={() => pick(r.id)}
                            title={usedBy ? `Already used by ${usedBy.name}` : r.description ?? r.full_name}>
                      <GitBranch size={13} aria-hidden className="spj-faint" />
                      <span className="spj-mono">{r.full_name.split("/")[1]}</span>
                      <span style={{ flex: 1 }} />
                      {usedBy ? <span className="spj-inuse" data-testid={`import-inuse-${r.id}`}>in use</span>
                        : <small className="spj-mono" data-testid={`import-age-${r.id}`}>{ageShort(r.pushed_at)}</small>}
                    </button>
                  </li>
                );
              })}
              {registry && !repos.length ? (
                <li className="spj-quiet" data-testid="import-none">
                  {query ? "No repo matches." : <>No repos yet · <a className="spj-link" href={addAccount}>Connect on Repo and models ›</a></>}
                </li>
              ) : null}
            </ul>
          </>
        ) : (
          <div className="spj-folder" data-testid="import-folder">
            <label className="bx-np-field">
              <span className="spj-lbl">Server</span>
              <span className="spj-select">
                <HardDrives size={14} aria-hidden />
                <select data-testid="import-folder-host" value={hostId} disabled={!hosts?.length}
                        onChange={(e) => { setHostId(e.target.value); setFolder(null); }}>
                  {!hosts ? <option value="">Reading…</option> : null}
                  {hosts && !hosts.length ? <option value="">No verified server yet</option> : null}
                  {(hosts ?? []).map((h) => <option key={h.id} value={h.id}>{h.name}{h.address && h.address !== h.name ? ` · ${h.address}` : ""}{h.shared ? " · shared" : ""}</option>)}
                </select>
              </span>
            </label>
            <label className="bx-np-field">
              <span className="spj-lbl">Folder</span>
              <input className="input spj-mono" data-testid="import-folder-path" placeholder="/srv/apps/helpdesk" value={path}
                     onChange={(e) => { setPath(e.target.value); setFolder(null); }}
                     onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void readFolder(); } }} />
            </label>
            <GatedButton permission="host:exec" projectId={projectId} className="ag-btn" data-testid="import-folder-read"
                         disabled={!hostId || !path.trim() || reading} title="Read the folder on the server (read-only)"
                         onClick={() => void readFolder()}>{reading ? "Reading…" : "Read folder"}</GatedButton>
            {hosts && !hosts.length ? (
              <p className="spj-quiet" data-testid="import-folder-nohost">No verified server yet · <a className="spj-link" href={consoleHref({ area: "infra", add: "host", new_project: undefined })}>Add one in Infra ›</a></p>
            ) : null}
            {folderError ? <p className="bx-np-error" role="alert" data-testid="import-folder-error">{folderError}</p> : null}
            <p className="spj-quiet">BOSS only reads it: one ssh session, nothing written.</p>
          </div>
        )}
      </section>

      <section className="spj-col spj-col-read" aria-labelledby="spj-col-read" data-empty={!live || undefined} data-testid="import-read">
        <h3 id="spj-col-read" className="spj-col-title"><span>2</span> Helm reads it{live ? <span className="spj-live" data-testid="import-live"><i aria-hidden />live</span> : null}</h3>
        {live ? (
          <p className="spj-reading">
            <AgentAvatar agent={chosen || "engineering_captain"} size={30} state="working" aiBadge={false} />
            <span>Reading <code>{shownName}</code>…</span>
          </p>
        ) : <p className="spj-quiet">{source === "github" ? "Pick a repo." : "Pick a server and a folder."}</p>}
        {factsError ? <p className="spj-quiet" role="status" data-testid="import-facts-error">Couldn&apos;t read it now: {factsError}</p> : null}
        {checks.length ? (
          <div className="spj-cks" data-testid="import-facts">
            {checks.map((c) => <CheckRow key={c.label} label={c.label} sub={c.sub} state={c.state} testId={`import-check-${c.label.split(" ")[0].toLowerCase()}`} />)}
            {map ? <CheckRow label="Map code" sub={map.sub} state={map.state} testId="import-mapping" /> : null}
          </div>
        ) : null}
        {map && live ? (
          <span className="spj-bar spj-bar-teal" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={map.pct} aria-label="Mapping the code">
            <i style={{ width: `${Math.max(map.pct, 2)}%` }} />
          </span>
        ) : null}
        {folder?.exists && folder.github ? (
          <p className="spj-quiet" data-testid="import-folder-remote">
            git · <span className="spj-mono">{folder.github}</span>{folderRepo ? " · joins the project too" : " · not in the registry; Helm maps it from the server"}
          </p>
        ) : null}
      </section>

      <section className="spj-col" aria-labelledby="spj-col-name">
        <h3 id="spj-col-name" className="spj-col-title"><span>3</span> Name &amp; captain</h3>
        <label className="bx-np-field">
          <span className="spj-lbl">Name</span>
          <input className="input" data-testid="import-name" maxLength={120} value={name} disabled={!ready}
                 onChange={(e) => { setName(e.target.value); named.current = true; }} />
        </label>
        {source === "github" && repo?.used_by?.length ? (
          <p className="spj-warn" role="status" data-testid="import-inuse-warning">
            <Warning size={13} weight="bold" aria-hidden />
            <span>Already used by {[...new Map(repo.used_by.map((u) => [u.name, u])).values()].slice(0, 2).map((u, i) => (
              <React.Fragment key={u.project_id}>{i ? ", " : ""}<a href={consoleHref({ project_id: u.project_id, area: "project", new_project: undefined })}>{u.name}</a></React.Fragment>
            ))}{new Set(repo.used_by.map((u) => u.name)).size > 2 ? ` +${new Set(repo.used_by.map((u) => u.name)).size - 2}` : ""}. A second one works on the same code.</span>
          </p>
        ) : null}
        <div className="bx-np-field">
          <span className="spj-lbl">Captain</span>
          <div className="spj-pick-wrap">
            <button type="button" className="spj-captain" aria-haspopup="listbox" aria-expanded={picking} data-testid="import-captain"
                    title="Who takes the project over once it exists — change later in Project › Settings"
                    onClick={() => setPicking((v) => !v)} disabled={!captain?.candidates.length}>
              <AgentAvatar agent={chosen || "engineering_captain"} size={22} aiBadge={false} />
              <b>{capName}</b><small>{capRole}</small><span style={{ flex: 1 }} /><CaretDown size={13} aria-hidden />
            </button>
            {picking ? (
              <ul className="spj-pick-list" role="listbox" aria-label="Captain" data-testid="import-captain-list">
                {(captain?.candidates ?? []).map((c) => (
                  <li key={c.role_key}>
                    <button type="button" role="option" aria-selected={c.role_key === chosen} data-testid={`import-captain-${c.role_key}`}
                            onClick={() => { setChosen(c.role_key); setPicking(false); }}>
                      <AgentAvatar agent={c.role_key} size={20} aiBadge={false} /><b>{c.name}</b><small>{c.role}</small>
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </div>
        <div className="bx-np-field">
          <span className="spj-lbl">Crew</span>
          <div className="spj-crew" data-testid="import-crew">
            <span className="spj-faces">{crew.map((m) => <AgentAvatar key={m.role_key} agent={m.role_key} size={22} aiBadge={false} tip={m.name} />)}</span>
            <small>default crew · change later</small>
          </div>
        </div>
        <label className="spj-check" title="Also find the business rules in its code (a knowledge-graph project)">
          <input type="checkbox" data-testid="import-rules" checked={rules} onChange={(e) => setRules(e.target.checked)} />
          {" "}Map business rules too (Knowledge Graph)
        </label>
        <span style={{ flex: 1 }} />
        <GatedButton permission="project:create" className="ag-btn ag-btn-primary spj-wide spj-go" data-testid="import-create"
                     disabled={!ready || !name.trim() || busy}
                     title="Create the project; Helm starts onboarding it"
                     onClick={() => void create()}>
          <RocketLaunch size={15} aria-hidden /> {busy ? "Creating…" : "Start onboarding"}
        </GatedButton>
        <p className="spj-foot" data-testid="import-estimate">{capName} takes it from here · ≈ 5 min · ≈ $1</p>
        {error ? <p className="bx-np-error" role="alert" data-testid="import-error">{error}</p> : null}
      </section>
    </div>
  );
}
