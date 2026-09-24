"use client";

import { useEffect, useState } from "react";

const TYPICAL_SECONDS = 7;

export default function ConsoleLoading() {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => setElapsed((seconds) => seconds + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <div className="bx-shell" data-testid="console-route-loading">
      <aside className="bx-sidebar" aria-hidden>
        <div className="bx-brand">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img className="bx-brand-logo" src="/boss-mark.png" alt="" />
          <span className="bx-brand-name">BOSS</span>
        </div>
        <div className="bx-skel-nav">
          {[0, 1, 2, 3, 4].map((item) => (
            <span className="bx-skel bx-skel-navitem" key={item} />
          ))}
        </div>
      </aside>

      <div className="bx-main">
        <header className="bx-topbar" aria-hidden>
          <span className="bx-skel bx-skel-line" style={{ width: 150 }} />
        </header>
        <div className="bx-loading" role="status" aria-live="polite" data-testid="console-route-loading-bar">
          <span className="bx-loading-bar" aria-hidden />
          <span className="bx-loading-text">
            Loading the console
            {elapsed >= 3 ? (
              <span className="bx-loading-elapsed" data-testid="console-route-loading-elapsed">
                {elapsed}s{elapsed >= TYPICAL_SECONDS * 2 ? " — longer than usual" : ""}
              </span>
            ) : null}
          </span>
        </div>
        <main className="bx-content bx-content-loading" aria-hidden>
          <span className="bx-skel bx-skel-title" />
          <span className="bx-skel bx-skel-line" style={{ width: "32%" }} />
          <div className="bx-skel-rows">
            {[0, 1, 2, 3].map((item) => (
              <span className="bx-skel bx-skel-row" key={item} />
            ))}
          </div>
        </main>
      </div>
    </div>
  );
}
