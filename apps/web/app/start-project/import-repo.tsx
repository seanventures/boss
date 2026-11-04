"use client";

/**
 * IMPORT A REPO (BOSSAPP-810, screen 2): three columns, left to right.
 *
 *   1 Pick the repo   an account (the GitHub accounts the org's registry reaches), its allowed
 *                     repositories (GET /v1/github/repos), "Repo not listed? Add a GitHub account"
 *   2 BOSS reads it   language, build & test, runs as, branch (GET /v1/github/repos/{id}/facts)
 *                     and how far the code is mapped
 *   3 Name it         the project name, prefilled → Create project: POST /v1/projects, then the
 *                     repository joins it (PUT /v1/projects/{id}/repos/{repo}, the Knowledge
 *                     Graph import's own call) and its mapping starts by itself
 *
 * "Map business rules too" makes it a knowledge-graph project (the old "Knowledge Graph for an
 * existing codebase" type), which opens on its guided setup.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";

import { GatedButton, useGate } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { IdentityError } from "../../lib/identity/api";
import { multirepo, refusalMessage, type Registry } from "../../lib/multirepo";
import {
  accountsOf, factRows, importable, mappingWords, startProjectApi, type RepoFacts,
} from "../../lib/start-project";
import { consoleHref } from "../console-href";

export function ImportRepo({ projectId, onCreated }: {
  /** The project open now: the org registry is read through it. */
  projectId: string;
  /** The new project's id, and whether it is a knowledge-graph one. */
  onCreated: (projectId: string, type: "app" | "knowledge_graph") => void;
}) {
  const guarded = useGuardedCall();
  const canCreate = useGate("project:create");
  const [registry, setRegistry] = useState<Registry | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [account, setAccount] = useState("");
  const [picked, setPicked] = useState<string>("");
  const [facts, setFacts] = useState<RepoFacts | null>(null);
  const [factsError, setFactsError] = useState<string | null>(null);
  const [name, setName] = useState("");
  // Typed by the person: a repo picked later never overwrites it.
  const named = useRef(false);
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
    return () => { alive = false; };
  }, [projectId]);

  const accounts = useMemo(() => accountsOf(registry), [registry]);
  const repos = useMemo(() => importable(registry, account), [registry, account]);
  const repo = repos.find((r) => r.id === picked) ?? null;

  useEffect(() => {
    if (!picked) { setFacts(null); return; }
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

  const create = async () => {
    if (!repo || !name.trim() || busy) return;
    if (!canCreate.allowed) { setError(canCreate.reason); return; }
    setBusy(true); setError(null);
    const type = rules ? "knowledge_graph" : "app";
    let made: { id: string } | null = null;
    try {
      made = await guarded("Create the project.", () => startProjectApi.create(name.trim(), { type }));
      const id = made.id;
      await guarded(`Add ${repo.full_name} to the project.`, () => multirepo.putProjectRepo(id, repo.id, {}));
      onCreated(id, type);
    } catch (e) {
      const exists = (e as { exists?: string | null }).exists;
      if (exists !== undefined) setError(`A project called ${name.trim()} exists. Choose another name.`);
      else if (e instanceof IdentityError && e.code === "step_up_cancelled") setError("Not created: you cancelled the check.");
      else setError(made ? `The project was created, but ${repo.full_name} was not added: ${refusalMessage(e)}` : refusalMessage(e));
      if (made) onCreated(made.id, type);
    } finally {
      setBusy(false);
    }
  };

  const addAccount = consoleHref({ area: "setup", focus: "repository", new_project: undefined });
  return (
    <div className="spj-import" data-testid="import-repo">
      <section className="spj-col" aria-labelledby="spj-col-pick">
        <h3 id="spj-col-pick" className="spj-col-title"><span>1</span> Pick the repo</h3>
        {loadError ? <p className="bx-np-error" role="alert" data-testid="import-load-error">{loadError}</p> : null}
        <label className="bx-np-field">
          <span>Account</span>
          <select className="input" data-testid="import-account" value={account} disabled={!accounts.length}
                  onChange={(e) => { setAccount(e.target.value); setPicked(""); }}>
            {!accounts.length ? <option value="">{registry ? "No GitHub account yet" : "Reading…"}</option> : null}
            {accounts.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
        <ul className="spj-repos" role="listbox" aria-label="Repositories" data-testid="import-repos">
          {repos.map((r) => (
            <li key={r.id}>
              <button type="button" role="option" aria-selected={r.id === picked} className="spj-repo"
                      data-testid={`import-repo-${r.id}`} onClick={() => pick(r.id)}>
                <span className="spj-mono">{r.full_name.split("/")[1]}</span>
                {r.language ? <small>{r.language}</small> : null}
              </button>
            </li>
          ))}
          {registry && !repos.length ? <li className="spj-quiet" data-testid="import-none">No repositories here yet.</li> : null}
        </ul>
        <a className="spj-link" href={addAccount} data-testid="import-add-account"
           title="Connect another GitHub account or allow more repositories in Setup">Repo not listed? Add a GitHub account</a>
      </section>

      <section className="spj-col" aria-labelledby="spj-col-read" data-empty={!repo || undefined}>
        <h3 id="spj-col-read" className="spj-col-title"><span>2</span> BOSS reads it</h3>
        {!repo ? <p className="spj-quiet">Pick a repo.</p> : null}
        {repo && !facts && !factsError ? <p className="spj-quiet" data-testid="import-reading">Reading {repo.full_name}…</p> : null}
        {factsError ? <p className="spj-quiet" role="status" data-testid="import-facts-error">Couldn&apos;t read it now: {factsError}</p> : null}
        {facts ? (
          <>
            <dl className="spj-facts" data-testid="import-facts">
              {factRows(facts).map((row) => (
                <div key={row.label} data-known={row.known || undefined}>
                  <dt>{row.label}</dt><dd>{row.value}</dd>
                </div>
              ))}
            </dl>
            <div className="spj-map" data-testid="import-mapping">
              <span>{mappingWords(facts.mapping)}</span>
              <span className="spj-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100}
                    aria-valuenow={facts.mapping.percent} aria-label="Mapping the code">
                <i style={{ width: `${facts.mapping.percent}%` }} />
              </span>
            </div>
            <p className="spj-quiet">You can continue now; mapping finishes in the background.</p>
          </>
        ) : null}
      </section>

      <section className="spj-col" aria-labelledby="spj-col-name">
        <h3 id="spj-col-name" className="spj-col-title"><span>3</span> Name it</h3>
        <label className="bx-np-field">
          <span>Project name</span>
          <input className="input" data-testid="import-name" maxLength={120} value={name} disabled={!repo}
                 onChange={(e) => { setName(e.target.value); named.current = true; }} />
        </label>
        <label className="spj-check" title="Also find the business rules in its code (a knowledge-graph project)">
          <input type="checkbox" data-testid="import-rules" checked={rules} onChange={(e) => setRules(e.target.checked)} />
          {" "}Map business rules too
        </label>
        <p className="spj-quiet">BOSS&apos;s agents, checks and budget are set up for you.</p>
        <GatedButton permission="project:create" className="ag-btn ag-btn-primary spj-wide" data-testid="import-create"
                     disabled={!repo || !name.trim() || busy}
                     title="Create the project with this repo; mapping starts by itself"
                     onClick={() => void create()}>
          {busy ? "Creating…" : "Create project"}
        </GatedButton>
        {error ? <p className="bx-np-error" role="alert" data-testid="import-error">{error}</p> : null}
      </section>
    </div>
  );
}
