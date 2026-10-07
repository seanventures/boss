"use client";

import React, { useEffect, useRef, useState } from "react";

import { HANDOFF_KEY, readHandoff, type ManifestHandoff } from "../../lib/identity/github-manifest";

/** Posts the manifest Setup left for this tab to GitHub (GitHub's manifest flow). */
export function GitHubAppHandoff({ origins }: { origins: string[] }) {
  const form = useRef<HTMLFormElement>(null);
  const [handoff, setHandoff] = useState<ManifestHandoff | null | undefined>(undefined);

  useEffect(() => {
    let raw: string | null = null;
    try {
      raw = window.sessionStorage.getItem(HANDOFF_KEY);
      window.sessionStorage.removeItem(HANDOFF_KEY);
    } catch { /* storage blocked: nothing to hand off */ }
    setHandoff(readHandoff(raw, origins));
  }, [origins]);

  useEffect(() => { if (handoff) form.current?.submit(); }, [handoff]);

  return (
    <main style={{ maxWidth: 420, margin: "80px auto", padding: "0 16px", font: "inherit", color: "var(--ink)" }}>
      {handoff === undefined || handoff ? (
        <>
          <h1 style={{ fontSize: 18, margin: "0 0 8px" }}>Opening GitHub…</h1>
          <p style={{ margin: "0 0 16px", color: "var(--muted)" }}>GitHub asks you to name and create the App, then sends you back to Setup.</p>
          {handoff ? (
            <form ref={form} method="POST" action={handoff.post_url}>
              <input type="hidden" name="manifest" value={JSON.stringify(handoff.manifest)} />
              <button type="submit" className="button" title="If GitHub did not open, continue by hand">Continue to GitHub</button>
            </form>
          ) : null}
        </>
      ) : (
        <>
          <h1 style={{ fontSize: 18, margin: "0 0 8px" }}>Nothing to send to GitHub</h1>
          <p style={{ margin: "0 0 16px", color: "var(--muted)" }}>Start again from Setup › GitHub access.</p>
          <a className="button" href="/?area=setup">Back to Setup</a>
        </>
      )}
    </main>
  );
}
