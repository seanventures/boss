"use client";

/**
 * THE GRAPH TAB (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.2–§5.5): `[tree | canvas | detail]` under one
 * toolbar — search (⌘K), Whole repo · Focus, hops 1–3, layer chips, zoom − / + / Fit, the repo
 * picker and List view; "Built … · Rebuild" sits on the line under it. The tree, the canvas and
 * the panel share one selection; every view change is pushed on a small history so Esc goes back.
 *
 * Tester round 1: Whole repo draws at most ten areas (the rest fold into "N more areas", one press
 * away) with only their strongest calls, every bubble labelled and nothing overlapping; Focus
 * names every neighbour along its ray; the canvas, the panel and the tree show the same
 * connections and the same selection; the work area fits the viewport and the canvas gets the room.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ListBullets, MagnifyingGlass, TreeStructure, X } from "@phosphor-icons/react";

import { useGate } from "../../components/gated";
import { useOnScreen } from "../../lib/area-shown";
import { useAreaKeydown } from "../../lib/area-keys";
import {
  FOCUS_CEILING, MORE_ID, focusLayout, isBubble, kindName, kindToken, legendKey, overviewLayout,
  pickEdges, pickOverview, placeNear,
} from "../../lib/kg-layout";
import {
  INDEX_NOW, distinctPaths, getLive, getNeighbourhood, getOverview, getSearch, indexNow, layersParam, rankHits,
  type KgRepository,
} from "../../lib/kg-view";
import type {
  KgLiveResponse, KgNeighbourhoodResponse, KgNodeDetailResponse, KgOverviewResponse, KgSearchHit, KgSearchResponse,
  KgTreeTab, KgViewEdge, KgViewNode,
} from "../../lib/kg-view-types";
import { GraphCanvas, GraphLegend, GraphTable, edgeKey, type CanvasEdge, type CanvasHandle, type CanvasNode } from "./graph-canvas";
import { CanvasLoading, KgBuilt, KgEmptyLayer, KgError, KgIndexing, KgReadError, KgTruncation, StageCard } from "./kg-states";
import { NodePanel, ScanForRules, type BubbleSummary } from "./node-panel";
import { ProjectTree, type TreePick } from "./project-tree";

type Layer = "code" | "rules" | "learning";
type View =
  | { mode: "overview"; area: string | null; page: number }
  | { mode: "focus"; node: string; depth: 1 | 2 | 3; limit: number };

type Load<T> = { data: T | null; error: { status: number; text: string; unavailable?: boolean } | null; loading: boolean };
const idle = <T,>(): Load<T> => ({ data: null, error: null, loading: false });

/** The neighbourhood a Focus asks for first, and what Show more raises it to (the server's cap). */
export const FOCUS_FIRST = 40;
export const FOCUS_MORE = FOCUS_CEILING;

const LAYER_CHIPS: Array<{ key: Layer; label: string }> = [
  { key: "code", label: "Code" }, { key: "rules", label: "Rules" }, { key: "learning", label: "Learning" },
];

/** Connections the panel lists that the canvas also draws around the Focus centre (P1-5): the
 *  structural and learning edges; a class's methods (defines) and plain references stay in the panel. */
const DRAWN_FROM_DETAIL = new Set(["calls", "extends", "implements", "overrides", "dispatches_to", "imports", "about",
  "evidenced_by", "governs", "consulted", "reused", "caused_by", "fixed_by", "produced", "ruled_out", "corrects",
  "supersedes", "approved_by", "changed"]);

export type GraphViewProps = {
  projectId: string;
  actor: string;
  orgId?: string | null;
  repos: KgRepository[] | null;
  onReposChanged: () => void;
  rulesLocked: string | null;
  archived: boolean;
  /** A deep link (?kg_node=) or a rule's evidence chip: select and focus it (seq makes a repeat count). */
  focusRequest?: { id: string; seq: number } | null;
  onEditRule: (ruleId: string) => void;
};

/** What a bubble says under its name: classes, or (a Python area) its functions (P2). */
export function bubbleSub(n: KgViewNode): string | undefined {
  const c = n.counts;
  if (!c || n.kind === "layer") return n.sub;
  const num = (v: number, one: string, many: string) => `${v.toLocaleString("en-US")} ${v === 1 ? one : many}`;
  if (c.classes) return num(c.classes, "class", "classes");
  if (c.functions) return num(c.functions, "function", "functions");
  if (c.methods) return num(c.methods, "function", "functions");
  if (c.packages) return num(c.packages, "package", "packages");
  if (c.areas) return num(c.areas, "area", "areas");
  return n.sub;
}

function toCanvas(n: KgViewNode, bubbleOk: boolean, fresh = false): CanvasNode {
  const bubble = bubbleOk && (isBubble(n.kind) || n.kind === "more");
  return {
    id: n.id, label: n.label, sub: bubble ? bubbleSub(n) : n.sub, kind: n.kind, symbol_kind: n.symbol_kind, layer: n.layer,
    token: n.kind === "more" ? "--kg-more" : kindToken(n.kind, n.symbol_kind, n.id), bubble, fresh,
    rank: n.kind === "layer" ? 1e6 : n.counts ? (n.counts.classes ?? Object.values(n.counts).reduce((a, b) => a + b, 0)) : 0,
  };
}

/** A label box's width (screen px at 100%): the layout keeps these apart. */
function boxWidth(n: KgViewNode): number {
  const name = Math.min(n.label.length, 28) * 9 + 16;
  const sub = (bubbleSub(n) ?? "").length * 7.2 + 16;
  return Math.max(120, name, sub);
}

function treeTabFor(layer: string): KgTreeTab {
  return layer === "rules" ? "rules" : layer === "learning" ? "learning" : "project";
}

/** The work area fits the viewport: from its top to the window's bottom (P1-11), never shorter than
 *  a usable canvas. */
function useFitHeight(): [React.RefObject<HTMLDivElement | null>, number | null] {
  const ref = useRef<HTMLDivElement>(null);
  const [h, setH] = useState<number | null>(null);
  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const measure = () => {
      const el = ref.current;
      if (!el || !el.offsetParent) return;
      const top = el.getBoundingClientRect().top + window.scrollY;
      setH(Math.max(420, Math.round(window.innerHeight - Math.max(0, top - window.scrollY) - 20)));
    };
    measure();
    const t = window.setTimeout(measure, 200);
    window.addEventListener("resize", measure);
    return () => { window.clearTimeout(t); window.removeEventListener("resize", measure); };
  });
  return [ref, h];
}

export function GraphView({ projectId, actor, orgId, repos, onReposChanged, rulesLocked, archived, focusRequest, onEditRule }: GraphViewProps) {
  const w = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const onScreen = useOnScreen();
  const [chips, setChips] = useState<Set<Layer>>(new Set());
  const [repo, setRepo] = useState<string | null>(null);
  const [view, setView] = useState<View>({ mode: "overview", area: null, page: 0 });
  const history = useRef<View[]>([]);
  const [overview, setOverview] = useState<Load<KgOverviewResponse>>({ ...idle(), loading: true });
  const [hood, setHood] = useState<Load<KgNeighbourhoodResponse>>(idle());
  const [selected, setSelected] = useState<{ id: string; node?: KgViewNode } | null>(null);
  const [treeTab, setTreeTab] = useState<KgTreeTab>("project");
  const [reveal, setReveal] = useState<{ label: string; nodeId: string } | null>(null);
  const [listView, setListView] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [treeOpen, setTreeOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const [nonce, setNonce] = useState(0);
  const [treeKey, setTreeKey] = useState(0);
  const [centerDetail, setCenterDetail] = useState<KgNodeDetailResponse | null>(null);
  const bubbleCache = useRef(new Map<string, BubbleSummary>());
  const canvasRef = useRef<CanvasHandle>(null);
  const [workRef, workHeight] = useFitHeight();
  // The panel as a drawer over the canvas (medium widths): the canvas fits beside it, not under it.
  const [occlude, setOcclude] = useState(0);
  useEffect(() => {
    const el = workRef.current?.querySelector<HTMLElement>(".kg-col-panel");
    if (!el || typeof getComputedStyle === "undefined") return;
    const drawer = getComputedStyle(el).position === "absolute";
    setOcclude(drawer && panelOpen && selected ? el.offsetWidth : 0);
  });
  // Rebuild (index now): KgBuilt pre-greys its button on the same gate; the press checks it too.
  const indexGate = useGate(INDEX_NOW);
  const layers = layersParam(chips);

  // ── the overview ─────────────────────────────────────────────────────────────────────────
  const area = view.mode === "overview" ? view.area : null;
  const loadOverview = useCallback(async (quiet = false) => {
    if (!quiet) setOverview((o) => ({ ...o, loading: true }));
    const r = await getOverview(w, { layers, repo, area });
    if (r.ok) setOverview({ data: r.data, error: null, loading: false });
    else setOverview((o) => ({ data: quiet ? o.data : null, error: { status: r.status, text: r.error, unavailable: r.unavailable }, loading: false }));
  }, [w, layers, repo, area]);
  // Nothing is read until the area is first on screen (it is mounted hidden, like every area).
  const seen = useRef(false);
  if (onScreen) seen.current = true;
  const armed = seen.current;
  useEffect(() => { if (armed) void loadOverview(); }, [loadOverview, nonce, armed]);

  // ── focus ────────────────────────────────────────────────────────────────────────────────
  const focusKey = view.mode === "focus" ? `${view.node}|${view.depth}|${view.limit}|${layers}` : "";
  useEffect(() => {
    if (view.mode !== "focus") { setHood(idle()); return undefined; }
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    setHood((h) => ({ ...h, loading: true, error: null }));
    void getNeighbourhood(w, view.node, { depth: view.depth, layers, limit: Math.min(view.limit, FOCUS_MORE) }, ctl?.signal).then((r) => {
      if (r.ok) setHood({ data: r.data, error: null, loading: false });
      else if (r.status !== -1) setHood({ data: null, error: { status: r.status, text: r.error, unavailable: r.unavailable }, loading: false });
    });
    return () => ctl?.abort();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusKey, w, nonce]);

  const go = useCallback((next: View) => {
    setView((cur) => {
      history.current = [...history.current.slice(-19), cur];
      return next;
    });
  }, []);
  const back = useCallback(() => {
    const prev = history.current.pop();
    if (prev) setView(prev);
  }, []);

  const select = useCallback((id: string, node?: KgViewNode) => {
    setSelected({ id, node });
    setPanelOpen(true);
    // The tree opens to whatever is selected, wherever the selection came from (P1-7).
    if (node && !/^(layer|more):/.test(id)) setReveal({ label: node.label.replace(/\(\)$/, ""), nodeId: id });
  }, []);
  const focusOn = useCallback((id: string, node?: KgViewNode, depth?: 1 | 2 | 3) => {
    select(id, node);
    go({ mode: "focus", node: id, depth: depth ?? (view.mode === "focus" ? view.depth : 1), limit: FOCUS_FIRST });
  }, [go, select, view]);

  // Deep link.
  const linked = useRef<number | null>(null);
  useEffect(() => {
    if (focusRequest && focusRequest.seq !== linked.current) {
      linked.current = focusRequest.seq;
      focusOn(focusRequest.id);
    }
  }, [focusRequest, focusOn]);

  // ── live: new learnings fade in where they hang (§4.4) ───────────────────────────────────
  const [live, setLive] = useState<{ active: number; delay: number; cursor: string | null; tick: number }>({ active: 0, delay: 5000, cursor: null, tick: 0 });
  const [liveAdds, setLiveAdds] = useState<{ nodes: KgViewNode[]; edges: KgViewEdge[] }>({ nodes: [], edges: [] });
  useEffect(() => { setLiveAdds({ nodes: [], edges: [] }); }, [focusKey, area]);
  useEffect(() => {
    if (!onScreen) return undefined;
    let cancelled = false;
    void getLive(w, { since: live.cursor }).then((r) => {
      if (cancelled || !r.ok) return;
      const fresh = r.data.learnings.length + r.data.rules.length + r.data.edges.length;
      setLive((l) => ({ ...l, active: r.data.active_runs, delay: Math.max(2000, r.data.poll_after_ms || 5000), cursor: r.data.cursor }));
      if (fresh && live.cursor) {
        setTreeKey((k) => k + (treeTab === "project" ? 0 : 1));
        if (view.mode === "overview") void loadOverview(true);
        else absorbLive(r.data);
      }
    });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [w, onScreen, live.tick]);
  useEffect(() => {
    if (!onScreen || live.active <= 0) return undefined;
    const t = setTimeout(() => setLive((l) => ({ ...l, tick: l.tick + 1 })), live.delay);
    return () => clearTimeout(t);
  }, [onScreen, live.active, live.delay, live.tick]);

  // The panel's detail of the Focus centre: what it lists, the canvas draws (P1-5).
  useEffect(() => { setCenterDetail(null); }, [focusKey]);

  // ── what the canvas draws ────────────────────────────────────────────────────────────────
  const page = view.mode === "overview" ? view.page : 0;
  const picture = useMemo(() => {
    if (view.mode === "overview") {
      const o = overview.data;
      if (!o) return null;
      const noun = o.level === "package" ? "packages" : "areas";
      const pick = pickOverview(o.areas, o.layers, o.edges, page, (folded, total) => ({
        id: MORE_ID, key: MORE_ID, kind: "more", layer: "code", trust_tier: "observed",
        label: `${folded.length.toLocaleString("en-US")} more ${noun}`,
        sub: `of ${total.toLocaleString("en-US")} · double-click for the next`,
        counts: { [noun]: folded.length },
      } as KgViewNode));
      const pinned = o.layers.map((l) => l.id);
      const pos = overviewLayout(pick.nodes.map((n) => ({ id: n.id, key: n.key, w: boxWidth(n) })), pick.edges, pinned);
      const { drawn, labelled } = pickEdges(pick.edges, selected?.id ?? null, { perNode: 2, labelled: 8 });
      return {
        nodes: pick.nodes.map((n) => toCanvas(n, true)), raw: pick.nodes, edges: drawn, allEdges: pick.edges, pos,
        rings: [] as number[], center: null as string | null, labelled: new Set([...labelled].map(edgeKey)),
        folded: pick.folded, pages: pick.pages, page: pick.page, pageOf: pick.pageOf, total: o.areas.length, noun,
        drawnCount: drawn.length, edgeTotal: o.edges.length,
      };
    }
    const h = hood.data;
    if (!h) return null;
    const nodes: Array<KgViewNode & { hop: number }> = h.nodes.slice(0, FOCUS_CEILING + 1);
    const ids = new Set(nodes.map((n) => n.id));
    const edges = h.edges.filter((e) => ids.has(e.src) && ids.has(e.dst));
    // One source of truth (P1-5): the centre's own connections from its detail join the picture.
    if (centerDetail && centerDetail.node.id === h.center.id) {
      for (const g of centerDetail.connections) {
        if (!DRAWN_FROM_DETAIL.has(g.kind)) continue;
        for (const item of g.items) {
          if (nodes.length > FOCUS_CEILING) break;
          if (!ids.has(item.id)) { ids.add(item.id); nodes.push({ ...item, hop: 1 }); }
          const e = g.direction === "out" ? { src: h.center.id, dst: item.id } : { src: item.id, dst: h.center.id };
          if (!edges.some((x) => x.src === e.src && x.dst === e.dst && x.kind === g.kind)) edges.push({ ...e, kind: g.kind, weight: 1, label: null });
        }
      }
    }
    const { pos, rings, angle, ring } = focusLayout(h.center.id, nodes, edges);
    const outer = rings.length - 1;
    const raw: KgViewNode[] = [...nodes];
    const canvasNodes = nodes.map((n) => {
      const c = toCanvas(n, n.kind === "area" || n.kind === "layer");
      if (n.id === h.center.id) return { ...c, labelMode: "below" as const, labelMax: 36 };
      const r = ring.get(n.id) ?? 0;
      // Every neighbour named along its ray: outward on the outer ring, inward on the first when
      // there are more; a middle ring waits for room (hover always names it).
      const mode = r === outer ? "out" as const : r === 0 ? "in" as const : "below" as const;
      return { ...c, labelMode: mode, angle: angle.get(n.id), ring: r, labelMax: 22 };
    });
    const extra: CanvasNode[] = [];
    const extraEdges: KgViewEdge[] = [];
    for (const n of liveAdds.nodes) {
      if (ids.has(n.id)) continue;
      const anchorEdge = liveAdds.edges.find((e) => e.src === n.id || e.dst === n.id);
      const anchor = anchorEdge ? pos.get(anchorEdge.src === n.id ? anchorEdge.dst : anchorEdge.src) : undefined;
      if (!anchor) continue;
      pos.set(n.id, placeNear(anchor, [...pos.values()], n.id));
      ids.add(n.id);
      raw.push(n);
      extra.push(toCanvas(n, false, true));
    }
    for (const e of liveAdds.edges) if (ids.has(e.src) && ids.has(e.dst)) extraEdges.push(e);
    // Weight labels on the strongest dozen; the rest on hover (P1-3).
    const strongest = new Set([...edges].filter((e) => e.weight > 1).sort((a, b) => b.weight - a.weight).slice(0, 12).map(edgeKey));
    const all = [...edges, ...extraEdges];
    return {
      nodes: [...canvasNodes, ...extra], raw, edges: all, allEdges: all, pos, rings, center: h.center.id, labelled: strongest,
      folded: [] as KgViewNode[], pages: 1, page: 0, pageOf: new Map<string, number>(), total: 0, noun: "", drawnCount: all.length, edgeTotal: all.length,
    };
  }, [view.mode, page, overview.data, hood.data, liveAdds, selected?.id, centerDetail]);

  function absorbLive(d: KgLiveResponse) {
    const h = hood.data;
    if (!h) return;
    const byKey = new Map(h.nodes.map((n) => [n.key, n]));
    const byId = new Set(h.nodes.map((n) => n.id));
    const nodes: KgViewNode[] = [];
    const edges: KgViewEdge[] = [];
    for (const l of d.learnings) {
      const anchor = l.about.symbols.map((k) => byKey.get(k)).find(Boolean);
      if (!anchor || byId.has(l.node.id)) continue;
      nodes.push(l.node);
      edges.push({ src: l.node.id, dst: anchor.id, kind: "about", weight: 1, label: null });
    }
    for (const r of d.rules) {
      const ev = r.evidence.find((e) => byId.has(e.symbol_id));
      if (!ev || byId.has(r.node.id)) continue;
      nodes.push(r.node);
      edges.push({ src: r.node.id, dst: ev.symbol_id, kind: "evidenced_by", weight: 1, label: null });
    }
    for (const e of d.edges) {
      if (byId.has(e.src) && byId.has(e.dst)) edges.push({ src: e.src, dst: e.dst, kind: e.kind, weight: 1, label: null });
    }
    if (nodes.length || edges.length) setLiveAdds((cur) => ({ nodes: [...cur.nodes, ...nodes], edges: [...cur.edges, ...edges] }));
  }

  const fitKey = view.mode === "overview"
    ? `o|${overview.data?.level}|${overview.data?.parent}|${overview.data?.areas.length}|${layers}|${repo}|${page}`
    : `f|${hood.data?.center.id}|${hood.data?.nodes.length}|${view.depth}|${layers}|${centerDetail ? 1 : 0}`;

  const legend = useMemo(() => {
    if (!picture) return [];
    const seen = new Map<string, { key: string; token: string; name: string }>();
    for (const n of picture.nodes) {
      const key = n.kind === "more" ? "more" : legendKey(n.kind, n.symbol_kind, n.id);
      if (!seen.has(key)) seen.set(key, { key, token: n.token, name: n.kind === "more" ? "More areas" : kindName(n.kind, n.symbol_kind, n.id) });
    }
    return [...seen.values()];
  }, [picture]);

  // ── the panel's subject ──────────────────────────────────────────────────────────────────
  const rawById = useMemo(() => new Map((picture?.raw ?? []).map((n) => [n.id, n])), [picture]);
  const bubble: BubbleSummary | null = useMemo(() => {
    if (!selected) return null;
    const node = rawById.get(selected.id) ?? selected.node;
    if (!node || !/^(area|pkg|layer|more):/.test(node.id)) return null;
    if (view.mode === "overview" && picture && rawById.has(node.id)) {
      const edges = picture.allEdges;
      const pair = (id: string) => rawById.get(id)!;
      const out = edges.filter((e) => e.src === node.id && rawById.has(e.dst)).map((edge) => ({ edge, other: pair(edge.dst) })).sort((a, b) => b.edge.weight - a.edge.weight);
      const inn = edges.filter((e) => e.dst === node.id && rawById.has(e.src)).map((edge) => ({ edge, other: pair(edge.src) })).sort((a, b) => b.edge.weight - a.edge.weight);
      const members = node.id === MORE_ID ? picture.folded : undefined;
      const summary: BubbleSummary = { node: { ...node, sub: bubbleSub(node) }, out, in: inn, members };
      bubbleCache.current.set(node.id, summary);
      return summary;
    }
    // Drilled in, or picked from the tree: what we last knew of it, or the row itself (P2).
    return bubbleCache.current.get(node.id) ?? { node, out: [], in: [] };
  }, [selected, view.mode, picture, rawById]);

  // The canvas's own edges for the selected node: the panel lists them (P1-5).
  const viewEdges = useMemo(() => {
    if (!selected || !picture || view.mode !== "focus") return null;
    return picture.edges.filter((e) => e.src === selected.id || e.dst === selected.id)
      .map((edge) => ({ edge, out: edge.src === selected.id, other: rawById.get(edge.src === selected.id ? edge.dst : edge.src)! }))
      .filter((x) => x.other).sort((a, b) => b.edge.weight - a.edge.weight);
  }, [selected, picture, view.mode, rawById]);

  // ── the selection, announced (§5.10) ─────────────────────────────────────────────────────
  const selNode = selected ? (rawById.get(selected.id) ?? selected.node) : undefined;
  const announce = selNode
    ? `Selected ${kindName(selNode.kind, selNode.symbol_kind, selNode.id).toLowerCase()} ${selNode.label}${view.mode === "focus" && hood.data && hood.data.center.id === selNode.id
      ? `, ${(hood.data.nodes.length - 1 + hood.data.hidden.nodes).toLocaleString("en-US")} connected` : ""}`
    : "";

  // ── search (§5.4): debounced 150 ms, top 8, Enter picks the best match ──────────────────
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState<{ q: string; res: KgSearchResponse | null; error: string | null; meaning: boolean } | null>(null);
  const [dropOpen, setDropOpen] = useState(false);
  const [active, setActive] = useState(0);
  const searchBox = useRef<HTMLInputElement>(null);
  const [meaning, setMeaning] = useState(false);
  useEffect(() => {
    const q = query.trim();
    if (!q) { setSearch(null); return undefined; }
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const t = setTimeout(async () => {
      const r = await getSearch(w, q, { layers, limit: 20, meaning }, ctl?.signal);
      if (r.ok) setSearch({ q, res: r.data, error: null, meaning });
      else if (r.status !== -1) setSearch({ q, res: null, error: r.error, meaning });
      setActive(0);
    }, 150);
    return () => { clearTimeout(t); ctl?.abort(); };
  }, [query, w, layers, meaning]);
  const hits = useMemo(() => rankHits(search?.q ?? "", search?.res?.hits ?? []).slice(0, 8), [search]);
  const hitPaths = useMemo(() => distinctPaths(hits.map((h) => h.path ?? h.node.sub ?? null)), [hits]);
  const pick = (hit: KgSearchHit) => {
    setDropOpen(false);
    setTreeTab(treeTabFor(hit.node.layer));
    focusOn(hit.node.id, hit.node);
  };
  // ⌘K / Ctrl-K focuses the Graph's search while the Graph is on screen (§5.2). Registered on the
  // document's capture phase, ahead of the shell's palette, which sees it was answered.
  useAreaKeydown((e: KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      searchBox.current?.focus();
      searchBox.current?.select();
    }
  }, { target: "document", capture: true });

  // ── intents ──────────────────────────────────────────────────────────────────────────────
  const onCanvasActivate = (id: string) => {
    const n = rawById.get(id);
    if (view.mode === "overview" && n) {
      if (id === MORE_ID) { setSelected(null); go({ mode: "overview", area, page: (picture?.page ?? 0) + 1 >= (picture?.pages ?? 1) ? 0 : (picture?.page ?? 0) + 1 }); return; }
      if (n.kind === "area") { select(id, n); go({ mode: "overview", area: id, page: 0 }); return; }
      if (n.kind === "layer") { setTreeTab(id === "layer:rules" ? "rules" : "learning"); setTreeOpen(true); select(id, n); return; }
    }
    focusOn(id, n);
  };
  const onTreePick = ({ nodeId, row }: TreePick) => {
    if (nodeId) { focusOn(nodeId); return; }
    // An area, package or group fills the panel (P1-8): what the overview knows of it, or the row.
    const known = rawById.get(row.key);
    const node: KgViewNode = known ?? {
      id: row.key, key: row.key, kind: row.level === "group" ? "group" : row.level, layer: row.layer, label: row.label,
      trust_tier: "observed", counts: row.level === "area" ? { packages: row.count } : row.level === "package" ? { classes: row.count } : { items: row.count },
    };
    setSelected({ id: row.key, node });
    setPanelOpen(true);
    showOnItsPage(row.key);
  };
  /** An area folded into "N more areas" is drawn on its own page: go there (it stays selected). */
  const showOnItsPage = (id: string) => {
    if (view.mode !== "overview" || area || !picture) return;
    const p = picture.pageOf.get(id);
    if (p !== undefined && p !== picture.page) go({ mode: "overview", area: null, page: p });
  };
  const toggleChip = (k: Layer | "all") => {
    if (k === "all") { setChips(new Set()); return; }
    setChips((s) => {
      const n = new Set(s);
      if (n.has(k)) n.delete(k); else n.add(k);
      return n.size === 3 ? new Set() : n;
    });
  };
  const rebuild = async () => {
    if (!indexGate.allowed || archived) return;
    setRebuilding(true); setNotice(null);
    const r = await indexNow(w, repo ?? "");
    setRebuilding(false);
    setNotice(r.ok ? { ok: true, text: r.data.message || "Indexing started. The graph rebuilds when it finishes." } : { ok: false, text: r.error });
    onReposChanged();
  };

  const indexingNow = (repos ?? []).some((r) => r.status === "queued" || r.status === "running");
  const o = overview.data;
  const focusCenter = hood.data?.center;
  const onlyLayer: "rules" | "learning" | null = chips.size === 1 && !chips.has("code") ? [...chips][0] as "rules" | "learning" : null;
  const emptyLayer: "rules" | "learning" | null = (() => {
    if (!onlyLayer) return null;
    if (view.mode === "focus") return hood.data && hood.data.nodes.length <= 1 ? onlyLayer : null;
    if (!o) return null;
    const b = o.layers.find((x) => x.id === `layer:${onlyLayer}`);
    const total = Object.values(b?.counts ?? {}).reduce((s, v) => s + v, 0);
    return total === 0 ? onlyLayer : null;
  })();

  // ── the canvas column's body ─────────────────────────────────────────────────────────────
  let stage: React.ReactNode;
  const err = view.mode === "overview" ? overview.error : hood.error;
  const busy = view.mode === "overview" ? overview.loading && !o : hood.loading && !hood.data;
  if (err && !(view.mode === "overview" ? o : hood.data)) {
    stage = indexingNow && repos ? <KgIndexing repos={repos} /> : err.unavailable ? (
      <KgReadError error={err} onRetry={() => setNonce((n) => n + 1)} testId="kg-error" what="The graph view" />
    ) : (
      <div className="kg-stage-wrap">
        <KgError text={err.status === 404 && view.mode === "focus" ? "That node is no longer in the graph. It may have been re-indexed." : err.text}
                 onRetry={() => setNonce((n) => n + 1)} />
        <StageCard testId="kg-error-stage" eyebrow="Graph" title="The graph could not be read"
                   body="The tree and the detail panel still work. Retry, or open a node from the tree." />
      </div>
    );
  } else if (busy) {
    stage = <CanvasLoading nodes={(repos ?? []).reduce((n, r) => n + (r.graph?.nodes ?? 0), 0) || null} />;
  } else if (view.mode === "overview" && o && !o.areas.length && !o.layers.some((l) => Object.values(l.counts ?? {}).some(Boolean))) {
    stage = <KgIndexing repos={repos ?? []} onIndex={() => void rebuild()} busy={rebuilding} />;
  } else if (emptyLayer) {
    // Only Rules (or Learning) on and nothing of it here: the designed prompt, with Scan for the
    // node in Focus when there is one (P1-6), never "try more hops".
    const here = view.mode === "focus" && focusCenter && focusCenter.layer === "code" ? focusCenter : null;
    stage = (
      <KgEmptyLayer layer={emptyLayer} scope={here ? "here" : "repo"}
                    onScan={here ? undefined : () => { setChips(new Set()); setNotice({ ok: true, text: "Select an area, then press Scan for rules here in the detail panel." }); }}>
        {here && emptyLayer === "rules" ? <ScanForRules w={w} node={here} archived={archived} compact /> : null}
      </KgEmptyLayer>
    );
  } else if (picture) {
    stage = listView ? (
      <GraphTable nodes={picture.nodes} edges={picture.allEdges as CanvasEdge[]} selectedId={selected?.id ?? null}
                  onSelect={(id) => select(id, rawById.get(id))} onActivate={onCanvasActivate} />
    ) : (
      <>
        <GraphCanvas ref={canvasRef} nodes={picture.nodes} edges={picture.edges as CanvasEdge[]} positions={picture.pos}
                     rings={picture.rings} centerId={picture.center} mode={view.mode} selectedId={selected?.id ?? null}
                     onSelect={(id) => select(id, rawById.get(id))} onActivate={onCanvasActivate} onBack={back}
                     onZoom={setZoom} fitKey={fitKey} labelled={picture.labelled} occludeRight={occlude}
                     ariaLabel={view.mode === "overview"
                       ? `Whole repository: ${picture.raw.length} bubbles and the ${picture.drawnCount} strongest of ${picture.edgeTotal} calls. Tab moves between nodes, Enter focuses one.`
                       : `Focus on ${focusCenter?.label ?? "a node"}: ${picture.nodes.length - 1} connected within ${view.depth} ${view.depth === 1 ? "hop" : "hops"}. Tab moves between nodes, Enter focuses one, Escape goes back.`} />
        <GraphLegend items={legend} />
        {(view.mode === "overview" ? overview.loading : hood.loading) ? <span className="kg-refreshing" role="status">Updating…</span> : null}
      </>
    );
  }

  const panel = (
    <NodePanel projectId={projectId} actor={actor} orgId={orgId} nodeId={selected?.id ?? null}
               preview={selNode ?? null} bubble={bubble} archived={archived} rulesLocked={rulesLocked}
               onFocusNode={(id, n) => {
                 // A hidden area in "N more areas": go to its page; anything else is focused.
                 if (n?.kind === "area" && view.mode === "overview") { select(id, n); showOnItsPage(id); return; }
                 focusOn(id, n);
               }}
               onShowConnected={selected && !(view.mode === "focus" && hood.data?.center.id === selected.id) && !/^(layer|more):/.test(selected.id)
                 && selNode?.kind !== "group" ? () => focusOn(selected.id, selNode) : null}
               onDrill={(id) => go({ mode: "overview", area: id, page: 0 })}
               onEditRule={onEditRule}
               onClose={() => setPanelOpen(false)}
               viewEdges={viewEdges}
               onDetail={(d) => setCenterDetail(d)}
               onChanged={() => { setNonce((n) => n + 1); setTreeKey((k) => k + 1); }} />
  );

  const folded = picture?.folded.length ?? 0;
  return (
    <div className="kg-graph" data-testid="kg-graph" data-mode={view.mode}>
      <div className="kg-toolbar" role="toolbar" aria-label="Graph controls">
        <button type="button" className="ag-btn kg-tree-toggle" aria-expanded={treeOpen} onClick={() => setTreeOpen((v) => !v)}
                data-testid="kg-tree-toggle" title="Show the project tree">
          <TreeStructure size={14} aria-hidden /> Tree
        </button>
        <div className="kg-search" data-testid="kg-search">
          <label className="inf-find kg-find">
            <MagnifyingGlass size={14} aria-hidden />
            <input ref={searchBox} type="text" value={query} placeholder="Search symbols, rules, learnings…"
                   aria-label="Search the graph" aria-expanded={dropOpen && !!query.trim()} aria-controls="kg-search-results"
                   aria-activedescendant={dropOpen && hits[active] ? `kg-hit-${active}` : undefined} role="combobox" aria-autocomplete="list"
                   data-testid="kg-search-input"
                   onChange={(e) => { setQuery(e.target.value); setDropOpen(true); }}
                   onFocus={() => setDropOpen(true)}
                   onBlur={() => setTimeout(() => setDropOpen(false), 150)}
                   onKeyDown={(e) => {
                     if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(hits.length - 1, a + 1)); }
                     else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
                     else if (e.key === "Enter") { e.preventDefault(); const h = hits[active] ?? hits[0]; if (h) pick(h); }
                     else if (e.key === "Escape") { if (query) { setQuery(""); } setDropOpen(false); }
                   }} />
            <kbd className="kg-kbd" aria-hidden>⌘K</kbd>
            {query ? <button type="button" className="ag-icon-btn" aria-label="Clear search" onClick={() => setQuery("")}><X size={12} aria-hidden /></button> : null}
          </label>
          {dropOpen && query.trim() && search?.q === query.trim() ? (
            <div className="kg-drop" id="kg-search-results" role="listbox" aria-label="Matches" data-testid="kg-search-results">
              {search.error ? <p className="kg-inline-err" role="alert">{search.error}</p> : null}
              {search.res && !hits.length ? <p className="kg-drop-none" data-testid="kg-search-none">No symbol, rule or learning matches “{search.q}”</p> : null}
              {hits.map((h, i) => (
                <button key={h.node.id} id={`kg-hit-${i}`} type="button" role="option" aria-selected={i === active} className="kg-hit"
                        data-testid="kg-search-hit" onMouseDown={(e) => e.preventDefault()} onClick={() => pick(h)} onMouseEnter={() => setActive(i)}
                        title={h.path ?? h.node.sub ?? h.node.label}>
                  <span className="kg-dot" style={{ background: `var(${kindToken(h.node.kind, h.node.symbol_kind, h.node.id)})` }} aria-hidden />
                  <span className="kg-hit-text">
                    <span className="kg-hit-line">
                      <span className="kg-hit-label">{h.node.label}</span>
                      <span className="kg-hit-kind">{kindName(h.node.kind, h.node.symbol_kind, h.node.id)}</span>
                    </span>
                    {hitPaths[i] ? <span className="kg-hit-path inf-mono" data-testid="kg-hit-path">{hitPaths[i]}</span> : null}
                  </span>
                  {h.match === "meaning" ? <span className="kg-chip">by meaning</span> : null}
                </button>
              ))}
              {search.res?.meaning_available && !search.meaning ? (
                <button type="button" className="inf-link kg-drop-meaning" data-testid="kg-search-meaning" onMouseDown={(e) => e.preventDefault()} onClick={() => setMeaning(true)}>Search by meaning</button>
              ) : null}
              {search.meaning ? <p className="kg-quiet kg-drop-meaning">Searched by meaning</p> : null}
            </div>
          ) : null}
        </div>
        <div className="inf-seg" role="radiogroup" aria-label="View">
          <button type="button" role="radio" className="inf-seg-btn" aria-checked={view.mode === "overview"} data-testid="kg-mode-overview"
                  onClick={() => { if (view.mode !== "overview") go({ mode: "overview", area: null, page: 0 }); }}>Whole repo</button>
          <button type="button" role="radio" className="inf-seg-btn" aria-checked={view.mode === "focus"} data-testid="kg-mode-focus"
                  aria-disabled={!selected || /^(layer|more):/.test(selected.id) || undefined}
                  title={!selected ? "Select a node first" : "This node and only what connects to it"}
                  onClick={() => { if (selected && view.mode !== "focus" && !/^(layer|more):/.test(selected.id)) focusOn(selected.id, selNode); }}>Focus</button>
        </div>
        {view.mode === "focus" ? (
          <div className="inf-seg" role="radiogroup" aria-label="Hops" data-testid="kg-hops" title="Hops from the centre">
            {([1, 2, 3] as const).map((d) => (
              <button key={d} type="button" role="radio" className="inf-seg-btn" aria-checked={view.depth === d} aria-label={`${d} ${d === 1 ? "hop" : "hops"}`}
                      data-testid={`kg-hops-${d}`} onClick={() => setView({ ...view, depth: d, limit: FOCUS_FIRST })}>{d === 1 ? "1 hop" : d}</button>
            ))}
          </div>
        ) : null}
        <div className="kg-chips" role="group" aria-label="Layers">
          <button type="button" className="kg-layer-chip" aria-pressed={chips.size === 0} data-testid="kg-chip-all" onClick={() => toggleChip("all")}>All</button>
          {LAYER_CHIPS.map((c) => {
            const locked = c.key !== "code" && rulesLocked;
            return (
              <button key={c.key} type="button" className="kg-layer-chip" aria-pressed={chips.has(c.key)} data-layer={c.key}
                      data-testid={`kg-chip-${c.key}`} aria-disabled={locked ? true : undefined} title={locked ? rulesLocked : `Show ${c.label}`}
                      onClick={() => { if (!locked) toggleChip(c.key); }}>
                <span className="kg-dot" data-layer={c.key} aria-hidden />{c.label}{locked ? <span className="ag-sr"> (locked: {rulesLocked})</span> : null}
              </button>
            );
          })}
        </div>
        <span className="ag-spacer" />
        {o && o.repos.length > 1 ? (
          <label className="kg-select">
            <span className="ag-sr">Repository</span>
            <select value={repo ?? o.repo ?? ""} onChange={(e) => { setRepo(e.target.value || null); go({ mode: "overview", area: null, page: 0 }); }} data-testid="kg-repo">
              {o.repos.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
        ) : null}
        <div className="kg-zoom" role="group" aria-label="Zoom">
          <button type="button" className="ag-btn ag-btn-icon" aria-label="Zoom out" data-testid="kg-zoom-out" onClick={() => canvasRef.current?.zoomOut()} disabled={listView}>−</button>
          <span className="kg-zoom-pct" data-testid="kg-zoom-pct" aria-live="off">{Math.round(zoom * 100)}%</span>
          <button type="button" className="ag-btn ag-btn-icon" aria-label="Zoom in" data-testid="kg-zoom-in" onClick={() => canvasRef.current?.zoomIn()} disabled={listView}>+</button>
          <button type="button" className="ag-btn" data-testid="kg-fit" onClick={() => canvasRef.current?.fit()} disabled={listView}>Fit</button>
        </div>
        <button type="button" className="ag-btn ag-btn-icon" aria-pressed={listView} data-testid="kg-list-toggle" onClick={() => setListView((v) => !v)}
                aria-label={listView ? "Graph view" : "List view"} title={listView ? "Back to the graph" : "List view: the current view as a table of nodes and edges"}>
          <ListBullets size={15} aria-hidden />
        </button>
      </div>

      <div className="kg-subbar">
        {view.mode === "overview" && o?.level === "package" ? (
          <nav className="kg-crumbs" aria-label="Breadcrumb" data-testid="kg-crumbs">
            <button type="button" className="inf-link" onClick={() => go({ mode: "overview", area: null, page: 0 })}>Repo</button>
            <span aria-hidden> › </span>
            <span aria-current="page">{o.parent?.split(":").pop() ?? area?.split(":").pop()}</span>
          </nav>
        ) : null}
        {view.mode === "focus" && focusCenter ? (
          <span className="kg-view-note" data-testid="kg-view-note">
            {focusCenter.label} and {(picture ? picture.nodes.length - 1 : hood.data!.nodes.length - 1).toLocaleString("en-US")} connected within {view.depth} {view.depth === 1 ? "hop" : "hops"}
            {history.current.length ? <> · <button type="button" className="inf-link" onClick={back}>Back</button></> : null}
          </span>
        ) : null}
        {view.mode === "overview" && o && picture ? (
          <span className="kg-view-note" data-testid="kg-overview-note">
            {folded
              ? `The ${picture.raw.filter((n) => n.kind !== "layer" && n.kind !== "more").length} ${picture.page ? "next" : "largest"} of ${picture.total.toLocaleString("en-US")} ${picture.noun}; the rest are in “${folded} more”. `
              : `${picture.total.toLocaleString("en-US")} ${picture.noun}. `}
            {picture.drawnCount < picture.edgeTotal ? `The strongest ${picture.drawnCount} of ${picture.edgeTotal.toLocaleString("en-US")} connections; select a bubble for all of its own. ` : ""}
            Double-click {o.level === "package" ? "a package to focus it" : "an area to open its packages"}.
          </span>
        ) : null}
        {view.mode === "overview" && picture && picture.page > 0 ? (
          <button type="button" className="inf-link" data-testid="kg-first-page" onClick={() => go({ mode: "overview", area, page: 0 })}>Back to the largest</button>
        ) : null}
        {view.mode === "focus" && hood.data?.truncated ? (
          <KgTruncation shown={hood.data.nodes.length - 1} hidden={hood.data.hidden.nodes} level={hood.data.level}
                        max={FOCUS_MORE} maxed={view.limit >= FOCUS_MORE} onMore={() => setView({ ...view, limit: FOCUS_MORE })} />
        ) : null}
        {view.mode === "focus" && hood.data && hood.data.nodes.length <= 1 && !emptyLayer ? (
          <span className="kg-view-note" data-testid="kg-focus-alone">Nothing connects to it in {chips.size ? "these layers" : "the graph"}{view.depth < 3 ? "; try more hops" : ""}.</span>
        ) : null}
        {live.active > 0 ? <span className="kg-live" data-testid="kg-live"><span className="kg-live-dot" aria-hidden />{live.active} {live.active === 1 ? "run" : "runs"} writing</span> : null}
        {notice ? <span className={notice.ok ? "kg-ok" : "kg-inline-err"} role={notice.ok ? "status" : "alert"} data-testid="kg-notice">{notice.text}</span> : null}
        <span className="ag-spacer" />
        {o ? <KgBuilt builtAt={o.built_at} stale={o.stale} onRebuild={() => void rebuild()} busy={rebuilding || archived} indexing={indexingNow} /> : null}
      </div>

      <div ref={workRef} className="kg-work" data-tree-open={treeOpen || undefined} data-panel-open={(panelOpen && !!selected) || undefined}
           style={workHeight ? { height: workHeight } : undefined}>
        <div className="kg-col-tree" data-testid="kg-col-tree">
          <ProjectTree projectId={projectId} actor={actor} tab={treeTab} onTab={setTreeTab} query={query}
                       selectedId={selected?.id ?? null} onPick={(p) => { onTreePick(p); setTreeOpen(false); }}
                       refreshKey={treeKey} reveal={reveal} />
        </div>
        <div className="kg-col-canvas" data-testid="kg-col-canvas">{stage}</div>
        <div className="kg-col-panel" data-testid="kg-col-panel">{panel}</div>
      </div>
      <span className="ag-sr" aria-live="polite" data-testid="kg-announce">{announce}</span>
    </div>
  );
}

