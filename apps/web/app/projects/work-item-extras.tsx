"use client";

/**
 * WHAT A WORK ITEM CARRIES BEYOND ITS STATUS (BOSSAPP-880..883), drawn inside the work tree's
 * opened row (work-tree.tsx ItemPanel):
 *
 *   Watch     — a person's watchdog on this item's subtree: which stops to be told about, and
 *               who already watches it (engine work_watchdogs).
 *   Outputs   — its PRs, previews, deploys, artifacts and documents with their status now, what
 *               its text mentions and which items mention it (engine work_outputs).
 */
import React, { useCallback, useEffect, useState } from "react";
import { ArrowClockwise, Bell, BellSlash, LinkSimple, Package } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { partyName } from "../../lib/work-items";
import {
  OUTPUT_WORDS, WATCH_WORDS, attachWatchdog, detachWatchdog, openable, readOutputs, readWatchdogs,
  recordOutput, refreshOutputs, type Outputs, type WatchCondition, type Watchdog,
} from "../../lib/work-ux";

const ALL: WatchCondition[] = ["stopped_unfinished", "blocked", "closed", "stalled"];

export function WatchSection({ projectId, id, demo }: { projectId: string; id: string; demo: boolean }) {
  const [dogs, setDogs] = useState<Watchdog[] | null>(null);
  const [picked, setPicked] = useState<WatchCondition[]>(["stopped_unfinished", "blocked"]);
  const [note, setNote] = useState("");
  const [said, setSaid] = useState<string | null>(null);
  const load = useCallback(async () => {
    const r = await readWatchdogs(projectId, id);
    setDogs(r.ok && r.data ? r.data : []);
  }, [projectId, id]);
  useEffect(() => { void load(); }, [load]);
  const toggle = (c: WatchCondition) =>
    setPicked((p) => (p.includes(c) ? p.filter((x) => x !== c) : [...p, c]));
  const after = async (r: { ok: boolean; error: string | null }, ok: string) => {
    setSaid(r.ok ? ok : r.error);
    if (r.ok) await load();
  };
  return (
    <details className="wt-policy" data-testid="wt-watch" open={Boolean(dogs?.length)}>
      <summary><Bell size={12} aria-hidden /> Watch {dogs?.length ? `· ${dogs.length}` : ""}</summary>
      <p className="wt-faint">Be told once each time this item or anything under it stops in one of these ways.</p>
      {dogs?.map((d) => (
        <div key={d.id} className="wt-line" data-testid={`wt-watchdog-${d.id}`}>
          <span className="wt-who">{partyName(d.watcher) || d.watcher}</span>
          <span className="wt-faint">{d.conditions.map((c) => WATCH_WORDS[c]).join(" · ")}</span>
          {d.fired_count ? <span className="wt-faint">told {d.fired_count}×</span> : null}
          <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo}
                       onClick={() => void detachWatchdog(projectId, id, d.id).then((r) => after(r, "Stopped watching."))}
                       data-testid="wt-unwatch">
            <BellSlash size={12} aria-hidden /> Stop
          </GatedButton>
        </div>
      ))}
      <div className="wt-line" role="group" aria-label="Tell me when it">
        {ALL.map((c) => (
          <label key={c} className="wt-field">
            <input type="checkbox" checked={picked.includes(c)} onChange={() => toggle(c)} disabled={demo}
                   data-testid={`wt-watch-${c}`} />
            {WATCH_WORDS[c]}
          </label>
        ))}
      </div>
      <div className="wt-line">
        <input className="input wt-new" value={note} onChange={(e) => setNote(e.target.value)} disabled={demo}
               placeholder="What to check when it fires (optional)" aria-label="Watch instructions" data-testid="wt-watch-note" />
        <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo || !picked.length}
                     onClick={() => void attachWatchdog(projectId, id, { conditions: picked, instructions: note.trim() })
                       .then((r) => after(r, "Watching. You'll be told through the project's notification channels."))}
                     data-testid="wt-watch-save">
          <Bell size={12} aria-hidden /> Watch
        </GatedButton>
      </div>
      {said ? <p className="wt-said" role="status" data-testid="wt-watch-said">{said}</p> : null}
    </details>
  );
}

/** Its PRs, previews, deploys, artifacts and documents with live status; what it mentions; who mentions it. */
export function OutputsSection({ projectId, id, demo }: { projectId: string; id: string; demo: boolean }) {
  const [data, setData] = useState<Outputs | null>(null);
  const [link, setLink] = useState("");
  const [said, setSaid] = useState<string | null>(null);
  const load = useCallback(async () => {
    const r = await readOutputs(projectId, id);
    setData(r.ok && r.data ? r.data : { outputs: [], refs: [], backlinks: [] });
  }, [projectId, id]);
  useEffect(() => { void load(); }, [load]);
  const after = async (r: { ok: boolean; error: string | null }, ok: string | null) => {
    setSaid(r.ok ? ok : r.error);
    if (r.ok) await load();
  };
  const total = (data?.outputs.length ?? 0) + (data?.refs.length ?? 0) + (data?.backlinks.length ?? 0);
  return (
    <details className="wt-policy" data-testid="wt-outputs" open={Boolean(data?.outputs.length)}>
      <summary><Package size={12} aria-hidden /> Outputs &amp; links {total ? `· ${total}` : ""}</summary>
      {data?.outputs.length ? (
        <ul className="wt-list" aria-label="Outputs">
          {data.outputs.map((o) => (
            <li key={o.id} className="wt-line" data-testid={`wt-output-${o.kind}`}>
              <span className="wt-label">{OUTPUT_WORDS[o.kind] ?? o.kind}</span>
              {openable(o.ref) ? <a href={o.ref} target="_blank" rel="noopener noreferrer">{o.title}</a> : <span>{o.title}</span>}
              {o.status ? <span className="wt-chip" data-testid="wt-output-status">{o.status.replace(/_/g, " ")}</span> : null}
            </li>
          ))}
        </ul>
      ) : <p className="wt-faint">No outputs yet. A run's PR, preview and deploy land here when it finishes.</p>}
      {data?.refs.length ? (
        <p className="wt-line wt-faint" data-testid="wt-refs">
          <LinkSimple size={12} aria-hidden /> Mentions:{" "}
          {data.refs.map((r) => (
            <span key={`${r.to_kind}-${r.to_ref}`} className="wt-label" title={r.source === "manual" ? "Added by hand" : "Mentioned in its text"}>
              {r.to_kind === "work_item" ? (r.human_key || r.title || r.to_ref) : `${OUTPUT_WORDS[r.to_kind] ?? r.to_kind} ${r.to_ref}`}
            </span>
          ))}
        </p>
      ) : null}
      {data?.backlinks.length ? (
        <p className="wt-line wt-faint" data-testid="wt-backlinks">
          Mentioned by: {data.backlinks.map((b) => <span key={b.id} className="wt-label">{b.human_key || b.title}</span>)}
        </p>
      ) : null}
      <div className="wt-line">
        <input className="input wt-new" value={link} onChange={(e) => setLink(e.target.value)} disabled={demo}
               placeholder="Add a link (a doc, a dashboard…)" aria-label="Add an output link" data-testid="wt-output-link" />
        <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo || !openable(link.trim())}
                     onClick={() => void recordOutput(projectId, id, { kind: "document", ref: link.trim() })
                       .then((r) => { if (r.ok) setLink(""); return after(r, null); })}
                     data-testid="wt-output-add">
          Add
        </GatedButton>
        <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo}
                     onClick={() => void refreshOutputs(projectId, id).then((r) => after(r, "Up to date."))}
                     data-testid="wt-output-refresh">
          <ArrowClockwise size={12} aria-hidden /> Refresh
        </GatedButton>
      </div>
      {said ? <p className="wt-said" role="status" data-testid="wt-outputs-said">{said}</p> : null}
    </details>
  );
}
