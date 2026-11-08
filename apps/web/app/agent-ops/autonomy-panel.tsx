"use client";

/**
 * AUTONOMY & SAFETY (design §4 L5, §9; spec §7 autonomy/settings/stop; stream 905) — Govern › Autonomy.
 *
 *   [ ■ STOP ALL AGENT WORK ]            Today  ████████░░  $3.35 spent · $0.40 held · $5 cap
 *   Task class        A0  A1  A2  A3   Track record
 *   CI fix            ●   ●   ◉   ○    ███████▉ 92% of 24   [Raise to A3]
 *   IaC drift         ●   ◉   ┄   ┄    —
 *
 * The safe direction is easy: Stop needs work_item:manage (operators too) and one confirmation.
 * Giving agents more room is privileged: Resume, and every level change, need autonomy:manage
 * (owner/admin). BOSS never raises a level itself; it suggests, and the "Raise" chip is the one
 * press that accepts the suggestion.
 */
import React, { useState } from "react";

import { GatedButton, useGate } from "../../components/gated";
import { useConsoleLive } from "../../lib/console-live";
import { usePolledRead } from "../../lib/log-stream";
import {
  LEVEL_MEANINGS, LEVEL_WORDS, MISSING, MODE_WORDS, autonomyPath, money, microsUsd, parseAutonomy, parseScorecard, parseSettings, polled, resumeAgents,
  scorecardPath, setAlwaysOn, setLevel, setReviewBuilds, setReviewBuildsInstall, settingsPath, stopAgents,
  type AlwaysOnMode, type ClassRow, type Envelope, type ModeState, type ReviewBuilds, type StopState,
} from "../../lib/ambient";
import { showToast } from "../client-forms";
import "./agent-ops.css";
import { MovedToProjectSettings } from "../projects/moved";

const LEVELS = ["A0", "A1", "A2", "A3"];
const rank = (l: string) => Math.max(0, LEVELS.indexOf(l));

/** Always-on mode (install-wide): Off · Ask first · Act within levels. Pinned when .env sets BOSS_AMBIENT_COS. */
export function AlwaysOnSwitch({ mode, onChanged }: { mode: ModeState; onChanged: () => void }) {
  const gate = useGate("autonomy:manage");
  const choose = (m: AlwaysOnMode) => {
    if (m === mode.mode || mode.pinned || !gate.allowed) return;
    void setAlwaysOn(m).then((r) => { showToast(r.ok ? "success" : "error", r.ok ? `Always-on: ${MODE_WORDS[m].label}` : `Not changed: ${r.error}`); onChanged(); });
  };
  const why = mode.pinned ? "Set on the server (BOSS_AMBIENT_COS in .env)" : gate.allowed ? "" : gate.reason;
  return (
    <div className="aa-mode" data-testid="aa-mode" data-mode={mode.mode} data-source={mode.source}>
      <b>Always-on agents</b>
      <span className="aa-mode-seg" role="radiogroup" aria-label="Always-on agents">
        {(Object.keys(MODE_WORDS) as AlwaysOnMode[]).map((m) => (
          <button key={m} type="button" role="radio" aria-checked={m === mode.mode} data-on={m === mode.mode || undefined}
                  aria-disabled={mode.pinned || !gate.allowed || undefined} title={why || MODE_WORDS[m].tip}
                  data-testid={`aa-mode-${m}`} onClick={() => choose(m)}>{MODE_WORDS[m].label}</button>
        ))}
      </span>
      {mode.pinned ? <span className="aa-sub" data-testid="aa-mode-pinned">Pinned in .env</span> : null}
    </div>
  );
}

/** Install-wide default: review always-on builds before they start. Off: they start on each project's
 *  Captain authority (build and PR only; merging and production deploys still need a person). */
export function ReviewBuildsDefault({ review, onChanged }: { review: ReviewBuilds; onChanged: () => void }) {
  const gate = useGate("autonomy:manage");
  const flip = () => {
    if (!gate.allowed) return;
    const next = !review.install;
    void setReviewBuildsInstall(next).then((r) => {
      showToast(r.ok ? "success" : "error", r.ok ? (next ? "Always-on builds wait for a review" : "Always-on builds start on the Captain's authority") : `Not changed: ${r.error}`);
      onChanged();
    });
  };
  return (
    <label className="aa-mode" data-testid="aa-review-builds" data-on={review.install || undefined}
           title={gate.allowed ? "Every project follows this until it sets its own in Project › Settings" : gate.reason}>
      <input type="checkbox" role="switch" checked={review.install} disabled={!gate.allowed} onChange={flip} data-testid="aa-review-builds-toggle" />
      <b>Review always-on builds before they start</b>
      <span className="aa-sub">{review.install ? "Each waits on a card for a person." : "Off: they start on the project's Captain authority. Merges still need a person."}</span>
    </label>
  );
}

const REVIEW_CHOICES: Array<{ key: string; label: string; value: boolean | null }> = [
  { key: "inherit", label: "Project's", value: null }, { key: "on", label: "Review", value: true }, { key: "off", label: "Start", value: false },
];

/** One class's own answer to "review always-on builds before they start" (null: the project's). */
function ReviewCell({ projectId, cls, onChanged }: { projectId: string; cls: ClassRow; onChanged: () => void }) {
  const gate = useGate("autonomy:manage", projectId);
  const now = cls.review_builds === null ? "inherit" : cls.review_builds ? "on" : "off";
  return (
    <select className="aa-review" aria-label={`${cls.title}: review always-on builds`} value={now} disabled={!gate.allowed}
            title={gate.allowed ? "Review always-on builds of this kind before they start" : gate.reason} data-testid="aa-review-class"
            onChange={(e) => {
              const pick = REVIEW_CHOICES.find((c) => c.key === e.target.value);
              if (!pick) return;
              void setReviewBuilds(projectId, pick.value, cls.task_class).then((r) => { showToast(r.ok ? "success" : "error", r.ok ? `${cls.title}: ${pick.label}` : `Not changed: ${r.error}`); onChanged(); });
            }}>
      {REVIEW_CHOICES.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
    </select>
  );
}

export function KillSwitch({ projectId, stop, onChanged, mode, startConfirming, onCancel }: {
  projectId: string; stop: StopState; onChanged: () => void; mode?: ModeState;
  /** Open on the confirm (Where people decide's header "Stop all" opens it here). */
  startConfirming?: boolean; onCancel?: () => void;
}) {
  const [confirming, setConfirmingRaw] = useState(!!startConfirming);
  const setConfirming = (v: boolean) => { setConfirmingRaw(v); if (!v) onCancel?.(); };
  const [reason, setReason] = useState("");
  const [everywhere, setEverywhere] = useState(false);
  const org = useGate("autonomy:manage");
  if (stop.stopped) {
    const global = stop.scope === "global";
    return (
      <div className="aa-kill" data-stopped data-testid="aa-kill">
        <b>⛔ Agent work is stopped{global ? " on every project" : ""}</b>
        <span className="aa-sub">{[stop.by && `by ${stop.by}`, stop.reason].filter(Boolean).join(" · ")}</span>
        {stop.scope === "env" ? <span className="aa-sub">Set on the server; clear BOSS_AMBIENT_DISABLED to resume.</span> : (
          <GatedButton permission="autonomy:manage" projectId={projectId} className="aa-kill-btn" data-resume data-testid="aa-resume"
                       title="Let agents act again within their levels"
                       onClick={() => void resumeAgents(projectId, "", global).then((r) => { showToast(r.ok ? "success" : "error", r.ok ? "Agents resumed" : `Not resumed: ${r.error}`); onChanged(); })}>
            Resume agents
          </GatedButton>
        )}
      </div>
    );
  }
  return (
    <div className="aa-kill" data-testid="aa-kill">
      {confirming ? (
        <form className="aa-confirm" data-testid="aa-kill-confirm"
              onSubmit={(e) => { e.preventDefault(); void stopAgents(projectId, reason.trim() || "stopped by a person", everywhere).then((r) => { showToast(r.ok ? "success" : "error", r.ok ? "Stopped — every agent lease released" : `Not stopped: ${r.error}`); setConfirming(false); onChanged(); }); }}>
          <b>Stop all agent work{everywhere ? " on every project" : " here"}?</b>
          <span className="aa-sub">Agents drop what they hold now. Nothing is undone.</span>
          <input type="text" aria-label="Reason" placeholder="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
          <label className="aa-sub" title={org.allowed ? "Stops agents on every project" : org.reason}>
            <input type="checkbox" checked={everywhere} disabled={!org.allowed} onChange={(e) => setEverywhere(e.target.checked)} data-testid="aa-kill-everywhere" /> Every project
          </label>
          <span style={{ display: "flex", gap: 6 }}>
            <GatedButton permission="work_item:manage" projectId={projectId} type="submit" className="aa-kill-btn" data-testid="aa-kill-confirm-go">Stop now</GatedButton>
            <button type="button" className="ag-btn" onClick={() => setConfirming(false)}>Cancel</button>
          </span>
        </form>
      ) : (
        <>
          <GatedButton permission="work_item:manage" projectId={projectId} className="aa-kill-btn" data-testid="aa-kill-btn"
                       title="Stop every agent here at once" onClick={() => setConfirming(true)}>■ Stop all agent work</GatedButton>
          <span className="aa-sub">{mode && mode.mode === "off" ? "Always-on is off: agents start nothing by themselves." : "Agents are on. They act only within the levels below."}</span>
        </>
      )}
    </div>
  );
}

export function EnvelopeMeter({ env }: { env: Envelope }) {
  const cap = env.cap_usd_micros || 1;
  const spent = Math.min(1, env.spent_usd_micros / cap);
  const held = Math.min(1 - spent, env.held_usd_micros / cap);
  return (
    <div className="aa-env" data-testid="aa-envelope" title={`Daily ambient budget; resets ${env.resets}`}>
      <b>Today's agent budget</b>
      <span className="aa-env-bar" aria-hidden>
        <span data-k="spent" style={{ width: `${spent * 100}%` }} /><span data-k="held" style={{ width: `${held * 100}%` }} />
      </span>
      <span className="aa-env-nums">
        <span><b>{money(microsUsd(env.spent_usd_micros))}</b> spent</span>
        <span><b>{money(microsUsd(env.held_usd_micros))}</b> held</span>
        <span><b>{money(microsUsd(env.available_usd_micros))}</b> left of {money(microsUsd(env.cap_usd_micros))}</span>
      </span>
    </div>
  );
}

function Track({ rec }: { rec: { decided: number; accepted_pct: number } | undefined }) {
  if (!rec || !rec.decided) return <span className="aa-sub" title="No agent decisions with an outcome yet">—</span>;
  const tone = rec.accepted_pct >= 85 ? "ok" : rec.accepted_pct >= 50 ? "warn" : "bad";
  return (
    <span className="aa-track" title={`${rec.accepted_pct}% of ${rec.decided} decisions accepted (85% over 20 earns a raise)`} data-testid="aa-track">
      <span className="aa-track-bar"><span data-tone={tone} style={{ width: `${rec.accepted_pct}%` }} /></span>
      {Math.round(rec.accepted_pct)}% · {rec.decided}
    </span>
  );
}

export function AutonomyMatrix({ projectId, classes, track, onChanged }: {
  projectId: string; classes: ClassRow[]; track: Record<string, { decided: number; accepted_pct: number }>; onChanged: () => void;
}) {
  const gate = useGate("autonomy:manage", projectId);
  const [pending, setPending] = useState<{ cls: ClassRow; level: string } | null>(null);
  const choose = (cls: ClassRow, level: string) => {
    if (!gate.allowed || level === cls.level || rank(level) > rank(cls.ceiling)) return;
    setPending({ cls, level });
  };
  const apply = async () => {
    if (!pending) return;
    const r = await setLevel(projectId, pending.cls.task_class, pending.level, rank(pending.level) > rank(pending.cls.level) ? "raised in console" : "lowered in console");
    showToast(r.ok ? "success" : "error", r.ok ? `${pending.cls.title} is now ${pending.level}` : `Not changed: ${r.error}`);
    setPending(null);
    onChanged();
  };
  return (
    <div className="aa-matrix-wrap">
      <table className="aa-matrix" data-testid="aa-matrix">
        <thead>
          <tr>
            <th>Task class</th>
            {LEVELS.map((l) => <th key={l} title={`${l} ${LEVEL_WORDS[l]} — ${LEVEL_MEANINGS[l]}`}>{l}<br /><span className="aa-sub">{LEVEL_WORDS[l]}</span></th>)}
            <th title="Review always-on builds of this kind before they start">Builds</th><th>Track record</th><th />
          </tr>
        </thead>
        <tbody>
          {classes.map((c) => (
            <React.Fragment key={c.task_class}>
              <tr data-testid="aa-class" data-class={c.task_class} data-level={c.level}>
                <td title={`Risk ${c.risk}${c.owner_role ? ` · owner ${c.owner_role.replace(/_/g, " ")}` : ""}`}>{c.title}</td>
                {LEVELS.map((l) => {
                  const locked = rank(l) > rank(c.ceiling);
                  return (
                    <td key={l}>
                      <button type="button" className="aa-cell" data-testid="aa-cell" data-level={l}
                              data-on={c.level === l || undefined} data-below={rank(l) < rank(c.level) || undefined}
                              data-locked={locked || undefined} data-suggested={c.suggested_level === l || undefined}
                              aria-label={`${c.title}: ${l}${c.level === l ? " (current)" : locked ? " (above the ceiling)" : ""}`}
                              title={locked ? `Ceiling is ${c.ceiling}` : c.level === l ? `Now: ${LEVEL_WORDS[l]}` : `Set to ${l} · ${LEVEL_WORDS[l]}`}
                              {...gate.props}
                              onClick={() => choose(c, l)}>{c.level === l ? "●" : ""}{gate.sr}</button>
                    </td>
                  );
                })}
                <td><ReviewCell projectId={projectId} cls={c} onChanged={onChanged} /></td>
                <td><Track rec={track[c.task_class]} /></td>
                <td>
                  {c.suggested_level && rank(c.suggested_level) > rank(c.level) ? (
                    <GatedButton permission="autonomy:manage" projectId={projectId} className="aa-raise" data-testid="aa-raise"
                                 title="BOSS earned it: 85%+ accepted, nothing reverted" onClick={() => choose(c, c.suggested_level!)}>
                      Raise to {c.suggested_level}
                    </GatedButton>
                  ) : null}
                </td>
              </tr>
              {pending && pending.cls.task_class === c.task_class ? (
                <tr className="aa-level-confirm" data-testid="aa-level-confirm">
                  <td colSpan={8}>
                    {rank(pending.level) > rank(c.level) ? "Raise" : "Lower"} {c.title} to <b>{pending.level} · {LEVEL_WORDS[pending.level]}</b>?{" "}
                    <GatedButton permission="autonomy:manage" projectId={projectId} className="ag-btn primary" data-testid="aa-level-apply" onClick={() => void apply()}>
                      Set {pending.level}
                    </GatedButton>{" "}
                    <button type="button" className="ag-btn" onClick={() => setPending(null)}>Cancel</button>
                  </td>
                </tr>
              ) : null}
            </React.Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function AutonomyPanel({ projectId }: { projectId: string }) {
  const live = useConsoleLive();
  const auto = usePolledRead({ key: autonomyPath(projectId), read: polled(autonomyPath(projectId), parseAutonomy), pollMs: 30_000, paused: !live, enabled: !!projectId });
  const settings = usePolledRead({ key: settingsPath(projectId), read: polled(settingsPath(projectId), parseSettings), pollMs: 15_000, paused: !live, enabled: !!projectId });
  const score = usePolledRead({ key: scorecardPath(projectId, 30), read: polled(scorecardPath(projectId, 30), parseScorecard), pollMs: 120_000, paused: !live, enabled: !!projectId });
  if (auto.error === MISSING) {
    return <div className="aa-empty" data-testid="aa-autonomy-missing"><b>Autonomous agents aren't on this install yet</b><span>Upgrade BOSS to set what agents may do on their own.</span></div>;
  }
  const track = Object.fromEntries((score.data?.by_class ?? []).map((c) => [c.task_class, c]));
  const reload = () => { auto.reload(); settings.reload(); };
  return (
    <section className="aa aa-safety" data-testid="aa-autonomy" aria-label="Autonomy and safety">
      {/* This project's level and ambient budget live on Project › Settings now (README parity). */}
      <MovedToProjectSettings section="autonomy" projectId={projectId} testid="aa-autonomy-moved" />
      <div className="aa-safety-top">
        {settings.data ? <AlwaysOnSwitch mode={settings.data.mode} onChanged={reload} /> : null}
        {settings.data ? <ReviewBuildsDefault review={settings.data.review_builds} onChanged={reload} /> : null}
        {settings.data ? <KillSwitch projectId={projectId} stop={settings.data.stop} mode={settings.data.mode} onChanged={reload} /> : <div className="aa-kill" />}
        {settings.data ? <EnvelopeMeter env={settings.data.envelope} /> : <div className="aa-env" />}
      </div>
      {auto.data ? <AutonomyMatrix projectId={projectId} classes={auto.data.classes} track={track} onChanged={reload} /> : null}
    </section>
  );
}
