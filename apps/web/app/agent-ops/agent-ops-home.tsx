"use client";

/**
 * AGENTS AT WORK (design §11.3, spec §9 stream 906), what BOSS's agents noticed, how they worked
 * it out together, and how it ended, as diagrams.
 *
 *   Agents at work   ● On          [Work | Roadmap]          [Check now] [Ask BOSS]
 *   ( 86% accepted )( 13% noise )( 4m MTTR )( $0.63/item )( 38m you answer )( ▁▃▅▇ decisions/day )
 *   [Needs you 1] [Running 2] [Resolved by agents 2] [Overridden 1] [Failed 1]
 *   ┌ 🔔 → (CoS) → (SRE) → ◇ → ✓ ┐  ┌ 🔔 → (SRE) → ◇ → ! ┐   ← each item a small diagram
 *   │ order-api 5xx, Incident, Resolved by agents, A2, $1.84 │
 *
 *   ?item=<id> opens one item as the full flow chart (left → right), with Take over / Dismiss and
 *   "What did you do and why?" for Ask BOSS; a node opens the drawer, the only place text lives.
 *   ?act=<id> (an Ask BOSS citation) opens the item that decision belongs to, on that decision.
 *
 * The acceptance test (§11.0 rule 6): a stranger says what happened, who did it and how it ended
 * within 10 seconds, without reading a paragraph.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { GatedButton, useGate } from "../../components/gated";
import { useConsoleLive } from "../../lib/console-live";
import { decisionHref } from "../../lib/command-center";
import { openDockAbout } from "../../lib/dock-subject";
import { usePolledRead } from "../../lib/log-stream";
import {
  AREA, MISSING, TABS, TAB_WORDS, aaRead, decisionPath, demoStatusPath, dismissItem, flowHref, isDemo, itemPath, listPath,
  loadDemo, messagesPath, parseDecision, parseDemoLoad, parseDemoStatus, parseGraph, parseList, parseMessages, parseScorecard,
  parseSettings, polled, roleOfParty, scorecardPath, settingsPath, sweepNow, takeOver, type Graph, type ListItem, type Tab,
} from "../../lib/ambient";
import { AgentAvatar } from "../agent-avatar";
import { showToast } from "../client-forms";
import { ItemBadgeRow } from "./badges";
import { NodeDrawer, type Target } from "./drawer";
import { FlowCanvas } from "./flow-canvas";
import { FlowThumb } from "./flow-thumb";
import { Roadmap } from "./roadmap";
import { ScorecardStrip } from "./scorecard";
import "./agent-ops.css";

export const LIST_POLL_MS = 10_000;
export const ITEM_POLL_MS = 5_000;

type Place = { view: "work" | "roadmap"; tab: Tab; item: string | null; act: string | null };

function readPlace(): Place {
  const q = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search);
  const tab = q.get("aat") as Tab | null;
  return { view: q.get("aav") === "roadmap" ? "roadmap" : "work", tab: tab && (TABS as readonly string[]).includes(tab) ? tab : "needs_you",
           item: q.get("item"), act: q.get("act") };
}
function writePlace(p: Place): void {
  try {
    const q = new URLSearchParams(window.location.search);
    q.set("area", AREA);
    const set = (k: string, v: string | null) => (v ? q.set(k, v) : q.delete(k));
    set("aav", p.view === "roadmap" ? "roadmap" : null);
    set("aat", p.tab === "needs_you" ? null : p.tab);
    set("item", p.item);
    set("act", p.act);
    window.history.replaceState(null, "", `${window.location.pathname}?${q.toString()}`);
  } catch { /* in-memory state still moves */ }
}

/** "What did you do and why?", Ask BOSS answers from the ledger with [act:…] citations (907). */
export function askAboutItem(item: { title: string; id: string } | null): void {
  openDockAbout({
    // No `work_item` dock subject yet (it needs an engine resolver, stream 907): the item's id
    // rides in the question so the ledger answer can cite [act:…] rows for it.
    subject: null,
    question: item ? `What did your agents do about "${item.title}" (${item.id}), and why?` : "What did your agents do today, and why?",
    role: null, history: "push",
  });
}

function ItemCard({ it, onOpen }: { it: ListItem; onOpen: () => void }) {
  const live = it.badges.outcome === "running" || it.badges.outcome === "needs_you";
  return (
    <button type="button" className="aa-card" data-testid="aa-item" data-item={it.id} data-outcome={it.badges.outcome} onClick={onOpen}
            title="Open the flow chart">
      <FlowThumb strip={it.strip} live={live} />
      <span className="aa-card-title" data-user-text>{it.title}</span>
      <ItemBadgeRow b={it.badges} compact />
    </button>
  );
}

function StopPill({ projectId }: { projectId: string }) {
  const live = useConsoleLive();
  const settings = usePolledRead({ key: settingsPath(projectId), read: polled(settingsPath(projectId), parseSettings), pollMs: 30_000, paused: !live, enabled: !!projectId });
  const stop = settings.data?.stop;
  if (!stop) return null;
  const href = `/?${new URLSearchParams({ area: "budget", tab: "autonomy" }).toString()}`;
  return (
    <a className="aa-pill" href={href} data-tone={stop.stopped ? "red" : "green"} data-testid="aa-stop-pill"
       title={stop.stopped ? `Stopped${stop.by ? ` by ${stop.by}` : ""}${stop.reason ? `, ${stop.reason}` : ""}. Open Autonomy & safety.` : "Agents may act within their levels. Open Autonomy & safety."}>
      <span className="aa-dot-live" aria-hidden />{stop.stopped ? "Stopped" : "On"}
    </a>
  );
}

const LOADER_WORDS: Record<string, string> = {
  morning: "morning brief", order_api_incident: "Order API incident", squad_cve: "CVE squad", roadmap: "roadmap",
};

/** "Load demo": seeds the sample stories into a DEMO project. Shown only when the API says this
 *  project can take them AND the signed-in person may manage autonomy (owner / admin). */
export function LoadDemoButton({ projectId, onLoaded }: { projectId: string; onLoaded: () => void }) {
  const gate = useGate("autonomy:manage", projectId);
  const status = usePolledRead({ key: demoStatusPath(projectId), read: polled(demoStatusPath(projectId), parseDemoStatus),
                                 pollMs: null, paused: false, enabled: !!projectId && gate.allowed && !isDemo() });
  const [busy, setBusy] = useState(false);
  if (!gate.allowed || !status.data?.available) return null;
  const what = status.data.loaders.map((l) => LOADER_WORDS[l] ?? l.replace(/_/g, " ")).join(", ");
  const press = async () => {
    setBusy(true);
    const r = await loadDemo(projectId);
    setBusy(false);
    if (!r.ok) { showToast("error", `Not loaded: ${r.error}`); return; }
    const d = parseDemoLoad(r.body);
    const skipped = Object.keys(d.skipped);
    showToast("success", `Demo loaded: ${d.loaded.length}${skipped.length ? `, skipped ${skipped.map((k) => LOADER_WORDS[k] ?? k).join(", ")}` : ""}`);
    onLoaded();
  };
  return (
    <button type="button" className="ag-btn aa-demo-load" data-testid="aa-load-demo" disabled={busy}
            title={`Add sample work to this demo project${what ? `: ${what}` : ""}`} onClick={() => void press()}>
      {busy ? "Loading…" : "Load demo"}
    </button>
  );
}

function ItemView({ projectId, orgId, id, focusAct, onBack }: { projectId: string; orgId?: string | null; id: string; focusAct: string | null; onBack: () => void }) {
  const live = useConsoleLive();
  const [target, setTarget] = useState<Target | null>(null);
  const [dismissing, setDismissing] = useState(false);
  const [reason, setReason] = useState("");
  const graphRead = usePolledRead<Graph>({ key: itemPath(projectId, id), read: polled(itemPath(projectId, id), parseGraph), pollMs: ITEM_POLL_MS, paused: !live, enabled: !!projectId });
  const msgs = usePolledRead({ key: messagesPath(projectId, id), read: polled(messagesPath(projectId, id), parseMessages), pollMs: ITEM_POLL_MS * 2, paused: !live, enabled: !!projectId });
  const g = graphRead.data && graphRead.data.item.id === id ? graphRead.data : null;
  // A citation lands on its decision.
  useEffect(() => {
    if (!g || !focusAct || target) return;
    const node = g.nodes.find((n) => n.id === `decision:${focusAct}`);
    if (node) setTarget({ node });
  }, [g, focusAct, target]);
  const reload = () => { graphRead.reload(); msgs.reload(); };
  const closed = g ? ["resolved", "failed", "dismissed"].includes(g.item.lifecycle) : true;
  const owner = roleOfParty(g?.item.lease_owner);

  return (
    <section className="aa-item" data-testid="aa-item-view" data-lifecycle={g?.item.lifecycle}>
      <header className="aa-item-h">
        <button type="button" className="aa-back" onClick={onBack} data-testid="aa-back">← All work</button>
        <div className="aa-item-title">
          {owner ? <AgentAvatar agent={owner} size={28} aiBadge={false} state={g?.item.lifecycle === "needs_you" ? "needs_you" : "working"} /> : null}
          <h2 data-user-text>{g?.item.title ?? "…"}</h2>
        </div>
        {g ? <ItemBadgeRow b={g.badges} /> : null}
        <span className="aa-spacer" />
        <button type="button" className="ag-btn" data-testid="aa-ask-why" title="Ask BOSS what the agents did and why, answered from the decision ledger"
                onClick={() => askAboutItem(g ? { id, title: g.item.title } : null)}>Why?</button>
        {!closed ? (
          <>
            <GatedButton permission="work_item:manage" projectId={projectId} className="ag-btn" data-testid="aa-take-over"
                         title="Take it from the agents; they stop at once"
                         onClick={() => void takeOver(projectId, id).then((r) => { showToast(r.ok ? "success" : "error", r.ok ? "It's yours, agents stopped on it" : `Not taken: ${r.error}`); reload(); })}>
              Take over
            </GatedButton>
            <GatedButton permission="work_item:manage" projectId={projectId} className="ag-btn" data-testid="aa-dismiss"
                         title="Not worth doing; BOSS cools down on this problem" onClick={() => setDismissing(true)}>Dismiss</GatedButton>
          </>
        ) : null}
      </header>
      {dismissing ? (
        <form className="aa-form" data-testid="aa-dismiss-form"
              onSubmit={(e) => { e.preventDefault(); void dismissItem(projectId, id, reason.trim()).then((r) => { showToast(r.ok ? "success" : "error", r.ok ? "Dismissed" : `Not dismissed: ${r.error}`); setDismissing(false); reload(); }); }}>
          <input aria-label="Why dismiss" placeholder="Why? (one line)" value={reason} onChange={(e) => setReason(e.target.value)} required maxLength={500} />
          <GatedButton permission="work_item:manage" projectId={projectId} type="submit" className="ag-btn danger" disabled={!reason.trim()}>Dismiss</GatedButton>
          <button type="button" className="ag-btn" onClick={() => setDismissing(false)}>Cancel</button>
        </form>
      ) : null}
      <div className="aa-stage" data-drawer={target ? "open" : undefined}>
        {graphRead.error && !g ? <div className="aa-empty"><b>Can't draw this item</b><span>{graphRead.error === MISSING ? "It is not on this project." : graphRead.error}</span></div> : null}
        {g ? (
          <FlowCanvas nodes={g.nodes} edges={g.edges} lifecycle={g.item.lifecycle}
                      selected={target?.node?.id ?? null} onSelect={setTarget}
                      onAnswer={g.item.needs_you_decision_id ? () => { window.location.href = decisionHref(g.item.needs_you_decision_id!, orgId); } : undefined} />
        ) : null}
        {g && target ? (
          <NodeDrawer target={target} graph={g} messages={msgs.data ?? []} projectId={projectId} orgId={orgId}
                      onClose={() => setTarget(null)} onChanged={reload} />
        ) : null}
      </div>
      <Legend />
    </section>
  );
}

export function Legend() {
  return (
    <div className="aa-legend-row" aria-label="Legend" data-testid="aa-legend">
      <span><i data-type="signal" />Signal</span><span><i data-type="agent" />Agent</span><span><i data-type="action" />Proposal</span>
      <span><i data-type="decision" />Decision</span><span><i data-type="run" />Work</span><span><i data-type="needs_you" />Needs you</span>
      <span><i data-type="outcome" />Outcome</span><span><i data-type="dissent" />Dissent</span>
    </div>
  );
}

export function AgentOpsHome({ projectId, orgId }: { projectId: string; actor?: string; orgId?: string | null }) {
  const live = useConsoleLive();
  const [place, setPlaceState] = useState<Place>({ view: "work", tab: "needs_you", item: null, act: null });
  const [arrived, setArrived] = useState(false);
  useEffect(() => { setPlaceState(readPlace()); setArrived(true); }, []);
  const setPlace = useCallback((patch: Partial<Place>) => {
    setPlaceState((p) => { const next = { ...p, ...patch }; writePlace(next); return next; });
  }, []);
  // ?act= resolves to the item it belongs to.
  useEffect(() => {
    if (!place.act || place.item || !projectId) return;
    void aaRead(decisionPath(projectId, place.act), parseDecision).then((r) => {
      if (r.ok && r.data?.work_item_id) setPlace({ item: r.data.work_item_id });
    });
  }, [place.act, place.item, projectId, setPlace]);

  const list = usePolledRead({ key: listPath(projectId, place.tab), read: polled(listPath(projectId, place.tab), parseList),
                               pollMs: LIST_POLL_MS, paused: !live, enabled: arrived && !!projectId && place.view === "work" });
  const score = usePolledRead({ key: scorecardPath(projectId), read: polled(scorecardPath(projectId), parseScorecard),
                                pollMs: 60_000, paused: !live, enabled: arrived && !!projectId });
  // The list starts on Needs you; with nothing there, it moves to the first tab that has work.
  const counts = list.data?.counts ?? {};
  const [autoTabbed, setAutoTabbed] = useState(false);
  useEffect(() => {
    if (autoTabbed || !list.data || place.tab !== "needs_you" || readPlace().tab !== "needs_you") return;
    setAutoTabbed(true);
    if (!counts.needs_you) {
      const first = TABS.find((t) => counts[t]);
      if (first) setPlace({ tab: first });
    }
  }, [list.data, autoTabbed, counts, place.tab, setPlace]);
  const items = useMemo(() => list.data?.items ?? [], [list.data]);
  const [roadmapRefresh, setRoadmapRefresh] = useState(0);
  const { reload: reloadList } = list;
  const { reload: reloadScore } = score;
  const afterDemo = useCallback(() => { reloadList(); reloadScore(); setRoadmapRefresh((x) => x + 1); }, [reloadList, reloadScore]);
  const missing = list.error === MISSING;
  const demo = isDemo();
  const demoHref = useMemo(() => {
    if (typeof window === "undefined") return "?area=agent_ops&aa_demo=1";
    const q = new URLSearchParams(window.location.search); q.set("area", AREA); q.set("aa_demo", "1"); return `?${q.toString()}`;
  }, []);

  return (
    <div className="aa" data-testid="agent-ops">
      <header className="aa-head">
        <h2>Agents at work</h2>
        <StopPill projectId={projectId} />
        {demo ? <span className="aa-pill" data-tone="amber" data-testid="aa-sample" title="Sample data, not your project">Sample data</span> : null}
        <span className="aa-views" role="tablist" aria-label="View">
          {(["work", "roadmap"] as const).map((v) => (
            <button key={v} type="button" role="tab" aria-selected={place.view === v} className="aa-view" data-testid={`aa-view-${v}`}
                    title={v === "work" ? "What agents did, as diagrams" : "What agents recommend you build next"}
                    onClick={() => setPlace({ view: v, item: null, act: null })}>{v === "work" ? "Work" : "Roadmap"}</button>
          ))}
        </span>
        <span className="aa-spacer" />
        <LoadDemoButton projectId={projectId} onLoaded={afterDemo} />
        <GatedButton permission="work_item:manage" projectId={projectId} className="ag-btn" data-testid="aa-check-now"
                     title="Look for new problems now"
                     onClick={() => void sweepNow(projectId).then((r) => { showToast(r.ok ? "success" : "error", r.ok ? "Checked, new problems appear here" : `Not checked: ${r.error}`); list.reload(); })}>
          Check now
        </GatedButton>
        <button type="button" className="ag-btn" data-testid="aa-ask" title="Ask BOSS what the agents did and why" onClick={() => askAboutItem(null)}>Ask BOSS</button>
      </header>

      {place.view === "roadmap" ? <Roadmap projectId={projectId} orgId={orgId} refresh={roadmapRefresh} /> : place.item ? (
        <ItemView projectId={projectId} orgId={orgId} id={place.item} focusAct={place.act} onBack={() => setPlace({ item: null, act: null })} />
      ) : (
        <>
          <ScorecardStrip card={score.data} />
          {missing ? (
            <div className="aa-empty" data-testid="aa-missing">
              <b>Autonomous agents aren't on this install yet</b>
              <span>Upgrade BOSS to see agents notice and fix problems on their own.</span>
              <a className="ag-btn" href={demoHref} data-testid="aa-see-sample">See a sample</a>
            </div>
          ) : (
            <>
              <nav className="aa-tabs" role="tablist" aria-label="Work by outcome">
                {TABS.map((t) => (
                  <button key={t} type="button" role="tab" aria-selected={place.tab === t} className="aa-tab" data-tab={t} data-testid={`aa-tab-${t}`}
                          title={TAB_WORDS[t].tip} onClick={() => setPlace({ tab: t })}>
                    {TAB_WORDS[t].label}{counts[t] ? <span className="aa-count" data-tone={t === "needs_you" ? "amber" : undefined}>{counts[t]}</span> : null}
                  </button>
                ))}
              </nav>
              {list.data && !items.length ? (
                <div className="aa-empty" data-testid="aa-list-empty"><b>Nothing here</b><span>{TAB_WORDS[place.tab].tip}.</span></div>
              ) : (
                <div className="aa-grid" data-testid="aa-list">
                  {items.map((it) => <ItemCard key={it.id} it={it} onOpen={() => setPlace({ item: it.id, act: null })} />)}
                </div>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}

export { flowHref };
