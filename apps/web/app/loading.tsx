"use client";

/**
 * What the browser shows during the six and a half seconds before the console exists.
 *
 * Measured on goldeneye, cold, against the URL an operator actually bookmarks:
 *
 *     https://boss.local.sean.network/?org=org_boss&area=code_tasks
 *     time_to_first_byte: 6.549s   total: 7.373s   size: 5.6 MB
 *
 * TIME TO FIRST BYTE — not time to finish. `HomePage` is an async server component that awaits
 * roughly fifty feeds before returning any JSX, and there was no route-level Suspense boundary,
 * so Next had nothing to send until all of it landed. For six and a half seconds the browser
 * held the previous page or a blank tab, and the tab spinner was the only evidence the click
 * had registered.
 *
 * This is why the complaint kept coming back after every fix. The console HAS indicators — a
 * sticky loading bar with an elapsed counter, a Syncing chip in the header — and every one of
 * them lives inside HTML that has not arrived yet. A better indicator inside the page could
 * never have worked: the page is the thing that is missing.
 *
 * `loading.tsx` renders from the static shell and streams IMMEDIATELY, before the first await
 * in the page runs. The wait now starts with something on screen.
 *
 * It mirrors the console's own frame — sidebar column, top bar, the same sticky loading strip —
 * rather than showing a centred spinner. Six seconds of a foreign-looking screen reads as
 * "something else opened"; six seconds of the console's own frame filling in reads as loading,
 * which is what is happening.
 */
import { useEffect, useState } from "react";

/** Roughly what a cold load costs, measured. Used for WORDING, never as a progress figure. */
const TYPICAL_SECONDS = 7;

export default function ConsoleLoading() {
  // The count is the difference between "it is working" and "it has stopped". A static sentence
  // for seven seconds means both, and the reader has to assume the worse one.
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setElapsed((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <div className="bx-shell" data-testid="console-route-loading">
      <aside className="bx-sidebar">
        <div className="bx-brand">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img className="bx-brand-logo" src="/boss-mark.png" alt="" />
          {/* `bx-brand-copy` in the real shell has no rule behind it, so it is left off here
              rather than copied: a class name that styles nothing is a thing the next reader
              has to look up before finding out it does not matter. */}
          <span className="bx-brand-name">BOSS</span>
          <span className="bx-brand-tag">Orchestration OS</span>
        </div>
        {/* The nav's shape without its labels. Naming the areas here would make them look
            clickable a second before they are, and a dead link is worse than a grey bar. */}
        <div className="bx-skel-nav" aria-hidden>
          {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
            <span className="bx-skel bx-skel-navitem" key={i}
                  style={{ animationDelay: `${i * 70}ms` }} />
          ))}
        </div>
      </aside>

      <div className="bx-main">
        <header className="bx-topbar" aria-hidden>
          <span className="bx-skel bx-skel-chip" style={{ width: 150 }} />
          <span className="bx-skel bx-skel-chip" style={{ marginLeft: "auto" }} />
          <span className="bx-skel bx-skel-chip" />
        </header>

        {/* The same strip the console uses once it is running, in the same place, so the
            message does not jump when the real one takes over. */}
        <div className="bx-loading" role="status" aria-live="polite"
             data-testid="console-route-loading-bar">
          <span className="bx-loading-bar" aria-hidden />
          <span className="bx-loading-text">
            Loading the console — the server is gathering every feed
            {/* Only after a few seconds: on a warm load a number that appears instantly makes a
                fast page feel slow. */}
            {elapsed >= 3 ? (
              <span className="bx-loading-elapsed" data-testid="console-route-loading-elapsed">
                {elapsed}s{elapsed >= TYPICAL_SECONDS * 2 ? " — longer than usual" : ""}
              </span>
            ) : null}
          </span>
        </div>

        {/* Placeholders, not content: enough that the page has a shape to arrive into, and no
            numbers anywhere. An invented figure in a skeleton is read as real for exactly as
            long as it is on screen, and this screen is on for seven seconds. */}
        <main className="bx-content bx-content-loading">
          <div className="bx-skel-grid">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <div className="bx-skel-card" key={i} style={{ animationDelay: `${i * 90}ms` }}>
                <span className="bx-skel bx-skel-line" style={{ width: "58%" }} />
                <span className="bx-skel bx-skel-line" style={{ width: "92%" }} />
                <span className="bx-skel bx-skel-line" style={{ width: "40%" }} />
              </div>
            ))}
          </div>
        </main>
      </div>
    </div>
  );
}
