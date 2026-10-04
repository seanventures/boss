"use client";

/**
 * The Chain: who handed what to whom on this run, as a strip above its lane (spec §3.6).
 *
 *   You → Juno → Atlas → Forge ⇉ Vera, Rook → You → Pilot
 *
 * Hops are in the order the work happened. Parallel work stacks in one column (⇉). A step's own
 * inner work folds under it (`+5 inside Build`) and opens into its sub-chain; a repair loop is
 * said once (`Forge ↔ Rook ×3`). Every face opens its Act card. Every edge's words are the
 * server's edge label, on hover, with the scope that crossed it.
 *
 * LIVE, TRUTHFULLY: the hop doing work right now rings `working` (from its act's status), and an
 * edge that appears while you watch draws in once. Nothing moves that is not happening.
 */
import { CaretDown, CaretRight } from "@phosphor-icons/react";
import React, { useRef, useState } from "react";

import type { Act, Chain } from "../../lib/agent-world-types";
import { ActFace } from "./act-card";
import { chainItems, whoDid, type ChainItem, type Hop } from "./chain-model";
import { nameOf } from "./run-face";
import { useIdentityVersion } from "../../lib/use-agent-identities";

/** The tooltip on the edge into an act: the server's words, then what scope came with it. */
function edgeTip(acts: Act[]): string | undefined {
  const lines: string[] = [];
  for (const a of acts) {
    const scope = [...a.scope.skills, ...a.scope.mcp];
    const said = [a.edgeLabel, scope.length ? scope.join(", ") : ""].filter(Boolean).join(" · ");
    if (said) lines.push(acts.length > 1 ? `${nameOf(a.roleKey).name}: ${said}` : said);
  }
  return lines.length ? lines.join("\n") : undefined;
}

function Edge({ acts, parallel, fresh }: { acts: Act[]; parallel?: boolean; fresh: boolean }) {
  useIdentityVersion(); // names and faces below are resolved at render: repaint when identities load
  const reconstructed = acts.some((a) => a.reconstructed);
  return (
    <span className="ag-chain-edge" data-testid="ag-chain-edge" data-tip={edgeTip(acts)}
          data-new={fresh ? "true" : undefined} data-reconstructed={reconstructed ? "true" : undefined}
          data-edge-label={acts.map((a) => a.edgeLabel).join("|")}>
      <i className="ag-chain-line" aria-hidden />
      <span className="ag-chain-arrow" aria-hidden>{parallel ? "⇉" : "→"}</span>
    </span>
  );
}

function Name({ act, viewer }: { act: Act; viewer: string }) {
  useIdentityVersion(); // names and faces below are resolved at render: repaint when identities load
  const who = whoDid(act, viewer);
  const n = nameOf(who.roleKey, who.variant, who.personId);
  return <span className="ag-chain-name" data-tip={n.role !== n.name ? n.role : undefined}>{n.name}</span>;
}

function HopView({ hop, viewer, fresh, depth }: {
  hop: Hop; viewer: string; fresh: Set<string>; depth: number;
}) {
  const [open, setOpen] = useState(false);
  return (
    <span className="ag-chain-hop" data-testid="ag-chain-hop" data-stack={hop.acts.length > 1 ? "true" : undefined}
          data-roles={hop.acts.map((a) => a.roleKey ?? "").join(",")}>
      <span className="ag-chain-col">
        {hop.acts.map((a) => (
          <span className="ag-chain-who" key={a.actId}>
            <ActFace act={a} viewer={viewer} size={depth ? 20 : 24} />
            <Name act={a} viewer={viewer} />
          </span>
        ))}
      </span>
      {hop.insideCount > 0 ? (
        <span className="ag-chain-inside">
          <button type="button" className="ag-chain-fold" aria-expanded={open} data-testid="ag-chain-fold"
                  onClick={() => setOpen((v) => !v)}>
            {open ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />}
            +{hop.insideCount} inside {hop.insideOf || "this"}
          </button>
          {open ? (
            <span className="ag-chain-sub" data-testid="ag-chain-sub">
              <Items items={hop.inside} viewer={viewer} fresh={fresh} depth={depth + 1} />
            </span>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}

function Items({ items, viewer, fresh, depth }: {
  items: ChainItem[]; viewer: string; fresh: Set<string>; depth: number;
}) {
  return (
    <>
      {items.map((it, i) => {
        const acts = it.kind === "hop" ? it.acts : [it.a.acts[0]];
        const isNew = acts.some((a) => fresh.has(a.actId));
        return (
          <React.Fragment key={it.key}>
            {i > 0 || depth > 0 ? (
              <Edge acts={acts} parallel={it.kind === "hop" && it.acts.length > 1} fresh={isNew} />
            ) : null}
            {it.kind === "hop" ? (
              <HopView hop={it} viewer={viewer} fresh={fresh} depth={depth} />
            ) : (
              <span className="ag-chain-hop ag-chain-loop" data-testid="ag-chain-loop"
                    data-roles={`${it.a.acts[0].roleKey ?? ""},${it.b.acts[0].roleKey ?? ""}`}>
                <span className="ag-chain-who">
                  <ActFace act={lastOf(it.acts, it.a.acts[0])} viewer={viewer} size={depth ? 20 : 24} />
                  <Name act={it.a.acts[0]} viewer={viewer} />
                </span>
                <span className="ag-chain-swap" aria-label="back and forth">↔</span>
                <span className="ag-chain-who">
                  <ActFace act={lastOf(it.acts, it.b.acts[0])} viewer={viewer} size={depth ? 20 : 24} />
                  <Name act={it.b.acts[0]} viewer={viewer} />
                </span>
                <span className="ag-chain-rounds" data-tip={`${it.rounds} rounds`}>×{it.rounds}</span>
              </span>
            )}
          </React.Fragment>
        );
      })}
    </>
  );
}

/** The latest act by the same agent in a loop: its card is the one that says how it ended. */
function lastOf(acts: Act[], like: Act): Act {
  let last = like;
  for (const a of acts) if (a.roleKey === like.roleKey) last = a;
  return last;
}

export function RunChain({ chain, viewer, defaultOpen = true }: {
  chain: Chain | null; viewer: string; defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  // The acts already here when the strip first drew: only work that starts while you watch
  // draws its edge in.
  const seen = useRef<Set<string> | null>(null);
  if (!chain || chain.acts.length === 0) return null;
  if (seen.current === null) seen.current = new Set(chain.acts.map((a) => a.actId));
  const fresh = new Set(chain.acts.filter((a) => !seen.current!.has(a.actId)).map((a) => a.actId));
  const items = chainItems(chain, viewer);
  const agents = new Set(chain.acts.map((a) => a.roleKey).filter(Boolean)).size;
  return (
    <section className="ag-chain" data-testid="ag-chain" data-open={open ? "true" : undefined}
             aria-label="Who handed work to whom">
      <button type="button" className="ag-chain-toggle" aria-expanded={open} data-testid="ag-chain-toggle"
              data-tip="Who handed work to whom, in order" onClick={() => setOpen((v) => !v)}>
        {open ? <CaretDown size={11} weight="bold" aria-hidden /> : <CaretRight size={11} weight="bold" aria-hidden />}
        Chain
        <span className="ag-chain-count">{agents} {agents === 1 ? "agent" : "agents"}</span>
      </button>
      {open ? (
        <div className="ag-chain-strip" data-testid="ag-chain-strip">
          <Items items={items} viewer={viewer} fresh={fresh} depth={0} />
        </div>
      ) : null}
    </section>
  );
}
