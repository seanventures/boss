"use client";

import React, { useEffect, useState } from "react";

import { API_URL, authHeaders } from "../lib/client";

export type PromotionCandidate = {
  id: string;
  node_key: string;
  body: string;
};

type Props = {
  projectId: string;
  actorId: string;
  initial?: PromotionCandidate[];
};

export function KnowledgePromotionQueue({ projectId, actorId, initial }: Props) {
  const [candidates, setCandidates] = useState<PromotionCandidate[] | null>(initial ?? null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    if (initial) return;
    if (!projectId || !actorId) {
      setNote("Knowledge queue needs a project and an actor.");
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
        if (!cancelled) setCandidates(body.promotion_candidates ?? []);
      })
      .catch(() => {
        if (!cancelled) setNote("Knowledge queue unavailable.");
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, actorId, initial]);

  async function decide(id: string, action: "promote" | "reject") {
    setNote(null);
    const response = await fetch(`${API_URL}/v1/knowledge/facts/${encodeURIComponent(id)}/${action}`, {
      method: "POST",
      headers: { ...authHeaders(), "content-type": "application/json" },
      body: JSON.stringify({ project_id: projectId, actor: actorId, step_up: "confirmed" }),
    });
    if (!response.ok) {
      setNote(`${action} failed.`);
      return;
    }
    setCandidates((current) => (current ?? []).filter((item) => item.id !== id));
    setNote(action === "promote" ? "Approved." : "Rejected.");
  }

  return (
    <section className="panel-stack" data-area="knowledge-promotion" aria-label="Knowledge promotion queue">
      <div className="section-head">
        <div>
          <span className="eyebrow">Decisions</span>
          <h2>Knowledge facts waiting for a person</h2>
        </div>
      </div>
      {note ? <p className="detail">{note}</p> : null}
      {candidates && candidates.length === 0 ? (
        <p className="empty-state">No inferred facts are ready for promotion.</p>
      ) : null}
      <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 8 }}>
        {(candidates ?? []).map((item) => (
          <li key={item.id} style={{ display: "grid", gap: 6 }}>
            <p className="detail" style={{ margin: 0 }}>{item.body}</p>
            <div style={{ display: "flex", gap: 8 }}>
              <button type="button" onClick={() => decide(item.id, "promote")}>Approve fact</button>
              <button type="button" onClick={() => decide(item.id, "reject")}>Reject fact</button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
