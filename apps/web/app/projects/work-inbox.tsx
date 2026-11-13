"use client";

/**
 * MINE — THE PERSONAL INBOX (BOSSAPP-875), the Work tab's fourth view. What waits on the signed-in
 * person, first what blocks the most: each row its kind, title and the few words of why it ranks
 * there ("overdue · blocks 3 · for you"), unread in bold; Read, Snooze 1 day, Archive. "Everyone"
 * shows the whole project's feed ranked the same way.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Archive, Bell, CheckCircle, Clock } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { KIND_WORDS, archiveEntry, markRead, readInbox, snoozeEntry, type Inbox } from "../../lib/inbox";
import "./work-tree.css";

export function InboxView({ projectId, demo = false }: { projectId: string; demo?: boolean }) {
  const [mine, setMine] = useState(true);
  const [feed, setFeed] = useState<Inbox | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const load = useCallback(async () => {
    const r = await readInbox(projectId, mine);
    if (r.ok && r.data) { setFeed(r.data); setError(null); }
    else if (!r.ok) setError(r.error === "missing" ? "This install has no inbox yet." : r.error);
  }, [projectId, mine]);
  useEffect(() => { void load(); }, [load]);
  const after = async (r: { ok: boolean; error: string | null }, ok: string) => {
    setSaid(r.ok ? ok : r.error);
    if (r.ok) await load();
  };
  if (error) return <div className="px-card wt-empty" data-testid="ib-error">{error}</div>;
  if (!feed) return <div className="px-card wt-empty" data-testid="ib-loading">Loading your inbox…</div>;
  return (
    <div className="wt" data-testid="ib">
      <div className="wt-bar">
        <span className="wt-counts"><Bell size={13} aria-hidden /> {feed.unread} unread · {feed.count} waiting
          {feed.snoozed ? ` · ${feed.snoozed} snoozed` : ""}{feed.archived ? ` · ${feed.archived} archived` : ""}</span>
        <span className="px-spacer" />
        <div className="px-seg" role="group" aria-label="Whose">
          <button type="button" aria-pressed={mine} onClick={() => setMine(true)} data-testid="ib-mine">Mine</button>
          <button type="button" aria-pressed={!mine} onClick={() => setMine(false)} data-testid="ib-all">Everyone</button>
        </div>
      </div>
      {!feed.items.length ? (
        <div className="px-card wt-empty" data-testid="ib-empty"><CheckCircle size={18} aria-hidden /> Nothing waits on you.</div>
      ) : (
        <ul className="wt-rows" aria-label="Needs you, ranked">
          {feed.items.map((e) => (
            <li key={e.item_key} data-testid={`ib-${e.item_key}`}>
              <div className="wt-row">
                <span className="wt-key">{KIND_WORDS[e.kind]}</span>
                <span className="wt-title" style={{ fontWeight: e.read ? 400 : 600 }}>{e.title}</span>
                {e.why.map((w) => <span key={w} className={w === "overdue" || w.startsWith("blocks") ? "wt-blocked" : "wt-label"}>{w}</span>)}
                <span className="wt-who">
                  {!e.read ? (
                    <GatedButton permission="work_item:read" projectId={projectId} className="px-btn" disabled={demo}
                                 onClick={() => void markRead(projectId, [e.item_key]).then((r) => after(r, "Marked read."))}
                                 data-testid={`ib-read-${e.item_key}`}>Read</GatedButton>
                  ) : null}
                  <GatedButton permission="work_item:read" projectId={projectId} className="px-btn" disabled={demo}
                               onClick={() => void snoozeEntry(projectId, e.item_key, 24 * 60).then((r) => after(r, "Snoozed for a day."))}
                               data-testid={`ib-snooze-${e.item_key}`}><Clock size={12} aria-hidden /> 1 day</GatedButton>
                  <GatedButton permission="work_item:read" projectId={projectId} className="px-btn" disabled={demo}
                               onClick={() => void archiveEntry(projectId, e.item_key).then((r) => after(r, "Archived."))}
                               data-testid={`ib-archive-${e.item_key}`} aria-label="Archive"><Archive size={12} aria-hidden /></GatedButton>
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {said ? <p className="wt-said" role="status" data-testid="ib-said">{said}</p> : null}
    </div>
  );
}
