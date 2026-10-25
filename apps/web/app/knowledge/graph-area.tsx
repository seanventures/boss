"use client";

/**
 * KNOWLEDGE GRAPH — code, rules and what agents learned (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5).
 *
 * Named for what it is (operator, 2026-10-24: "people without context can't assume it is a
 * knowledge graph"): the rail says Knowledge Graph, a first visit gets the intro strip (the three
 * layers, and that every node says which agent added it and who approved it), and a project with
 * no graph gets "Start a knowledge graph for this project", never an empty canvas. The Setup tab
 * (kg-setup.tsx) is a knowledge-graph project's guided flow — import code, assign the team, first
 * scan, review — and such a project opens on it until its code is in (?kg_tab=setup links to it).
 *
 * The area's shell: the tab row (§5.1: Graph · Rules review · Settings, and Setup), the project's
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
import { getSetup, hasIndexedCode, setupOpens, type KgSetupResponse } from "../../lib/kg-setup";
import type { ProjectType } from "@boss/api-client";
import { GraphView } from "./graph-view";
import { KgIntro, KgSetup, KgSetupStrip, KgStart } from "./kg-setup";
import { KgArchived, KgIndexing, KgLoading } from "./kg-states";
import { KgSettings } from "./kg-settings";
import { RulesReview } from "./rules-review";
import "../infra/infra.css";
import "./kg.css";

type GraphTab = "graph" | "rules" | "settings" | "setup";

type Deep = { tab: GraphTab | null; node: string | null; scan: string | null; sub: "facts" | "rules" | null };

function readDeep(): Deep {
  if (typeof window === "undefined") return { tab: null, node: null, scan: null, sub: null };
  const p = new URLSearchParams(window.location.search);
  if (p.get("area") !== "knowledge") return { tab: null, node: null, scan: null, sub: null };
  const t = p.get("kg_tab");
  const node = p.get("kg_node");
  return {
    tab: t === "rules" || t === "settings" || t === "graph" || t === "setup" ? t : node ? "graph" : null,
    node,
    scan: p.get("kg_scan"),
    sub: p.get("kg_sub") === "facts" ? "facts" : null,
  };
}

export function GraphArea({ projectId, actor, orgId, archived = false, projectType }: {
  projectId: string; actor: string; orgId?: string | null;
  /** The project is archived (S5): read-only, scans and review off, with the reason. */
  archived?: boolean;
  /** What the project is for: a knowledge-graph project opens on its Setup until its code is in. */
  projectType?: ProjectType;
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
  const [setup, setSetup] = useState<{ data: KgSetupResponse | null; error: string | null }>({ data: null, error: null });
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
  const loadSetup = useCallback(async () => {
    const r = await getSetup(w);
    if (r.ok) setSetup({ data: r.data, error: null });
    // Said where the setup shows (an older API without the route says so in words, never a spinner).
    else setSetup((cur) => ({ data: cur.data, error: r.error }));
  }, [w]);
  useEffect(() => {
    if (!armed) return;
    void loadRepos();
    void loadSetup();
    void getGraphStatus(w).then((r) => { if (r.ok) setStatus(r.data); });
  }, [armed, loadRepos, loadSetup, w]);

  // A knowledge-graph project whose code is not in yet opens on its Setup (unless a link named a tab).
  const routed = useRef(false);
  useEffect(() => {
    if (routed.current || !setup.data) return;
    routed.current = true;
    if (!deep.tab && setupOpens(setup.data)) setTab("setup");
  }, [setup.data, deep.tab]);
  const isKgProject = (setup.data?.project_type ?? projectType) === "knowledge_graph";
  const setupStep = setup.data?.steps.find((st) => st.key === "import");
  const setupWorking = !!setup.data && (setupStep?.state === "working" || setup.data.scans.running > 0);
  // The setup follows indexing and a running scan by itself — only while it is the tab on screen.
  useEffect(() => {
    if (!onScreen || tab !== "setup" || !setupWorking) return undefined;
    const t = setInterval(() => { void loadSetup(); void loadRepos(); }, 5000);
    return () => clearInterval(t);
  }, [onScreen, tab, setupWorking, loadSetup, loadRepos]);

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

  const stepsLeft = setup.data ? setup.data.steps.length - setup.data.done_count : 0;
  // Any project with a graph has a setup to finish (its team, a first scan), not only one created
  // as a knowledge-graph project (tester round 2: NorthPeak Bank had no strip).
  const hasGraph = hasIndexedCode(setup.data);
  const showSetupTab = isKgProject || hasGraph || tab === "setup";
  const TABS: HeadTab[] = [
    ...(showSetupTab ? [{ key: "setup", label: "Setup", tip: "Import code, assign the team, a first scan, review: this project's knowledge graph, in order",
                           count: stepsLeft ? stepsLeft : undefined }] : []),
    { key: "graph", label: "Graph", tip: "The whole repository, or one symbol, rule or learning and what connects to it" },
    { key: "rules", label: "Rules review", tip: "Business rules agents proposed: approve, edit or reject", count: proposed ? proposed : undefined },
    { key: "settings", label: "Settings", tip: "Indexing, embeddings and retention" },
  ];

  // The Graph tab stays mounted while another tab shows (tester round 2: Rules review and back
  // dropped the focus, the search and the selection); only its key handling pauses.
  let graphBody: React.ReactNode = null;
  {
    if (!repos.data && !repos.error) graphBody = <KgLoading nodes={totalNodes || null} />;
    else if (repos.data && repos.data.length === 0) graphBody = <KgStart archived={archived} onStart={() => changeTab("setup")} />;
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
  } else if (tab === "setup") {
    body = (
      <KgSetup w={w} setup={setup.data} error={setup.error} archived={archived}
               onReload={() => { void loadSetup(); void loadRepos(); }}
               onOpenTab={(next) => { void loadRepos(); changeTab(next); }} />
    );
  }

  return (
    <div className="kg" data-testid="kg" data-project={projectId} data-actor={actor} data-tab={tab}>
      <AreaHead area="kg" title="Knowledge Graph" tabs={TABS} active={tab} onTab={(k) => changeTab(k as GraphTab)}
                counts={totalNodes ? <span>{totalNodes.toLocaleString("en-US")} code nodes</span> : undefined} />
      {archived ? <KgArchived /> : null}
      {/* Over a graph only: the Start card already says what the three layers are. */}
      {tab === "graph" && anyIndexed ? <KgIntro /> : null}
      {tab === "graph" && (isKgProject || hasGraph) && setup.data?.next && anyIndexed ? (
        <KgSetupStrip setup={setup.data} onOpen={() => changeTab("setup")} />
      ) : null}
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
