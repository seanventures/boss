"use client";

/**
 * A KNOWLEDGE-GRAPH PROJECT'S HOME CARD (BOSSAPP-768 project flow), on the Command Center: the
 * graph's lifecycle — code indexed → rules proposed and approved → what agents learned — with the
 * real counts, the team that works on it, and the one next thing (continue the setup, review the
 * rules waiting, or open the graph). One read: GET /v1/knowledge/setup, the stepper's own.
 */
import React, { useEffect, useMemo, useState } from "react";

import { AgentAvatar } from "../agent-avatar";
import { consoleHref } from "../console-href";
import { getSetup, hasIndexedCode, SETUP_STEPS, stepsLeftWords, type KgSetupResponse } from "../../lib/kg-setup";
import "./kg.css";

function n(v: number): string { return v.toLocaleString("en-US"); }

export function KgLifecycleCard({ projectId, actor, orgId, projectType }: {
  projectId: string; actor: string; orgId?: string | null;
  /** A knowledge-graph project shows the card from the first paint; any other only once it has a graph. */
  projectType?: string;
}) {
  const w = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const [setup, setSetup] = useState<KgSetupResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    void getSetup(w, ctl.signal).then((r) => {
      if (ctl.signal.aborted) return;
      if (r.ok) setSetup(r.data); else if (r.status !== -1) setError(r.error);
    });
    return () => ctl.abort();
  }, [w]);
  const href = (params: Record<string, string | undefined>) =>
    consoleHref({ area: "knowledge", kg_tab: undefined, ...(orgId ? { org: orgId } : {}), ...params });

  // SHOWN FOR ANY PROJECT WITH A CODE GRAPH (tester round 2: NorthPeak Bank had 51k nodes and no
  // card): a knowledge-graph project always, an app project once something is indexed.
  const isKg = (setup?.project_type ?? projectType) === "knowledge_graph";
  if (setup ? !(isKg || hasIndexedCode(setup)) : !isKg) return null;
  if (!setup) {
    return (
      <section className="kg-life" data-testid="kg-lifecycle" aria-busy={!error}>
        <header className="kg-life-head"><span className="kg-empty-eyebrow">Knowledge graph</span></header>
        {error ? <p className="kg-inline-err" role="alert">Couldn&apos;t read the knowledge graph: {error}</p>
          : <div className="kg-shimmer kg-life-loading" aria-hidden />}
      </section>
    );
  }
  const indexed = setup.repositories.filter((r) => r.status === "indexed").length;
  const indexing = setup.repositories.some((r) => ["queued", "running", "pending"].includes(r.status));
  const team = setup.team.candidates.filter((c) => setup.team.role_keys.includes(c.role_key));
  const next = SETUP_STEPS.find((s) => s.key === setup.next);
  const stages = [
    { key: "code", layer: "code", label: "Code indexed", value: n(setup.code_nodes),
      sub: setup.repositories.length ? `${indexed} of ${setup.repositories.length} repositories${indexing ? " · indexing" : ""}` : "No repository yet" },
    { key: "rules", layer: "rules", label: "Rules", value: n(setup.rules.approved),
      sub: `approved · ${n(setup.rules.proposed)} waiting for review` },
    { key: "learning", layer: "learning", label: "Learnings", value: n(setup.learnings),
      sub: "recorded by agents as they work" },
  ];
  return (
    <section className="kg-life" data-testid="kg-lifecycle" aria-label="Knowledge graph">
      <header className="kg-life-head">
        <span className="kg-empty-eyebrow">Knowledge graph</span>
        <span className="kg-life-progress" data-testid="kg-lifecycle-progress">{next ? `Setup · ${stepsLeftWords(setup)}` : "Set up"}</span>
        <span className="ag-spacer" />
        {next ? (
          <a className="ag-btn ag-btn-primary" href={href({ kg_tab: "setup" })} data-testid="kg-lifecycle-continue">Continue: {next.label}</a>
        ) : setup.rules.proposed ? (
          <a className="ag-btn ag-btn-primary" href={href({ kg_tab: "rules" })} data-testid="kg-lifecycle-review">Review {n(setup.rules.proposed)} rules</a>
        ) : null}
        <a className="ag-btn" href={href({})} data-testid="kg-lifecycle-open">Open the Knowledge Graph</a>
      </header>
      <ol className="kg-life-stages" data-testid="kg-lifecycle-stages">
        {stages.map((s, i) => (
          <li key={s.key} className="kg-life-stage" data-testid={`kg-lifecycle-${s.key}`}>
            <span className="kg-dot" data-layer={s.layer} aria-hidden />
            <span className="kg-life-value">{s.value}</span>
            <span className="kg-life-label">{s.label}</span>
            <small>{s.sub}</small>
            {i < stages.length - 1 ? <span className="kg-life-arrow" aria-hidden>→</span> : null}
          </li>
        ))}
      </ol>
      <div className="kg-life-team" data-testid="kg-lifecycle-team">
        <span className="kg-life-team-label">{setup.team.chosen ? "Team" : "Team (BOSS's default until you choose)"}</span>
        <ul>
          {team.slice(0, 8).map((c) => (
            <li key={c.role_key} title={c.does ?? c.mission ?? c.display_name}>
              <AgentAvatar agent={c.role_key} size={22} />
              <span className="kg-life-name">{c.display_name}</span>
            </li>
          ))}
          {team.length > 8 ? <li className="kg-quiet">+{team.length - 8} more</li> : null}
        </ul>
      </div>
    </section>
  );
}
