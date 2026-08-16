"use client";

import React, { useEffect, useState } from "react";

import {
  fetchFactoryFlowBoard,
  type FactoryFlowBoard,
  emptyFactoryFlowBoard,
} from "../lib/factory-flow";

export function FactoryFlowBoard({ projectId }: { projectId: string }) {
  const [board, setBoard] = useState<FactoryFlowBoard>(emptyFactoryFlowBoard("loading", projectId));

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const next = await fetchFactoryFlowBoard(projectId);
      if (!cancelled) setBoard(next);
    };
    void load();
    const timer = window.setInterval(() => void load(), 20000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [projectId]);

  return (
    <div className="panel" data-testid="factory-flow-board">
      <div className="section-head">
        <div>
          <span className="eyebrow">Factory flow</span>
          <h2>Where official work is, and why it stalled.</h2>
        </div>
        <p className="detail">
          in flight {board.inFlight} · stalled {board.stalled}
          {board.blockedReason ? ` · ${board.blockedReason}` : ""}
        </p>
      </div>
      {board.items.length === 0 ? (
        <p className="empty-state">No factory flow yet. Findings stay empty until a run exists.</p>
      ) : (
        <ul className="stack">
          {board.items.slice(0, 8).map((item) => (
            <li key={item.id}>
              <strong>{item.title || item.id}</strong>{" "}
              <span>
                {item.status}/{item.phase} · {item.diagnosis.state} · {item.diagnosis.klass}
              </span>
              {item.processRunId ? (
                <div className="detail">swarm {item.processRunId} ({item.processStatus})</div>
              ) : null}
              <div className="detail">{item.diagnosis.title}</div>
              {item.diagnosis.recommendedFix ? (
                <div className="detail">{item.diagnosis.recommendedFix}</div>
              ) : null}
              {item.captain?.assignments?.length ? (
                <div className="detail">
                  captain now:{" "}
                  {item.captain.assignments
                    .map((row) => `${row.name || row.agent_id}:${row.task}`)
                    .join(", ")}
                </div>
              ) : null}
              {item.captain?.panel ? (
                <div className="detail">
                  panel ({item.captain.panel.size}): {(item.captain.panel.roles || []).join(", ")}
                </div>
              ) : null}
              {item.captain?.finish?.verdict ? (
                <div className="detail">finish: {item.captain.finish.verdict}</div>
              ) : null}
              {item.steps.length > 0 ? (
                <p className="detail">
                  {item.steps
                    .slice(-8)
                    .map((step) => `${step.lane}:${step.name}${step.status ? `(${step.status})` : ""}`)
                    .join(" → ")}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {board.findings.length > 0 ? (
        <div>
          <h3>Improve loop findings</h3>
          <ul className="stack">
            {board.findings.slice(0, 6).map((finding) => (
              <li key={finding.id}>
                <strong>{finding.klass}</strong> {finding.title}
                <div className="detail">{finding.recommendedFix}</div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
