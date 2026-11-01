"use client";

/**
 * BY WORDS AND BY MEANING, SIDE BY SIDE (rulebook source G9). The same query two ways: the words
 * column is the Graph's own search (names and the words of names and bodies); the meaning column
 * is the embedding model's nearest items alone — no words. An item only meaning found is marked,
 * so a viewer sees what a keyword search misses ("What happens when a payment is short?" finds the
 * rule that says "a payment less than a periodic payment").
 */
import React, { useEffect, useState } from "react";
import { X } from "@phosphor-icons/react";

import { getSearchCompare } from "../../lib/kg-view";
import type { KgSearchCompareResponse, KgSearchHit } from "../../lib/kg-view-types";

type Who = { projectId: string; actor: string };

function Column({ title, testId, hits, only, empty, onPick }: {
  title: string; testId: string; hits: KgSearchHit[]; only: Set<string>; empty: string; onPick: (hit: KgSearchHit) => void;
}) {
  return (
    <section className="kg-cmp-col" data-testid={testId} aria-label={title}>
      <h4 className="kg-cmp-title">{title} <span className="kg-quiet">{hits.length}</span></h4>
      {hits.length ? (
        <ol className="kg-cmp-list">
          {hits.map((h) => (
            <li key={h.node.id}>
              <button type="button" className="kg-cmp-hit" data-testid="kg-cmp-hit" data-only={only.has(h.node.id) || undefined}
                      onClick={() => onPick(h)} title={h.node.label}>
                <span className="kg-dot" data-layer={h.node.layer ?? undefined} aria-hidden />
                <span className="kg-cmp-label" data-user-text>{h.node.label}</span>
                {only.has(h.node.id) ? <span className="kg-chip kg-cmp-only" data-testid="kg-cmp-only">only by meaning</span> : null}
              </button>
            </li>
          ))}
        </ol>
      ) : <p className="kg-quiet">{empty}</p>}
    </section>
  );
}

export function SearchCompare({ w, q, layers, onPick, onClose }: {
  w: Who; q: string; layers?: string; onPick: (hit: KgSearchHit) => void; onClose: () => void;
}) {
  const [data, setData] = useState<KgSearchCompareResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setData(null); setError(null);
    void getSearchCompare(w, q, { layers, limit: 10 }).then((r) => {
      if (!live) return;
      if (r.ok) setData(r.data); else setError(r.error);
    });
    return () => { live = false; };
  }, [w, q, layers]);
  const only = new Set(data?.only_by_meaning ?? []);
  return (
    <div className="kg-cmp" role="dialog" aria-label={`“${q}” by words and by meaning`} data-testid="kg-search-compare">
      <div className="kg-cmp-head">
        <span className="kg-eyebrow">“<span data-user-text>{q}</span>” by words and by meaning</span>
        <button type="button" className="ag-icon-btn" aria-label="Close the comparison" onClick={onClose}><X size={12} aria-hidden /></button>
      </div>
      {error ? <p className="kg-inline-err" role="alert">{error}</p> : null}
      {!data && !error ? <p className="kg-quiet" aria-busy="true">Searching both ways…</p> : null}
      {data ? (
        <>
          <div className="kg-cmp-cols">
            <Column title="By words" testId="kg-cmp-words" hits={data.words} only={new Set()} onPick={onPick}
                    empty="No name or text holds these words." />
            <Column title="By meaning" testId="kg-cmp-meaning" hits={data.meaning} only={only} onPick={onPick}
                    empty={data.reason ?? "Nothing near in meaning."} />
          </div>
          <p className="kg-quiet" data-testid="kg-cmp-summary">
            {data.meaning.length
              ? only.size
                ? `${only.size} of the ${data.meaning.length} found by meaning share no searched words with the query.`
                : "Meaning found nothing the words did not."
              : data.reason ?? ""}
          </p>
        </>
      ) : null}
    </div>
  );
}
