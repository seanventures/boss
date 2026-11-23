"use client";

/**
 * WORK › DECIDED FOR YOU (BOSSAPP-1441, approved wireframe 2026-11-23): what BOSS's agents decided
 * on their own under dev autonomy, latest first. Each row: what happened, the agent · dev
 * autonomy, why it was not production, when, Run ›, and Undo (or "Can't undo" when the engine
 * stored none; a refused undo says the API's own 409 sentence) and the "Looks good" tick.
 *
 * Never amber: the Work tab's Needs-you count is the one amber signal on the screen.
 */
import React, { useState } from "react";
import {
  ArrowCounterClockwise, ArrowSquareOut, ArrowUUpLeft, Check, CheckCircle, CurrencyDollar, GitMerge, IdentificationCard,
  Play, Power, Prohibit, RocketLaunch, StopCircle, type Icon,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import {
  UNDO_LABEL, agentName, readDecided, reviewDecided, runLink, undoDecided, whatHappened, type Decided,
} from "../../lib/autonomy";
import { useConsoleLive } from "../../lib/console-live";
import { localWith } from "../../lib/local-time";
import { usePolledRead } from "../../lib/log-stream";
import { pxHref } from "../../lib/project-experience";
import { AgentAvatar } from "../agent-avatar";
import "./autonomy.css";

const KIND_ICON: Record<string, Icon> = {
  revert_merge: GitMerge, rollback_deploy: RocketLaunch, send_back: CheckCircle, remove_self_fix: Power,
  disable_skill: Power, revoke_grant: IdentificationCard, cancel_run: Play,
};
const UNDO_ICON: Record<string, Icon> = {
  revert_merge: ArrowCounterClockwise, rollback_deploy: ArrowUUpLeft, send_back: ArrowUUpLeft, remove_self_fix: Power,
  disable_skill: Power, revoke_grant: Prohibit, cancel_run: StopCircle,
};
function iconOf(d: Decided): Icon {
  if (d.undo && KIND_ICON[d.undo]) return KIND_ICON[d.undo];
  const t = `${d.action ?? ""} ${d.question}`.toLowerCase();
  if (/merge/.test(t)) return GitMerge;
  if (/deploy|ship|release/.test(t)) return RocketLaunch;
  if (/top.?up|spend|budget|\$/.test(t)) return CurrencyDollar;
  if (/install|self.?fix/.test(t)) return Power;
  return CheckCircle;
}
const DT: Intl.DateTimeFormatOptions = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false };

export function DecidedForYou({ projectId }: { projectId: string }) {
  const live = useConsoleLive();
  const read = usePolledRead({ key: `au-decided:${projectId}`, read: (e, sig) => readDecided(projectId, e, sig), pollMs: 30_000, paused: !live, enabled: !!projectId });
  const items = read.data ?? [];
  if (!items.length) return null;
  return (
    <section className="px-card au-lane" data-testid="au-decided" aria-label="Decided for you">
      <span className="px-lbl">Decided for you</span>
      <div className="au-rows" role="list">
        {items.map((d) => <Row key={d.id} d={d} projectId={projectId} onChanged={read.reload} />)}
      </div>
    </section>
  );
}

function Row({ d, projectId, onChanged }: { d: Decided; projectId: string; onChanged: () => void }) {
  const [busy, setBusy] = useState<"undo" | "ok" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [reviewed, setReviewed] = useState(d.reviewed);
  const I = iconOf(d);
  const U = d.undo ? UNDO_ICON[d.undo] ?? ArrowCounterClockwise : null;
  const run = runLink(d, projectId);
  const name = agentName(d.agent);
  const done = reviewed || d.reviewed;

  const undo = async () => {
    setBusy("undo"); setErr(null);
    const r = await undoDecided(projectId, d.id);
    setBusy(null);
    if (!r.ok) { setErr(r.error || "Could not undo."); return; }
    onChanged();
  };
  const looksGood = async () => {
    setBusy("ok"); setErr(null);
    const r = await reviewDecided(projectId, d.id);
    setBusy(null);
    if (!r.ok) { setErr(r.error || "Not saved."); return; }
    setReviewed(true);
    onChanged();
  };

  return (
    <div className="au-item" role="listitem" data-testid={`au-decided-${d.id}`} data-undone={d.undone || undefined}>
      <span className="au-ic" aria-hidden><I size={16} /></span>
      <div style={{ minWidth: 0 }}>
        <div className="au-what" data-testid={`au-decided-${d.id}-what`}>{whatHappened(d.question)}</div>
        <div className="au-meta">
          <span className="au-who"><AgentAvatar agent={d.agent} size={18} tip={name} />{name} · dev autonomy</span>
          {d.why ? <span className="au-tag" title={d.why}>{d.why}</span> : null}
          {d.at ? <time dateTime={d.at}>{localWith(d.at, DT)}</time> : null}
          <a className="au-link" href={run.external ? run.href : pxHref(run.href)} data-testid={`au-decided-${d.id}-run`}
             {...(run.external ? { target: "_blank", rel: "noreferrer noopener" } : {})}>Run <ArrowSquareOut size={12} aria-hidden /></a>
        </div>
      </div>
      <div className="au-act">
        {d.undone ? (
          <span className="au-btn" data-off data-testid={`au-decided-${d.id}-undone`}>Undone</span>
        ) : d.undo && U ? (
          <GatedButton permission="decision:approve" projectId={projectId} className="au-btn" disabled={busy !== null}
                       onClick={() => void undo()} data-testid={`au-decided-${d.id}-undo`}>
            <U size={13} aria-hidden />{UNDO_LABEL[d.undo] ?? "Undo"}</GatedButton>
        ) : (
          <span className="au-btn" data-off title={d.noUndo ?? undefined} data-testid={`au-decided-${d.id}-noundo`}>Can&apos;t undo</span>
        )}
        <GatedButton permission="decision:approve" projectId={projectId} className="au-tick" aria-pressed={done} disabled={done || busy !== null}
                     onClick={() => void looksGood()} aria-label="Looks good" title={done ? "Reviewed" : "Looks good"} data-testid={`au-decided-${d.id}-ok`}>
          <Check size={14} weight="bold" aria-hidden /></GatedButton>
      </div>
      {err ? <div className="au-err" role="alert" data-testid={`au-decided-${d.id}-err`}>{err}</div> : null}
    </div>
  );
}
