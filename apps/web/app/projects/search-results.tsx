"use client";

/**
 * ⌘K, "FOUND IN" (BOSSAPP-881): below the jump rows, what the query matches INSIDE the open
 * project's work — a PRD's text, a decision's summary, a build's instructions, an artifact, a work
 * item, what agents wrote into the knowledge graph (GET /v1/search, only the sources the viewer
 * may read). Each row is a palette item (↑/↓ and ↵ reach it like the others): what it is, its
 * title, and the words around the match.
 */
import React, { useEffect, useState } from "react";
import { MagnifyingGlass } from "@phosphor-icons/react";

import { isPxDemo, navigateTo, pxHref } from "../../lib/project-experience";
import { SEARCH_KIND_WORDS, searchWork, type SearchHit } from "../../lib/work-ux";

export function SearchResults({ query, projectId, onDone }: { query: string; projectId: string; onDone: () => void }) {
  const q = query.trim();
  const [hits, setHits] = useState<SearchHit[]>([]);
  useEffect(() => {
    if (q.length < 2 || !projectId || isPxDemo()) { setHits([]); return undefined; }
    const ctl = new AbortController();
    const t = window.setTimeout(() => {
      void searchWork(projectId, q, ctl.signal).then((r) => { if (!ctl.signal.aborted) setHits(r.ok && r.data ? r.data.items : []); });
    }, 220);
    return () => { window.clearTimeout(t); ctl.abort(); };
  }, [q, projectId]);
  if (!hits.length) return null;
  return (
    <div className="pxj-g" role="group" aria-label="Found in" data-testid="palette-search">
      <span className="pxj-lbl">Found in</span>
      {hits.map((h) => (
        <button key={`${h.kind}-${h.id}`} type="button" className="bx-palette-item pxj-i" data-testid={`palette-search-${h.kind}`}
                title={h.snippet || undefined} onClick={() => { onDone(); navigateTo(pxHref(h.href)); }}>
          <span className="pxj-ico" data-c="blue" aria-hidden><MagnifyingGlass size={14} /></span>
          <span className="pxj-t">{h.title}{h.match === "body" && h.snippet ? <span className="pxj-snip"> · {h.snippet}</span> : null}</span>
          <span className="pxj-k">{SEARCH_KIND_WORDS[h.kind] ?? h.kind}</span>
        </button>
      ))}
    </div>
  );
}
