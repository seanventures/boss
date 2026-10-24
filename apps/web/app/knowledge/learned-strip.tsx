"use client";

/**
 * LEARNED SO FAR (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §4.4): a strip in the build cockpit and the
 * workflow run view that lists what this run's agents have learned, and the rules and learnings
 * they consulted, as they arrive. Each item opens the Graph focused on it.
 *
 * It polls GET /v1/knowledge/view/live?run= only while it is on screen, and only while the run is
 * live or the feed says a run is writing ("an area off screen polls nothing"). Before the live
 * route exists (404) it draws nothing: a strip that is not there yet must not read as an error.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";

import { useOnScreen } from "../../lib/area-shown";
import { kindName, kindToken } from "../../lib/kg-layout";
import { getLive, kgNodeHref, kgWhen } from "../../lib/kg-view";
import type { KgLearningItem, KgLiveEdge, KgRule } from "../../lib/kg-view-types";
import "./kg.css";

type Item = { id: string; kind: string; title: string; step: string | null; at: string; how: "learned" | "consulted" | "reused" };

function merge(prev: Item[], learnings: KgLearningItem[], rules: KgRule[], edges: KgLiveEdge[], runIds: Set<string>): Item[] {
  const by = new Map(prev.map((i) => [i.id, i]));
  for (const l of learnings) {
    by.set(l.node.id, { id: l.node.id, kind: l.learning_kind, title: l.title, step: l.provenance.step ?? null, at: l.at, how: "learned" });
  }
  const titles = new Map<string, { kind: string; title: string }>();
  for (const r of rules) titles.set(r.node.id, { kind: "rule", title: r.title });
  for (const l of learnings) titles.set(l.node.id, { kind: l.learning_kind, title: l.title });
  for (const e of edges) {
    if ((e.kind !== "consulted" && e.kind !== "reused") || (e.run_id && runIds.size && !runIds.has(e.run_id))) continue;
    if (by.get(e.dst)?.how === "learned") continue;
    const t = titles.get(e.dst) ?? by.get(e.dst);
    by.set(e.dst, { id: e.dst, kind: t?.kind ?? "rule", title: t?.title ?? "an item in the graph", step: null, at: e.at, how: e.kind });
  }
  return [...by.values()].sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0));
}

/** The viewer's own zone, labelled (lib/kg-view.ts kgWhen). */
const clock = kgWhen;

export function LearnedStrip({ projectId, actor, runId, live, orgId }: {
  projectId: string; actor: string;
  /** The run: a build (code task run id) or a workflow run id. */
  runId: string;
  /** The run is still going: keep reading. */
  live: boolean;
  orgId?: string | null;
}) {
  const w = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const onScreen = useOnScreen();
  const [items, setItems] = useState<Item[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "absent" | "error">("loading");
  const [error, setError] = useState("");
  const [active, setActive] = useState(0);
  const cursor = useRef<string | null>(null);
  const delay = useRef(5000);
  const [tick, setTick] = useState(0);

  useEffect(() => { cursor.current = null; setItems([]); setState("loading"); }, [runId]);

  useEffect(() => {
    if (!onScreen) return undefined;
    let cancelled = false;
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    void getLive(w, { since: cursor.current, run: runId }, ctl?.signal).then((r) => {
      if (cancelled) return;
      if (!r.ok) {
        if (r.status === -1) return;
        if (r.status === 404) { setState("absent"); return; }
        setError(r.error);
        setState("error");
        return;
      }
      cursor.current = r.data.cursor;
      delay.current = Math.max(2000, r.data.poll_after_ms || 5000);
      setActive(r.data.active_runs);
      setItems((prev) => merge(prev, r.data.learnings, r.data.rules, r.data.edges, new Set([runId])));
      setState("ready");
    });
    return () => { cancelled = true; ctl?.abort(); };
  }, [w, runId, onScreen, tick]);

  // Read again only while the run (or some run) is writing, and only while on screen.
  useEffect(() => {
    if (!onScreen || state === "absent" || !(live || active > 0)) return undefined;
    const t = setTimeout(() => setTick((n) => n + 1), delay.current);
    return () => clearTimeout(t);
  }, [onScreen, state, live, active, tick]);

  if (state === "absent") return null;
  if (state === "loading" && !items.length) return null;
  if (state === "ready" && !items.length && !live) return null;
  const learned = items.filter((i) => i.how === "learned").length;
  const used = items.length - learned;
  return (
    <section className="kg-strip" data-testid="kg-learned-strip" aria-label="Learned so far" data-live={live || undefined}>
      <header className="kg-strip-head">
        <span className="kg-eyebrow">Learned so far</span>
        {items.length ? (
          <span className="kg-strip-count" data-testid="kg-learned-count">
            {learned ? `${learned} learned` : ""}{learned && used ? " · " : ""}{used ? `${used} consulted` : ""}
          </span>
        ) : null}
        {live ? <span className="kg-live" title="New items appear here as the agents record them"><span className="kg-live-dot" aria-hidden />live</span> : null}
        <span className="ag-spacer" />
        <a className="inf-link" href={`/?${orgId ? `org=${encodeURIComponent(orgId)}&` : ""}area=knowledge`} data-testid="kg-strip-graph">Open the Graph</a>
      </header>
      {state === "error" ? (
        <span className="kg-inline-err" role="alert" data-testid="kg-strip-error" title={error}>
          Couldn&apos;t load{" "}
          <button type="button" className="inf-link" onClick={() => setTick((n) => n + 1)}>Retry</button>
        </span>
      ) : null}
      {!items.length && state === "ready" ? (
        <span className="kg-quiet" data-testid="kg-strip-empty" title="What the agents rule out, find and fix appears here as they record it">Nothing learned yet</span>
      ) : null}
      {items.length ? (
        <ul className="kg-strip-list" aria-live="polite">
          {items.map((i) => (
            <li key={i.id}>
              <a className="kg-strip-item" href={kgNodeHref(i.id, orgId)} data-testid="kg-learned-item" data-how={i.how}
                 title={`${kindName(i.kind)} · open it in the Graph`}>
                <span className="kg-dot" style={{ background: `var(${kindToken(i.kind)})` }} aria-hidden />
                <span className="kg-strip-how">{i.how === "learned" ? kindName(i.kind) : i.how === "reused" ? "Reused" : "Consulted"}</span>
                <span className="kg-strip-title" data-user-text>{i.title}</span>
                <span className="kg-strip-when">{[i.step, clock(i.at)].filter(Boolean).join(" · ")}</span>
              </a>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
