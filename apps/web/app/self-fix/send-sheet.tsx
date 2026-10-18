"use client";

/**
 * SEND TO THE BOSS TEAM (docs/product/BOSS_FIXES_ITSELF.md §7.1–7.4, §10.4). Nothing has been
 * sent; this is exactly what will be. Opening it asks BOSS to build the preview and store its
 * exact bytes; Send sends those bytes.
 *
 *   TITLE · LABELS · DIFF (not editable, View) · BODY and EVIDENCE (editable, scrubbed: n masked)
 *   TRAILERS · Similar: (dedupe) · where it goes (a PR from your fork / evidence on #n / the
 *   collector / a patch) · the check (a private name, address or secret in the diff BLOCKS it)
 *   ☐ I checked this for private content     [ Send ] [ Export patch ] [ Cancel ]
 *
 * Editing the body or evidence resets the tick, and Send waits until the edit is saved (the
 * stored bytes are what is sent). With BOSS_TELEMETRY=off or DO_NOT_TRACK=1 only Export patch is
 * offered, with the reason.
 */
import React, { useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { showToast } from "../client-forms";
import {
  fileCount, readSelfFix, selfFixHref, selfFixWrite, type Send, type UpstreamView,
} from "../../lib/self-fix";

const DEDUPE: Record<Send["dedupe"]["state"], string> = {
  none: "none open · none merged", merged: "already merged upstream", open_pr: "an open pull request",
  closed_pr: "a closed pull request", open_issue: "an open issue", skipped: "not checked",
};

export function channelSentence(s: Send): string {
  const up = s.target?.upstream || "the BOSS repository";
  if (s.channel === "github_pr") {
    return s.target?.fork ? `Opens a pull request on github.com/${up} from your fork ${s.target.fork}.`
      : `Opens a pull request on github.com/${up}; it needs your fork first.`;
  }
  if (s.channel === "github_comment") {
    return `Adds this install's evidence to #${s.dedupe?.match?.number ?? "?"} instead of a new pull request.`;
  }
  if (s.channel === "collector") return "Goes to the BOSS team's collector; they open the pull request.";
  return "Exported as a patch for you to send.";
}

export function SendSheet({ projectId, actor, selfFixId, onClose, onSent }: {
  projectId: string; actor: string; selfFixId: string; onClose: () => void; onSent?: (s: Send) => void;
}) {
  const [send, setSend] = useState<Send | null>(null);
  const [off, setOff] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [body, setBody] = useState("");
  const [evidence, setEvidence] = useState("");
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState("");
  const [showDiff, setShowDiff] = useState(false);
  const who = { project_id: projectId, actor, id: selfFixId };

  /** A stored preview is what the sheet shows; an answer that is not one leaves the sheet as it was. */
  const isSend = (s: unknown): s is Send => !!s && typeof (s as Send).send_id === "string";
  const take = (s: unknown) => {
    if (!isSend(s)) { setError("BOSS did not return a preview"); return; }
    setSend(s); setBody(s.body ?? ""); setEvidence(s.evidence ?? ""); setChecked(false); setError("");
  };
  useEffect(() => {
    let live = true;
    void (async () => {
      const got = await readSelfFix<UpstreamView>("upstream", { project_id: projectId, id: selfFixId });
      if (!live) return;
      if (got.ok && !got.notModified) {
        setOff(got.data.telemetry_off ?? null);
        if (got.data.send && got.data.send.status === "previewed") { take(got.data.send); return; }
      }
      // No stored preview yet (or the last one was sent): build it. BOSS stores the exact bytes.
      const built = await selfFixWrite("preview", who);
      if (!live) return;
      if (built.ok) take(built.data);
      else setError(built.error);
    })();
    return () => { live = false; };
  }, [projectId, selfFixId]); // eslint-disable-line react-hooks/exhaustive-deps

  const edited = !!send && (body !== (send.body ?? "") || evidence !== (send.evidence ?? ""));
  const blocked = !!send && !send.check?.ok;
  const needsFork = !!send && send.channel === "github_pr" && !send.target?.fork;
  const canSend = !!send && !off && !blocked && !edited && checked && !needsFork && !busy && send.status === "previewed";

  const saveEdits = async () => {
    if (!send) return;
    setBusy("save");
    const got = await selfFixWrite("edit_preview", { ...who, send_id: send.send_id, body, evidence });
    setBusy("");
    if (got.ok) { take(got.data); showToast("success", "Preview updated"); }
    else showToast("error", got.error);
  };
  const doSend = async () => {
    if (!send || !canSend) return;
    setBusy("send");
    const got = await selfFixWrite("send", { ...who, send_id: send.send_id, checked: true });
    setBusy("");
    if (got.ok) {
      const s = got.data as unknown as Send;
      if (!isSend(s)) { showToast("error", "BOSS did not say what it sent"); return; }
      setSend(s);
      showToast(s.status === "sent" ? "success" : "error", s.status === "sent" ? "Sent to BOSS team" : `Not sent: ${s.status}`);
      onSent?.(s);
    } else showToast("error", got.error);
  };
  const makeFork = async () => {
    setBusy("fork");
    const got = await selfFixWrite("fork", who);
    if (got.ok) {
      const again = await selfFixWrite("preview", who);
      if (again.ok) take(again.data);
    } else showToast("error", got.error);
    setBusy("");
  };

  return (
    <div className="sfx-sheet" role="dialog" aria-label="Send to the BOSS team" data-testid="sfx-send">
      <header className="sfx-sheet-head">
        <h3 className="sfx-sheet-title">Send to BOSS team</h3>
        <span className="sfx-sub" title="BOSS stored the exact bytes; Send sends those and nothing else">
          Nothing sent yet ⓘ
        </span>
      </header>
      {error ? <div role="alert" data-testid="sfx-send-error">{error}</div> : null}
      {!send && !error ? <div className="sfx-sub">Building the preview…</div> : null}
      {send ? (
        <>
          <div className="sfx-send-where" data-testid="sfx-send-channel" title={send.channel_why || undefined}>
            {channelSentence(send)}
          </div>
          <div className="sfx-send-similar" data-testid="sfx-send-similar" title={send.dedupe?.why || "Searched by the fix's class signature"}>
            Similar: {DEDUPE[send.dedupe?.state ?? "skipped"]}
            {send.dedupe?.match ? (
              <> · <a className="ag-link" href={send.dedupe.match.url} target="_blank" rel="noreferrer"
                      title={send.dedupe.match.title}>#{send.dedupe.match.number}</a></>
            ) : null}
          </div>
          <dl className="sfx-card-grid">
            <dt>Title</dt><dd data-testid="sfx-send-title" data-user-text>{send.title}</dd>
            <dt>Labels</dt>
            <dd data-testid="sfx-send-labels">
              {(send.labels ?? []).map((l) => <span key={l} className="cap-badge tone-muted">{l}</span>)}
            </dd>
            <dt>Diff</dt>
            <dd data-testid="sfx-send-diff">
              {fileCount(send.diff_stat)} file(s) · +{send.diff_stat?.added ?? 0} −{send.diff_stat?.removed ?? 0}{" "}
              <button type="button" className="ag-link" data-testid="sfx-send-view-diff" aria-expanded={showDiff}
                      title="Not editable: to change the code, request changes on the fix"
                      onClick={() => setShowDiff((v) => !v)}>View</button>
            </dd>
            <dt>Body</dt>
            <dd>
              <textarea className="textarea sfx-text" aria-label="Pull request body" data-testid="sfx-send-body"
                        value={body} rows={6} onChange={(e) => { setBody(e.target.value); setChecked(false); }} />
              <span className="sfx-sub" title="Install names, hosts, addresses and secrets BOSS masked">
                scrubbed: {send.scrubbed?.body ?? 0} masked
              </span>
            </dd>
            <dt>Evidence</dt>
            <dd>
              <textarea className="textarea sfx-text" aria-label="Evidence" data-testid="sfx-send-evidence"
                        value={evidence} rows={5} onChange={(e) => { setEvidence(e.target.value); setChecked(false); }} />
              <span className="sfx-sub" title="Install names, hosts, addresses and secrets BOSS masked">
                scrubbed: {send.scrubbed?.evidence ?? 0} masked
              </span>
            </dd>
            <dt>Trailers</dt>
            <dd data-testid="sfx-send-trailers">
              {Object.entries(send.trailers ?? {}).map(([k, v]) => <code key={k} className="sfx-trailer">{k}: {v}</code>)}
            </dd>
          </dl>
          {showDiff ? <pre className="sfx-diff" data-testid="sfx-send-diff-text" data-user-text>{send.diff}</pre> : null}
          {blocked ? (
            <div className="sfx-blocked" role="alert" data-testid="sfx-send-blocked">
              <strong>Blocked:</strong>{" "}
              <span title="The diff is checked, never masked. Request changes so BOSS rewrites it with neutral names.">
                private content found ⓘ
              </span>
              <ul>
                {(send.check?.findings ?? []).map((f, i) => (
                  <li key={i} data-testid="sfx-send-finding">{f.where}: {f.kind}{f.detail ? ` (${f.detail})` : ""}</li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="sfx-ok" data-testid="sfx-send-check-ok" title="Checked against the private-infrastructure list, this install's names, addresses and secrets">
              ✓ No private names, addresses or secrets found
            </div>
          )}
          {off ? (
            <div className="sfx-sub" role="status" data-testid="sfx-send-off" title="Sharing is turned off on this install; nothing leaves it">
              {off}: export only
            </div>
          ) : null}
          {send.status === "sent" ? (
            <div className="sfx-ok" data-testid="sfx-send-sent">
              Sent{send.result?.url ? <> · <a className="ag-link" href={send.result.url} target="_blank" rel="noreferrer">Open ↗</a></> : null}
            </div>
          ) : null}
          {send.result?.error ? <div role="alert" data-testid="sfx-send-result-error">{send.result.error}</div> : null}
          <label className="sfx-tick">
            <input type="checkbox" data-testid="sfx-send-checked" checked={checked} disabled={!!off || blocked}
                   onChange={(e) => setChecked(e.target.checked)} />
            I checked this for private content
          </label>
          <div className="sfx-sheet-actions">
            {edited ? (
              <GatedButton permission="improvement:manage" projectId={projectId} className="ag-btn"
                           data-testid="sfx-send-save" disabled={!!busy} onClick={saveEdits}
                           title="Store the edited text as what will be sent">Save edits</GatedButton>
            ) : null}
            {needsFork && !off ? (
              <GatedButton permission="improvement:manage" projectId={projectId} className="ag-btn"
                           data-testid="sfx-send-fork" disabled={!!busy} onClick={makeFork}
                           title="Fork the BOSS repository with your own GitHub connection">Create my fork</GatedButton>
            ) : null}
            {off ? null : (
              <GatedButton permission="improvement:manage" projectId={projectId} className="ag-btn sfx-primary"
                           data-testid="sfx-send-go" disabled={!canSend} onClick={doSend}
                           title={blocked ? "Blocked: private content in the diff" : edited ? "Save your edits first"
                             : !checked ? "Tick that you checked it first" : "Send exactly this"}>
                {busy === "send" ? "Sending…" : "Send"}
              </GatedButton>
            )}
            <a className="ag-btn" data-testid="sfx-send-export" download
               href={selfFixHref("patch", { project_id: projectId, id: selfFixId })}
               title="The fix and its evidence as a file, to send yourself">Export patch</a>
            <button type="button" className="ag-link" data-testid="sfx-send-cancel" onClick={onClose}>Cancel</button>
          </div>
        </>
      ) : (
        <div className="sfx-sheet-actions">
          <button type="button" className="ag-link" data-testid="sfx-send-cancel" onClick={onClose}>Cancel</button>
        </div>
      )}
    </div>
  );
}
