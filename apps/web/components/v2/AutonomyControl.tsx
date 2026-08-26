"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React, { useState } from "react";

/**
 * The autonomy switch — Supervised or Full auto.
 *
 * This is the one control in the console that reduces human oversight, so it behaves
 * differently from every other:
 *
 *   IT SAYS WHAT WILL CHANGE BEFORE IT CHANGES IT. Not "Full auto" as a label, but the actual
 *   consequence: reversible gates under the threshold stop waiting for you, and everything
 *   else still does.
 *
 *   IT ASKS TWICE. Turning oversight OFF is confirmed; turning it back ON is not, because the
 *   safe direction should never have friction.
 *
 *   IT REPORTS WHAT THE API SAID. Not what it hoped. "Retry started" printed regardless of the
 *   response is how a control ends up lying about work that never began.
 */

export type AutonomyState = {
  mode: string;
  detail: string;
  modes_available: string[];
  threshold_usd?: number;
  set_by?: string | null;
  set_at?: string | null;
};

export function AutonomyControl({
  projectId,
  actor,
  initial,
}: {
  projectId: string;
  /** Never defaulted. An autonomy change with no owner makes every later act
   *  unattributable. */
  actor: string;
  initial: AutonomyState;
}) {
  const [state, setState] = useState<AutonomyState>(initial);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const isAuto = state.mode === "Full auto";
  const threshold = state.threshold_usd ?? 500;

  const change = async (mode: "supervised" | "full_auto") => {
    setBusy(true);
    setNote(null);
    setConfirming(false);
    try {
      const res = await fetch("/api/autonomy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, actor, mode }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setNote(String((data as { error?: string })?.error ?? `HTTP ${res.status}`));
        return;
      }
      setState(data as AutonomyState);
      setNote(mode === "full_auto" ? "Full auto is on." : "Back to supervised.");
    } catch (e) {
      setNote(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="v2-autonomy" data-testid="v2-autonomy" data-mode={state.mode}>
      <span className="v2-label">Autonomy</span>
      <span className="v2-autonomy-mode">
        <span className="v2-dot" data-status={isAuto ? "STUCK" : "RUNNING"} aria-hidden />
        {state.mode}
      </span>
      {/* The consequence, not the label. */}
      <p className="v2-autonomy-detail" data-testid="v2-autonomy-detail">
        {state.detail}
      </p>

      {confirming ? (
        <div className="v2-autonomy-confirm" data-testid="v2-autonomy-confirm">
          {/* Spelled out at the moment of the decision, not in a tooltip somewhere. */}
          <p>
            Gates under ${threshold.toLocaleString()} will approve themselves with no human
            review. Deploys, merges, sends and anything irreversible will still wait for you.
          </p>
          <button
            type="button"
            className="v2-btn"
            disabled={busy}
            onClick={() => void change("full_auto")}
            data-testid="v2-autonomy-confirm-yes"
          >
            Turn on Full auto
          </button>
          <button
            type="button"
            className="v2-btn v2-btn-quiet"
            disabled={busy}
            onClick={() => setConfirming(false)}
            data-testid="v2-autonomy-cancel"
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="v2-btn v2-btn-quiet v2-autonomy-toggle"
          disabled={busy}
          // Turning oversight OFF is confirmed; turning it back ON is immediate, because the
          // safe direction should never have friction.
          onClick={() => (isAuto ? void change("supervised") : setConfirming(true))}
          data-testid="v2-autonomy-toggle"
        >
          {busy ? "Saving…" : isAuto ? "Return to supervised" : "Switch to Full auto"}
        </button>
      )}

      {note ? (
        <p className="v2-autonomy-note" data-testid="v2-autonomy-note">
          {note}
        </p>
      ) : null}

      {/* Who turned it off, kept visible while it is off. */}
      {isAuto && state.set_by ? (
        <p className="v2-autonomy-note" data-testid="v2-autonomy-setby">
          set by {state.set_by}
        </p>
      ) : null}
    </div>
  );
}
