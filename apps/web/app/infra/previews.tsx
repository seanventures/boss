"use client";

/**
 * Infra › Previews (spec §4.7): what previews are running, where, and until when. LIVE, FAILED ·
 * 24H and ENDED · 24H; each row says its state, app · branch, substrate, port, host, when it
 * started and — while it runs — how long it has left (a bar of the time remaining out of the
 * time it was given; amber in its last five minutes). `Open ↗`, `Log` (the drawer streams while
 * live), `Restart`, `Stop` and `Build ↗`, each only where the row's `can` allows. One poll
 * (`usePreviews`, 10 s); the bar moves only when the poll lands.
 */
import React, { useState } from "react";

import { showToast } from "../client-forms";
import { buildHref, usePreviews, type PreviewRow } from "../../lib/infra-view";
import { goConsole } from "./deploy-actions";
import type { InfraTabProps } from "./infra-home";
import { afterWrite, restartPreview, stopPreview } from "./infra-actions";
import { ago, localTime, useNow } from "./infra-util";
import { Empty, Skeleton, StateWord, ZoneHead } from "./kit";
import { PreviewLogDrawer } from "./preview-log";
import "./deploys.css";

const AMBER_MS = 5 * 60_000;

/** Time left: the fraction of (expires_at − created_at) still to run, and its words. */
export function ttlOf(row: Pick<PreviewRow, "createdAt" | "expiresAt">, now: number): { fraction: number; leftMs: number; label: string } | null {
  const end = row.expiresAt ? Date.parse(row.expiresAt) : NaN;
  const start = row.createdAt ? Date.parse(row.createdAt) : NaN;
  if (!Number.isFinite(end)) return null;
  const left = Math.max(0, end - now);
  const total = Number.isFinite(start) && end > start ? end - start : left;
  const fraction = total > 0 ? Math.min(1, left / total) : 0;
  const mins = Math.ceil(left / 60_000);
  const label = left <= 0 ? "Ending" : mins >= 120 ? `${Math.floor(mins / 60)}h left` : `${mins}m left`;
  return { fraction, leftMs: left, label };
}

function TtlBar({ row, now }: { row: PreviewRow; now: number }) {
  const t = ttlOf(row, now);
  if (!t) return <span className="inf-ttl" aria-hidden />;
  const amber = t.leftMs < AMBER_MS;
  return (
    <span className="inf-ttl" data-tone={amber ? "amber" : undefined} title={`Expires at ${localTime(row.expiresAt)}`}
          data-testid="inf-preview-ttl">
      <svg className="inf-ttl-bar" viewBox="0 0 100 4" preserveAspectRatio="none" role="img"
           aria-label={`${t.label} of the preview's time`}>
        <rect className="inf-ttl-track" x="0" y="0" width="100" height="4" rx="2" />
        <rect className="inf-ttl-fill" x="0" y="0" width={Math.round(t.fraction * 1000) / 10} height="4" rx="2" />
      </svg>
      <span className="inf-num inf-ttl-label">{t.label}</span>
    </span>
  );
}

function PreviewRowView({ row, projectId, actor, now, onLog, onChanged, live }: {
  row: PreviewRow; projectId: string; actor: string; now: number | null; live: boolean;
  onLog: (id: string) => void; onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const act = async (key: "stop" | "restart") => {
    setBusy(key); setError(null);
    const r = key === "stop" ? await stopPreview(row.id, actor) : await restartPreview(projectId, row.id, actor);
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", key === "stop" ? "Preview stopped" : "Preview restarting");
    afterWrite(onChanged);
  };
  const heartbeat = row.lastSeenAt ? `Last heartbeat ${ago(row.lastSeenAt, now)} ago` : undefined;
  return (
    <li className="inf-pv-row" data-testid="inf-preview-row" data-id={row.id} data-tone={row.tone}>
      <StateWord w={{ word: row.word, tone: row.tone }} />
      <span className="inf-pv-name inf-trunc" data-copy="data" title={`${row.app}${row.branch ? ` · ${row.branch}` : ""}`}>
        {row.app}{row.branch ? <span className="inf-faint"> · {row.branch}</span> : null}
      </span>
      <span className="inf-pvchip" title="Where it runs">{row.substrate}</span>
      <span className="inf-mono inf-faint">{row.port !== null ? `:${row.port}` : ""}</span>
      <span className="inf-pv-host inf-trunc inf-muted" data-copy="data">{row.host?.name ?? ""}</span>
      <span className="inf-num inf-faint" title={heartbeat ?? (row.createdAt ? localTime(row.createdAt) : undefined)}>
        {row.createdAt ? (live ? `started ${ago(row.createdAt, now)}` : `${ago(row.createdAt, now)} ago`) : ""}
      </span>
      {live && now !== null ? <TtlBar row={row} now={now} /> : <span className="inf-ttl" aria-hidden />}
      <span className="inf-pv-act">
        {row.can.open && row.url ? (
          <a className="ag-btn" href={row.url} target="_blank" rel="noreferrer" data-testid="inf-preview-open">Open ↗</a>
        ) : null}
        {row.can.log ? (
          <button type="button" className="ag-btn" data-testid="inf-preview-log-btn" onClick={() => onLog(row.id)}>Log</button>
        ) : null}
        {row.can.restart ? (
          <button type="button" className="ag-btn" disabled={busy !== null} data-testid="inf-preview-restart"
                  title="Start it again with the same build" onClick={() => void act("restart")}>
            {busy === "restart" ? "Restarting…" : "Restart"}</button>
        ) : null}
        {row.can.stop ? (
          <button type="button" className="ag-btn" disabled={busy !== null} data-testid="inf-preview-stop"
                  title="Stops the preview now. Start it again from the build." onClick={() => void act("stop")}>
            {busy === "stop" ? "Stopping…" : "Stop"}</button>
        ) : null}
        {row.runId ? (
          <a className="inf-link" href={buildHref(row.runId)} title="Open the build that made this" data-testid="inf-preview-build"
             onClick={(e) => { if (!(e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0)) { e.preventDefault(); goConsole(buildHref(row.runId as string)); } }}>
            Build ↗</a>
        ) : null}
        {error ? <span className="inf-error inf-trunc" role="alert" title={error} data-copy="data">Couldn&apos;t do that</span> : null}
      </span>
    </li>
  );
}

export function PreviewsTab({ projectId, actor, url, go, query }: InfraTabProps) {
  const poll = usePreviews(projectId);
  const now = useNow(30_000);
  const [showEnded, setShowEnded] = useState(false);
  const d = poll.data;
  const q = query.trim().toLowerCase();
  const keep = (r: PreviewRow) => !q || [r.app, r.branch, r.host?.name, r.word, r.substrate].filter(Boolean).join(" ").toLowerCase().includes(q);
  const all = d ? [...d.live, ...d.failed24h, ...d.ended24h] : [];
  const open = url.preview ? all.find((r) => r.id === url.preview) ?? null : null;
  const rowProps = { projectId, actor, now, onLog: (id: string) => go({ preview: id }), onChanged: poll.refresh };

  if (poll.loading) return <Skeleton testId="inf-previews-loading" />;
  return (
    <div className="inf-previews" data-testid="inf-previews">
      {!d && poll.error ? <span className="inf-error" role="alert" title={poll.error}>Can&apos;t reach BOSS</span> : null}
      {d ? (
        <>
          <section aria-labelledby="inf-pv-live">
            <ZoneHead id="inf-pv-live" label="LIVE" count={d.counts.live} />
            {d.live.filter(keep).length ? (
              <ul className="inf-pv-rows">{d.live.filter(keep).map((r) => <PreviewRowView key={r.id} row={r} live {...rowProps} />)}</ul>
            ) : <Empty testId="inf-previews-none">Nothing running</Empty>}
          </section>
          <section aria-labelledby="inf-pv-failed">
            <ZoneHead id="inf-pv-failed" label="FAILED · 24H" count={d.counts.failed24h} />
            {d.failed24h.filter(keep).length ? (
              <ul className="inf-pv-rows">{d.failed24h.filter(keep).map((r) => <PreviewRowView key={r.id} row={r} live={false} {...rowProps} />)}</ul>
            ) : <Empty>None failed</Empty>}
          </section>
          <section aria-labelledby="inf-pv-ended">
            <ZoneHead id="inf-pv-ended" label="ENDED · 24H" count={d.counts.ended24h}>
              {d.ended24h.length ? (
                <button type="button" className="inf-link" aria-expanded={showEnded} data-testid="inf-previews-show-ended"
                        onClick={() => setShowEnded((v) => !v)}>{showEnded ? "Hide" : "Show"}</button>
              ) : null}
            </ZoneHead>
            {showEnded ? (
              <ul className="inf-pv-rows">{d.ended24h.filter(keep).map((r) => <PreviewRowView key={r.id} row={r} live={false} {...rowProps} />)}</ul>
            ) : d.ended24h.length ? null : <Empty>None ended</Empty>}
          </section>
        </>
      ) : null}
      {url.preview ? <PreviewLogDrawer previewId={url.preview} row={open} onClose={() => go({ preview: null })} /> : null}
    </div>
  );
}
