"use client";

/**
 * SETUP › EVENTS (P9a-7, BOSSAPP-1099): what wakes your agents. One place under Setup with five
 * tabs, Connections (E1) · Rules (E4) · Stream (E5) · Dead letters (E6) · People (E7), and the
 * Connect Slack page (E2). Lives in the Setup area at `?area=setup&events=<tab>` (`&conn=` the
 * connection a rules or people tab is on, `&ev=` the open event), so Back and a shared link land
 * on the same screen; the breadcrumb's Setup goes back to the board.
 *
 * What every tab needs is read once here (the project's connections and its deployed agents)
 * and handed down through EvCtx; each tab reads the rest itself.
 */
import React, { useEffect, useMemo } from "react";

import { readAgents, readConnections } from "../../lib/intake";
import { SetupBoard } from "../setup/setup-board";
import { ConnectSlack } from "./connect-slack";
import { ConnectTeams } from "./connect-teams";
import { ConnectionsTab } from "./connections";
import { DeadLettersTab } from "./dead-letters";
import { EvCtx, type EvShared } from "./frame";
import { PeopleTab } from "./people";
import { RulesTab } from "./rules";
import { StreamTab } from "./stream";
import { goEvents, useEvPlace, useRead, type EvPlace } from "./parts";
import "./events.css";

export function EventsHome({ projectId, actor = "", place }: { projectId: string; actor?: string; place: EvPlace }) {
  const conns = useRead(() => readConnections(projectId), [projectId]);
  const agents = useRead(() => readAgents(projectId), [projectId]);
  const dead = conns.read?.ok ? conns.read.data.reduce((n, c) => n + c.dead, 0) : 0;
  const shared: EvShared = useMemo(() => ({
    projectId, actor, conns: conns.read, reloadConns: conns.reload, agents: agents.read, dead, conn: place.conn, ev: place.ev, kind: place.kind,
  }), [projectId, actor, conns.read, conns.reload, agents.read, dead, place.conn, place.ev, place.kind]);
  // A Connect page for a kind that has its own page only (Slack or Teams); anything else opens the sheet.
  // (Read from the address itself: the first render's place has no `kind` yet.)
  const connectKind = place.kind || (typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("kind") || "" : "");
  useEffect(() => {
    if (place.tab !== "connect") return;
    const k = new URLSearchParams(window.location.search).get("kind");
    if (k !== "slack" && k !== "teams") goEvents({ events: "connections", kind: null }, "replace");
  }, [place.tab, place.kind]);
  return (
    <EvCtx.Provider value={shared}>
      {place.tab === "connect" ? (connectKind === "teams" ? <ConnectTeams /> : <ConnectSlack />)
        : place.tab === "rules" ? <RulesTab />
        : place.tab === "stream" ? <StreamTab />
        : place.tab === "dead" ? <DeadLettersTab />
        : place.tab === "people" ? <PeopleTab />
        : <ConnectionsTab />}
    </EvCtx.Provider>
  );
}

/**
 * THE SETUP AREA: the board, or Events when the URL says `events=`. `initialEvents` is what the
 * server saw in the URL, so the first paint is already the right screen.
 */
export function SetupArea({ projectId, actor = "", initialEvents = "" }: { projectId: string; actor?: string; initialEvents?: string }) {
  const place = useEvPlace(initialEvents);
  if (place.tab) return <EventsHome projectId={projectId} actor={actor} place={place} />;
  return <SetupBoard projectId={projectId} actor={actor} layout="setup" />;
}
