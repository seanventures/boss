"use client";

/**
 * Govern › Sharing (docs/product/RUN_SCORECARDS.md, "Implementation"): what this install tells
 * the BOSS project, and the controls over it.
 *
 *   SHARING       Improve BOSS     <the consent wording, verbatim>          [ off ]
 *                 Daily heartbeat  random id · version · OS · …             [ on  ]
 *   NEXT PAYLOAD  1.9 KB gzip · 6.2 KB JSON · Frozen 3m ago · Sends ~Tue 03:17   [Export batch]
 *                 { the exact bytes the next send posts, decoded }
 *   SENT          the last 30 sends: day · kind · status · size                  [View]
 *   INSTALL ID    bi_…                                     [Rotate] [Delete what was sent]
 *   SCORE         Score v1 · the weights, editable                               [Save]
 *   COLLECTOR     Collector mode (BOSS's own server)                             [ off ]
 *
 * The preview IS what is sent: reading it freezes the next payload (services/improve_boss.cut).
 */
import React, { useEffect, useState } from "react";

import {
  ago, improveHref, improveWrite, isSharing, kb, polledImprove, readImprove, stamp,
  type PreviewView, type SendRow, type SharingView,
} from "../../lib/improve";
import { usePolledRead } from "../../lib/log-stream";
import { showToast } from "../client-forms";
import { ConsentText, HeartbeatFields } from "../improve/consent";
import "../improve/improve.css";

const STATUS: Record<string, { word: string; tone: string }> = {
  sent: { word: "Sent", tone: "green" }, failed: { word: "Failed", tone: "red" },
  dropped: { word: "Dropped", tone: "amber" }, exported: { word: "Exported", tone: "grey" },
};
const KIND: Record<string, string> = { daily: "Daily", share: "Shared run", delete: "Delete", export: "Export" };

function whenWords(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

function Toggle({ on, label, disabled, onFlip, testId, tip }: {
  on: boolean; label: string; disabled?: boolean; onFlip: () => void; testId: string; tip?: string;
}) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} className="im-switch" disabled={disabled}
            data-testid={testId} title={tip} onClick={onFlip}>{on ? "On" : "Off"}</button>
  );
}

export function SharingTab({ projectId, actor, paused }: { projectId: string; actor: string; paused: boolean }) {
  const read = usePolledRead<SharingView>({
    key: projectId, pollMs: 30_000, paused, read: polledImprove<SharingView>("sharing", { project_id: projectId }),
  });
  const [preview, setPreview] = useState<PreviewView | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState<{ id: string; json: unknown } | null>(null);
  const [confirmForget, setConfirmForget] = useState(false);
  const [weights, setWeights] = useState<Record<string, string>>({});
  const [url, setUrl] = useState<string | null>(null);
  const [rotated, setRotated] = useState("");
  const v = isSharing(read.data) ? read.data : null;

  const loadPreview = async () => {
    const got = await readImprove<PreviewView>("preview", { project_id: projectId });
    if (got.ok && !got.notModified && got.data && typeof got.data === "object" && "pending" in got.data) {
      setPreview(got.data); setPreviewError("");
    }
    else if (!got.ok) setPreviewError(got.error);
  };
  useEffect(() => { void loadPreview(); }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps

  if (read.error && !v) {
    return <div className="ag-empty" role="alert"><span title={read.error}>Can&apos;t reach BOSS</span>
      <button type="button" className="ag-btn" onClick={read.reload}>Retry</button></div>;
  }
  if (!v) return <div className="gv-skel" aria-busy="true" />;

  const locked = Boolean(v.env_disabled);
  const flip = async (name: "improve" | "heartbeat" | "collector", on: boolean) => {
    setBusy(name);
    const r = await improveWrite("switch", { project_id: projectId, actor, switch: name, on });
    setBusy("");
    if (!r.ok) { showToast("error", r.status === 403 ? "You can't change this" : "Not changed"); return; }
    const word = { improve: "Sharing", heartbeat: "Heartbeat", collector: "Collector" }[name];
    showToast("success", `${word} ${on ? "on" : "off"}`);
    read.reload();
    void loadPreview();
  };
  const rotate = async () => {
    setBusy("rotate");
    const r = await improveWrite("rotate", { project_id: projectId, actor });
    setBusy("");
    if (!r.ok) { showToast("error", r.status === 403 ? "You can't change this" : "Not rotated"); return; }
    // SAY WHAT HAPPENED TO THE OLD ID (P4): deleted, kept until its delete goes, or never sent.
    const old = ((r.data as { rotated?: { old?: string } }).rotated ?? {}).old;
    setRotated(old === "deleted" ? "Old id deleted at the collector"
      : old === "kept" ? "Old id kept until its delete goes through" : "Old id never sent");
    showToast("success", "New install id");
    read.reload(); void loadPreview();
  };
  const forget = async () => {
    setBusy("forget");
    const r = await improveWrite("forget", { project_id: projectId, actor });
    setBusy("");
    setConfirmForget(false);
    const deleted = r.ok && (r.data as { deleted?: boolean }).deleted;
    showToast(deleted ? "success" : "error", deleted ? "Sent data deleted" : "Delete failed");
    if (!deleted) setRotated("Not deleted yet · kept to retry");
    read.reload();
    void loadPreview();
  };
  const view = async (row: SendRow) => {
    if (open?.id === row.id) { setOpen(null); return; }
    const got = await readImprove<{ json: unknown }>("send", { project_id: projectId, id: row.id });
    if (got.ok && !got.notModified) setOpen({ id: row.id, json: got.data.json });
  };
  const changed = Object.entries(weights).filter(([k, val]) => {
    const w = v.score.weights.find((x) => x.key === k);
    return w && val !== "" && Number(val) !== w.value;
  });
  const saveWeights = async (reset = false) => {
    const body = reset ? Object.fromEntries(v.score.weights.map((w) => [w.key, w.default]))
      : Object.fromEntries(changed.map(([k, val]) => [k, Number(val)]));
    setBusy("weights");
    const r = await improveWrite("weights", { project_id: projectId, actor, weights: body });
    setBusy("");
    if (r.ok) { showToast("success", "Weights saved"); setWeights({}); read.reload(); }
    else showToast("error", r.status === 403 ? "You can't change this" : r.error);
  };
  const sw = v.switches;
  const p = preview?.pending;
  const stop = v.blocked ?? null;
  const saveUrl = async () => {
    setBusy("url");
    const r = await improveWrite("collector_url", { project_id: projectId, actor, url: (url ?? "").trim() });
    setBusy("");
    if (r.ok) { showToast("success", "Collector saved"); setUrl(null); read.reload(); void loadPreview(); }
    else showToast("error", r.status === 403 ? "You can't change this" : "Not saved");
  };

  return (
    <div className="im-sharing" data-testid="gv-sharing">
      {stop ? (
        <div className="im-lock" role="status" data-testid="gv-sharing-locked">
          <b>Nothing is sent</b>
          <span className="gv-muted" title={locked ? "Set in this install's environment. Remove it to share again."
            : "Set a collector below, or BOSS_TELEMETRY_URL. A release will name a public one."}>
            {locked ? `Off by ${v.env_disabled}` : "No collector set"}
          </span>
        </div>
      ) : null}

      <section className="gv-sec" aria-labelledby="im-sh-head">
        <h3 className="gv-sec-head" id="im-sh-head">SHARING</h3>
        <ul className="gv-rows">
          <li className="im-row" data-testid="gv-sharing-improve">
            <span className="im-row-name">Improve BOSS</span>
            <span className="im-row-what">
              <ConsentText />
              <span className="im-who" title="Recorded in Govern changes">
                {sw.improve.asked ? `${sw.improve.on ? "On" : "Off"} · ${sw.improve.by_name ?? "—"} · ${ago(sw.improve.at)}` : "Off by default"}
              </span>
            </span>
            <Toggle on={sw.improve.on && !locked} label="Improve BOSS" disabled={locked || busy !== ""}
                    testId="gv-switch-improve" onFlip={() => void flip("improve", !sw.improve.on)} />
          </li>
          <li className="im-row" data-testid="gv-sharing-heartbeat">
            <span className="im-row-name">Daily heartbeat</span>
            <span className="im-row-what">
              <HeartbeatFields />
              <span className="im-who" title="Separate from Improve BOSS. On by default; this is the opt-out.">
                {sw.heartbeat.asked ? `${sw.heartbeat.on ? "On" : "Off"} · ${sw.heartbeat.by_name ?? "—"} · ${ago(sw.heartbeat.at)}` : "On by default"}
              </span>
            </span>
            <Toggle on={sw.heartbeat.on && !locked} label="Daily heartbeat" disabled={locked || busy !== ""}
                    testId="gv-switch-heartbeat" onFlip={() => void flip("heartbeat", !sw.heartbeat.on)} />
          </li>
          <li className="im-row" data-testid="gv-sharing-url">
            <span className="im-row-name">Send to</span>
            <span className="im-row-what">
              {v.url_source === "env" ? (
                <span className="im-id" data-user-text="" title="Set by BOSS_TELEMETRY_URL in this install's environment">{v.url}</span>
              ) : (
                <input className="im-input" value={url ?? v.url ?? ""} placeholder="No collector set"
                       aria-label="Collector URL" data-testid="gv-url" spellCheck={false}
                       title="https://…/v1/telemetry/ingest. Empty sends nothing."
                       onChange={(e) => setUrl(e.target.value)} />
              )}
            </span>
            {v.url_source !== "env" ? (
              <button type="button" className="ag-btn" disabled={busy !== "" || url === null || url === v.url}
                      data-testid="gv-url-save" onClick={() => void saveUrl()}>Save</button>
            ) : <span />}
          </li>
        </ul>
      </section>

      <section className="gv-sec" aria-labelledby="im-next-head">
        <h3 className="gv-sec-head" id="im-next-head">NEXT PAYLOAD
          {p ? <span className="gv-badge" title="Compressed, as it goes over the wire">{kb(p.bytes_gz)}</span> : null}
        </h3>
        <div className="im-payload" data-testid="gv-payload">
          {p ? (
            <>
              <span className="im-payload-line">
                <span title="Uncompressed JSON">{kb(p.bytes_raw)} JSON</span>
                <span title={`Cut ${new Date(p.cut_at).toLocaleString()}. Later work goes in the next one.`}>Frozen {ago(p.cut_at)}</span>
                {preview?.next_due_at && !stop ? <span title="A random time each day; one send a day at most">Sends ~{whenWords(preview.next_due_at)}</span>
                  : stop ? <span className="gv-value-note" title="It is shown and exportable; nothing goes out">Not sent · {locked ? "off" : "no collector"}</span> : null}
                {Object.keys(p.aggregated ?? {}).length ? <span className="gv-value-note" title="Over the size cap or many runs: sent as counts">Aggregated</span> : null}
                <span className="gv-spacer" />
                <a className="ag-btn" href={improveHref("export", { project_id: projectId, actor })} download
                   data-testid="gv-export" title="The same JSON, as a file to send by hand (or not)">Export batch</a>
              </span>
              <pre className="im-json" data-user-text="" data-copy="data" data-testid="gv-payload-json"
                   tabIndex={0} aria-label="The exact next payload">{JSON.stringify(p.json, null, 2)}</pre>
            </>
          ) : (
            <span className="im-payload-line" data-testid="gv-payload-none">
              <span title={preview?.why_not ?? previewError}>Nothing will be sent</span>
            </span>
          )}
        </div>
      </section>

      <section className="gv-sec" aria-labelledby="im-sent-head">
        <h3 className="gv-sec-head" id="im-sent-head">SENT
          <span className="gv-badge" title="The last 30 sends, kept on this install">{v.sends.length}</span>
          {v.unsent_batches ? <span className="gv-badge" title="Batches waiting; at most 7 are kept">{v.unsent_batches} waiting</span> : null}
        </h3>
        {v.sends.length ? (
          <ul className="im-log" data-testid="gv-sends">
            {v.sends.map((s) => {
              const st = STATUS[s.status] ?? { word: s.status, tone: "grey" };
              return (
                <li key={s.id}>
                  <div className="im-log-row" data-testid="gv-send">
                    <span title={`Batch day ${s.day}`}>{stamp(s.sent_at ?? s.cut_at)}</span>
                    <span>{KIND[s.kind] ?? s.kind}</span>
                    <span className="im-status" data-tone={st.tone}
                          title={s.error ? `Error: ${s.error}` : s.http_status ? `HTTP ${s.http_status}` : undefined}>{st.word}</span>
                    <span title={`${kb(s.bytes_raw)} JSON`}>{kb(s.bytes_gz)}{Object.keys(s.aggregated ?? {}).length ? " · aggregated" : ""}</span>
                    {s.kind === "daily" ? (
                      <button type="button" className="ag-link" data-testid="gv-send-view" aria-expanded={open?.id === s.id}
                              onClick={() => void view(s)}>{open?.id === s.id ? "Hide" : "View"}</button>
                    ) : <span />}
                  </div>
                  {open?.id === s.id ? (
                    <pre className="im-json" data-user-text="" data-copy="data">{JSON.stringify(open.json, null, 2)}</pre>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : <span className="gv-muted" data-testid="gv-sends-none">Nothing sent yet</span>}
      </section>

      <section className="gv-sec" aria-labelledby="im-id-head">
        <h3 className="gv-sec-head" id="im-id-head">INSTALL ID</h3>
        <div className="im-payload">
          <span className="im-payload-line">
            <span className="im-id" data-user-text="" data-testid="gv-install-id"
                  title="Random. Not derived from this machine or anyone">{v.install_id}</span>
            <span className="gv-spacer" />
            <span className="im-actions">
              <button type="button" className="ag-btn" disabled={busy !== ""} data-testid="gv-rotate"
                      title="A new random id; the old one stops being used" onClick={() => void rotate()}>Rotate</button>
              <button type="button" className="ag-btn" disabled={busy !== "" || Boolean(stop)} data-testid="gv-forget"
                      title={stop ? `Can't reach a collector: ${stop}` : "Deletes everything sent under every id this install used, then rotates"}
                      onClick={() => setConfirmForget(true)}>Delete what was sent</button>
            </span>
          </span>
          {stop ? <span className="im-muted" data-testid="gv-forget-why">Delete needs a collector · {locked ? `off by ${v.env_disabled}` : "none set"}</span> : null}
          {rotated ? <span className="im-muted" role="status" data-testid="gv-rotated">{rotated}</span> : null}
          {(v.retired_waiting ?? 0) > 0 ? (
            <span className="im-muted" data-testid="gv-retired" title="Their deletes are retried with the daily send">
              {v.retired_waiting} old id{v.retired_waiting === 1 ? "" : "s"} awaiting delete
            </span>
          ) : null}
          {confirmForget ? (
            <span className="im-confirm" role="alertdialog" aria-label="Delete what was sent">
              <span>Delete everything sent?</span>
              <button type="button" className="ag-btn ag-btn-primary" data-testid="gv-forget-yes" disabled={busy !== ""}
                      onClick={() => void forget()}>{busy === "forget" ? "Deleting…" : "Delete"}</button>
              <button type="button" className="ag-btn" onClick={() => setConfirmForget(false)}>Cancel</button>
            </span>
          ) : null}
        </div>
      </section>

      <section className="gv-sec" aria-labelledby="im-score-head">
        <h3 className="gv-sec-head" id="im-score-head">SCORE
          <span className="gv-badge" title="Scores only compare within one version">v{v.score.version}</span>
        </h3>
        <div className="im-weights" data-testid="gv-weights">
          {v.score.weights.map((w) => {
            const val = weights[w.key] ?? String(w.value);
            return (
              <label key={w.key} className="im-weight" data-changed={Number(val) !== w.value ? "" : undefined}
                     title={`Default ${w.default}. 0 to 1.`}>
                <span>{w.label}</span>
                <input type="number" min={0} max={1} step={0.01} value={val} aria-label={w.label}
                       data-testid={`gv-weight-${w.key}`}
                       onChange={(e) => setWeights((cur) => ({ ...cur, [w.key]: e.target.value }))} />
              </label>
            );
          })}
          <span className="im-actions">
            <button type="button" className="ag-btn ag-btn-primary" disabled={!changed.length || busy !== ""}
                    data-testid="gv-weights-save" title="Makes a new score version; every row is rescored"
                    onClick={() => void saveWeights()}>Save</button>
            {v.score.weights.some((w) => w.value !== w.default) ? (
              <button type="button" className="ag-btn" disabled={busy !== ""} onClick={() => void saveWeights(true)}>Reset</button>
            ) : null}
          </span>
        </div>
      </section>

      <section className="gv-sec" aria-labelledby="im-col-head">
        <h3 className="gv-sec-head" id="im-col-head">COLLECTOR</h3>
        <ul className="gv-rows">
          <li className="im-row" data-testid="gv-sharing-collector">
            <span className="im-row-name">Collector mode</span>
            <span className="im-row-what">
              <span title="Receive heartbeats and batches from other installs. For BOSS's own server.">
                Receive other installs&apos; sends
              </span>
              {sw.collector.on ? <a className="ag-link" href="/?area=observability&view=installs" data-testid="gv-installs-link">Installs ↗</a> : null}
            </span>
            <Toggle on={sw.collector.on} label="Collector mode" disabled={busy !== ""} testId="gv-switch-collector"
                    onFlip={() => void flip("collector", !sw.collector.on)} />
          </li>
        </ul>
      </section>
    </div>
  );
}
