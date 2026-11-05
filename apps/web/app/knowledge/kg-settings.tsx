"use client";

/**
 * GRAPH › SETTINGS (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.1): what was Reliability › Knowledge
 * settings, moved here whole — what agents get, each repository's code graph with Index now,
 * retention, the embedding model and vector index, budget, sources, the case study and its cost.
 */

import React, { useCallback, useEffect, useMemo, useState } from "react";

import { GatedButton } from "../../components/gated";
import { INDEX_NOW, getGraphStatus, indexNow, type KgGraphStatus, type KgRepository } from "../../lib/kg-view";

/** The knowledge_graph setting's values, in words (engine knowledge/runtime.MODES). */
const AGENT_ACCESS: Record<string, string> = {
  on: "Everything the graph knows: search, the graph, learned facts and precedent.",
  keyword: "Keyword search only: no graph, meaning search, learned facts or precedent.",
  off: "Nothing: agents work without the graph. It keeps indexing.",
};

const TONE: Record<string, string> = { indexed: "green", queued: "accent", running: "accent", failed: "red", too_large: "amber", pending: "grey" };

function RepoRow({ repo, onIndex, busy }: { repo: KgRepository; onIndex: (repo: string) => void; busy: boolean }) {
  const indexed = repo.status === "indexed";
  const working = repo.status === "queued" || repo.status === "running";
  return (
    <li className="kg-repo" data-testid="knowledge-code-repo" data-status={repo.status}>
      <span className="inf-dot" data-tone={TONE[repo.status] ?? "grey"} aria-hidden />
      <span className="kg-repo-name inf-mono">{repo.repo}</span>
      <span className="kg-repo-summary">{repo.summary ?? repo.status}</span>
      <GatedButton permission={INDEX_NOW} className="ag-btn" data-testid="kg-index-now" disabled={busy || working}
                   title={indexed
                     ? "Read this repository's branch again now, in full: files, symbols, calls and imports. No model is called."
                     : "Build this repository's code graph now: parse its branch into files, symbols, calls and imports. No model is called."}
                   onClick={() => onIndex(repo.repo)}>
        {working ? "Indexing…" : indexed ? "Re-index" : "Index"}
      </GatedButton>
    </li>
  );
}

export function KgSettings({ projectId, actor, initial }: { projectId: string; actor: string; initial?: KgGraphStatus }) {
  const w = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const [status, setStatus] = useState<KgGraphStatus | null>(initial ?? null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [indexing, setIndexing] = useState(false);

  const load = useCallback(async () => {
    const r = await getGraphStatus(w);
    if (r.ok) { setStatus(r.data); setError(null); } else setError(r.error);
  }, [w]);
  useEffect(() => { if (!initial) void load(); }, [load, initial]);

  const run = async (repository: string) => {
    setIndexing(true); setNote(null);
    const r = await indexNow(w, repository);
    setIndexing(false);
    if (!r.ok) { setNote({ ok: false, text: `Could not start the index: ${r.error}` }); return; }
    setStatus((cur) => (cur ? { ...cur, repositories: r.data.repositories ?? cur.repositories } : cur));
    setNote({ ok: true, text: r.data.message || "Indexing started." });
  };

  const years = status?.retention_years ?? 1;
  const em = status?.embedding_model;
  return (
    <section className="kg-settings" data-testid="kg-settings" data-area="knowledge-settings" aria-label="Knowledge settings">
      {error ? (
        <div className="kg-banner" data-tone="red" role="alert" data-testid="kg-settings-error">
          <span>{error}</span><button type="button" className="inf-link" onClick={() => void load()}>Retry</button>
        </div>
      ) : null}
      {!status && !error ? <div className="kg-panel-skel" aria-busy="true">{Array.from({ length: 6 }, (_, i) => <span key={i} className="kg-skel-row" />)}</div> : null}
      {status ? (
        <dl className="kg-settings-list">
          {status.agent_access ? (
            <div className="kg-setting">
              <dt>Agents get</dt>
              <dd data-testid="knowledge-agent-access" title={`Project setting knowledge_graph = ${status.agent_access}`}>
                {AGENT_ACCESS[status.agent_access] ?? status.agent_access}
              </dd>
            </div>
          ) : null}
          <div className="kg-setting">
            <dt title="Each registered repository is parsed into the graph when it is registered and when its branch moves">Code</dt>
            <dd>
              {status.repositories?.length ? (
                <ul className="kg-repos" data-testid="knowledge-code">
                  {status.repositories.map((repo) => <RepoRow key={repo.repo} repo={repo} onIndex={(r) => void run(r)} busy={indexing} />)}
                </ul>
              ) : <span data-testid="knowledge-code-none">No repository is registered in this project. <a className="inf-link" href="?area=setup">Repo and models</a></span>}
              {note ? <p className={note.ok ? "kg-ok" : "kg-inline-err"} role={note.ok ? "status" : "alert"} data-testid="kg-index-note">{note.text}</p> : null}
            </dd>
          </div>
          <div className="kg-setting">
            <dt>Retention</dt>
            <dd data-testid="knowledge-retention">Facts stay until they are superseded, then {years === 1 ? "one year" : `${years} years`}.</dd>
          </div>
          <div className="kg-setting">
            <dt>Embedding</dt>
            <dd data-testid="knowledge-embedding">
              {em ? `${em.model} (${em.dimension} dimensions), check ${em.check}.` : "No embedding model is set."}{" "}
              {status.retrieval?.reason ? <span data-testid="knowledge-search-off">{status.retrieval.reason}. </span> : null}
              Vector index: {status.vector_index === "not_created" || !status.vector_index ? "not created" : status.vector_index}.
              <a className="inf-link" href="?area=setup&focus=embedding_model" data-testid="knowledge-embedding-set">Set it in Setup › Models › Manage</a>.
            </dd>
          </div>
          <div className="kg-setting"><dt>Budget</dt><dd>Budget category: {status.budget_category ?? "knowledge"}</dd></div>
          <div className="kg-setting"><dt>Sources</dt><dd>{(status.sources ?? ["adr folder", "connector document"]).join(", ")}. Live fetch stays off.</dd></div>
          <div className="kg-setting"><dt>Case study</dt><dd>{status.ship_gate === "case_study_primary_not_run" ? "The case study has not been run." : status.ship_gate ?? "—"}</dd></div>
          <div className="kg-setting"><dt>Cost</dt><dd>{status.cost_usd == null ? "Cost has not been measured." : `$${status.cost_usd.toFixed(2)}`}</dd></div>
        </dl>
      ) : null}
    </section>
  );
}
