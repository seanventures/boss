"use client";

import React, { useEffect, useState } from "react";

import { API_URL, authHeaders } from "../lib/client";

export type KnowledgeHit = {
  node_id: string;
  name: string;
  body: string;
  trust_tier: string;
};

export type KnowledgeWhy = {
  name: string;
  body: string;
  trust_tier: string;
};

export type KnowledgeStatusView = {
  retention_years: number;
  embedding_model: string | null;
  vector_index: string;
  budget_category: string;
  ship_gate: string;
  cost_usd: number | null;
  sources: string[];
};

type ExplorerProps = {
  projectId: string;
  actorId: string;
  initialHits?: KnowledgeHit[];
  initialWhy?: KnowledgeWhy[];
  initialBlast?: string[];
};

type SettingsProps = {
  projectId: string;
  actorId: string;
  initial?: KnowledgeStatusView;
};

export function KnowledgeExplorer({
  projectId,
  actorId,
  initialHits,
  initialWhy,
  initialBlast,
}: ExplorerProps) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<KnowledgeHit[] | null>(initialHits ?? null);
  const [why, setWhy] = useState<KnowledgeWhy[] | null>(initialWhy ?? null);
  const [blast, setBlast] = useState<string[] | null>(initialBlast ?? null);
  const [note, setNote] = useState<string | null>(null);
  const [selected, setSelected] = useState<string>(initialHits?.[0]?.node_id ?? "");

  useEffect(() => {
    if (initialHits) return;
    if (!projectId || !actorId) {
      setNote("Knowledge search needs a project and an actor.");
    }
  }, [projectId, actorId, initialHits]);

  async function search(event: React.FormEvent) {
    event.preventDefault();
    const term = query.trim();
    if (!term) {
      setNote("Enter a word to search.");
      return;
    }
    setNote(null);
    const url = `${API_URL}/v1/knowledge/search?project_id=${encodeURIComponent(projectId)}&actor=${encodeURIComponent(actorId)}&q=${encodeURIComponent(term)}`;
    try {
      const response = await fetch(url, { headers: authHeaders(), cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      const body = await response.json();
      const next = (body.hits ?? []) as KnowledgeHit[];
      setHits(next);
      setWhy(null);
      setBlast(null);
      setSelected(next[0]?.node_id ?? "");
      if (next.length === 0) setNote("Nothing matched.");
    } catch {
      setNote("Knowledge search unavailable.");
    }
  }

  async function openWhy(nodeId: string) {
    setSelected(nodeId);
    setNote(null);
    const url = `${API_URL}/v1/knowledge/nodes/${encodeURIComponent(nodeId)}?project_id=${encodeURIComponent(projectId)}&actor=${encodeURIComponent(actorId)}`;
    try {
      const response = await fetch(url, { headers: authHeaders(), cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      const body = await response.json();
      setWhy(body.why ?? []);
    } catch {
      setNote("Why is unavailable for that node.");
    }
  }

  async function openBlast() {
    if (!selected) {
      setNote("Choose a node before asking for blast radius.");
      return;
    }
    const url = `${API_URL}/v1/knowledge/blast-radius?project_id=${encodeURIComponent(projectId)}&actor=${encodeURIComponent(actorId)}&node_id=${encodeURIComponent(selected)}`;
    try {
      const response = await fetch(url, { headers: authHeaders(), cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      const body = await response.json();
      setBlast((body.affected ?? []).map((item: { name?: string }) => item.name || "unnamed"));
    } catch {
      setNote("Blast radius is unavailable.");
    }
  }

  return (
    <section className="panel-stack" data-area="knowledge-explorer" aria-label="Knowledge explorer">
      <div className="section-head">
        <div>
          <span className="eyebrow">Knowledge</span>
          <h2>Search what the project knows</h2>
        </div>
      </div>
      <form onSubmit={search} style={{ display: "flex", gap: 8 }}>
        <label htmlFor="knowledge-query" className="detail">Search</label>
        <input
          id="knowledge-query"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="symbol, fact, or decision"
        />
        <button type="submit">Search</button>
        <button type="button" onClick={openBlast}>Show blast radius</button>
      </form>
      {note ? <p className="detail">{note}</p> : null}
      <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 8 }}>
        {(hits ?? []).map((hit) => (
          <li key={hit.node_id}>
            <button type="button" onClick={() => openWhy(hit.node_id)}>
              {hit.name} · {hit.trust_tier}
            </button>
          </li>
        ))}
      </ul>
      {why && why.length > 0 ? (
        <div>
          <h3>Why</h3>
          <ul>
            {why.map((fact) => (
              <li key={fact.body}>
                {fact.body} · {fact.trust_tier}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {blast && blast.length > 0 ? (
        <div>
          <h3>Blast radius</h3>
          <ul>
            {blast.map((name) => (
              <li key={name}>{name}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

export function KnowledgeSettings({ projectId, actorId, initial }: SettingsProps) {
  const [status, setStatus] = useState<KnowledgeStatusView | null>(initial ?? null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    if (initial) return;
    if (!projectId || !actorId) {
      setNote("Knowledge settings need a project and an actor.");
      return;
    }
    let cancelled = false;
    const url = `${API_URL}/v1/knowledge/graph-status?project_id=${encodeURIComponent(projectId)}&actor=${encodeURIComponent(actorId)}`;
    fetch(url, { headers: authHeaders(), cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      })
      .then((body) => {
        if (!cancelled) setStatus(body);
      })
      .catch(() => {
        if (!cancelled) setNote("Knowledge settings unavailable.");
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, actorId, initial]);

  const years = status?.retention_years ?? 1;
  return (
    <section className="panel-stack" data-area="knowledge-settings" aria-label="Knowledge settings">
      <div className="section-head">
        <div>
          <span className="eyebrow">Knowledge</span>
          <h2>Knowledge settings</h2>
        </div>
      </div>
      {note ? <p className="detail">{note}</p> : null}
      <dl className="detail">
        <dt>Retention</dt>
        <dd>Facts stay until they are superseded, then {years === 1 ? "one year" : `${years} years`}.</dd>
        <dt>Embedding</dt>
        <dd>
          {status?.embedding_model
            ? status.embedding_model
            : "No embedding model is configured."}{" "}
          Vector index: {status?.vector_index === "not_created" ? "not created" : status?.vector_index ?? "not created"}.
        </dd>
        <dt>Budget</dt>
        <dd>Budget category: {status?.budget_category ?? "knowledge"}</dd>
        <dt>Sources</dt>
        <dd>{(status?.sources ?? ["adr folder", "connector document"]).join(", ")}. Live fetch stays off.</dd>
        <dt>Case study</dt>
        <dd>
          {status?.ship_gate === "case_study_primary_not_run"
            ? "The case study has not been run."
            : status?.ship_gate}
        </dd>
        <dt>Cost</dt>
        <dd>{status?.cost_usd == null ? "Cost has not been measured." : status.cost_usd}</dd>
      </dl>
    </section>
  );
}

export function BasedOn({ factIds }: { factIds: string[] }) {
  if (factIds.length === 0) return null;
  return (
    <p className="detail" data-testid="based-on">
      Based on {factIds.join(", ")}
    </p>
  );
}
