"use client";

import React from "react";

/**
 * The last resort: an error in the root layout itself, where app/error.tsx cannot render. Same
 * promise — say what happened and what to do — with no stylesheet to lean on, because the
 * layout that loads it is the thing that failed.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body style={{ fontFamily: "system-ui, sans-serif", padding: 32, maxWidth: 640 }}>
        <h1>The console could not load</h1>
        <p>
          The page failed before it could draw. This usually means the console could not reach
          the BOSS API, or the API refused the request.
        </p>
        {error?.digest ? (
          <p>
            Error id <code>{error.digest}</code> — search the console container&apos;s log for it to
            see the full reason.
          </p>
        ) : null}
        <p>Try again; if it fails the same way, check that the BOSS API is running, then reload.</p>
        <button type="button" onClick={() => reset()}>Try again</button>{" "}
        <a href="/">Back to Command Center</a>
      </body>
    </html>
  );
}
