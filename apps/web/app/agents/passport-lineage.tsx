"use client";

/**
 * Lineage: who hands this agent work, and who it hands work to (spec §3.4 › Lineage).
 *
 * An ego graph, observed by default: the agent in the middle, whoever handed it work on the left
 * (people, other agents, BOSS), whoever it handed work to on the right. An edge is as thick as the
 * work that moved along it in the window and is labelled with how it mostly moved — the API's
 * label (`Picked by BOSS ×14`), never one composed here. Rebuilt history is dashed and says
 * `Reconstructed`. Declared shows the planned lines instead: who made it, who it reports to.
 * Pressing an edge lists the last ten pieces of work that crossed it.
 */
import React, { useEffect, useMemo, useState } from "react";

import type { Act, LineageGraph } from "../../lib/agent-world-types";
import { getLineage, identityOf, listActs } from "../../lib/agent-world";
import { AgFace } from "./agent-face";
import { useIdentityVersion } from "../../lib/use-agent-identities";

const W = 640;
const ROW = 64;
const PAD = 36;
const LEFT_X = 92;
const RIGHT_X = W - 92;
const CENTER_X = W / 2;

type Node = LineageGraph["nodes"][number];
type Edge = LineageGraph["edges"][number];
type Pos = { x: number; y: number };

export type EgoLayout = {
  height: number; center: Node & Pos;
  left: (Node & Pos)[]; right: (Node & Pos)[]; top: (Node & Pos)[];
  edges: (Edge & { a: Pos; b: Pos; width: number; side: "in" | "out" | "top" })[];
};

/** Edge width from the work that moved along it: 1.5px for one, 7px at the busiest. */
export function edgeWidth(acts: number, max: number): number {
  if (max <= 1) return 1.5 + Math.min(acts, 1) * 0.5;
  return 1.5 + 5.5 * (Math.log(1 + acts) / Math.log(1 + max));
}

/** Place the graph: in-edges left, out-edges right; declared lines (born by, reports to) on top. */
export function layoutEgo(graph: LineageGraph, roleKey: string, declared = false): EgoLayout {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const centerNode = graph.nodes.find((n) => n.roleKey === roleKey) ?? byId.get(roleKey)
    ?? { id: roleKey, kind: "agent" as const, roleKey };
  const cid = centerNode.id;
  const inIds: string[] = [];
  const outIds: string[] = [];
  const topIds: string[] = [];
  for (const e of graph.edges) {
    if (e.to === cid && e.from !== cid) (declared ? topIds : inIds).push(e.from);
    else if (e.from === cid && e.to !== cid) (declared ? topIds : outIds).push(e.to);
  }
  const uniq = (xs: string[]) => [...new Set(xs)];
  const topRow = topIds.length ? 1 : 0;
  const rows = Math.max(uniq(inIds).length, uniq(outIds).length, 1);
  const height = PAD * 2 + topRow * ROW + rows * ROW;
  const colY = (i: number, n: number) => PAD + topRow * ROW + ((rows - n) * ROW) / 2 + i * ROW + ROW / 2;
  const node = (id: string): Node => byId.get(id) ?? { id, kind: "agent", roleKey: id };
  const left = uniq(inIds).map((id, i, all) => ({ ...node(id), x: LEFT_X, y: colY(i, all.length) }));
  const right = uniq(outIds).map((id, i, all) => ({ ...node(id), x: RIGHT_X, y: colY(i, all.length) }));
  const top = uniq(topIds).map((id, i, all) => ({ ...node(id), x: CENTER_X + (i - (all.length - 1) / 2) * 180, y: PAD + ROW / 2 - 8 }));
  const center = { ...centerNode, x: CENTER_X, y: PAD + topRow * ROW + (rows * ROW) / 2 };
  const pos = new Map<string, Pos>([[cid, center], ...[...left, ...right, ...top].map((n) => [n.id, n] as [string, Pos])]);
  const max = Math.max(1, ...graph.edges.map((e) => e.acts));
  const edges = graph.edges.filter((e) => pos.has(e.from) && pos.has(e.to) && e.from !== e.to).map((e) => {
    const side = top.some((t) => t.id === e.from || t.id === e.to) ? "top" as const
      : e.to === cid ? "in" as const : "out" as const;
    return { ...e, a: pos.get(e.from)!, b: pos.get(e.to)!, width: edgeWidth(e.acts, max), side };
  });
  return { height, center, left, right, top, edges };
}

/** An edge's identity across renders: graph objects are rebuilt on every read. */
export const edgeKey = (e: { from: string; to: string }) => `${e.from}>${e.to}`;

type AnyAct = Act & { from?: { kind: string; id?: string; roleKey?: string } };

/** Did `sender` hand this act its work? Lineage node ids are not role keys: an agent is matched
 *  by its role key, a person by their actor id, BOSS by kind. */
export function handedBySender(a: AnyAct, sender: Node): boolean {
  const kind = a.from?.kind ?? a.handedBy.kind;
  if (sender.kind === "boss") return kind === "boss";
  if (sender.kind === "person") return kind === "person" && (a.from?.id ?? a.handedBy.id) === sender.id;
  const role = sender.roleKey ?? sender.id;
  return kind === "agent" && (a.from?.roleKey ?? a.from?.id ?? a.handedBy.id) === role;
}

const LAST = 10;
const MAX_PAGES = 10;

/** The last ten pieces of work that crossed an edge, paging through the receiver's work until
 *  ten are found or it runs out (a busy agent's first page may hold none of them). */
export async function actsAcross(projectId: string, graph: LineageGraph, edge: Edge, days: number): Promise<Act[]> {
  const node = (id: string): Node => graph.nodes.find((n) => n.id === id)
    ?? { id, kind: "agent", roleKey: id };
  const sender = node(edge.from);
  const receiver = node(edge.to);
  if (receiver.kind !== "agent") return [];
  const receiverRole = receiver.roleKey ?? receiver.id;
  const found: Act[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < MAX_PAGES && found.length < LAST; page++) {
    const r = await listActs(projectId, receiverRole, { days, cursor });
    if (!r.ok) break;
    for (const a of r.data.items) if (handedBySender(a, sender) && found.length < LAST) found.push(a);
    cursor = r.data.next;
    if (!cursor) break;
  }
  return found;
}

function NodeChip({ n, actor, center = false }: { n: Node & Pos; actor: string; center?: boolean }) {
  useIdentityVersion(); // names and faces below are resolved at render: repaint when identities load
  const label = n.kind === "boss" ? "BOSS" : n.kind === "person" ? (n.id === actor ? "You" : n.id)
    : (identityOf(n.roleKey ?? n.id).callsign || identityOf(n.roleKey ?? n.id).name);
  const variant = n.kind === "boss" ? "boss" : n.kind === "person" ? (n.id === actor ? "you" : "person") : "agent";
  return (
    <span className="ag-ln-node" data-center={center || undefined} data-kind={n.kind}
          style={{ left: `${(n.x / W) * 100}%`, top: n.y }}>
      <AgFace roleKey={n.roleKey ?? n.id} size={center ? 48 : 32} variant={variant} decorative />
      <span className="ag-ln-name">{label}</span>
    </span>
  );
}

export function PassportLineage({ projectId, actor, roleKey }: { projectId: string; actor: string; roleKey: string }) {
  const [mode, setMode] = useState<"observed" | "declared">("observed");
  const [days, setDays] = useState<7 | 30>(30);
  const [graph, setGraph] = useState<LineageGraph | null>(null);
  const [failed, setFailed] = useState("");
  const [picked, setPicked] = useState<{ key: string; label: string; acts: Act[] | null } | null>(null);

  useEffect(() => {
    let alive = true;
    setGraph(null); setPicked(null);
    void getLineage(projectId, roleKey, { days, mode }).then((r) => {
      if (!alive) return;
      if (r.ok) { setGraph(r.data); setFailed(""); }
      else { setGraph({ nodes: [], edges: [] }); setFailed(r.status === 404 ? "" : r.error); }
    });
    return () => { alive = false; };
  }, [projectId, roleKey, days, mode]);

  const layout = useMemo(() => (graph ? layoutEgo(graph, roleKey, mode === "declared") : null),
                         [graph, roleKey, mode]);

  const pick = async (edge: Edge) => {
    setPicked({ key: edgeKey(edge), label: edge.label, acts: null });
    const acts = graph ? await actsAcross(projectId, graph, edge, days) : [];
    setPicked((p) => (p && p.key === edgeKey(edge) ? { ...p, acts } : p));
  };

  const empty = layout && !layout.edges.length;
  return (
    <div className="ag-lineage" data-testid="ag-lineage">
      <div className="ag-filters">
        <span className="ag-seg" role="group" aria-label="Mode">
          <button type="button" aria-pressed={mode === "observed"} onClick={() => setMode("observed")}>Observed</button>
          <button type="button" aria-pressed={mode === "declared"} onClick={() => setMode("declared")}>Declared</button>
        </span>
        <span className="ag-seg" role="group" aria-label="Window">
          {([7, 30] as const).map((d) => (
            <button key={d} type="button" aria-pressed={days === d} onClick={() => setDays(d)}>{d}d</button>
          ))}
        </span>
      </div>
      {failed ? <div className="ag-bad" role="alert">{failed}</div> : null}
      {!layout ? <div className="ag-muted">Reading…</div> : null}
      {empty ? (
        <div className="ag-empty" data-testid="ag-lineage-empty">
          <span>{mode === "observed" ? "No handoffs yet" : "No declared lines"}</span>
        </div>
      ) : null}
      {layout && !empty ? (
        <div className="ag-ln-stage" style={{ height: layout.height }}>
          <svg className="ag-ln-edges" viewBox={`0 0 ${W} ${layout.height}`} preserveAspectRatio="none" aria-hidden>
            {layout.edges.map((e) => {
              const mx = (e.a.x + e.b.x) / 2;
              const d = e.side === "top"
                ? `M${e.a.x} ${e.a.y} L${e.b.x} ${e.b.y}`
                : `M${e.a.x} ${e.a.y} C${mx} ${e.a.y} ${mx} ${e.b.y} ${e.b.x} ${e.b.y}`;
              return (
                <path key={`${e.from}>${e.to}`} d={d} className="ag-ln-edge"
                      data-dashed={e.reconstructed || e.side === "top" || undefined}
                      style={{ strokeWidth: e.width }} />
              );
            })}
          </svg>
          <NodeChip n={layout.center} actor={actor} center />
          {[...layout.left, ...layout.right, ...layout.top].map((n) => <NodeChip key={n.id} n={n} actor={actor} />)}
          {layout.edges.map((e) => (
            <button key={`l:${e.from}>${e.to}`} type="button" className="ag-ln-label"
                    data-testid={`ag-ln-edge-${e.from}-${e.to}`}
                    data-reconstructed={e.reconstructed || undefined}
                    aria-pressed={picked?.key === edgeKey(e)}
                    style={{ left: `${(((e.a.x + e.b.x) / 2) / W) * 100}%`, top: (e.a.y + e.b.y) / 2 }}
                    title={e.reconstructed ? "Rebuilt from older records; the handoff order may be approximate." : undefined}
                    onClick={() => void pick(e)}>
              {e.label}{e.reconstructed ? " · Reconstructed" : ""}
            </button>
          ))}
        </div>
      ) : null}
      {picked ? (
        <div className="ag-ln-acts" data-testid="ag-ln-acts">
          <div className="ag-day-head">{picked.label}</div>
          {picked.acts === null ? <span className="ag-muted">Reading…</span>
            : picked.acts.length ? (
              <ul className="ag-acts">
                {picked.acts.map((a) => (
                  <li key={a.actId} className="ag-act-row">
                    <span className="ag-act-label" data-user-text>{a.label}</span>
                    <span className="ag-act-edge">{a.edgeLabel}</span>
                  </li>
                ))}
              </ul>
            ) : <span className="ag-muted">None in this window</span>}
        </div>
      ) : null}
    </div>
  );
}
