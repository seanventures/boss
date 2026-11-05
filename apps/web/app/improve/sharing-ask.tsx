"use client";

/**
 * Setup's last question (docs/product/RUN_SCORECARDS.md, "Consent"): Improve BOSS, asked ONCE,
 * in the doc's own words, off unless the person turns it on. "Not now" is an answer too — it is
 * recorded (Govern's log) so the question is not asked again; Govern › Sharing changes it later.
 *
 * Under it, said plainly because it is on by default: the daily heartbeat, what it carries, and
 * its one opt-out. The installer's final message does not carry this (install.sh is unchanged),
 * so Setup and the README are where an operator meets it.
 */
import { GatedButton } from "../../components/gated";
import React, { useEffect, useState } from "react";

import { improveWrite, isSharing, readImprove, type SharingView } from "../../lib/improve";
import { showToast } from "../client-forms";
import { ConsentText, HeartbeatFields } from "./consent";
import "./improve.css";

export function SharingAsk({ projectId, actor }: { projectId: string; actor: string }) {
  const [v, setV] = useState<SharingView | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    void readImprove<SharingView>("sharing", { project_id: projectId }).then((got) => {
      if (live && got.ok && !got.notModified && isSharing(got.data)) setV(got.data);
    });
    return () => { live = false; };
  }, [projectId]);
  if (!v) return null;
  const answer = async (on: boolean) => {
    setBusy(true);
    const r = await improveWrite("switch", { project_id: projectId, actor, switch: "improve", on });
    setBusy(false);
    if (!r.ok) { showToast("error", r.status === 403 ? "You can't change this" : "Not saved"); return; }
    if (isSharing(r.data)) setV(r.data);
    showToast("success", on ? "Sharing on" : "Sharing off");
  };
  const sw = v.switches;
  const govern = "/?area=budget&tab=sharing";
  // ASKED ONCE, THEN GONE (BOSSAPP-898, "this should have only Model and Repos setup"): once
  // answered, or set off by the environment, Setup shows nothing; Govern › Sharing changes it.
  if (v.env_disabled || sw.improve.asked) return null;
  return (
    <section className="im-ask" aria-label="Improve BOSS" data-testid="su-sharing">
      {v.env_disabled ? (
        <span className="im-ask-head">
          <h3 className="im-ask-title">Sharing</h3>
          <span className="im-muted" title="Set in this install's environment">Off by {v.env_disabled}</span>
        </span>
      ) : !sw.improve.asked ? (
        <>
          <ConsentText />
          <span className="im-ask-head">
            <GatedButton permission="telemetry:manage" type="button" className="ag-btn ag-btn-primary" disabled={busy} data-testid="su-sharing-on"
                    onClick={() => void answer(true)}>Turn on</GatedButton>
            <GatedButton permission="telemetry:manage" type="button" className="ag-btn" disabled={busy} data-testid="su-sharing-no"
                    title="Off. Change it any time in Govern › Sharing." onClick={() => void answer(false)}>Not now</GatedButton>
          </span>
        </>
      ) : (
        <span className="im-ask-head">
          <h3 className="im-ask-title">Improve BOSS</h3>
          <span className="im-muted" data-testid="su-sharing-state">{sw.improve.on ? "On" : "Off"}</span>
          <a className="ag-link" href={govern} title="See the next payload, or change it">Change ↗</a>
        </span>
      )}
      {!v.env_disabled ? (
        <span className="im-ask-head" data-testid="su-heartbeat">
          <span className="im-muted" title="Once a day, separate from Improve BOSS. Nothing else.">
            Daily heartbeat {sw.heartbeat.on ? "on" : "off"}{v.blocked ? " · no collector set" : ""}
          </span>
          <HeartbeatFields />
          <a className="ag-link" href={govern} data-testid="su-heartbeat-optout"
             title="Turn it off in Govern › Sharing, or set BOSS_TELEMETRY=off">{sw.heartbeat.on ? "Opt out ↗" : "Change ↗"}</a>
        </span>
      ) : null}
    </section>
  );
}
