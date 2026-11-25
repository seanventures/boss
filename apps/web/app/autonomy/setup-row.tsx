"use client";

/**
 * SETUP › AUTONOMY (BOSSAPP-1441, approved wireframe): one line — "Development: agents act ·
 * Production: you decide" — and Change › to Govern › Where people decide, where it is set.
 */
import React, { useEffect, useState } from "react";
import { ArrowSquareOut, RocketLaunch } from "@phosphor-icons/react";

import { DEV_WORDS, readPolicy } from "../../lib/autonomy";
import { usePolledRead } from "../../lib/log-stream";
import { consoleHref } from "../console-href";
import { SectionLabel } from "../section-label";
import "./autonomy.css";

export function AutonomySetupRow({ projectId }: { projectId: string }) {
  const read = usePolledRead({ key: `au-setup:${projectId}`, read: (e, sig) => readPolicy(projectId, e, sig), pollMs: null, paused: false, enabled: !!projectId });
  // The link carries the org from the address bar; set after hydration so the server's text matches.
  const [href, setHref] = useState("?area=budget&tab=autonomy");
  useEffect(() => { setHref(consoleHref({ area: "budget", tab: "autonomy" })); }, []);
  const p = read.data?.policy ?? null;
  return (
    <section className="su-row" data-testid="su-row-autonomy" aria-label="Autonomy" id="su-autonomy">
      <div className="su-row-label">
        <SectionLabel as="span" tip="When agents act without asking. Production always waits for you.">Autonomy</SectionLabel>
      </div>
      <div className="su-row-body">
        <div className="au-line" data-testid="su-autonomy-line">
          <RocketLaunch size={16} className="au-ico" aria-hidden />
          {p ? (
            <>
              <span>Development: <b data-testid="su-autonomy-dev">{DEV_WORDS[p.dev]}</b></span>
              <span className="au-faint" aria-hidden>·</span>
              <span>Production: <b>you decide</b></span>
            </>
          ) : <span className="au-faint">{read.error ? "Could not read the autonomy policy" : "…"}</span>}
          <span className="au-spacer" />
          <a className="au-link" href={href} data-testid="su-autonomy-change" title="Govern › Where people decide">
            Change <ArrowSquareOut size={13} aria-hidden /></a>
        </div>
      </div>
    </section>
  );
}
