"use client";

/**
 * Share this run (docs/product/RUN_SCORECARDS.md): a button, not a setting. The run's bundle —
 * steps, agent turns, errors, log tails — after the redaction gate, in a box the person can edit.
 * Sent ONCE, with a note, only when they press Send. Separate from the Improve BOSS switch, and the
 * only way content ever reaches the BOSS project.
 */
import React, { useEffect, useState } from "react";

import { improveWrite, isSharing, readImprove, type SharingView } from "../../lib/improve";
import { showToast } from "../client-forms";
import { Sheet } from "../infra/kit";
import "./improve.css";

export function ShareRunSheet({ projectId, actor, unitKind, unitId, onClose }: {
  projectId: string; actor: string; unitKind: "build" | "workflow_run"; unitId: string; onClose: () => void;
}) {
  const [bundle, setBundle] = useState<string | null>(null);
  const [masked, setMasked] = useState(0);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // NOTHING LEAVES WHEN SHARING IS OFF OR NO COLLECTOR IS SET (P2): the reason, not a button.
  const [stop, setStop] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    void readImprove<SharingView>("sharing", { project_id: projectId }).then((got) => {
      if (live && got.ok && !got.notModified && isSharing(got.data)) setStop(got.data.blocked ?? null);
    });
    void readImprove<{ bundle: string; masked: Record<string, number> }>("share_bundle", {
      project_id: projectId, unit_kind: unitKind, unit_id: unitId, actor,
    }).then((got) => {
      if (!live) return;
      if (got.ok && !got.notModified) {
        setBundle(got.data.bundle);
        setMasked(Object.values(got.data.masked ?? {}).reduce((a, b) => a + b, 0));
      } else if (!got.ok) setError(got.status === 403 ? "You can't share runs" : got.error);
    });
    return () => { live = false; };
  }, [projectId, unitKind, unitId, actor]);

  const send = async () => {
    if (bundle === null) return;
    setBusy(true);
    const r = await improveWrite("share_run", { project_id: projectId, actor, bundle, note });
    setBusy(false);
    if (r.ok) { showToast("success", "Run shared"); onClose(); }
    else setError(r.error);
  };

  return (
    <Sheet title="Share this run" testId="share-run" onClose={onClose}>
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
          <textarea value={bundle} onChange={(e) => setBundle(e.target.value)} aria-label="What will be sent"
                    data-testid="share-run-bundle" spellCheck={false} />
        ) : null}
        <textarea className="im-share-note" value={note} onChange={(e) => setNote(e.target.value)}
                  aria-label="A note for the BOSS project" placeholder="What went wrong?" data-testid="share-run-note" />
        <span className="im-actions">
          <button type="button" className="ag-btn ag-btn-primary" disabled={busy || Boolean(stop) || bundle === null || !bundle.trim()}
                  data-testid="share-run-send" title="Sent once, now. Not part of Improve BOSS."
                  onClick={() => void send()}>{busy ? "Sending…" : "Send"}</button>
          <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
          {error ? <span className="im-err" role="alert" title={error}>Not sent</span> : null}
        </span>
      </div>
    </Sheet>
  );
}

/** The button that opens it, for surfaces without a menu of their own (a workflow run). */
export function ShareRunButton({ projectId, actor, unitKind, unitId }: {
  projectId: string; actor: string; unitKind: "build" | "workflow_run"; unitId: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="ag-link" data-testid="share-run-open"
              title="Send this run, redacted and edited by you, to the BOSS project"
              onClick={() => setOpen(true)}>Share this run</button>
      {open ? <ShareRunSheet projectId={projectId} actor={actor} unitKind={unitKind} unitId={unitId}
                             onClose={() => setOpen(false)} /> : null}
    </>
  );
}
