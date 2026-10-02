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
        <h1 className="org-gate-title">That action did not go through</h1>
        <p className="org-gate-sub">
          {hidden
            ? "A request this page made to the BOSS API failed, and the server did not say why here."
            : message}
        </p>
        {error?.digest ? (
          <p className="org-gate-sub">
            Error id <code className="mono">{error.digest}</code> — the same id is in the console
            container&apos;s log next to the full reason.
          </p>
        ) : null}
        <p className="org-gate-sub">
          Nothing you had typed was sent twice. Try it again; if it fails the same way, check that
          the BOSS API is running (Repo and model shows its status), then retry.
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
