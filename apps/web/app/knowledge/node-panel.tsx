"use client";

/**
 * THE DETAIL PANEL (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.6): what one node is, top to bottom —
 * header (layer badge, kind, title, copy id), statement, attributes, connections (clickable,
 * paged by 20), vector, learned → used, provenance, and the actions its kind and the viewer's
 * permissions allow. Area, package and layer bubbles are summarised from the overview the canvas
 * already holds; every other node is read from GET /node/{id}.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { Copy, X } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { roleWords } from "../agents/chain-model";
import { actorWords, reviewerWords } from "../../lib/actor-words";
import { isLearnedKind, type KgConnectionGroup, type KgNodeDetailResponse, type KgProvenance, type KgViewEdge, type KgViewNode } from "../../lib/kg-view-types";
import { kindName, kindToken } from "../../lib/kg-layout";
import {
  FACT_MANAGE, RULE_REVIEW, SCAN_START, approveRule, codeHref, forgetFact, getBlastRadius, getNode, getWhy,
  kgRunHref, rejectRule,
  kgWhen,
} from "../../lib/kg-view";
import { post } from "../workflows/delivery-api";
import { StartRefusal, asReadiness, type PendingRefusal } from "../workflow-readiness-ui";
import { edgeWords } from "./graph-canvas";

export type BubbleSummary = {
  node: KgViewNode;
  /** The "N more areas" bubble: the areas it holds, each one press from its own page. */
  members?: KgViewNode[];
  out: Array<{ edge: KgViewEdge; other: KgViewNode }>;
  in: Array<{ edge: KgViewEdge; other: KgViewNode }>;
};

const LAYER_NAME: Record<string, string> = { code: "Code", rules: "Rules", learning: "Learning", domain: "Domain" };

/** One formatter for the area: the viewer's own zone, labelled (lib/kg-view.ts kgWhen). */
const when = kgWhen;

function agentWords(p: { agent_name?: string | null; agent_role?: string | null }): string {
  // BOSS itself, when no agent was addressed in the turn: its name says so ("BOSS (general)").
  const role = p.agent_role && p.agent_role !== "boss" ? roleWords(p.agent_role) : "";
  if (p.agent_name && role) return `${p.agent_name} (${role})`;
  return p.agent_name || role || "an agent";
}

function who(p: KgProvenance): string {
  if (p.created_by_kind === "system") return "the code index";
  if (p.created_by_kind === "agent") return agentWords(p);
  return actorWords(p.created_by);
}

export function NodePanel({ projectId, actor, orgId, nodeId, preview, bubble, archived, rulesLocked,
  onFocusNode, onShowConnected, onDrill, onEditRule, onClose, onChanged, viewEdges, onDetail }: {
  projectId: string; actor: string; orgId?: string | null;
  nodeId: string | null;
  /** What the canvas already knows (label, kind), shown while the detail loads. */
  preview?: KgViewNode | null;
  bubble?: BubbleSummary | null;
  archived?: boolean;
  rulesLocked?: string | null;
  onFocusNode: (id: string, node?: KgViewNode) => void;
  /** "Show only what connects to this": Focus on the panel's node. */
  onShowConnected?: (() => void) | null;
  /** Open an area's packages (Whole repo drill-down). */
  onDrill?: ((areaId: string) => void) | null;
  onEditRule?: (ruleId: string) => void;
  onClose?: () => void;
  /** A write changed the graph (a rule approved): the views reload. */
  onChanged?: () => void;
  /** Its edges in the canvas's current view, so the panel and the canvas never disagree (P1-5). */
  viewEdges?: Array<{ edge: KgViewEdge; other: KgViewNode; out: boolean }> | null;
  /** The detail, once read: the canvas draws the connections it lists (one source of truth). */
  onDetail?: (d: KgNodeDetailResponse) => void;
}) {
  const w = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const [detail, setDetail] = useState<KgNodeDetailResponse | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const detailRef = useRef<KgNodeDetailResponse | null>(null);
  detailRef.current = detail;
  const isBubble = !!bubble || (nodeId ? /^(area|pkg|layer):/.test(nodeId) : false);

  // A new node clears the panel; a reload of the same one (after an action) keeps it on screen.
  const shownId = useRef<string | null>(null);
  useEffect(() => {
    const fresh = shownId.current !== nodeId;
    if (fresh) { setDetail(null); shownId.current = nodeId; }
    setError(null);
    if (!nodeId || isBubble) return undefined;
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    if (fresh || !detailRef.current) setLoading(true);
    void getNode(w, nodeId, undefined, ctl?.signal).then((r) => {
      if (r.ok) { setDetail(r.data); onDetail?.(r.data); }
      else if (r.status !== -1) setError({ status: r.status, text: r.error });
      if (r.ok || r.status !== -1) setLoading(false);
    });
    return () => ctl?.abort();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [w, nodeId, isBubble, attempt]);

  if (!nodeId) {
    return (
      <aside className="kg-panel" data-testid="kg-panel" data-state="none" aria-label="Details">
        <div className="kg-panel-none" data-testid="kg-panel-none">
          <span className="kg-eyebrow">Details</span>
          <p>Select a node, a row in the tree, or a search result to see what it is, what it connects to and where it came from.</p>
        </div>
      </aside>
    );
  }

  const node = bubble?.node ?? detail?.node ?? preview ?? null;
  const layer = node?.layer ?? "code";
  const kind = node ? kindName(node.kind, node.symbol_kind, node.id) : "";
  const close = onClose ? (
    <button type="button" className="ag-icon-btn kg-panel-close" aria-label="Close details" onClick={onClose} data-testid="kg-panel-close">
      <X size={14} weight="bold" aria-hidden />
    </button>
  ) : null;

  return (
    <aside className="kg-panel" data-testid="kg-panel" data-state={loading ? "loading" : error ? "error" : "ready"} aria-label="Details"
           data-node={nodeId}>
      <header className="kg-panel-head">
        <div className="kg-panel-badges">
          <span className="kg-badge" data-layer={layer} data-testid="kg-panel-layer">{LAYER_NAME[layer] ?? layer}</span>
          {kind ? <span className="kg-panel-kind" data-testid="kg-panel-kind">{kind}{detail?.language ? ` · ${cap(detail.language)}` : ""}</span> : null}
          <span className="ag-spacer" />
          <CopyId id={nodeId} />
          {close}
        </div>
        <h3 className="kg-panel-title" data-testid="kg-panel-title" data-user-text>{soft(node?.label ?? "…")}</h3>
        {node?.sub && !isBubble ? <p className="kg-panel-sub inf-mono" title={node.sub}>{node.sub}</p> : null}
        <div className="kg-panel-actions">
          {onShowConnected ? (
            <button type="button" className="ag-btn" data-testid="kg-panel-focus" onClick={onShowConnected}
                    title="Focus: this node and only what connects to it">Show only what connects to this</button>
          ) : null}
          {bubble && node?.kind === "area" && onDrill ? (
            <button type="button" className="ag-btn" data-testid="kg-panel-drill" onClick={() => onDrill(node.id)}>Open its packages</button>
          ) : null}
        </div>
      </header>

      {isBubble && bubble ? <BubbleBody b={bubble} onFocusNode={onFocusNode} /> : null}
      {!isBubble && loading ? <PanelSkeleton /> : null}
      {!isBubble && error ? (
        <div className="kg-inline-err" role="alert" data-testid="kg-panel-error">
          <span>{error.status === 404 ? "This node is no longer in the graph (it may have been re-indexed)." : error.text}</span>
          <button type="button" className="inf-link" onClick={() => setAttempt((a) => a + 1)}>Retry</button>
        </div>
      ) : null}
      {!isBubble && detail ? (
        <DetailBody d={detail} w={w} orgId={orgId} onFocusNode={onFocusNode} viewEdges={viewEdges ?? null} />
      ) : null}

      <PanelActions w={w} node={node} detail={detail} archived={!!archived} rulesLocked={rulesLocked ?? null}
                    onEditRule={onEditRule} onChanged={() => { setAttempt((a) => a + 1); onChanged?.(); }} orgId={orgId} />
    </aside>
  );
}

/** Long identifiers and paths break at their own seams (a capital, a dot, a slash), never
 *  mid-word (tester round 1 P2). */
export function soft(text: string): React.ReactNode {
  const parts = text.split(/(?<=[/._])|(?<=[a-z0-9])(?=[A-Z])/);
  if (parts.length < 2) return text;
  return parts.map((p, i) => <React.Fragment key={i}>{i ? <wbr /> : null}{p}</React.Fragment>);
}

function cap(s: string) { return s.charAt(0).toUpperCase() + s.slice(1); }

function CopyId({ id }: { id: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="ag-icon-btn" aria-label={done ? "Id copied" : "Copy id"} title={done ? "Copied" : `Copy id ${id}`}
            data-testid="kg-panel-copy"
            onClick={() => { try { void navigator.clipboard?.writeText(id); } catch { /* no clipboard */ } setDone(true); setTimeout(() => setDone(false), 1500); }}>
      <Copy size={14} aria-hidden />
    </button>
  );
}

function PanelSkeleton() {
  return (
    <div className="kg-panel-skel" aria-hidden data-testid="kg-panel-skeleton">
      {Array.from({ length: 7 }, (_, i) => <span key={i} className="kg-skel-row" style={{ width: `${50 + ((i * 29) % 45)}%` }} />)}
    </div>
  );
}

function Section({ title, children, testId }: { title: string; children: React.ReactNode; testId: string }) {
  return (
    <section className="kg-sec" data-testid={testId}>
      <h4 className="kg-eyebrow">{title}</h4>
      {children}
    </section>
  );
}

function NodeRow({ n, note, onPick }: { n: KgViewNode; note?: string; onPick: () => void }) {
  return (
    <li>
      <button type="button" className="kg-conn" onClick={onPick} title={`${kindName(n.kind, n.symbol_kind, n.id)} · ${n.label}${n.sub ? ` · ${n.sub}` : ""}`}
              data-testid="kg-conn-item">
        <span className="kg-dot" style={{ background: `var(${kindToken(n.kind, n.symbol_kind, n.id)})` }} aria-hidden />
        <span className="kg-conn-label" data-user-text>{n.label}</span>
        {note ? <span className="kg-conn-note">{note}</span> : null}
        <span className="kg-conn-go" aria-hidden>›</span>
      </button>
    </li>
  );
}

function BubbleBody({ b, onFocusNode }: { b: BubbleSummary; onFocusNode: (id: string, n?: KgViewNode) => void }) {
  const counts = Object.entries(b.node.counts ?? {}).filter(([, v]) => v > 0);
  return (
    <>
      <Section title="Attributes" testId="kg-panel-attrs">
        <dl className="kg-attrs">
          {counts.map(([k, v]) => (
            <div key={k} className="kg-attr"><dt>{k.replace(/_/g, " ")}</dt><dd>{v.toLocaleString("en-US")}</dd></div>
          ))}
          {b.node.sub ? <div className="kg-attr"><dt>summary</dt><dd>{b.node.sub}</dd></div> : null}
        </dl>
      </Section>
      {b.members?.length ? (
        <Section title={`In "${b.node.label}"`} testId="kg-panel-members">
          <ul className="kg-conn-list">
            {b.members.map((m) => <NodeRow key={m.id} n={m} note={m.sub} onPick={() => onFocusNode(m.id, m)} />)}
          </ul>
        </Section>
      ) : null}
      <Section title="Connections" testId="kg-panel-connections">
        {!b.out.length && !b.in.length ? <p className="kg-quiet">Nothing in this view connects to it.</p> : null}
        {b.out.length ? <h5 className="kg-group-head">Out <span>{b.out.length}</span></h5> : null}
        <ul className="kg-conn-list">
          {b.out.map(({ edge, other }) => <NodeRow key={`o-${other.id}-${edge.kind}`} n={other} note={edge.label ?? edgeWords(edge.kind)} onPick={() => onFocusNode(other.id, other)} />)}
        </ul>
        {b.in.length ? <h5 className="kg-group-head">In <span>{b.in.length}</span></h5> : null}
        <ul className="kg-conn-list">
          {b.in.map(({ edge, other }) => <NodeRow key={`i-${other.id}-${edge.kind}`} n={other} note={edge.label ?? edgeWords(edge.kind)} onPick={() => onFocusNode(other.id, other)} />)}
        </ul>
      </Section>
      <Section title="Vector" testId="kg-panel-vector">
        <p className="kg-quiet">{b.node.kind === "layer" ? "Each item in this layer carries its own vector when an embedding model is set (Settings); open one to see its model and nearest items." : `${b.node.kind === "package" ? "A package" : b.node.kind === "group" ? "A group" : "An area"} is a summary of what it holds; code symbols are matched by structure, not meaning.`}</p>
      </Section>
      <Section title="Provenance" testId="kg-panel-provenance">
        <p className="kg-quiet">{b.node.kind === "layer" ? (b.node.id === "layer:rules" ? "Proposed by agents, approved by people in Rules review." : "Written by agents while they work, with the run and step that wrote it.") : "Rolled up from the code index after each index."}</p>
      </Section>
    </>
  );
}

const ATTR_SKIP = new Set(["line", "end_line", "module", "lang", "language", "symbol_kind", "type_kind", "kind", "statement", "provenance", "review", "scope", "detail", "about", "evidence", "category", "confidence", "status"]);

function DetailBody({ d, w, orgId, onFocusNode, viewEdges }: {
  d: KgNodeDetailResponse; w: { projectId: string; actor: string }; orgId?: string | null;
  onFocusNode: (id: string, n?: KgViewNode) => void;
  viewEdges: Array<{ edge: KgViewEdge; other: KgViewNode; out: boolean }> | null;
}) {
  const a = d.attrs ?? {};
  const href = codeHref(d.repo, d.path, d.line && d.end_line ? [d.line, d.end_line] : d.line);
  const isRule = d.node.kind === "rule";
  const statusWord = d.node.status ?? (typeof a.status === "string" ? a.status : null);
  const trust = isRule && d.provenance.approved_by
    ? `Approved by ${reviewerWords(d.provenance.approved_by, d.provenance.approved_by_name)}${d.provenance.approved_at ? ` · ${when(d.provenance.approved_at)}` : ""}`
    : isRule ? `${cap(statusWord ?? "proposed")} · not yet trusted by agents` : d.node.trust_tier === "inferred" ? "Inferred by an agent · not reviewed" : null;
  const extra = Object.entries(a).filter(([k, v]) => !ATTR_SKIP.has(k) && (typeof v === "string" || typeof v === "number" || typeof v === "boolean"));
  const detail = (a.detail && typeof a.detail === "object") ? (a.detail as Record<string, unknown>) : null;
  return (
    <>
      {d.statement ? (
        <Section title={isRule ? "Rule" : isLearnedKind(d.node.kind) ? "What was learned" : "Statement"} testId="kg-panel-statement">
          <div className="kg-statement" data-layer={d.node.layer}>
            <p data-user-text>{d.statement}</p>
            {trust ? <p className="kg-statement-trust" data-testid="kg-panel-trust">{trust}</p> : null}
          </div>
        </Section>
      ) : null}
      <Section title="Attributes" testId="kg-panel-attrs">
        <dl className="kg-attrs">
          {d.path ? (
            <div className="kg-attr"><dt>path</dt><dd className="inf-mono">
              {href ? <a href={href} target="_blank" rel="noreferrer" className="inf-link kg-path" data-testid="kg-panel-code-link" title="Open on GitHub at these lines">
                {d.path.split("/").pop()}{d.line ? `:${d.line}${d.end_line && d.end_line !== d.line ? `–${d.end_line}` : ""}` : ""} ↗</a>
                : <span>{d.path}{d.line ? `:${d.line}` : ""}</span>}
            </dd></div>
          ) : null}
          {d.package ? <div className="kg-attr"><dt>package</dt><dd className="inf-mono">{d.package}</dd></div> : null}
          {d.area ? <div className="kg-attr"><dt>area</dt><dd className="inf-mono">{d.area}</dd></div> : null}
          {d.node.symbol_kind ? <div className="kg-attr"><dt>symbol kind</dt><dd>{d.node.symbol_kind}</dd></div> : null}
          {d.language ? <div className="kg-attr"><dt>language</dt><dd>{d.language}</dd></div> : null}
          {typeof a.category === "string" ? <div className="kg-attr"><dt>category</dt><dd>{a.category.replace(/_/g, " ")}</dd></div> : null}
          {typeof a.confidence === "number" ? <div className="kg-attr"><dt>confidence</dt><dd>{Math.round(a.confidence * 100)}%</dd></div> : null}
          {d.repo ? <div className="kg-attr"><dt>repository</dt><dd className="inf-mono">{d.repo}</dd></div> : null}
          <div className="kg-attr"><dt>trust</dt><dd>{d.node.trust_tier}</dd></div>
          {detail ? Object.entries(detail).filter(([, v]) => typeof v === "string" || typeof v === "number").map(([k, v]) => (
            <div key={`d-${k}`} className="kg-attr"><dt>{k.replace(/_/g, " ")}</dt><dd data-user-text>{String(v)}</dd></div>
          )) : null}
          {extra.map(([k, v]) => <div key={k} className="kg-attr"><dt>{k.replace(/_/g, " ")}</dt><dd className="inf-mono">{String(v)}</dd></div>)}
        </dl>
      </Section>
      {isType(d.node) ? <Members d={d} w={w} onFocusNode={onFocusNode} /> : null}
      <Connections d={d} w={w} onFocusNode={onFocusNode} viewEdges={viewEdges} />
      <Section title="Vector" testId="kg-panel-vector">
        {d.vector.embedded ? (
          <>
            <p className="kg-quiet" data-testid="kg-panel-vector-model">Embedded with {d.vector.model_id} ({d.vector.dimensions.toLocaleString("en-US")} dimensions). Nearest by meaning:</p>
            <ul className="kg-conn-list">
              {d.vector.neighbours.map((n) => <NodeRow key={n.node.id} n={n.node} note={n.score.toFixed(2)} onPick={() => onFocusNode(n.node.id, n.node)} />)}
            </ul>
          </>
        ) : <p className="kg-quiet" data-testid="kg-panel-vector-reason">{d.vector.reason}</p>}
      </Section>
      {d.lineage ? <Lineage d={d} orgId={orgId} /> : null}
      {d.usage.consulted_runs || d.usage.reused_runs ? (
        <p className="kg-usage" data-testid="kg-panel-usage">
          {d.usage.consulted_runs ? `Consulted by ${d.usage.consulted_runs.toLocaleString("en-US")} ${d.usage.consulted_runs === 1 ? "run" : "runs"}` : ""}
          {d.usage.consulted_runs && d.usage.reused_runs ? " · " : ""}
          {d.usage.reused_runs ? `reused by ${d.usage.reused_runs.toLocaleString("en-US")}` : ""}
        </p>
      ) : null}
      <Provenance p={d.provenance} orgId={orgId} />
      {d.node.layer === "code" ? <WhyAndBlast w={w} nodeId={d.node.id} detail={d} onFocusNode={onFocusNode} /> : null}
    </>
  );
}

const TYPE_KINDS = new Set(["class", "interface", "enum", "record"]);
/** A class (or interface, enum, record): the node the graph focuses at class level. */
export function isType(n: KgViewNode): boolean {
  return n.kind === "symbol" && TYPE_KINDS.has(n.symbol_kind ?? "class");
}

/**
 * A CLASS'S METHODS (round 2: the class is the node). Its methods are listed here, each with its
 * own connections one press away; pressing one opens the method-level view on the canvas.
 */
function Members({ d, w, onFocusNode }: {
  d: KgNodeDetailResponse; w: { projectId: string; actor: string }; onFocusNode: (id: string, n?: KgViewNode) => void;
}) {
  const first = d.connections.find((g) => g.kind === "defines" && g.direction === "out") ?? null;
  const [group, setGroup] = useState<KgConnectionGroup | null>(first);
  const [busy, setBusy] = useState(false);
  useEffect(() => setGroup(d.connections.find((g) => g.kind === "defines" && g.direction === "out") ?? null), [d]);
  if (!group || !group.items.length) {
    return <Section title="Methods" testId="kg-panel-members"><p className="kg-quiet">It declares no methods of its own.</p></Section>;
  }
  const more = async () => {
    setBusy(true);
    const r = await getNode(w, d.node.id, { edge: "defines", direction: "out", offset: group.items.length });
    setBusy(false);
    const page = r.ok ? r.data.connections.find((x) => x.kind === "defines" && x.direction === "out") : null;
    if (page) setGroup({ ...group, items: [...group.items, ...page.items.filter((i) => !group.items.some((y) => y.id === i.id))] });
  };
  return (
    <Section title={`Methods · ${group.total.toLocaleString("en-US")}`} testId="kg-panel-members">
      <ul className="kg-conn-list">
        {group.items.map((m) => <MemberRow key={m.id} m={m} w={w} onFocusNode={onFocusNode} />)}
      </ul>
      {group.items.length < group.total ? (
        <button type="button" className="inf-link kg-more" data-testid="kg-members-more" disabled={busy} onClick={() => void more()}>
          {busy ? "Loading…" : `Show all · next ${Math.min(20, group.total - group.items.length)} (${group.items.length} of ${group.total.toLocaleString("en-US")})`}
        </button>
      ) : null}
    </Section>
  );
}

function MemberRow({ m, w, onFocusNode }: { m: KgViewNode; w: { projectId: string; actor: string }; onFocusNode: (id: string, n?: KgViewNode) => void }) {
  const [open, setOpen] = useState(false);
  const [conn, setConn] = useState<KgConnectionGroup[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && !conn) {
      const r = await getNode(w, m.id);
      if (r.ok) setConn(r.data.connections.filter((g) => g.kind !== "defines")); else setErr(r.error);
    }
  };
  return (
    <li className="kg-member" data-testid="kg-member">
      <span className="kg-member-row">
        <button type="button" className="ag-icon-btn kg-member-toggle" aria-expanded={open} aria-label={`${open ? "Hide" : "Show"} what ${m.label} connects to`}
                data-testid="kg-member-toggle" onClick={() => void toggle()}>{open ? "▾" : "▸"}</button>
        <button type="button" className="kg-conn" onClick={() => onFocusNode(m.id, m)} data-testid="kg-member-open"
                title={`Open the method-level view of ${m.label}`}>
          <span className="kg-dot" style={{ background: `var(${kindToken(m.kind, m.symbol_kind, m.id)})` }} aria-hidden />
          <span className="kg-conn-label" data-user-text>{m.label}</span>
          <span className="kg-conn-go" aria-hidden>›</span>
        </button>
      </span>
      {open ? (
        <div className="kg-member-conn" data-testid="kg-member-conn">
          {err ? <p className="kg-inline-err" role="alert">{err}</p> : null}
          {!conn && !err ? <p className="kg-quiet">Reading…</p> : null}
          {conn && !conn.length ? <p className="kg-quiet">Nothing connects to it.</p> : null}
          {conn?.length ? (
            <>
              <p className="kg-quiet">{conn.map((g) => `${g.label} ${g.total.toLocaleString("en-US")}`).join(" · ")}</p>
              <ul className="kg-conn-list">
                {conn.flatMap((g) => g.items.slice(0, 3).map((n) => ({ g, n }))).slice(0, 6).map(({ g, n }) => (
                  <NodeRow key={`${g.kind}-${g.direction}-${n.id}`} n={n} note={g.label} onPick={() => onFocusNode(n.id, n)} />
                ))}
              </ul>
            </>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function Connections({ d, w, onFocusNode, viewEdges }: {
  d: KgNodeDetailResponse; w: { projectId: string; actor: string }; onFocusNode: (id: string, n?: KgViewNode) => void;
  viewEdges: Array<{ edge: KgViewEdge; other: KgViewNode; out: boolean }> | null;
}) {
  // A type's own members are listed under Methods (round 2), not again here.
  const own = (gs: KgConnectionGroup[]) => (isType(d.node) ? gs.filter((g) => !(g.kind === "defines" && g.direction === "out")) : gs);
  const [groups, setGroups] = useState<KgConnectionGroup[]>(own(d.connections));
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setGroups(own(d.connections)), [d]);
  const summary = groups.map((g) => `${g.label} ${g.total.toLocaleString("en-US")}`).join(" · ");
  const more = async (g: KgConnectionGroup) => {
    const k = `${g.kind}:${g.direction}`;
    setBusy(k); setErr(null);
    const r = await getNode(w, d.node.id, { edge: g.kind, direction: g.direction, offset: g.items.length });
    setBusy(null);
    if (!r.ok) { setErr(r.error); return; }
    const page = r.data.connections.find((x) => x.kind === g.kind && x.direction === g.direction);
    if (!page) return;
    setGroups((gs) => gs.map((x) => (x.kind === g.kind && x.direction === g.direction
      ? { ...x, items: [...x.items, ...page.items.filter((i) => !x.items.some((y) => y.id === i.id))] } : x)));
  };
  return (
    <Section title="Connections" testId="kg-panel-connections">
      {groups.length ? <p className="kg-quiet" data-testid="kg-panel-conn-summary">{summary}</p> : !viewEdges?.length ? <p className="kg-quiet">Nothing connects to it.</p> : null}
      {viewEdges?.length ? (
        // What the canvas draws for it right now — class calls rolled up from its methods
        // included — so a line on the canvas is always a row here.
        <div className="kg-group" data-testid="kg-conn-view">
          <h5 className="kg-group-head" title="The connections drawn on the canvas now">In this view <span>{viewEdges.length.toLocaleString("en-US")}</span></h5>
          <ul className="kg-conn-list">
            {viewEdges.map(({ edge, other, out }) => (
              <NodeRow key={`v-${other.id}-${edge.kind}-${out}`} n={other}
                       note={`${out ? "" : "← "}${edge.label ?? edgeWords(edge.kind)}`} onPick={() => onFocusNode(other.id, other)} />
            ))}
          </ul>
        </div>
      ) : null}
      {groups.map((g) => {
        const k = `${g.kind}:${g.direction}`;
        return (
          <div key={k} className="kg-group" data-testid="kg-conn-group" data-kind={g.kind} data-direction={g.direction}>
            <h5 className="kg-group-head">{g.label} <span>{g.total.toLocaleString("en-US")}</span></h5>
            <ul className="kg-conn-list">
              {g.items.map((n) => <NodeRow key={n.id} n={n} onPick={() => onFocusNode(n.id, n)} />)}
            </ul>
            {g.items.length < g.total ? (
              <button type="button" className="inf-link kg-more" data-testid="kg-conn-more" disabled={busy === k} onClick={() => void more(g)}>
                {busy === k ? "Loading…" : `Show all · next ${Math.min(20, g.total - g.items.length)} (${g.items.length} of ${g.total.toLocaleString("en-US")})`}
              </button>
            ) : null}
          </div>
        );
      })}
      {err ? <p className="kg-inline-err" role="alert">{err}</p> : null}
    </Section>
  );
}

function Lineage({ d, orgId }: { d: KgNodeDetailResponse; orgId?: string | null }) {
  const l = d.lineage!;
  const learnedRun = kgRunHref(d.provenance, orgId);
  return (
    <Section title="Learned → used" testId="kg-panel-lineage">
      <p className="kg-lineage">
        Learned by <strong>{agentWords(l.learned)}</strong>
        {l.learned.run_id ? <> in {learnedRun ? <a className="inf-link" href={learnedRun}>run {short(l.learned.run_id)}</a> : <>run {short(l.learned.run_id)}</>}</> : null}
        {l.learned.step || l.learned.at ? ` (${[l.learned.step ? `step ${l.learned.step}` : "", when(l.learned.at)].filter(Boolean).join(", ")})` : ""}
        {l.used.length ? " → used by " : " · not used by a later run yet"}
        {l.used.map((u, i) => (
          <React.Fragment key={`${u.run_id}-${i}`}>
            {i ? ", " : ""}
            <strong>{agentWords(u)}</strong> in run {short(u.run_id)} ({u.how}, {when(u.at)})
          </React.Fragment>
        ))}
      </p>
    </Section>
  );
}

function short(id: string): string {
  return id.length > 14 ? `${id.slice(0, 12)}…` : id;
}

function Provenance({ p, orgId }: { p: KgProvenance; orgId?: string | null }) {
  const run = kgRunHref(p, orgId);
  const lines: React.ReactNode[] = [];
  lines.push(<>{p.created_by_kind === "system" ? "Indexed by" : p.created_by_kind === "agent" ? "Written by" : "Added by"} <strong>{who(p)}</strong>{p.at ? ` · ${when(p.at)}` : ""}</>);
  if (p.run_id || run) lines.push(<>Run {run ? <a className="inf-link" href={run} data-testid="kg-panel-run-link">{short(p.code_task_run_id ?? p.workflow_run_id ?? p.run_id ?? "")} ↗</a> : short(p.run_id ?? "")}{p.step ? ` · step ${p.step}` : ""}</>);
  if (p.model_id) lines.push(<>Model {p.model_id}</>);
  if (p.approved_by) lines.push(<>Approved by <strong>{reviewerWords(p.approved_by, p.approved_by_name)}</strong>{p.approved_at ? ` · ${when(p.approved_at)}` : ""}</>);
  if (p.version && p.version > 1) lines.push(<>Version {p.version}, edited from v{p.version - 1}</>);
  return (
    <Section title="Provenance" testId="kg-panel-provenance">
      <ul className="kg-prov">{lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
      {p.events.length > 1 ? (
        <ol className="kg-timeline" data-testid="kg-panel-timeline">
          {p.events.map((e, i) => (
            <li key={i}><span className="kg-timeline-at">{when(e.at)}</span> {e.what}{e.by ? ` by ${reviewerWords(e.by, e.by_name)}` : ""}</li>
          ))}
        </ol>
      ) : null}
    </Section>
  );
}

/** Parity with the old explorer (§5.1): why a node is believed, and what a change to it reaches. */
/** What depends on a node (tester round 2: blast radius said "reaches nothing" for a class with
 *  112 referrers and 53 importers): every incoming code edge — callers, references, imports,
 *  dispatch, implementations, subclasses, overrides — from the detail the panel already holds. */
export function dependents(d: KgNodeDetailResponse): KgConnectionGroup[] {
  const reach = new Set(["calls", "references", "imports", "dispatches_to", "implements", "extends", "overrides", "reads", "writes", "governs"]);
  return d.connections.filter((g) => g.direction === "in" && reach.has(g.kind) && g.total > 0);
}

function WhyAndBlast({ w, nodeId, detail, onFocusNode }: { w: { projectId: string; actor: string }; nodeId: string; detail: KgNodeDetailResponse; onFocusNode: (id: string) => void }) {
  const [why, setWhy] = useState<Array<{ name: string; body: string; trust_tier: string }> | null>(null);
  const [blast, setBlast] = useState<Array<{ id?: string; node_id?: string; name?: string; kind?: string }> | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<"why" | "blast" | null>(null);
  useEffect(() => { setWhy(null); setBlast(null); setNote(null); }, [nodeId]);
  return (
    <Section title="Why and blast radius" testId="kg-panel-why">
      <div className="kg-panel-actions">
        <button type="button" className="ag-btn" data-testid="kg-why" disabled={busy !== null} onClick={async () => {
          setBusy("why"); setNote(null);
          const r = await getWhy(w, nodeId);
          setBusy(null);
          if (r.ok) setWhy(r.data.why ?? []); else setNote(r.error);
        }}>{busy === "why" ? "Reading…" : "Why"}</button>
        <button type="button" className="ag-btn" data-testid="kg-blast" disabled={busy !== null} onClick={async () => {
          setBusy("blast"); setNote(null);
          const r = await getBlastRadius(w, nodeId);
          setBusy(null);
          if (r.ok) setBlast(r.data.affected ?? []); else setNote(r.error);
        }}>{busy === "blast" ? "Reading…" : "Blast radius"}</button>
      </div>
      {note ? <p className="kg-inline-err" role="alert">{note}</p> : null}
      {why ? (why.length ? (
        <ul className="kg-why" data-testid="kg-why-list">{why.map((f) => <li key={f.body}><span data-user-text>{f.body}</span> <span className="kg-quiet">· {f.trust_tier}</span></li>)}</ul>
      ) : <p className="kg-quiet" data-testid="kg-why-list">No fact explains it yet.</p>) : null}
      {blast ? (() => {
        const direct = dependents(detail);
        const total = direct.reduce((n, g) => n + g.total, 0);
        if (!direct.length && !blast.length) return <p className="kg-quiet" data-testid="kg-blast-list">Nothing in the graph depends on it: a change here reaches nothing else.</p>;
        return (
          <div data-testid="kg-blast-list">
            {direct.length ? (
              <>
                <p className="kg-quiet" data-testid="kg-blast-summary">
                  A change here reaches {total.toLocaleString("en-US")} {total === 1 ? "place" : "places"} directly: {direct.map((g) => `${g.label.toLowerCase()} ${g.total.toLocaleString("en-US")}`).join(" · ")}
                </p>
                <ul className="kg-conn-list">
                  {direct.flatMap((g) => g.items.slice(0, 4).map((n) => ({ g, n }))).slice(0, 10).map(({ g, n }) => (
                    <NodeRow key={`${g.kind}-${n.id}`} n={n} note={g.label} onPick={() => onFocusNode(n.id)} />
                  ))}
                </ul>
              </>
            ) : null}
            {blast.length ? <p className="kg-quiet">Further out, through what depends on those:</p> : null}
            {blast.length ? (
        <ul className="kg-conn-list">
          {blast.map((b, i) => {
            const id = b.id ?? b.node_id;
            return (
              <li key={`${id ?? b.name}-${i}`}>
                {id ? <button type="button" className="kg-conn" onClick={() => onFocusNode(id)}><span className="kg-conn-label">{b.name ?? id}</span><span className="kg-conn-go" aria-hidden>›</span></button>
                  : <span className="kg-conn">{b.name ?? "unnamed"}</span>}
              </li>
            );
          })}
        </ul>
            ) : null}
          </div>
        );
      })() : null}
    </Section>
  );
}

function PanelActions({ w, node, detail, archived, rulesLocked, onEditRule, onChanged, orgId }: {
  w: { projectId: string; actor: string }; node: KgViewNode | null; detail: KgNodeDetailResponse | null;
  archived: boolean; rulesLocked: string | null; onEditRule?: (id: string) => void; onChanged: () => void; orgId?: string | null;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [confirmForget, setConfirmForget] = useState(false);
  useEffect(() => { setMsg(null); setConfirmForget(false); }, [node?.id]);
  if (!node) return null;
  const archivedWhy = "This project is archived: nothing new runs on it. Restore it to change it.";
  const scannable = node.kind === "area" || node.kind === "package" || (node.kind === "symbol" && node.symbol_kind !== "method" && node.symbol_kind !== "function");
  const proposedRule = node.kind === "rule" && (node.status ?? detail?.node.status) === "proposed";
  const learned = node.layer === "learning" && detail;
  const run = learned ? kgRunHref(detail.provenance, orgId) : null;
  const act = async (label: string, fn: () => Promise<{ ok: boolean; error?: string }>, done: string) => {
    setBusy(label); setMsg(null);
    const r = await fn();
    setBusy(null);
    if (r.ok) { setMsg({ ok: true, text: done }); onChanged(); } else setMsg({ ok: false, text: r.error ?? "It did not go through." });
  };
  if (!scannable && !proposedRule && !run && node.kind !== "fact") return null;
  return (
    <section className="kg-sec kg-sec-actions" data-testid="kg-panel-actions">
      <h4 className="kg-eyebrow">Actions</h4>
      {scannable ? (rulesLocked ? <p className="kg-quiet" data-testid="kg-scan-locked">{rulesLocked}</p>
        : <ScanForRules w={w} node={node} archived={archived} archivedWhy={archivedWhy} />) : null}
      {proposedRule ? (
        <div className="kg-panel-actions">
          <GatedButton permission={RULE_REVIEW} className="ag-btn ag-btn-primary" data-testid="kg-panel-approve"
                       disabled={busy !== null || archived} title={archived ? archivedWhy : "Approve: every agent may rely on it from now on"}
                       onClick={() => void act("approve", () => approveRule(w, node.id), "Approved. Agents rely on it from now on.")}>
            {busy === "approve" ? "Approving…" : "Approve"}
          </GatedButton>
          <GatedButton permission={RULE_REVIEW} className="ag-btn" data-testid="kg-panel-edit" disabled={archived}
                       title={archived ? archivedWhy : "Edit it in Rules review"} onClick={() => onEditRule?.(node.id)}>Edit</GatedButton>
          <GatedButton permission={RULE_REVIEW} className="ag-btn" data-tone="reject" data-testid="kg-panel-reject"
                       disabled={busy !== null || archived} title={archived ? archivedWhy : "Reject: agents will not be given it"}
                       onClick={() => void act("reject", () => rejectRule(w, node.id), "Rejected.")}>
            {busy === "reject" ? "Rejecting…" : "Reject"}
          </GatedButton>
        </div>
      ) : null}
      {run ? <a className="ag-btn" href={run} data-testid="kg-panel-open-run">Open in Factory run</a> : null}
      {node.kind === "fact" ? (
        confirmForget ? (
          <span className="kg-confirm" role="alertdialog" aria-label="Forget this fact?">
            <span>Forget this fact? Agents stop being given it.</span>
            <GatedButton permission={FACT_MANAGE} className="ag-btn" data-tone="reject" data-testid="kg-panel-forget-yes"
                         onClick={() => { setConfirmForget(false); void act("forget", () => forgetFact(w, node.id), "Forgotten."); }}>Forget</GatedButton>
            <button type="button" className="inf-link" onClick={() => setConfirmForget(false)}>Cancel</button>
          </span>
        ) : (
          <GatedButton permission={FACT_MANAGE} className="ag-btn" data-testid="kg-panel-forget" onClick={() => setConfirmForget(true)}>Forget</GatedButton>
        )
      ) : null}
      {msg ? <p className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"} data-testid="kg-panel-msg">{msg.text}</p> : null}
    </section>
  );
}

/** "Scan for rules here" (§4.5): a cost preview first, then the business_rules_scan workflow. */
export function ScanForRules({ w, node, archived, archivedWhy, compact, onStarted, reader = "Scout" }: {
  w: { projectId: string; actor: string }; node: Pick<KgViewNode, "id" | "key" | "label" | "kind">;
  archived?: boolean; archivedWhy?: string; compact?: boolean;
  /** Told once the scan started (the Knowledge Graph setup re-reads its step). */
  onStarted?: () => void;
  /** Who reads the code, by name (the setup names the team's scanner). */
  reader?: string;
}) {
  const [open, setOpen] = useState(false);
  const [budget, setBudget] = useState("2.00");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const amount = Number(budget);
  const valid = Number.isFinite(amount) && amount > 0 && amount <= 50;
  const [refusal, setRefusal] = useState<PendingRefusal | null>(null);
  const start = async (startAnyway: string[] = []) => {
    setBusy(true); setMsg(null); setRefusal(null);
    const r = await post("start", {
      project_id: w.projectId, workflow_key: "business_rules_scan", actor: w.actor,
      input: { scope: [node.key || node.id], max_files: 60, max_rules: 25, budget_usd: amount },
      ...(startAnyway.length ? { start_anyway: startAnyway } : {}),
    });
    setBusy(false);
    if (r.ok) { setOpen(false); setMsg({ ok: true, text: "Scan started. Proposed rules arrive in Rules review with the code that shows them." }); onStarted?.(); return; }
    // A start refused for what the project lacks: the shared checklist and "Scan anyway".
    const readiness = asReadiness(r.readiness);
    if (readiness) { setRefusal({ label: "Scan", message: String(r.error ?? ""), readiness, retry: (keys) => start(keys) }); return; }
    setMsg({ ok: false, text: r.error ? String(r.error) : "The scan did not start." });
  };
  if (!open) {
    return (
      <div className="kg-scan">
        <GatedButton permission={SCAN_START} className={compact ? "ag-btn ag-btn-primary" : "ag-btn"} data-testid="kg-scan"
                     disabled={archived} title={archived ? archivedWhy : `${reader} reads ${node.label} and proposes the business rules it finds`}
                     onClick={() => setOpen(true)}>Scan for rules here</GatedButton>
        {msg ? <p className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"} data-testid="kg-scan-msg">{msg.text}</p> : null}
      </div>
    );
  }
  return (
    <form className="kg-scan-preview" data-testid="kg-scan-preview" noValidate onSubmit={(e) => { e.preventDefault(); if (valid) void start(); }}>
      <p className="kg-scan-what">
        {reader} reads {node.kind === "symbol" ? <><span data-user-text>{node.label}</span> and the code it calls</> : <>the code in <span data-user-text>{node.label}</span></>}
        — up to <strong>60 files</strong>, the most connected first — and proposes up to
        <strong> 25 rules</strong>, each with the lines that show it. It stops at whichever limit comes first. No rule is trusted until
        a person approves it.</p>
      <label className="kg-field">
        <span>Spend at most (USD)</span>
        <input className="input" inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)} aria-label="Budget in US dollars"
               aria-invalid={!valid || undefined} data-testid="kg-scan-budget" />
      </label>
      {!valid ? <p className="kg-inline-err" role="alert">Enter an amount above $0 and at most $50.</p> : null}
      <p className="kg-quiet">Reserved from the project's knowledge budget before the first model call.</p>
      <div className="kg-panel-actions">
        <GatedButton permission={SCAN_START} type="submit" className="ag-btn ag-btn-primary" data-testid="kg-scan-start" disabled={busy || !valid}>
          {busy ? "Starting…" : `Start scan · up to $${valid ? amount.toFixed(2) : "—"}`}
        </GatedButton>
        <button type="button" className="inf-link" onClick={() => setOpen(false)}>Cancel</button>
      </div>
      {msg && !msg.ok ? <p className="kg-inline-err" role="alert" data-testid="kg-scan-msg">{msg.text}</p> : null}
      {refusal ? <StartRefusal refusal={refusal} projectId={w.projectId} busy={busy} onDismiss={() => setRefusal(null)} /> : null}
    </form>
  );
}

