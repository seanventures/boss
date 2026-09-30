"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React, { useState } from "react";

/**
 * Start a project from a brief.
 *
 * SAYS WHAT WILL HAPPEN, THEN SAYS WHAT DID
 * -----------------------------------------
 * An operator once clicked an idea, watched "Working", and nothing else ever happened — the
 * task really was created and really did sit at `open` forever, because the path it used ran
 * one planning job and stopped, and nothing downstream consumes a plan. The button was honest
 * about starting something and silent about what.
 *
 * So this names the sequence before the click (panel, PRD, your approval, code task) and
 * returns the ids afterwards, linked. A control that cannot confirm what it started cannot
 * report a failure either.
 */

export function LaunchModal({
  projectId,
  actor,
  orgId,
  onClose,
}: {
  projectId: string;
  actor: string;
  orgId?: string;
  onClose: () => void;
}) {
  const [brief, setBrief] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState<{ meeting_id?: string; id?: string } | null>(null);

  const launch = async () => {
    const directive = brief.trim();
    if (!directive || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/launch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, actor, directive }),
      });
      const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
      if (!res.ok) {
        setError(String(data.error ?? `HTTP ${res.status}`));
        return;
      }
      setStarted(data as { meeting_id?: string; id?: string });
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const q = orgId ? `?org=${encodeURIComponent(orgId)}` : "";
  const meetingId = started?.meeting_id ?? started?.id;

  return (
    <div className="v2-modal-backdrop" data-testid="v2-launch">
      <div className="v2-modal" role="dialog" aria-label="Start a project">
        <header className="v2-modal-head">
          <h2 className="v2-modal-title">Start a project</h2>
          <button
            type="button"
            className="v2-chat-close"
            onClick={onClose}
            aria-label="Close"
            data-testid="v2-launch-close"
          >
            ×
          </button>
        </header>

        {started ? (
          // What ACTUALLY happened, with a link. Not "Working".
          <div className="v2-modal-body" data-testid="v2-launch-started">
            <p>
              A panel is convening on this now. It will debate the directive, draft a PRD, and
              wait for your approval before any code task runs.
            </p>
            {meetingId ? (
              <a
                className="v2-meta-link"
                href={`/?area=meetings${q ? `&${q.slice(1)}` : ""}#${encodeURIComponent(String(meetingId))}`}
                data-testid="v2-launch-link"
              >
                Watch the panel →
              </a>
            ) : (
              // Started, but we cannot say where to look. Better than implying we can.
              <p className="v2-row-meta-dim" data-testid="v2-launch-noid">
                Started, but the API did not return an id to link to.
              </p>
            )}
          </div>
        ) : (
          <div className="v2-modal-body">
            {/* The sequence, before the click. */}
            <p className="v2-modal-note" data-testid="v2-launch-what">
              A panel of agents will debate this, draft a PRD, and wait for your approval. No
              code runs until you approve it.
            </p>
            <textarea
              className="v2-chat-input v2-launch-input"
              rows={5}
              value={brief}
              disabled={busy}
              placeholder="Describe the outcome you want. Be specific about what done looks like."
              onChange={(e) => setBrief(e.target.value)}
              data-testid="v2-launch-input"
            />
            {error ? (
              <p className="v2-launch-error" data-testid="v2-launch-error">
                {error}
              </p>
            ) : null}
            <div className="v2-controls-row">
              <button
                type="button"
                className="v2-btn"
                disabled={busy || !brief.trim()}
                onClick={() => void launch()}
                data-testid="v2-launch-go"
              >
                {busy ? "Convening the panel…" : "Convene the panel"}
              </button>
              <button
                type="button"
                className="v2-btn v2-btn-quiet"
                disabled={busy}
                onClick={onClose}
                data-testid="v2-launch-cancel"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
