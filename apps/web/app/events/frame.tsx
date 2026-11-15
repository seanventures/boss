"use client";

/**
 * The frame every Events screen sits in (wireframes E1 E4 E5 E6 E7): the breadcrumb back to
 * Setup, the screen's title and one line, its ONE main action, and the five tabs. What the tabs
 * share (the project, its connections, its agents) is read once by EventsHome and handed down here.
 */
import React, { createContext, useContext, type ReactNode } from "react";
import { Plug, ListBullets, Pulse, XCircle, UsersThree } from "@phosphor-icons/react";

import type { Agent, Connection, Read } from "../../lib/intake";
import { goEvents, type EvTab } from "./parts";

export type EvShared = {
  projectId: string;
  /** The signed-in member the console acts as (agent writes carry it). */
  actor: string;
  conns: Read<Connection[]> | null;
  reloadConns: () => void;
  agents: Read<Agent[]> | null;
  dead: number;
  /** The connection a per-connection tab is on (`&conn=`), or the first one. */
  conn: string;
  ev: string;
  kind: string;
};
export const EvCtx = createContext<EvShared>({ projectId: "", actor: "", conns: null, reloadConns: () => {}, agents: null, dead: 0, conn: "", ev: "", kind: "" });
export const useEv = () => useContext(EvCtx);

/** The connections a screen can pick from, and the one picked (the URL's, else the first chat one, else the first). */
export function useConnPick(filter?: (c: Connection) => boolean): { list: Connection[]; picked: Connection | null } {
  const { conns, conn } = useEv();
  const list = (conns?.ok ? conns.data : []).filter((c) => (filter ? filter(c) : true));
  const picked = list.find((c) => c.id === conn) ?? list.find((c) => c.kind === "slack") ?? list[0] ?? null;
  return { list, picked };
}

const TABS: { id: EvTab; word: string; icon: ReactNode; tip: string }[] = [
  { id: "connections", word: "Connections", icon: <Plug size={14} aria-hidden />, tip: "Every source and whether it is healthy" },
  { id: "rules", word: "Rules", icon: <ListBullets size={14} aria-hidden />, tip: "Which agent wakes for what" },
  { id: "stream", word: "Stream", icon: <Pulse size={14} aria-hidden />, tip: "Everything that arrived, and what BOSS did" },
  { id: "dead", word: "Dead letters", icon: <XCircle size={14} aria-hidden />, tip: "Events that kept failing" },
  { id: "people", word: "People", icon: <UsersThree size={14} aria-hidden />, tip: "Who each chat user is in BOSS" },
];

export function EvFrame({ tab, title, sub, crumb, actions, children, testId }: {
  tab: EvTab; title: ReactNode; sub: string; crumb?: string; actions?: ReactNode; children: ReactNode; testId?: string;
}) {
  const { dead } = useEv();
  return (
    <div className="ev" data-testid={testId || `ev-${tab}`}>
      <div className="ev-head">
        <div className="ev-head-text">
          <nav className="ev-crumb" aria-label="Breadcrumb">
            <button type="button" onClick={() => goEvents({ events: null })} data-testid="ev-crumb-setup">Setup</button>
            <span aria-hidden>›</span>
            {crumb ? <><button type="button" onClick={() => goEvents({ events: "connections", kind: null })}>Events</button><span aria-hidden>›</span><span>{crumb}</span></> : <span>Events</span>}
          </nav>
          <h1 className="ev-h1">{title}</h1>
          <span className="ev-sub">{sub}</span>
        </div>
        {actions ? <div className="ev-head-actions">{actions}</div> : null}
      </div>
      {tab === "connect" ? null : (
        <div className="ev-tabs" role="tablist" aria-label="Events">
          {TABS.map((t) => (
            <button key={t.id} type="button" role="tab" className="ev-tab" aria-selected={t.id === tab} title={t.tip} data-testid={`ev-tab-${t.id}`}
                    onClick={() => goEvents({ events: t.id, ev: null })}>
              {t.icon}{t.word}
              {t.id === "dead" && dead > 0 ? <span className="ev-tab-count" data-tone="red">{dead}</span> : null}
            </button>
          ))}
        </div>
      )}
      {children}
    </div>
  );
}
