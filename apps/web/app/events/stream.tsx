"use client";

/**
 * SETUP › EVENTS › STREAM (P9a-7, wireframe E5): everything that arrived, and what BOSS did with it.
 * Status chips with the period's counts (each one filters), search, source and period; the list
 * newest first, live (a quiet re-read every few seconds while the tab is visible; Pause stops it).
 * Opening an event draws it as a FLOW (event-flow.tsx): source → checked → saved → rule → for whom
 * → run → reply, with where it stopped in red. Guards, identity and the raw event are behind
 * Details.
 */
import React, { useEffect, useMemo, useState } from "react";
import { CaretRight, MagnifyingGlass, Pause, Play, Pulse, ShieldCheck, Tray } from "@phosphor-icons/react";

import {
  STATUS_CHIPS, eventFlow, labelOfKind, methodWords, placeOf, readEvent, readEvents, readRunStatus,
  type EventDetail, type EventList, type EventSummary, type Read,
} from "../../lib/intake";
import { EvFrame, useEv } from "./frame";
import { EventFlowView } from "./event-flow";
import { Chip, Empty, Face, ReadState, SourceMark, StatusChip, goEvents } from "./parts";

const PERIODS: { hours: number; word: string }[] = [{ hours: 1, word: "Last hour" }, { hours: 24, word: "Last 24 hours" }, { hours: 168, word: "Last 7 days" }];
const LIVE_MS = 5000;
const clock = (iso: string) => {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const d = new Date(t);
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((x) => String(x).padStart(2, "0")).join(":");
};
const secs = (a: string, b: string) => {
  const x = Date.parse(a), y = Date.parse(b);
  return Number.isFinite(x) && Number.isFinite(y) && x >= y ? (x - y) / 1000 : null;
};
const fmtSecs = (s: number) => (s < 10 ? `${s.toFixed(2)} s` : s < 120 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);

/** "Priya in #support", "GitHub · payments-api", from what the event says about itself. */
export function eventTitle(e: EventSummary): string {
  const who = e.actor.display || e.actor.external_id;
  if (e.kind === "slack" || e.kind === "teams") return `${who || "Someone"} in ${placeOf(e)}`;
  // A record's subject often repeats its connection ("org/repo" · "org/repo@refs/heads/main"):
  // say the connection once, then what happened.
  const what = e.subject && !e.subject.startsWith(e.connection_name) ? e.subject : labelOfKind(e.kind);
  return [e.connection_name, what].filter(Boolean).join(" · ") || e.type;
}

export function StreamTab() {
  const { projectId, conns, ev } = useEv();
  const [status, setStatus] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [hours, setHours] = useState(24);
  const [live, setLive] = useState(true);
  const [read, setRead] = useState<Read<EventList> | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => { const t = window.setTimeout(() => setQuery(q.trim()), 350); return () => window.clearTimeout(t); }, [q]);
  useEffect(() => {
    let alive = true;
    void readEvents(projectId, { status: status.join(","), q: query, connection_id: source, hours, limit: 100 }).then((r) => { if (alive) setRead(r); });
    return () => { alive = false; };
  }, [projectId, status, query, source, hours, tick]);
  useEffect(() => {
    if (!live) return;
    const t = window.setInterval(() => { if (document.visibilityState === "visible") setTick((x) => x + 1); }, LIVE_MS);
    return () => window.clearInterval(t);
  }, [live]);

  const list = read?.ok ? read.data : null;
  const toggle = (st: string) => setStatus((cur) => (cur.includes(st) ? cur.filter((x) => x !== st) : [...cur, st]));
  const chips = STATUS_CHIPS.filter((c) => ["routed", "ignored", "filtered", "refused", "dead", "digest"].includes(c.status) || (list?.counts[c.status] ?? 0) > 0);
  const items = list?.events ?? [];
  const sources = conns?.ok ? conns.data : [];

  return (
    <EvFrame tab="stream" title="Event stream" sub="Everything that arrived, and what BOSS did with it."
             actions={<>
               <Chip tone={live ? "green" : "grey"} tip={live ? "New events appear as they arrive" : "Paused: the list stays as it is"} testId="ev-live">
                 <Pulse size={12} aria-hidden />{live ? "Live" : "Paused"}</Chip>
               <button type="button" className="ev-btn" data-size="sm" onClick={() => setLive((v) => !v)} data-testid="ev-live-toggle" aria-pressed={!live}>
                 {live ? <><Pause size={13} aria-hidden /> Pause stream</> : <><Play size={13} aria-hidden /> Resume</>}</button>
             </>}>
      <div className="ev-chips" role="group" aria-label="Filter by what happened" data-testid="ev-status-chips">
        {chips.map((c) => (
          <button key={c.status} type="button" className="ev-chip" data-tone={c.tone} title={c.tip} aria-pressed={status.includes(c.status)}
                  onClick={() => toggle(c.status)} data-testid={`ev-chip-${c.status}`}>
            {c.word} · {(list?.counts[c.status] ?? 0).toLocaleString("en-US")}
          </button>
        ))}
      </div>
      <div className="ev-filters">
        <label className="ev-search"><MagnifyingGlass size={15} aria-hidden />
          <input type="search" placeholder="Search text, INC number, person" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search events" data-testid="ev-search" /></label>
        <select className="ev-select" value={source} onChange={(e) => setSource(e.target.value)} aria-label="Source" data-testid="ev-source">
          <option value="">Source: all</option>
          {sources.map((c) => <option key={c.id} value={c.id}>{c.label}{c.name && c.name !== c.label ? ` · ${c.name}` : ""}</option>)}
        </select>
        <select className="ev-select" value={hours} onChange={(e) => setHours(Number(e.target.value))} aria-label="Period" data-testid="ev-period">
          {PERIODS.map((p) => <option key={p.hours} value={p.hours}>{p.word}</option>)}
        </select>
      </div>
      <div className="ev-split">
        <section aria-labelledby="ev-list-h" className="ev-form">
          <h2 id="ev-list-h" className="ev-sec-title">Events</h2>
          <ReadState read={read} what="Events" onRetry={() => setTick((x) => x + 1)} testId="ev-stream" />
          {list?.search_truncated ? (
            <span className="ev-faint" data-testid="ev-search-truncated"
                  title="A search checks each event with URL credentials hidden, and without “View people” with people's emails and BOSS accounts hidden too, so it stops after the newest ones. Narrow the period or source to reach older events.">
              Searched the newest {(list.search_limit || 5000).toLocaleString("en-US")} events
            </span>
          ) : null}
          {list && !items.length ? (
            <Empty icon={<Tray size={30} aria-hidden />} title={status.length || query || source ? "Nothing matches" : "No events yet"}
                   line={status.length || query || source ? "Clear a chip or the search." : "Events show up here the moment a source sends one."} testId="ev-stream-empty" />
          ) : null}
          {items.length ? (
            <div className="ev-list" data-testid="ev-list">
              {items.map((e) => (
                <button key={e.id} type="button" className="ev-row" aria-current={e.id === ev} onClick={() => goEvents({ ev: e.id })} data-testid={`ev-row-${e.id}`}>
                  <span className="ev-row-time">{clock(e.received_at)}</span>
                  <SourceMark kind={e.kind} size="sm" />
                  <span className="ev-row-main">
                    <span className="ev-row-text">{e.actor.display ? <Face name={e.actor.display} /> : null}<span>{e.text || e.subject || e.type}</span></span>
                    <span className="ev-row-where">{e.kind === "slack" || e.kind === "teams" ? placeOf(e) : e.connection_name || e.type}</span>
                  </span>
                  <StatusChip status={e.status} />
                  <span className="ev-row-agent" aria-hidden>{e.runs.length ? <Play size={14} weight="fill" /> : <CaretRight size={14} />}</span>
                </button>
              ))}
            </div>
          ) : null}
        </section>
        <EventPanel id={ev || items[0]?.id || ""} projectId={projectId} />
      </div>
    </EvFrame>
  );
}

/** One event in full: who, where and the words; the flow; then the details behind a fold. */
export function EventPanel({ id, projectId }: { id: string; projectId: string }) {
  const [read, setRead] = useState<Read<EventDetail> | null>(null);
  const [runDone, setRunDone] = useState<boolean | null>(null);
  useEffect(() => {
    let alive = true;
    setRead(null); setRunDone(null);
    if (!id) return;
    void readEvent(id).then((r) => {
      if (!alive) return;
      setRead(r);
      const main = r.ok ? r.data.outcome.find((o) => o.run_id && o.thread_id) : null;
      if (main) void readRunStatus(main.thread_id, main.run_id, projectId).then((s) => {
        if (alive && s.ok) setRunDone(["completed", "succeeded", "done"].includes(s.data) ? true : ["failed", "cancelled", "error"].includes(s.data) ? null : false);
      });
    });
    return () => { alive = false; };
  }, [id, projectId]);
  const names = useMemo(() => {
    const m: Record<string, string> = {};
    if (read?.ok) for (const f of read.data.fires) if (f.trigger_name) m[f.trigger_id] = f.trigger_name;
    return m;
  }, [read]);
  if (!id) return <aside className="ev-card ev-faint" data-testid="ev-detail-none">Pick an event to see what happened to it.</aside>;
  if (!read || !read.ok) return <aside className="ev-card"><ReadState read={read} what="The event" onRetry={() => setRead(null)} testId="ev-detail" /></aside>;
  const e = read.data;
  const nodes = eventFlow(e, (t) => names[t] || `rule ${t.slice(-4)}`, runDone);
  const saved = secs(e.received_at, e.event_time);
  const done = secs(e.processed_at, e.received_at);
  const subject = e.outcome.find((o) => o.subject)?.subject ?? null;
  const env = e.envelope as { data?: Record<string, unknown>; source?: string; bossconversation?: Record<string, unknown> };
  return (
    <aside className="ev-card ev-detail" aria-labelledby="ev-detail-h" data-testid="ev-detail">
      <div className="ev-detail-head">
        {e.actor.display ? <Face name={e.actor.display} size="lg" /> : <SourceMark kind={e.kind} />}
        <div><h2 id="ev-detail-h" className="ev-h2">{eventTitle(e)}</h2>{e.text ? <span className="ev-quote">“{e.text}”</span> : null}</div>
        <StatusChip status={e.status} testId="ev-detail-status" />
      </div>
      <EventFlowView nodes={nodes} testId="ev-detail-flow" />
      <div className="ev-timing">
        {saved !== null && saved < 600 ? <span title="From the source's own time to BOSS saving it">{fmtSecs(saved)} to saved</span> : null}
        {done !== null ? <span title="From saved to the router's answer">{fmtSecs(done)} to routed</span> : null}
        {e.screen_verdict ? <span title="Checked for hidden instructions before any agent read it"><ShieldCheck size={13} aria-hidden /> text screened: {e.screen_verdict}</span> : null}
        {e.attempts > 1 ? <span>{e.attempts} tries</span> : null}
        {e.replays ? <span>replayed {e.replays}×</span> : null}
      </div>
      {e.last_error ? <span className={e.last_error.startsWith("deferred") ? "ev-faint" : "ev-dl-why"} data-testid="ev-detail-error">{e.last_error.replace(/^deferred: /, "Held: ")}</span> : null}
      <details className="ev-more" data-testid="ev-detail-more">
        <summary><CaretRight size={12} aria-hidden /> Details · guards, identity, raw event</summary>
        <div className="ev-form">
          <div className="ev-chips">
            {e.outcome.filter((o) => o.reason || o.filtered).map((o, i) => (
              <Chip key={i} tone={o.fire_outcome === "admitted" ? "green" : o.fire_outcome === "refused" ? "amber" : "grey"} tip={o.reason || o.filtered}>
                {(o.trigger_id ? `${names[o.trigger_id] || "rule"}: ` : "")}{(o.reason || o.filtered).slice(0, 60)}</Chip>
            ))}
          </div>
          <dl className="ev-kv">
            <dt>type</dt><dd>{e.type}</dd>
            <dt>source</dt><dd>{String(env.source || "")}{env.data?.channel ? ` · channel ${String(env.data.channel)}` : ""}</dd>
            <dt>actor</dt><dd data-testid="ev-detail-actor">{e.actor.external_id || "—"}{subject?.kind === "person"
              ? (e.people_redacted || !subject.principal_id
                ? <> → <span className="ev-faint" title="Who each person is needs “View people” (Owner, Admin or Auditor)" data-testid="ev-detail-person-hidden">A BOSS person · name hidden</span> ({methodWords(subject.method)})</>
                : ` → ${subject.display || subject.principal_id} (${methodWords(subject.method)})`)
              : subject ? ` → ${subject.kind}` : ""}</dd>
            {e.conversation_key ? <><dt>thread</dt><dd>{e.conversation_key}</dd></> : null}
            <dt>event</dt><dd>{e.id}{e.duplicates ? ` · ${e.duplicates} repeats` : ""}</dd>
          </dl>
          {e.raw !== null ? <pre className="ev-pre" data-testid="ev-detail-raw">{JSON.stringify(e.raw, null, 2).slice(0, 8000)}</pre> : null}
        </div>
      </details>
    </aside>
  );
}
