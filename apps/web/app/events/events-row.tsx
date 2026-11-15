"use client";

/**
 * SETUP › EVENTS on the board (P9a-7): one line that says what wakes the agents and whether
 * anything is stuck, and the way in (Setup › Events, wireframe E1). Self-loading, like the ticket
 * systems row.
 */
import React from "react";
import { ArrowRight, Lightning } from "@phosphor-icons/react";

import { readConnections } from "../../lib/intake";
import { SectionLabel } from "../section-label";
import { goEvents, SourceMark, useRead } from "./parts";
import "./events.css";

export function EventsRow({ projectId }: { projectId: string }) {
  const { read } = useRead(() => readConnections(projectId), [projectId]);
  const items = read?.ok ? read.data : [];
  const dead = items.reduce((n, c) => n + c.dead, 0);
  return (
    <section className="su-row" data-testid="su-row-events" aria-label="Events" id="su-events">
      <div className="su-row-label">
        <SectionLabel as="span" tip="Slack, GitHub or any signed webhook wakes an agent by itself.">Events</SectionLabel>
      </div>
      <div className="su-row-body su-stack">
        <div className="ev-row-line">
          {items.length ? (
            <span className="ev-row-line" data-testid="su-events-sources">
              {items.slice(0, 5).map((c) => <SourceMark key={c.id} kind={c.kind} size="sm" />)}
              <span className="su-faint">{items.length} source{items.length === 1 ? "" : "s"}{dead ? ` · ${dead} dead letter${dead === 1 ? "" : "s"}` : " · healthy"}</span>
            </span>
          ) : <span className="su-faint"><Lightning size={13} aria-hidden /> What wakes your agents</span>}
          <span className="ev-spacer" />
          <button type="button" className="su-btn su-btn-sm" onClick={() => goEvents({ events: "connections" })} data-testid="su-events-open">
            Open Events <ArrowRight size={13} aria-hidden /></button>
        </div>
      </div>
    </section>
  );
}
