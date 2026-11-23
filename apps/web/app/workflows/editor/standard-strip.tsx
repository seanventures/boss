"use client";

/**
 * A STANDARD, EDITED IN PLACE: what changed from what BOSS shipped, and the way back.
 *
 *     "we should make all the workflows editable, and if they make mistakes we should go back
 *      to factory mode which is reset to original value"
 *
 * One strip under the editor's bar, only on a standard:
 *   - unchanged: one line, and Save as your copy (secondary: Save edits the standard itself);
 *   - Modified: "Changes from standard · N" (a line per step added, removed or changed), and
 *     Reset to standard, confirmed here on the page (never window.confirm);
 *   - New standard available: what the newer standard adds, your changes, and Take it with my
 *     changes (only when they apply cleanly) / Take the new standard only / Not now.
 * Runs already going keep the version they started on; the strip says so.
 */
import React, { useState } from "react";

import type { EditorMeta, StandardAct, StandardChange } from "../../../lib/workflow-editor-types";

const MARK: Record<StandardChange["change"], string> = { added: "a", removed: "r", changed: "c", setting: "c" };

function Changes({ changes, testId }: { changes: StandardChange[]; testId: string }) {
  return (
    <ul className="wf-std-diff" data-testid={testId} data-user-text>
      {changes.map((c, i) => (
        <li key={`${c.step_id ?? "setting"}-${i}`} data-change={MARK[c.change]}>{c.text}</li>
      ))}
    </ul>
  );
}

export function StandardStrip({ meta, blocked, busy, offerOpen, onOfferOpen, onAct, onSaveAsCopy, copyBlocked }: {
  meta: EditorMeta;
  /** Why the acts are unavailable now (unsaved changes, an older version open, no permission). */
  blocked: string | null;
  busy: StandardAct | null;
  offerOpen: boolean;
  onOfferOpen(open: boolean): void;
  onAct(act: StandardAct): void;
  onSaveAsCopy(): void;
  copyBlocked: string | null;
}): React.JSX.Element | null {
  const [confirming, setConfirming] = useState(false);
  const standard = meta.standard;
  if (!meta.is_catalogue || !standard) return null;
  const offer = standard.new_standard;
  const n = standard.changes.length;

  const act = (which: StandardAct) => { if (!blocked && !busy) onAct(which); };

  return (
    <section className="wf-std" aria-label="Standard workflow" data-testid="wf-standard"
             data-modified={standard.modified || undefined}>
      {offer && offerOpen ? (
        <div className="wf-std-offer" data-testid="wf-std-offer">
          <div className="wf-std-row">
            <strong>New standard available</strong>
            <span className="wf-std-hint" data-tip="Unmodified standards update by themselves. Modified ones always ask.">
              v{offer.version}, from BOSS
            </span>
            <span className="wf-std-sp" />
            <button type="button" className="wf-link" onClick={() => onOfferOpen(false)}
                    data-testid="wf-std-offer-close">Close</button>
          </div>
          <Changes changes={offer.changes.map((c) => ({ ...c, text: `Standard now: ${c.text}` }))}
                   testId="wf-std-offer-changes" />
          {standard.changes.length ? (
            <div className="wf-std-hint" data-testid="wf-std-yours">
              <span>Your changes:</span>{" "}
              <span data-user-text>{standard.changes.map((c) => c.text.replace(/^[+~−-]\s*/, "")).join(" · ")}</span>
            </div>
          ) : null}
          <div className="wf-std-row">
            <button type="button" className="wf-btn wf-btn-primary" data-testid="wf-std-take-keep"
                    aria-disabled={blocked || !offer.applies_cleanly ? true : undefined}
                    data-tip={blocked || (offer.applies_cleanly ? "Your changes, on the new standard"
                      : `Your changes and the new standard touch the same things: ${offer.conflicts.slice(0, 3).join("; ")}`)}
                    onClick={() => { if (offer.applies_cleanly) act("take_new_keep"); }}>
              {busy === "take_new_keep" ? "Taking…" : "Take it, keep my changes"}
            </button>
            <button type="button" className="wf-btn" data-testid="wf-std-take-new"
                    aria-disabled={blocked ? true : undefined} data-tip={blocked || "Your changes stay in the version list"}
                    onClick={() => act("take_new")}>
              {busy === "take_new" ? "Taking…" : "Take the new one only"}
            </button>
            <button type="button" className="wf-btn wf-btn-ghost" data-testid="wf-std-not-now"
                    aria-disabled={blocked ? true : undefined} data-tip={blocked || undefined}
                    onClick={() => act("dismiss")}>
              Not now
            </button>
          </div>
        </div>
      ) : null}

      {standard.modified ? (
        <details className="wf-std-changes" data-testid="wf-std-changes" open>
          <summary>Changes from standard · {n}</summary>
          <Changes changes={standard.changes} testId="wf-std-change-list" />
        </details>
      ) : null}

      {confirming ? (
        <div className="wf-std-confirm" role="alertdialog" aria-label="Reset to standard?" data-testid="wf-std-confirm"
             onKeyDown={(e) => { if (e.key === "Escape") setConfirming(false); }}>
          <span data-tip="New runs use the shipped steps. Your edits stay in the version list; Restore brings them back.">
            Reset? Your edits stay in history.
          </span>
          <button type="button" className="wf-btn" autoFocus onClick={() => setConfirming(false)}
                  data-testid="wf-std-reset-cancel">Cancel</button>
          <button type="button" className="wf-btn wf-btn-danger" data-testid="wf-std-reset-confirm"
                  aria-disabled={blocked ? true : undefined} data-tip={blocked || undefined}
                  onClick={() => { if (!blocked) { setConfirming(false); act("reset"); } }}>
            {busy === "reset" ? "Resetting…" : "Reset"}
          </button>
        </div>
      ) : (
        <div className="wf-std-row">
          <span className="wf-std-hint" data-testid="wf-std-line"
                data-tip={standard.modified ? "Runs already going keep the version they started on."
                  : "Edit it here, or keep it and make your own copy."}>
            {standard.modified ? `New runs use v${meta.version ?? "?"}` : "Shipped by BOSS, editable"}
          </span>
          <span className="wf-std-sp" />
          {offer && !offerOpen ? (
            <button type="button" className="wf-link" onClick={() => onOfferOpen(true)}
                    data-testid="wf-std-review">Review new standard</button>
          ) : null}
          {standard.modified ? (
            <button type="button" className="wf-btn wf-btn-danger" data-testid="wf-std-reset"
                    aria-disabled={blocked ? true : undefined} data-tip={blocked || "Back to what BOSS shipped"}
                    onClick={() => { if (!blocked) setConfirming(true); }}>
              Reset to standard
            </button>
          ) : (
            <button type="button" className="wf-btn wf-btn-ghost" data-testid="wf-std-save-copy"
                    aria-disabled={copyBlocked ? true : undefined}
                    data-tip={copyBlocked || "Keep the standard as it is and make your own copy"}
                    onClick={() => { if (!copyBlocked) onSaveAsCopy(); }}>
              Save as your copy
            </button>
          )}
        </div>
      )}
    </section>
  );
}
