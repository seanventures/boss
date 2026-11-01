"use client";

/**
 * An agent's answer with its citations as links (rulebook source G6). "Which rules protect the
 * loan balance?" is answered citing `[R:kgn_…]` / `[D:kgn_…]`; here each becomes a link named by
 * the cited rule or clause, and the answer ends with a row of what it cited, each opening the
 * item in the Knowledge Graph. Names come from GET /v1/knowledge/view/names (never a raw id).
 */
import React, { useEffect, useMemo, useState } from "react";

import { type CitedName, citedIdsIn, withCitationLinks } from "../../lib/answer-citations";
import { getNodeNames, kgNodeHref } from "../../lib/kg-view";
import { Markdown } from "../markdown";

export function CitedAnswer({ projectId, actor, text, className }: {
  projectId: string; actor: string; text: string; className?: string;
}) {
  const ids = useMemo(() => citedIdsIn(text), [text]);
  const key = ids.join(",");
  const [names, setNames] = useState<Record<string, CitedName>>({});
  useEffect(() => {
    if (!ids.length || !projectId) return undefined;
    let live = true;
    void getNodeNames({ projectId, actor }, ids).then((r) => { if (live && r.ok) setNames(r.data.names); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, projectId, actor]);
  const linked = useMemo(() => withCitationLinks(text, names, (id) => kgNodeHref(id)), [text, names]);
  const named = ids.filter((id) => names[id]);
  return (
    <>
      <Markdown className={className} text={linked} />
      {named.length ? (
        <div className="agent-cites" data-testid="agent-cites" aria-label="What this answer cites">
          <span className="agent-cites-label">Cited</span>
          {named.map((id) => (
            <a key={id} className="agent-cite" href={kgNodeHref(id)} data-testid="agent-cite" data-kind={names[id].kind}
               title={`${names[id].kind_label} · open in the Knowledge Graph`}>
              <span className="agent-cite-kind">{names[id].kind_label}</span>
              <span className="agent-cite-title" data-user-text>{names[id].title}</span>
            </a>
          ))}
        </div>
      ) : null}
    </>
  );
}
