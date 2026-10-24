"use client";

/**
 * GRAPH — code, rules and what agents learned (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5).
 *
 * The area's shell: the tab row (§5.1: Graph · Rules review · Settings), the project's
 * repositories and graph status (indexing, the add-on entitlement), the archived banner, and deep
 * links (?kg_node= focuses a node, ?kg_tab=rules&kg_scan= opens a scan's review, ?kg_sub=facts
 * the fact queue). The tabs' bodies are graph-view.tsx, rules-review.tsx and kg-settings.tsx.
 * Tokens only (kg.css). Nothing is read until the area is first on screen, and the indexing
 * state refreshes only while it is on screen.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AreaHead, type HeadTab } from "../infra/kit";
import { useOnScreen } from "../../lib/area-shown";
import { useGate } from "../../components/gated";
import { INDEX_NOW, getGraphStatus, getRepositories, indexNow, rulesLock, type KgGraphStatus, type KgRepository } from "../../lib/kg-view";
import { GraphView } from "./graph-view";
import { KgArchived, KgIndexing, KgLoading, KgNoRepo } from "./kg-states";
import { KgSettings } from "./kg-settings";
import { RulesReview } from "./rules-review";
import "../infra/infra.css";
import "./kg.css";

type GraphTab = "graph" | "rules" | "settings";

type Deep = { tab: GraphTab | null; node: string | null; scan: string | null; sub: "facts" | "rules" | null };

function readDeep(): Deep {
  if (typeof window === "undefined") return { tab: null, node: null, scan: null, sub: null };
  const p = new URLSearchParams(window.location.search);
  if (p.get("area") !== "knowledge") return { tab: null, node: null, scan: null, sub: null };
  const t = p.get("kg_tab");
  const node = p.get("kg_node");
  return {
    tab: t === "rules" || t === "settings" || t === "graph" ? t : node ? "graph" : null,
    node,
    scan: p.get("kg_scan"),
    sub: p.get("kg_sub") === "facts" ? "facts" : null,
  };
}

export function GraphArea({ projectId, actor, orgId, archived = false }: {
  projectId: string; actor: string; orgId?: string | null;
  /** The project is archived (S5): read-only, scans and review off, with the reason. */
  archived?: boolean;
}) {
  const w = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const onScreen = useOnScreen();
  const [deep, setDeep] = useState<Deep>(() => readDeep());
  const [tab, setTab] = useState<GraphTab>(() => deep.tab ?? "graph");
  const [repos, setRepos] = useState<{ data: KgRepository[] | null; error: string | null }>({ data: null, error: null });
  const [status, setStatus] = useState<KgGraphStatus | null>(null);
  const [proposed, setProposed] = useState<number | null>(null);
  const [editId, setEditId] = useState<string | null>(null);
  const [focus, setFocus] = useState<{ id: string; seq: number } | null>(() => (deep.node ? { id: deep.node, seq: 1 } : null));
  const [indexing, setIndexing] = useState(false);
  const seq = useRef(1);

  // Deep links arrive on load and on every in-console navigation (ConsoleShell dispatches popstate).
  useEffect(() => {
    const on = () => {
      const d = readDeep();
      setDeep(d);
      if (d.tab) setTab(d.tab);
      if (d.node) { seq.current += 1; setFocus({ id: d.node, seq: seq.current }); }
    };
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);

  const seen = useRef(false);
  if (onScreen) seen.current = true;
  const armed = seen.current;

  const loadRepos = useCallback(async () => {
    const r = await getRepositories(w);
    if (r.ok) setRepos({ data: r.data.repositories ?? [], error: null });
    else setRepos((cur) => ({ data: cur.data, error: r.error }));
  }, [w]);
  useEffect(() => {
    if (!armed) return;
    void loadRepos();
    void getGraphStatus(w).then((r) => { if (r.ok) setStatus(r.data); });
  }, [armed, loadRepos, w]);

  // Indexing refreshes itself — only while on screen.
  const working = (repos.data ?? []).some((r) => r.status === "queued" || r.status === "running" || r.status === "pending");
  useEffect(() => {
    if (!onScreen || !working) return undefined;
    const t = setInterval(() => void loadRepos(), 5000);
    return () => clearInterval(t);
  }, [onScreen, working, loadRepos]);

  // Index now from the "not built yet" card (the card's button is pre-greyed on the same gate).
  const indexGate = useGate(INDEX_NOW);
  const startIndex = async () => {
    if (!indexGate.allowed || archived) return;
    setIndexing(true);
    await indexNow(w, "");
    setIndexing(false);
    void loadRepos();
  };

  /** A tab is also the URL's (?kg_tab=), so a reload or a shared link opens the same tab. */
  const changeTab = (next: GraphTab) => {
    setTab(next);
    try {
      const url = new URL(window.location.href);
      if (url.searchParams.get("area") === "knowledge") {
        if (next === "graph") url.searchParams.delete("kg_tab"); else url.searchParams.set("kg_tab", next);
        window.history.replaceState(window.history.state, "", url.toString());
      }
    } catch { /* no location */ }
  };

  const rulesLocked = rulesLock(status);
  const anyIndexed = (repos.data ?? []).some((r) => r.status === "indexed" || (r.graph?.nodes ?? 0) > 0);
  const totalNodes = (repos.data ?? []).reduce((s, r) => s + (r.graph?.nodes ?? 0), 0);

  const TABS: HeadTab[] = [
    { key: "graph", label: "Graph", tip: "The whole repository, or one symbol, rule or learning and what connects to it" },
    { key: "rules", label: "Rules review", tip: "Business rules agents proposed: approve, edit or reject", count: proposed ? proposed : undefined },
    { key: "settings", label: "Settings", tip: "Indexing, embeddings and retention" },
  ];

  // The Graph tab stays mounted while another tab shows (tester round 2: Rules review and back
  // dropped the focus, the search and the selection); only its key handling pauses.
  let graphBody: React.ReactNode = null;
  {
    if (!repos.data && !repos.error) graphBody = <KgLoading nodes={totalNodes || null} />;
    else if (repos.data && repos.data.length === 0) graphBody = <KgNoRepo orgId={orgId} />;
    else if (repos.data && !anyIndexed && working) graphBody = <KgIndexing repos={repos.data} />;
    else if (repos.data && !anyIndexed) {
      graphBody = <KgIndexing repos={repos.data} busy={indexing} onIndex={() => void startIndex()} />;
    } else {
      graphBody = (
        <GraphView projectId={projectId} actor={actor} orgId={orgId} repos={repos.data} onReposChanged={() => void loadRepos()}
                   rulesLocked={rulesLocked} archived={archived} focusRequest={focus} inactive={tab !== "graph"}
                   onEditRule={(id) => { setEditId(id); changeTab("rules"); }} />
      );
    }
  }
  let body: React.ReactNode = null;
  if (tab === "rules") {
    body = (
      <RulesReview projectId={projectId} actor={actor} orgId={orgId} archived={archived} locked={rulesLocked}
                   initialScan={deep.scan} initialSub={deep.sub ?? undefined} editId={editId} onCount={setProposed}
                   onFocusNode={(id) => { seq.current += 1; setFocus({ id, seq: seq.current }); changeTab("graph"); }} />
    );
  } else if (tab === "settings") {
    body = <KgSettings projectId={projectId} actor={actor} />;
  }

  return (
    <div className="kg" data-testid="kg" data-project={projectId} data-actor={actor} data-tab={tab}>
      <AreaHead area="kg" title="Graph" tabs={TABS} active={tab} onTab={(k) => changeTab(k as GraphTab)}
                counts={totalNodes ? <span>{totalNodes.toLocaleString("en-US")} code nodes</span> : undefined} />
      {archived ? <KgArchived /> : null}
      {repos.error && !repos.data && tab === "graph" ? (
        <div className="kg-banner" data-tone="red" role="alert" data-testid="kg-repos-error">
          <span>Couldn&apos;t read this project&apos;s repositories: {repos.error}</span>
          <button type="button" className="inf-link" onClick={() => void loadRepos()}>Retry</button>
        </div>
      ) : null}
      <div className="kg-tab-graph" hidden={tab !== "graph"} data-testid="kg-graph-pane">{graphBody}</div>
      {body}
    </div>
  );
}
