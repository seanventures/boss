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
import { CaretDown, CornersIn, CornersOut, ListBullets, MagnifyingGlass, TreeStructure, X } from "@phosphor-icons/react";

import { useGate } from "../../components/gated";
import { useOnScreen } from "../../lib/area-shown";
import { useAreaKeydown } from "../../lib/area-keys";
import {
  CLUSTER_ARC, CLUSTER_R, FOCUS_CEILING, LABEL_ARC, FOCUS_CLUSTERS, FOCUS_RING_CAP, MORE_ID, OVERVIEW_AREAS, RING_ASPECT_MAX, RING_ASPECT_MIN, fitTransform, focusEdges, focusKeepLabels, LEARNING_EDGES, learningHow, mergeLearningEdges, focusLayout, isBubble, kindName, kindToken, legendKey, overviewLayout,
  charEllipsis, groupFocus, pickEdges, pickOverview, placeMembers, placeNear, slotMeasure, type Pt,
} from "../../lib/kg-layout";
import {
  INDEX_NOW, distinctPaths, focusCounts, focusNote, getLive, getNeighbourhood, getOverview, getSearch, indexNow, layersParam, rankHits,
  type KgRepository,
} from "../../lib/kg-view";
import type {
  KgLiveResponse, KgNeighbourhoodResponse, KgNodeDetailResponse, KgOverviewResponse, KgSearchHit, KgSearchResponse,
  KgTreeTab, KgViewEdge, KgViewNode,
} from "../../lib/kg-view-types";
import { productLayerOf } from "../../lib/kg-view-types";
import { GraphCanvas, GraphLegend, GraphTable, READABLE, edgeKey, frameExtent, framePadding, visibleWidth, type CanvasEdge, type CanvasHandle, type CanvasNode } from "./graph-canvas";
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
/** sessionStorage: whether the details drawer was left open. */
export const PANEL_KEY = "boss.kg.panel";

const LAYER_CHIPS: Array<{ key: Layer; label: string }> = [
  { key: "code", label: "Code" }, { key: "rules", label: "Rules" }, { key: "learning", label: "Learning" },
];

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
  /** Another tab of the area shows: the Graph stays mounted, its keys pause. */
  inactive?: boolean;
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
    // An area or package always says what it holds: classes, or (Python, JS) its functions.
    id: n.id, label: n.label, sub: bubble || n.kind === "area" || n.kind === "package" ? bubbleSub(n) : n.sub, kind: n.kind, symbol_kind: n.symbol_kind, layer: n.layer,
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

/** A Focus opens at this scale or more: rings fold neighbours into groups until it does. */
export const FOCUS_READABLE = 0.8;
/** The room a first-ring label takes along its ring (two 13 px lines beside a node at the sides,
 *  about 84 px across at the top, where labels also take turns on a second row), and a cluster's. */
export const RING_SLOT = { row: 36, w: 84 };
export const CLUSTER_SLOT = { row: 56, w: 132 };

/** The scale a picture is framed at (fitTransform with the canvas's label room). */
function fittedScale(pos: Map<string, Pt>, nodes: CanvasNode[], mode: "overview" | "focus", w: number, h: number): number {
  return fitTransform([...pos.values()], { w, h }, { extent: frameExtent(nodes, mode, 1, w), padding: framePadding(mode) }).k;
}

/**
 * THE FOCUS PICTURE (tester round 5 P1: 2 hops / Show 200 a hairball, rethought, not tuned). The
 * first ring is the centre's own neighbours, each a node; every further hop is drawn as PACKAGE
 * CLUSTERS ("loanaccount/domain · 37 classes"), each beside its one way in; a cluster's classes
 * are drawn only when it is opened. Nodes on a ring are spaced by the room their LABELS need
 * (RING_SLOT, CLUSTER_SLOT) and a ring holds only as many as it has label slots at the scale the
 * picture opens at — the rest fold into "+N in <package>" groups — so every drawn node is named.
 */
export function layFocus(center: string, read: Array<KgViewNode & { hop: number }>, inView: KgViewEdge[], view: { w: number; h: number }) {
  const ringAspect = view.w / Math.max(1, view.h);
  // Between two rings: a first-ring label beside its node (about 150 px at the sides, a row at
  // the top and bottom) and the cluster bubble beyond it — labels never lie on a ring.
  const gap = Math.max(92, 190 / Math.min(RING_ASPECT_MAX, Math.max(RING_ASPECT_MIN, ringAspect)));
  // With an outer ring (2 hops and more) the first ring opens wider (tester round 6: at 2 hops it
  // was about 280 px across, its labels cut or missing): it takes the room between the centre and
  // the clusters that was empty, so its names fit where they sit.
  // A learning at the centre (tester, KG video): its few neighbours are joined by the loop's
  // words ("produced", "reused"), which need a line long enough to sit on beside two circles.
  const learningCentre = read.some((n) => n.id === center && productLayerOf(n.kind) === "learning");
  const first = read.some((n) => n.hop >= 2) ? 120 : learningCentre ? 150 : 90;
  const one = (cap1: number, clusters: number) => {
    const grouped = groupFocus(center, read, inView, { 1: cap1 }, { clusterFrom: 2, clusters });
    const nodes = grouped.nodes as Array<KgViewNode & { hop: number; cluster?: boolean }>;
    const laid = focusLayout(center, nodes, grouped.edges, { aspect: ringAspect, gap, first, slot: (hop) => (hop <= 1 ? RING_SLOT : CLUSTER_SLOT) });
    const canvasNodes: CanvasNode[] = nodes.map((n) => {
      const count = (grouped.members.get(n.id) ?? []).length;
      const c: CanvasNode = n.cluster
        ? { ...toCanvas(n, false), bubble: true, radius: CLUSTER_R, token: "--kg-more", labelMax: 34, short: `${count} more` }
        : n.kind === "group"
          ? { ...toCanvas(n, false), token: "--kg-more", labelMax: n.label.length, short: `+${count} more` }
          // Same-size nodes on a ring (its arc is sized for them): bubbles are Whole repo's.
          : toCanvas(n, false);
      if (n.id === center) return { ...c, labelMode: "below" as const, labelMax: 36 };
      // Named on the side facing away from the centre (or on two lines, or shortened), never on
      // another label and never inward (rounds 3-4).
      return { ...c, labelMode: "out" as const, angle: laid.angle.get(n.id), ring: laid.ring.get(n.id) ?? 0, labelMax: c.labelMax ?? 22 };
    });
    return { grouped, nodes, laid, canvasNodes, k: fittedScale(laid.pos, canvasNodes, "focus", view.w, view.h) };
  };
  type Res = ReturnType<typeof one>;
  const onRing = (r: Res, i: number) => r.canvasNodes.filter((n) => n.ring === i && n.id !== center).length;
  let cap1 = FOCUS_RING_CAP[1];
  let clusters = FOCUS_CLUSTERS;
  let res = one(cap1, clusters);
  for (let round = 0; round < 8; round += 1) {
    const over0 = onRing(res, 0) - ringSlots(res.laid, 0, res.k);
    const over1 = res.laid.rings.length > 1 ? onRing(res, 1) - ringSlots(res.laid, 1, res.k) : 0;
    if (over0 <= 0 && over1 <= 0) break;
    // The first ring is what the centre IS connected to: it gives way last. A crowded first ring
    // first takes fewer clusters (a smaller outer ring opens the picture larger), then fewer
    // names of its own, never below eight.
    const shrinkOuter = over1 > 0 || (over0 > 0 && clusters > 5);
    const nextClusters = shrinkOuter ? Math.max(4, clusters - Math.max(1, over1)) : clusters;
    const nextCap = over0 > 0 && !(shrinkOuter && nextClusters !== clusters) ? Math.max(8, cap1 - over0) : cap1;
    if (nextCap === cap1 && nextClusters === clusters) break;
    cap1 = nextCap; clusters = nextClusters;
    res = one(cap1, clusters);
  }
  return res;
}

/** How many labels ring `i` has room for at scale `k` (labels shrink only to 80%: MIN_TEXT). */
export function ringSlots(laid: { rings: number[]; aspect: number }, i: number, k: number): number {
  const r = laid.rings[i];
  if (r === undefined) return 0;
  const k0 = Math.min(1, k);
  const t = Math.max(0.8, k0);
  const sl = i === 0 ? RING_SLOT : CLUSTER_SLOT;
  return Math.floor(slotMeasure(r * laid.aspect * k0, r * k0, { row: sl.row * t, w: sl.w * t }) + 0.5);
}

function treeTabFor(layer: string): KgTreeTab {
  return layer === "rules" ? "rules" : layer === "learning" ? "learning" : "project";
}

/** The work area fits the viewport: from its top to the window's bottom (P1-11), never shorter than
 *  a usable canvas — the height is the window's less the chrome above and below it, measured, never
 *  a fixed number. Full screen (round 6), it is the window less the toolbar and the status line. */
function useFitHeight(full = false): [React.RefObject<HTMLDivElement | null>, number | null] {
  const ref = useRef<HTMLDivElement>(null);
  const [h, setH] = useState<number | null>(null);
  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const measure = () => {
      const el = ref.current;
      if (!el || !el.offsetParent) return;
      // Everything above the work area and everything the page draws below it (its card's padding,
      // the page's own) fits in the window with it: the document is never taller than the viewport.
      const r = el.getBoundingClientRect();
      if (full) { setH(Math.max(260, Math.round(window.innerHeight - r.top))); return; }
      const top = r.top + window.scrollY;
      // What the page draws under the work area: the rest of its card, then the page's own padding.
      const card = el.closest("section") ?? el.parentElement;
      const cardBottom = card ? card.getBoundingClientRect().bottom - r.bottom : 0;
      const main = el.closest("main");
      const pad = main ? parseFloat(getComputedStyle(main).paddingBottom) || 0 : 0;
      const below = Math.max(0, cardBottom) + pad;
      // Never taller than the room left (round 5: a 420 px floor made the document taller than an
      // 800 px window, and the page scrolled under the pointer).
      setH(Math.max(260, Math.round(window.innerHeight - top - below - 2)));
    };
    measure();
    const t = window.setTimeout(measure, 200);
    window.addEventListener("resize", measure);
    return () => { window.clearTimeout(t); window.removeEventListener("resize", measure); };
  });
  return [ref, h];
}

export function GraphView({ projectId, actor, orgId, repos, onReposChanged, rulesLocked, archived, focusRequest, onEditRule, inactive = false }: GraphViewProps) {
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
  // Wide: the tree is a column that can be put away (tester round 2: not collapsible at 1280).
  const [treeCollapsed, setTreeCollapsed] = useState(false);
  // The details panel is a drawer, closed until something is selected (round 2): the graph has the
  // whole width. Its open/closed state is remembered for the session.
  const [panelOpen, setPanelOpenState] = useState(false);
  const setPanelOpen = useCallback((open: boolean) => {
    setPanelOpenState(open);
    try { window.sessionStorage.setItem(PANEL_KEY, open ? "open" : "closed"); } catch { /* private mode */ }
  }, []);
  const [rebuilding, setRebuilding] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const [nonce, setNonce] = useState(0);
  const [treeKey, setTreeKey] = useState(0);
  const bubbleCache = useRef(new Map<string, BubbleSummary>());
  const canvasRef = useRef<CanvasHandle>(null);
  // Full screen (round 6, the demo's centrepiece): the graph over the whole window; Esc exits.
  const [full, setFull] = useState(false);
  const [workRef, workHeight] = useFitHeight(full);
  useEffect(() => {
    if (!full) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // Esc in the search box clears it first; anywhere else it leaves full screen.
      if ((e.target as HTMLElement | null)?.closest?.("input, textarea, select")) return;
      e.preventDefault();
      e.stopPropagation();
      setFull(false);
    };
    document.addEventListener("keydown", onKey, true);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", onKey, true); document.body.style.overflow = prev; };
  }, [full]);
  // Another tab of the area: never left covering the window.
  useEffect(() => { if (inactive) setFull(false); }, [inactive]);
  // The drawer over the canvas's right edge: the selection is panned out from under it.
  const [occlude, setOcclude] = useState(0);
  // The canvas's size (to 20 px) and the drawer's width: every picture is laid out to what the
  // canvas holds at a readable scale — how many areas one Whole-repo page draws, how many
  // neighbours a Focus ring names before folding the rest into groups (tester rounds 2-3).
  const [stageSize, setStageSize] = useState<{ w: number; h: number; panel: number }>({ w: 1000, h: 520, panel: 0 });
  useEffect(() => {
    const el = workRef.current?.querySelector<HTMLElement>(".kg-col-panel");
    // The panel is its own column (round 5) except in a narrow graph, where it is a drawer over
    // the canvas: only then does it cover anything. The layouts always leave a drawer's width
    // free, open or not, so opening it never re-lays the picture out.
    const drawer = !!el && typeof getComputedStyle !== "undefined" && getComputedStyle(el).position === "absolute";
    if (el) setOcclude(drawer && panelOpen && selected ? el.offsetWidth : 0);
    const c = workRef.current?.querySelector<HTMLElement>(".kg-canvas-wrap") ?? workRef.current?.querySelector<HTMLElement>(".kg-col-canvas");
    if (c && c.clientWidth > 0 && c.clientHeight > 0) {
      const next = { w: Math.round(c.clientWidth / 20) * 20, h: Math.round(c.clientHeight / 20) * 20, panel: el && drawer ? Math.round(el.offsetWidth / 20) * 20 : 0 };
      if (next.w !== stageSize.w || next.h !== stageSize.h || next.panel !== stageSize.panel) setStageSize(next);
    }
  });
  // The one layout area every picture is laid out to: the canvas minus a drawer's width.
  const layoutW = visibleWidth(stageSize.w, stageSize.panel);
  const aspect = layoutW / Math.max(1, stageSize.h);
  // A resize changes the drawer's width (it is a share of the canvas) and the canvas's: measure
  // again, so the picture is framed beside the drawer as it is now, not as it was (round 3).
  const [, setResized] = useState(0);
  useEffect(() => {
    const el = workRef.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(() => setResized((n) => n + 1));
    ro.observe(el);
    return () => ro.disconnect();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
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
    void getNeighbourhood(w, view.node, { depth: view.depth, layers, limit: Math.min(view.limit * view.depth, FOCUS_MORE) }, ctl?.signal).then((r) => {
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
      // A deep link restores the drawer as this session left it.
      let closed = false;
      try { closed = window.sessionStorage.getItem(PANEL_KEY) === "closed"; } catch { /* private mode */ }
      focusOn(focusRequest.id);
      if (closed) setPanelOpen(false);
    }
  }, [focusRequest, focusOn]);

  // ── live: new learnings fade in where they hang (§4.4) ───────────────────────────────────
  const [live, setLive] = useState<{ active: number; delay: number; cursor: string | null; tick: number }>({ active: 0, delay: 5000, cursor: null, tick: 0 });
  const [liveAdds, setLiveAdds] = useState<{ nodes: KgViewNode[]; edges: KgViewEdge[] }>({ nodes: [], edges: [] });
  useEffect(() => { setLiveAdds({ nodes: [], edges: [] }); }, [focusKey, area]);
  // The outer hops' package clusters opened by a click (round 5); a new Focus starts closed.
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  useEffect(() => { setExpanded(new Set()); }, [focusKey]);
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

  // ── what the canvas draws ────────────────────────────────────────────────────────────────
  const page = view.mode === "overview" ? view.page : 0;
  // The Whole-repo layout: as many areas a page as the canvas shows at a readable scale, laid
  // out once per page — selecting a bubble never re-lays it out (round 3 P2).
  const overviewPicture = useMemo(() => {
    const o = overview.data;
    if (view.mode !== "overview" || !o) return null;
    const noun = o.level === "package" ? "packages" : "areas";
    const pinned = o.layers.map((l) => l.id);
    const lay = (per: number) => {
      const pick = pickOverview(o.areas, o.layers, o.edges, page, (folded, total) => ({
        id: MORE_ID, key: MORE_ID, kind: "more", layer: "code", trust_tier: "observed",
        label: `${folded.length.toLocaleString("en-US")} more ${noun}`,
        sub: `of ${total.toLocaleString("en-US")} · double-click for the next`,
        counts: { [noun]: folded.length },
      } as KgViewNode), per);
      const pos = overviewLayout(pick.nodes.map((n) => ({ id: n.id, key: n.key, w: boxWidth(n) })), pick.edges, pinned, aspect);
      // Every Whole-repo bubble is named, the Rules and Learning bubbles included (round 4).
      const nodes = pick.nodes.map((n) => ({ ...toCanvas(n, true), keepLabel: true }));
      return { pick, pos, nodes, k: fittedScale(pos, nodes, "overview", layoutW, stageSize.h) };
    };
    let best = lay(OVERVIEW_AREAS);
    for (let per = OVERVIEW_AREAS - 1; per >= 4 && best.k < READABLE; per -= 1) {
      const next = lay(per);
      if (next.k > best.k + 0.01) best = next;
    }
    return { ...best, noun };
  }, [view.mode, page, overview.data, aspect, layoutW, stageSize.h]);

  const picture = useMemo(() => {
    if (view.mode === "overview") {
      const o = overview.data;
      if (!o || !overviewPicture) return null;
      const { pick, pos, nodes, noun } = overviewPicture;
      const { drawn, labelled } = pickEdges(pick.edges, selected?.id ?? null, { perNode: 2, labelled: 8 });
      // The banner counts the picture, not the selection's extra edges (round 5: "strongest 9"
      // became "strongest 15" on a click).
      const base = pickEdges(pick.edges, null, { perNode: 2, labelled: 8 }).drawn.length;
      return {
        nodes, raw: pick.nodes, edges: drawn, allEdges: pick.edges, pos,
        rings: [] as number[], ringAspect: 1, center: null as string | null, labelled: new Set([...labelled].map(edgeKey)),
        folded: pick.folded, pages: pick.pages, page: pick.page, pageOf: pick.pageOf, total: o.areas.length, noun,
        groups: new Map<string, KgViewNode[]>(), readCount: 0,
        drawnCount: base, edgeTotal: o.edges.length, layoutCount: nodes.length,
      };
    }
    const h = hood.data;
    if (!h) return null;
    const read: Array<KgViewNode & { hop: number }> = h.nodes.slice(0, FOCUS_CEILING + 1);
    const readIds = new Set(read.map((n) => n.id));
    // Exactly what the server answered — the same count every time (tester round 2) — with each
    // hop's ring capped and the rest folded into "+N in <package>" groups, so 2 hops or 200
    // neighbours still read: every node labelled, none on another.
    // The learning loop one line per pair, its most telling word on it (mergeLearningEdges).
    const inView = mergeLearningEdges(h.edges.filter((e) => readIds.has(e.src) && readIds.has(e.dst)));
    // Each ring names as many neighbours as the canvas shows at a readable scale (beside the
    // drawer, which a Focus opens); the rest fold into "+N in <package>" groups (round 3 P0).
    const visW = layoutW;
    const center = h.center.id;
    const best = layFocus(center, read, inView, { w: visW, h: stageSize.h });
    const { grouped, nodes } = best;
    const pos = new Map(best.laid.pos);
    const { rings } = best.laid;
    const edges = grouped.edges;
    // What is drawn (tester round 4): every edge to the centre, each outer node's (cluster's)
    // strongest way in, and the selection's own edges.
    const hops = new Map(nodes.map((n) => [n.id, n.id === center ? 0 : n.hop]));
    // A selected group or cluster adds no edges of its own: they are sums over its members, and
    // all of them at once re-drew the knot (its panel lists them).
    const selEdges = selected && !selected.id.startsWith("group:") ? selected.id : null;
    const drawnEdges = focusEdges(center, hops, edges, selEdges);
    // Labels per ring capped by its arc length at the scale it opens at (round 5): the strongest
    // connected first; the rest named on hover, when selected, or zoomed in. The selection's
    // neighbours are always offered a label.
    const keep = focusKeepLabels(center, new Map(), edges, selected?.id ?? null);
    const wTo = new Map<string, number>();
    for (const e of edges) {
      if (e.src === center) wTo.set(e.dst, (wTo.get(e.dst) ?? 0) + e.weight);
      if (e.dst === center) wTo.set(e.src, (wTo.get(e.src) ?? 0) + e.weight);
    }
    const named = new Set<string>();
    rings.forEach((_r, i) => {
      const onRing = best.canvasNodes.filter((n) => n.ring === i && n.id !== center)
        .sort((a, b) => (wTo.get(b.id) ?? 0) - (wTo.get(a.id) ?? 0) || (a.id < b.id ? -1 : 1));
      // Clusters and groups are always named (few, and their name is what they are); then the
      // strongest connected, as many as the ring has slots for (layFocus sized it to fit them).
      const always = onRing.filter((n) => n.bubble || n.kind === "group");
      always.forEach((n) => named.add(n.id));
      onRing.filter((n) => !(n.bubble || n.kind === "group")).slice(0, Math.max(0, ringSlots(best.laid, i, best.k) - always.length)).forEach((n) => named.add(n.id));
    });
    const canvasNodes: CanvasNode[] = best.canvasNodes.map((n) => ({ ...n, keepLabel: false, labelCull: n.id !== center && !named.has(n.id) && !keep.has(n.id) }));
    const ids = new Set(nodes.map((n) => n.id));
    const raw: KgViewNode[] = [...nodes];
    const extra: CanvasNode[] = [];
    const extraEdges: KgViewEdge[] = [];
    // Opened clusters: their classes on an arc beyond the outer ring, a dotted spoke to each.
    // Nothing already drawn moves, and the frame stays where it is (Fit shows all).
    const outer = rings.length ? rings[rings.length - 1] : 0;
    for (const cid of expanded) {
      const at = pos.get(cid);
      const members = (grouped.members.get(cid) ?? []) as Array<KgViewNode & { hop: number }>;
      if (!at || !members.length) continue;
      const placed = placeMembers(at, outer, best.laid.aspect, members.map((m) => m.id));
      members.forEach((m, j) => {
        const p = placed.get(m.id);
        if (!p || ids.has(m.id)) return;
        pos.set(m.id, p);
        ids.add(m.id);
        raw.push(m);
        extra.push({ ...toCanvas(m, false), labelMode: "out", angle: Math.atan2(p.y, p.x), ring: rings.length, labelMax: 22, labelCull: j >= 12 });
        extraEdges.push({ src: cid, dst: m.id, kind: "member" as KgViewEdge["kind"], weight: 1, label: null });
      });
    }
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
    // Weight labels on the strongest dozen at 1 hop, half a dozen further out; the rest on hover.
    const strongest = new Set([...drawnEdges].filter((e) => e.weight > 1).sort((a, b) => b.weight - a.weight).slice(0, rings.length > 1 ? 6 : 12).map(edgeKey));
    const all = [...edges, ...extraEdges.filter((e) => (e.kind as string) !== "member")];
    return {
      // A live addition of an edge already drawn is the same line (mergeLearningEdges), never a second chip.
      nodes: [...canvasNodes, ...extra], raw, edges: mergeLearningEdges([...drawnEdges, ...extraEdges]), allEdges: mergeLearningEdges(all), pos, rings, ringAspect: best.laid.aspect, center: h.center.id, labelled: strongest,
      groups: grouped.members as Map<string, KgViewNode[]>, readCount: read.length - 1,
      folded: [] as KgViewNode[], pages: 1, page: 0, pageOf: new Map<string, number>(), total: 0, noun: "", drawnCount: all.length, edgeTotal: all.length,
      layoutCount: canvasNodes.length,
    };
  }, [view.mode, overview.data, overviewPicture, hood.data, liveAdds, selected?.id, stageSize, layoutW, expanded]);

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

  // THE ONE FRAMING KEY (tester round 5 P1: Whole repo with a rule selected opened as a knot until
  // Fit): every mode, scope, page, focus, hop, layer, repo or layout-area change frames the
  // picture again, through the canvas's one framing function. A selection, an opened cluster or
  // a live addition is not in it: they never move the frame.
  const fitKey = view.mode === "overview"
    ? `o|${area}|${overview.data?.level}|${overview.data?.parent}|${overview.data?.areas.length}|${layers}|${repo}|${page}|${stageSize.w}x${stageSize.h}|${layoutW}|${picture?.layoutCount}`
    : `f|${view.node}|${hood.data?.center.id}|${hood.data?.nodes.length}|${view.depth}|${view.limit}|${layers}|${stageSize.w}x${stageSize.h}|${layoutW}|${picture?.layoutCount}`;

  const legend = useMemo(() => {
    if (!picture) return [];
    const seen = new Map<string, { key: string; token: string; name: string }>();
    for (const n of picture.nodes) {
      const key = n.kind === "more" ? "more" : legendKey(n.kind, n.symbol_kind, n.id);
      if (!seen.has(key)) seen.set(key, { key, token: n.token, name: n.kind === "more" ? "More areas" : kindName(n.kind, n.symbol_kind, n.id) });
    }
    // The learning edges drawn, each by its word and colour (a line swatch).
    const hows = new Set<string>();
    for (const e of picture.edges as CanvasEdge[]) {
      const how = learningHow(e);
      if (how) hows.add(how);
      for (const a of e.also ?? []) hows.add(a);
    }
    // First: the legend is one row, and the loop's words are what a Focus on a learning shows.
    const lines = LEARNING_EDGES.filter((x) => hows.has(x.how))
      .map((x) => ({ key: `edge:${x.how}`, token: x.token, name: x.word, title: x.legend, line: true }));
    return [...lines, ...seen.values()];
  }, [picture]);

  // ── the panel's subject ──────────────────────────────────────────────────────────────────
  const rawById = useMemo(() => new Map((picture?.raw ?? []).map((n) => [n.id, n])), [picture]);
  const bubble: BubbleSummary | null = useMemo(() => {
    if (!selected) return null;
    const node = rawById.get(selected.id) ?? selected.node;
    if (!node || !/^(area|pkg|layer|more|group):/.test(node.id)) return null;
    if (node.id.startsWith("group:") && picture) {
      // A Focus group: the neighbours it holds, each one press from its own view.
      return { node: { ...node, sub: undefined }, out: [], in: [], members: picture.groups.get(node.id) ?? [] };
    }
    // What the canvas draws for it now, in either mode (round 3 P2: a package in Focus said
    // "Nothing in this view connects" beside three drawn edges).
    if (picture && rawById.has(node.id)) {
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
  // ONE count source for the Focus header, the truncation banner and the panel (round 4).
  const counts = view.mode === "focus" && hood.data
    ? focusCounts(hood.data, picture && view.mode === "focus" ? picture : null, { max: FOCUS_MORE, asked: Math.min(view.limit * view.depth, FOCUS_MORE) })
    : null;
  const viewEdges = useMemo(() => {
    if (!selected || !picture || view.mode !== "focus") return null;
    // One row per neighbour (tester round 2: a node joined by two edges was listed twice).
    const rows = new Map<string, { edge: KgViewEdge; out: boolean; other: KgViewNode }>();
    for (const edge of picture.edges) {
      if (edge.src !== selected.id && edge.dst !== selected.id) continue;
      const out = edge.src === selected.id;
      const other = rawById.get(out ? edge.dst : edge.src);
      if (!other) continue;
      const prev = rows.get(other.id);
      if (!prev || edge.weight > prev.edge.weight) rows.set(other.id, { edge, out, other });
    }
    return [...rows.values()].sort((a, b) => b.edge.weight - a.edge.weight);
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
      searchBox.current?.focus({ preventScroll: true });
      searchBox.current?.select();
    }
  }, { target: "document", capture: true, enabled: !inactive });

  // ── intents ──────────────────────────────────────────────────────────────────────────────
  const onCanvasActivate = (id: string) => {
    const n = rawById.get(id);
    // A group or cluster is not a node of the graph: it has no Focus of its own (its panel lists it).
    if (id.startsWith("group:")) return;
    if (view.mode === "overview" && n) {
      if (id === MORE_ID) { setSelected(null); go({ mode: "overview", area, page: (picture?.page ?? 0) + 1 >= (picture?.pages ?? 1) ? 0 : (picture?.page ?? 0) + 1 }); return; }
      if (n.kind === "area") { select(id, n); go({ mode: "overview", area: id, page: 0 }); return; }
      if (n.kind === "layer") { setTreeTab(id === "layer:rules" ? "rules" : "learning"); setTreeOpen(true); select(id, n); return; }
    }
    focusOn(id, n);
  };
  const onTreePick = ({ nodeId, row }: TreePick) => {
    if (nodeId) { focusOn(nodeId); return; }
    // A package is shown on the canvas: its own Focus, its connected packages (tester round 2).
    if (row.level === "package") { focusOn(row.key, { id: row.key, key: row.key, kind: "package", layer: "code", label: row.label, trust_tier: "observed", counts: { classes: row.count } }); return; }
    // An area, package or group fills the panel (P1-8): what the overview knows of it, or the row.
    const known = rawById.get(row.key);
    const node: KgViewNode = known ?? {
      id: row.key, key: row.key, kind: row.level === "group" ? "group" : row.level, layer: row.layer, label: row.label,
      trust_tier: "observed", counts: row.level === "area" ? { packages: row.count } : { items: row.count },
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
                     rings={picture.rings} ringAspect={picture.ringAspect} centerId={picture.center} mode={view.mode} selectedId={selected?.id ?? null}
                     onSelect={(id) => {
                       // A click on a package cluster opens it (its classes fan out beyond the
                       // outer ring) or closes it again; either way it is selected.
                       if ((rawById.get(id) as { cluster?: boolean } | undefined)?.cluster) {
                         setExpanded((cur) => { const n = new Set(cur); if (n.has(id)) n.delete(id); else n.add(id); return n; });
                       }
                       select(id, rawById.get(id));
                     }} onActivate={onCanvasActivate}
                     onBack={() => { if (panelOpen && selected) setPanelOpen(false); else back(); }}
                     onClear={() => { setSelected(null); setPanelOpen(false); }}
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
    <NodePanel projectId={projectId} actor={actor} orgId={orgId} nodeId={panelOpen ? selected?.id ?? null : null}
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
               viewNote={counts && selected && hood.data?.center.id === selected.id && counts.groups
                 ? `${counts.drawn.toLocaleString("en-US")} drawn, ${counts.grouped.toLocaleString("en-US")} in ${counts.groups} ${counts.groups === 1 ? "group" : "groups"}` : null}
               onChanged={() => { setNonce((n) => n + 1); setTreeKey((k) => k + 1); }} />
  );

  const folded = picture?.folded.length ?? 0;
  return (
    <div className="kg-graph" data-testid="kg-graph" data-mode={view.mode} data-full={full || undefined}
         role={full ? "dialog" : undefined} aria-modal={full || undefined} aria-label={full ? "Knowledge graph, full screen" : undefined}>
      <div className="kg-toolbar" role="toolbar" aria-label="Graph controls">
        <button type="button" className="ag-btn kg-tree-toggle" aria-expanded={treeOpen || !treeCollapsed}
                onClick={() => {
                  // Always a column (round 6): shown by default on a wide graph (Tree collapses it),
                  // put away on a narrower one (Tree opens it, pushing the canvas over).
                  const col = workRef.current?.querySelector<HTMLElement>(".kg-col-tree");
                  if (col && getComputedStyle(col).getPropertyValue("--kg-tree-mode").trim() === "opens") setTreeOpen((v) => !v);
                  else setTreeCollapsed((v) => !v);
                }}
                data-testid="kg-tree-toggle" aria-label="Project tree" title={treeCollapsed ? "Show the project tree" : "Hide the project tree"}>
          <TreeStructure size={14} aria-hidden /><span className="kg-tree-word"> Tree</span>
        </button>
        <div className="kg-search" data-testid="kg-search">
          <label className="inf-find kg-find">
            <MagnifyingGlass size={14} aria-hidden />
            <input ref={searchBox} type="text" value={query} placeholder="Search the graph…" title="Search symbols, rules and learnings (⌘K)"
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
                        title={hitTip(h.node.label, h.path ?? h.node.sub ?? null)}>
                  <span className="kg-dot" style={{ background: `var(${kindToken(h.node.kind, h.node.symbol_kind, h.node.id)})` }} aria-hidden />
                  <span className="kg-hit-text">
                    <span className="kg-hit-line">
                      <FitTitle label={h.node.label} />
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
        <div className="kg-toolbar-end">
        {o && o.repos.length > 1 ? (
          <label className="kg-select">
            <span className="ag-sr">Repository</span>
            <select value={repo ?? o.repo ?? ""} onChange={(e) => { setRepo(e.target.value || null); go({ mode: "overview", area: null, page: 0 }); }} data-testid="kg-repo">
              {o.repos.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
        ) : null}
        {/* One row at 1333 px in Focus (tester round 6: the toolbar wrapped to two rows): zoom
            steps and List view, the rarely used ones, are in the zoom menu; Fit stays a button. */}
        <ViewMenu zoom={zoom} listView={listView}
                  onZoomIn={() => canvasRef.current?.zoomIn()} onZoomOut={() => canvasRef.current?.zoomOut()}
                  onList={() => setListView((v) => !v)} />
        <button type="button" className="ag-btn" data-testid="kg-fit" onClick={() => canvasRef.current?.fit()} disabled={listView}
                title="Fit: the whole picture in view">Fit</button>
        <button type="button" className="ag-btn ag-btn-icon" aria-pressed={full} data-testid="kg-fullscreen" onClick={() => setFull((v) => !v)}
                aria-label={full ? "Exit full screen" : "Full screen"} title={full ? "Exit full screen (Esc)" : "Full screen: the graph over the whole window (Esc exits)"}>
          {full ? <CornersIn size={15} aria-hidden /> : <CornersOut size={15} aria-hidden />}
        </button>
        <button type="button" className="ag-btn" aria-expanded={panelOpen && !!selected} aria-controls="kg-details"
                data-testid="kg-details-toggle" disabled={!selected}
                title={selected ? (panelOpen ? "Hide the details (Esc)" : "Show the details of the selection") : "Select a node to see its details"}
                onClick={() => setPanelOpen(!panelOpen)}>Details</button>
        </div>
      </div>

      <div className="kg-subbar">
        {view.mode === "overview" && o?.level === "package" ? (
          <nav className="kg-crumbs" aria-label="Breadcrumb" data-testid="kg-crumbs">
            <button type="button" className="inf-link" onClick={() => go({ mode: "overview", area: null, page: 0 })}>Repo</button>
            <span aria-hidden> › </span>
            <span aria-current="page">{o.parent?.split(":").pop() ?? area?.split(":").pop()}</span>
          </nav>
        ) : null}
        {view.mode === "focus" && focusCenter ? (() => {
          const note = counts ? focusNote(focusCenter.label, view.depth, counts) : focusCenter.label;
          return (
            <>
              {/* One status line (round 6): the sentence ellipsizes, all of it is the tooltip. */}
              <span className="kg-view-note" data-testid="kg-view-note" title={note}>{note}</span>
              {history.current.length ? <button type="button" className="inf-link kg-back" data-testid="kg-back" onClick={back}>Back</button> : null}
            </>
          );
        })() : null}
        {view.mode === "overview" && o && picture ? (
          (() => {
            const drawnAreas = picture.raw.filter((n) => n.kind !== "layer" && n.kind !== "more").length;
            const text = `${folded
              ? `The ${drawnAreas} ${picture.page ? "next" : "largest"} of ${picture.total.toLocaleString("en-US")} ${picture.noun}; the rest are in “${folded} more”. `
              : `${picture.total.toLocaleString("en-US")} ${picture.noun}. `}${picture.drawnCount < picture.edgeTotal
              ? `The strongest ${picture.drawnCount} of ${picture.edgeTotal.toLocaleString("en-US")} connections; select a bubble for all of its own. ` : ""}Double-click ${o.level === "package" ? "a package to focus it" : "an area to open its packages"}.`;
            return <span className="kg-view-note" data-testid="kg-overview-note" title={text}>{text}</span>;
          })()
        ) : null}
        {view.mode === "overview" && picture && picture.page > 0 ? (
          <button type="button" className="inf-link" data-testid="kg-first-page" onClick={() => go({ mode: "overview", area, page: 0 })}>Back to the largest</button>
        ) : null}
        {view.mode === "focus" && hood.data?.truncated ? (
          <KgTruncation shown={hood.data.nodes.length - 1} hidden={hood.data.hidden.nodes} level={hood.data.level} counts={counts}
                        max={FOCUS_MORE} maxed={view.limit >= FOCUS_MORE}
                        onMore={Math.min(view.limit * view.depth, FOCUS_MORE) < FOCUS_MORE ? () => setView({ ...view, limit: FOCUS_MORE }) : undefined} />
        ) : null}
        {view.mode === "focus" && hood.data && hood.data.nodes.length <= 1 && !emptyLayer ? (
          <span className="kg-view-note" data-testid="kg-focus-alone">{(() => {
            // A class whose methods only call each other (round 3: "0 connected"): say what it holds.
            const c = hood.data.center;
            const methods = c.counts?.methods ?? 0;
            if (c.kind === "symbol" && c.symbol_kind !== "method" && c.symbol_kind !== "function" && methods > 0 && !chips.size) {
              return `Nothing outside ${c.label} connects to it: its ${methods.toLocaleString("en-US")} ${methods === 1 ? "method calls" : "methods call"} only each other. They are listed under Methods in Details.`;
            }
            return `Nothing connects to it in ${chips.size ? "these layers" : "the graph"}${view.depth < 3 ? "; try more hops" : ""}.`;
          })()}</span>
        ) : null}
        {live.active > 0 ? <span className="kg-live" data-testid="kg-live"><span className="kg-live-dot" aria-hidden />{live.active} {live.active === 1 ? "run" : "runs"} writing</span> : null}
        {notice ? <span className={`kg-notice-line ${notice.ok ? "kg-ok" : "kg-inline-err"}`} title={notice.text} role={notice.ok ? "status" : "alert"} data-testid="kg-notice">{notice.text}</span> : null}
        <span className="ag-spacer" />
        {o ? <KgBuilt builtAt={o.built_at} stale={o.stale} onRebuild={() => void rebuild()} busy={rebuilding || archived} indexing={indexingNow} /> : null}
      </div>

      <div ref={workRef} className="kg-work" data-tree-open={treeOpen || undefined} data-tree-collapsed={treeCollapsed || undefined} data-panel-open={(panelOpen && !!selected) || undefined}
           style={{ ...(workHeight ? { height: workHeight } : {}), ["--kg-occlude" as string]: `${occlude}px` }}>
        <div className="kg-col-tree" data-testid="kg-col-tree">
          <ProjectTree projectId={projectId} actor={actor} tab={treeTab} onTab={setTreeTab} query={query}
                       selectedId={selected?.id ?? null} onPick={onTreePick}
                       refreshKey={treeKey} reveal={reveal} />
        </div>
        <div className="kg-col-canvas" data-testid="kg-col-canvas">{stage}</div>
        <div className="kg-col-panel" id="kg-details" data-testid="kg-col-panel" role="region" aria-label="Details"
             aria-hidden={!(panelOpen && selected) || undefined}
             onKeyDown={(e) => {
               // Esc closes the drawer from anywhere inside it and hands the keyboard back to the graph.
               if (e.key !== "Escape" || e.defaultPrevented) return;
               if ((e.target as HTMLElement).closest("input, textarea, select")) return;
               e.preventDefault();
               setPanelOpen(false);
               workRef.current?.querySelector<HTMLElement>(".kg-canvas, [data-testid=kg-tree-row][tabindex='0']")?.focus({ preventScroll: true });
             }}>{panel}</div>
      </div>
      <span className="ag-sr" aria-live="polite" data-testid="kg-announce">{announce}</span>
    </div>
  );
}

/** The zoom menu (tester round 6): the trigger says the zoom; in it, Zoom in, Zoom out and List
 *  view. The wheel, a pinch and the + / − keys zoom the canvas too. Esc and an outside press close it. */
function ViewMenu({ zoom, listView, onZoomIn, onZoomOut, onList }: {
  zoom: number; listView: boolean; onZoomIn: () => void; onZoomOut: () => void; onList: () => void;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey, true); };
  }, [open]);
  const pct = `${Math.round(zoom * 100)}%`;
  return (
    <span className="inf-pop kg-view-menu" ref={box}>
      <button type="button" className="ag-btn kg-view-menu-btn" aria-haspopup="menu" aria-expanded={open} data-testid="kg-view-menu"
              aria-label={listView ? "List view: back to the graph, or zoom" : `Zoom ${pct}: zoom in or out, or List view`}
              title="Zoom in or out, or show the view as a list" onClick={() => setOpen((v) => !v)}>
        <span className="kg-zoom-pct" data-testid="kg-zoom-pct">{listView ? "List" : pct}</span>
        <CaretDown size={11} weight="bold" aria-hidden />
      </button>
      {open ? (
        <span className="inf-menu kg-view-menu-list" role="menu" aria-label="Zoom and view">
          {/* Zoom steps keep the menu open, so a few presses in a row need no reopening. */}
          <button type="button" role="menuitem" data-testid="kg-zoom-in" disabled={listView} onClick={onZoomIn}>
            <span>Zoom in</span><kbd aria-hidden>+</kbd>
          </button>
          <button type="button" role="menuitem" data-testid="kg-zoom-out" disabled={listView} onClick={onZoomOut}>
            <span>Zoom out</span><kbd aria-hidden>−</kbd>
          </button>
          <button type="button" role="menuitem" data-testid="kg-list-toggle" aria-pressed={listView} onClick={() => { onList(); setOpen(false); }}
                  title={listView ? "Back to the graph" : "The current view as a table of nodes and edges"}>
            <span>{listView ? "Graph view" : "List view"}</span><ListBullets size={14} aria-hidden />
          </button>
        </span>
      ) : null}
    </span>
  );
}

/** A search result's title (tester round 6: "Allocator that skips the remaining-amount dec…", cut
 *  mid-word by CSS with no tooltip): up to two lines, cut at a word; the whole of it is the row's
 *  tooltip, with where it is. */
export const HIT_TITLE_MAX = 96;
export function hitTitle(label: string): string {
  return charEllipsis(label, HIT_TITLE_MAX);
}
export function hitTip(label: string, where: string | null): string {
  return where && where !== label ? `${label}\n${where}` : label;
}
/** A long identifier wraps at its words — camelCase humps, ".", "/", "_", "-" — never mid-word
 *  ("…TransactionProc / essor"): a word-break opportunity at each. */
export function breakParts(text: string): string[] {
  return text.split(/(?<=[a-z0-9])(?=[A-Z])|(?<=[./_-])(?=[^\s])/);
}

/** A search result's title in at most two lines, cut at a word (hitTitle): shortened a few
 *  characters at a time, at words, until it fits its two lines — the CSS clamp alone cut
 *  mid-word ("…the interest amount o…"). The whole title is the row's tooltip. */
function FitTitle({ label }: { label: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  // The span's text is this effect's own (React renders it empty): set, measured and shortened in
  // one pass before paint, and again whenever the row's width changes (the list's scrollbar).
  React.useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const fit = () => {
      for (let max = HIT_TITLE_MAX; ; max -= 4) {
        const text = charEllipsis(label, Math.max(24, max));
        el.replaceChildren(...breakParts(text).flatMap((p, i) => (i ? [document.createElement("wbr"), p] : [p])));
        if (max <= 24 || el.scrollHeight <= el.clientHeight + 1) break;
      }
    };
    fit();
    if (typeof ResizeObserver === "undefined") return undefined;
    let w = el.clientWidth;
    const ro = new ResizeObserver(() => { if (el.clientWidth !== w) { w = el.clientWidth; fit(); } });
    ro.observe(el);
    return () => ro.disconnect();
  }, [label]);
  return <span ref={ref} className="kg-hit-label" />;
}
