"use client";

/**
 * Share this run (docs/product/RUN_SCORECARDS.md): a button, not a setting. The run's bundle —
 * steps, agent turns, errors, log tails — after the redaction gate, in a box the person can edit.
 * Sent ONCE, with a note, only when they press Send. Separate from the Improve BOSS switch, and the
 * only way content ever reaches the BOSS project.
 */
import { GatedButton } from "../../components/gated";
import React, { useEffect, useRef, useState } from "react";

import { improveWrite, isSharing, readImprove, type SharingView } from "../../lib/improve";
import { showToast } from "../client-forms";
import { Sheet } from "../infra/kit";
import "./improve.css";

export function ShareRunSheet({ projectId, actor, unitKind, unitId, onClose, localOnly = false }: {
  projectId: string; actor: string; unitKind: "build" | "workflow_run"; unitId: string; onClose: () => void;
  /** Prepare a local report without reading collector configuration or offering Send. */
  localOnly?: boolean;
}) {
  const scope = JSON.stringify([projectId, actor, unitKind, unitId, localOnly]);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const [prepared, setPrepared] = useState<{ scope: string; text: string } | null>(null);
  const bundle = prepared?.scope === scope ? prepared.text : null;
  const [masked, setMasked] = useState(0);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [inspected, setInspected] = useState(false);
  const downloadRequest = useRef<AbortController | null>(null);
  const downloadUrl = useRef<string | null>(null);
  const releaseTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // NOTHING LEAVES WHEN SHARING IS OFF OR NO COLLECTOR IS SET (P2): the reason, not a button.
  const [stop, setStop] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setPrepared(null); setMasked(0); setNote(""); setError(""); setBusy(false); setInspected(false); setStop(null);
    if (!localOnly) {
      void readImprove<SharingView>("sharing", { project_id: projectId }).then((got) => {
        if (live && got.ok && !got.notModified && isSharing(got.data)) setStop(got.data.blocked ?? null);
      });
    }
    void readImprove<{ bundle: string; masked: Record<string, number> }>("share_bundle", {
      project_id: projectId, unit_kind: unitKind, unit_id: unitId, actor,
    }).then((got) => {
      if (!live) return;
      if (got.ok && !got.notModified) {
        if (typeof got.data.bundle !== "string" || got.data.bundle.length > 400_000) {
          setError("The diagnostic report was unavailable or too large."); return;
        }
        setPrepared({ scope, text: got.data.bundle });
        setMasked(Object.values(got.data.masked ?? {}).reduce((a, b) => a + b, 0));
      } else if (!got.ok) setError(got.status === 403 ? "You can't share runs" : got.error);
    });
    return () => {
      live = false;
      downloadRequest.current?.abort();
      if (releaseTimer.current) clearTimeout(releaseTimer.current);
      if (downloadUrl.current) URL.revokeObjectURL(downloadUrl.current);
      downloadUrl.current = null;
    };
  }, [projectId, unitKind, unitId, actor, localOnly, scope]);

  const download = async () => {
    if (bundle === null || !bundle.trim() || !inspected || busy) return;
    const requestedScope = scope;
    const report = bundle;
    setBusy(true); setError("");
    const controller = new AbortController();
    downloadRequest.current = controller;
    // Recheck the existing project/run permission before saving the edited snapshot.
    // This read neither sends the report nor contacts a model or provider.
    const got = await readImprove<{ bundle: string }>("share_bundle", {
      project_id: projectId, unit_kind: unitKind, unit_id: unitId, actor,
    }, { signal: controller.signal });
    if (controller.signal.aborted || currentScope.current !== requestedScope) return;
    downloadRequest.current = null;
    setBusy(false);
    if (!got.ok || got.notModified || typeof got.data.bundle !== "string") {
      setError(!got.ok && got.status === 403 ? "You can't download this run" : "Couldn't verify access. Try again.");
      return;
    }
    try {
      if (releaseTimer.current) clearTimeout(releaseTimer.current);
      if (downloadUrl.current) URL.revokeObjectURL(downloadUrl.current);
      const url = URL.createObjectURL(new Blob([report], { type: "text/plain;charset=utf-8" }));
      downloadUrl.current = url;
      const link = document.createElement("a");
      link.href = url; link.download = "boss-run-diagnostics.txt";
      document.body.appendChild(link);
      try { link.click(); } finally { link.remove(); }
      releaseTimer.current = setTimeout(() => {
        URL.revokeObjectURL(url);
        if (downloadUrl.current === url) downloadUrl.current = null;
      }, 1000);
    } catch {
      if (downloadUrl.current) URL.revokeObjectURL(downloadUrl.current);
      downloadUrl.current = null;
      setError("Couldn't save the report. Try again.");
    }
  };

  const send = async () => {
    if (bundle === null) return;
    setBusy(true);
    const r = await improveWrite("share_run", { project_id: projectId, actor, bundle, note });
    setBusy(false);
    if (r.ok) { showToast("success", "Run shared"); onClose(); }
    else setError(r.error);
  };

  return (
    <Sheet title={localOnly ? "Download diagnostics" : "Share this run"} testId="share-run" onClose={onClose}>
      <div className="im-share">
        {stop ? (
          <span className="im-lock" role="status" data-testid="share-run-blocked">
            <b>Can&apos;t send</b><span className="gv-muted">{stop}</span>
          </span>
        ) : null}
        <span className="im-check" role="note" data-testid="share-run-check">
          <b>Check for anything private</b>
          <span className="im-muted" title="Secrets, emails, hosts, URLs, file paths, repositories and names are masked. Edit anything else out.">
            {masked ? `${masked} masked` : "Nothing masked"} · edit below
          </span>
        </span>
        {bundle === null && !error ? <span className="im-muted" aria-busy="true">Loading…</span> : null}
        {bundle !== null ? (
          <textarea value={bundle} maxLength={400_000} onChange={(e) => { setPrepared({ scope, text: e.target.value }); setInspected(false); }} aria-label={localOnly ? "Diagnostic report to save" : "What will be sent"}
                    data-testid="share-run-bundle" spellCheck={false} />
        ) : null}
        {localOnly ? <span className="im-muted">Selected run · redacted, bounded snapshot · saved locally. Inspect it before sharing.</span> :
          <textarea className="im-share-note" value={note} onChange={(e) => setNote(e.target.value)}
                    aria-label="A note for the BOSS project" placeholder="What went wrong?" data-testid="share-run-note" />}
        <label className="im-check">
          <input type="checkbox" checked={inspected} onChange={(e) => setInspected(e.target.checked)} data-testid="diagnostic-inspected" />
          <span>I checked the report for private content.</span>
        </label>
        <span className="im-actions">
          <GatedButton permission="telemetry:share" type="button" className="ag-btn" disabled={busy || !inspected || bundle === null || !bundle.trim()}
                       data-testid="diagnostic-download" onClick={() => void download()}>Download report</GatedButton>
          {!localOnly ? <GatedButton permission="telemetry:share" type="button" className="ag-btn ag-btn-primary" disabled={busy || Boolean(stop) || bundle === null || !bundle.trim()}
                  data-testid="share-run-send" title="Sent once, now. Not part of Improve BOSS."
                  onClick={() => void send()}>{busy ? "Sending…" : "Send"}</GatedButton> : null}
          <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
          {error ? <span className="im-err" role="alert" title={error}>{localOnly ? "Report unavailable" : "Not sent"}</span> : null}
        </span>
      </div>
    </Sheet>
  );
}

/** The button that opens it, for surfaces without a menu of their own (a workflow run). */
export function ShareRunButton({ projectId, actor, unitKind, unitId }: {
  projectId: string; actor: string; unitKind: "build" | "workflow_run"; unitId: string;
}) {
  const [open, setOpen] = useState<"share" | "local" | null>(null);
  return (
    <>
      <GatedButton permission="telemetry:share" type="button" className="ag-link" data-testid="share-run-open"
              title="Send this run, redacted and edited by you, to the BOSS project"
              onClick={() => setOpen("share")}>Share this run</GatedButton>
      <GatedButton permission="telemetry:share" type="button" className="ag-link" data-testid="diagnostic-open"
                   onClick={() => setOpen("local")}>Download diagnostics</GatedButton>
      {open ? <ShareRunSheet projectId={projectId} actor={actor} unitKind={unitKind} unitId={unitId}
                             localOnly={open === "local"} onClose={() => setOpen(null)} /> : null}
    </>
  );
}
