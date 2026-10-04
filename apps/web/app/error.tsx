"use client";

import React from "react";

/**
 * WHAT HAPPENED AND WHAT TO DO, instead of Next's "Application error".
 *
 * The console had no error boundary. Ninety-odd server actions return void and throw when the
 * API refuses them — onboarding an organization, the identity forms, the meeting-role forms — so
 * any refusal landed on Next's generic page with no word about what failed and no way back to
 * where the operator was. This boundary keeps the console's chrome-free page honest: it says a
 * request failed, gives the reason when there is one (and the error id the server logged when
 * production hides the message), and offers the three ways forward.
 *
 * IT DOES NOT KNOW WHAT FAILED, so it does not claim to. The first version was headed "That
 * action did not go through" and promised "Nothing you had typed was sent twice". This boundary
 * also catches a render or a data fetch failing with no action taken, and nothing here can know
 * whether a pressed button reached the API before the error, so both lines could be untrue
 * (review of milestone/console-b, 2026-10-02). It says what it knows and how to check the rest.
 */
export default function ConsoleError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const message = (error?.message || "").trim();
  // Production replaces a server error's message with this sentence; it says nothing useful.
  const hidden = !message || /omitted in production builds/i.test(message);
  return (
    <main className="org-gate" data-testid="console-error">
      <div className="org-gate-inner">
        <h1 className="org-gate-title">This page hit an error</h1>
        <p className="org-gate-sub">
          {hidden
            ? "Loading this page or a request it made to the BOSS API failed, and the server did not say why here."
            : message}
        </p>
        {error?.digest ? (
          <p className="org-gate-sub">
            Error id <code className="mono">{error.digest}</code> — the same id is in the console
            container&apos;s log next to the full reason.
          </p>
        ) : null}
        <p className="org-gate-sub">
          If you had just pressed a button, it may or may not have reached BOSS: look at the area
          you were in before pressing it again. Otherwise try again; if it fails the same way,
          check that the BOSS API is running (Repo and models shows its status), then retry.
        </p>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button type="button" className="cap-btn primary" onClick={() => reset()}>
            Try again
          </button>
          <button type="button" className="cap-btn" onClick={() => window.location.reload()}>
            Reload this page
          </button>
          <a className="cap-btn" href="/">Back to Command Center</a>
        </div>
      </div>
    </main>
  );
}
