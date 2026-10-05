"use client";

/**
 * One deploy as a 56px rail row (infra-observability spec §4.6, §6.3): state dot + word,
 * `app · env`, sha7, host or account, the trigger word, the approver's face, one step dot per
 * manifest step, duration, age, the one action the row's `can` allows, and `↗` to its drawer.
 * The words and `can` are the server's; this file maps no raw status. `DeployRail` groups rows by
 * day (TODAY · YESTERDAY · <weekday>) — the Deploys tab and the target drawer's RECENT DEPLOYS
 * both draw it.
 */
import React, { useState } from "react";

import { showToast } from "../client-forms";
import type { DeployRow, StepState } from "../../lib/infra-view";
import { RunFace } from "../agents/run-face";
import { afterWrite, decideDeploy, rollbackDeploy } from "./infra-actions";
import { ago, dur, localTime, useNow } from "./infra-util";
import { StateWord } from "./kit";
import { LOCAL, useLocalTime, type LocalTime } from "../../lib/local-time";

const GLYPH: Record<StepState, string> = { done: "●", running: "◐", failed: "✗", pending: "○", skipped: "–" };
const STEP_WORD: Record<StepState, string> = { done: "done", running: "running", failed: "failed", pending: "waiting", skipped: "skipped" };

/** One dot per manifest step: filled done, ◐ running (sweeps), ✗ failed, ○ waiting. */
export function StepDots({ steps, durationOf }: { steps: { s: StepState; t: string }[]; durationOf?: (i: number) => string }) {
  if (!steps.length) return <span className="inf-steps inf-faint" aria-label="No steps">—</span>;
  const done = steps.filter((s) => s.s === "done").length;
  return (
    <span className="inf-steps" role="img" aria-label={`${done} of ${steps.length} steps done`} data-testid="inf-step-dots">
      {steps.map((s, i) => (
        <span key={i} className="inf-step" data-s={s.s} data-anim={s.s === "running" ? "" : undefined}
              title={[s.t || `Step ${i + 1}`, STEP_WORD[s.s], durationOf?.(i)].filter(Boolean).join(" · ")}
              aria-hidden>{GLYPH[s.s]}</span>
      ))}
    </span>
  );
}

/** `manual` · `merge` · `workflow` · `redeploy`, or `↩ <sha7>` for a rollback. */
export function triggerWord(row: Pick<DeployRow, "trigger" | "rollbackOfSha7">): string {
  return row.trigger === "rollback" ? (row.rollbackOfSha7 ? `↩ ${row.rollbackOfSha7}` : "rollback") : row.trigger;
}

/** Who approved: their face, `You`, or nothing (an unapproved or ungated deploy). */
export function Approver({ approver }: { approver: DeployRow["approver"] }) {
  if (!approver) return <span className="inf-dep-who" aria-hidden />;
  if (approver.roleKey) {
    return <span className="inf-dep-who" title="Approved by"><RunFace roleKey={approver.roleKey} size={20} standalone /></span>;
  }
  const you = approver.actor === "you";
  return (
    <span className="inf-dep-who" title={you ? "Approved by you" : `Approved by ${approver.actor ?? "a person"}`}>
      <RunFace roleKey={null} variant={you ? "you" : "person"} personId={you ? undefined : approver.actor} size={20} standalone />
    </span>
  );
}

export type DeployRowProps = {
  row: DeployRow;
  projectId: string;
  actor: string;
  /** Open its drawer (`deploy=<ref>`). */
  onOpen: (ref: string) => void;
  /** A write here succeeded: re-read the list. */
  onChanged?: () => void;
  /** The target drawer's rows omit the row action. */
  actions?: boolean;
  now: number | null;
};

export function DeployRowView({ row, projectId, actor, onOpen, onChanged, actions = true, now }: DeployRowProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const where = row.host?.name ?? row.account ?? "";
  const when = row.startedAt ?? row.finishedAt;
  const age = ago(row.finishedAt ?? row.startedAt, now);
  const tip = [where, row.approver?.actor === "you" ? "approved by you" : ""].filter(Boolean).join(" · ");

  const decide = async (verdict: "approve" | "reject") => {
    if (!row.decisionId) return;
    setBusy(verdict); setError(null);
    const r = await decideDeploy(row.decisionId, actor, verdict);
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", verdict === "approve" ? "Deploy approved" : "Deploy rejected");
    afterWrite(onChanged);
  };
  const rollback = async () => {
    setBusy("rollback"); setError(null);
    const r = await rollbackDeploy(projectId, row.ref, actor);
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    const sha = typeof (r.data as { sha7?: unknown }).sha7 === "string" ? (r.data as { sha7: string }).sha7 : row.rollbackToSha7 ?? "";
    showToast("success", sha ? `Rolling back to ${sha}` : "Rolling back");
    afterWrite(onChanged);
  };

  return (
    <li className="inf-dep-row" data-testid="inf-deploy-row" data-ref={row.ref} data-tone={row.tone} title={tip || undefined}>
      <StateWord w={{ word: row.word, tone: row.tone }} />
      <button type="button" className="inf-dep-name" onClick={() => onOpen(row.ref)} title={`${row.app} · ${row.env}`}>
        <span data-copy="data">{row.app}</span><span className="inf-faint"> · </span><span data-copy="data">{row.env}</span>
      </button>
      <span className="inf-mono inf-dep-sha" data-copy="data" title={row.refName ? `Ref ${row.refName}` : undefined}>{row.sha7 ?? "—"}</span>
      <span className="inf-dep-host inf-trunc inf-muted" data-copy="data" title={where || undefined}>{where || "—"}</span>
      <span className="inf-dep-trigger inf-muted">{triggerWord(row)}</span>
      <Approver approver={row.approver} />
      <StepDots steps={row.steps} />
      <span className="inf-num inf-muted inf-dep-dur">{dur(row.durationMs) || "—"}</span>
      <span className="inf-num inf-faint inf-dep-age" title={when ? localTime(when) : undefined}>{age || "now"}</span>
      <span className="inf-dep-act">
        {actions && row.can.approve ? (
          <button type="button" className="ag-btn ag-btn-primary" disabled={busy !== null} data-testid="inf-deploy-row-approve"
                  onClick={() => void decide("approve")}>{busy === "approve" ? "Approving…" : "Approve"}</button>
        ) : null}
        {actions && row.can.reject ? (
          <button type="button" className="ag-btn" disabled={busy !== null} data-testid="inf-deploy-row-reject"
                  onClick={() => void decide("reject")}>Reject</button>
        ) : null}
        {actions && row.can.rollback ? (
          <button type="button" className="ag-btn" disabled={busy !== null} data-testid="inf-deploy-rollback"
                  title="Redeploy the last commit that went live here" onClick={() => void rollback()}>
            {busy === "rollback" ? "Rolling back…" : "Roll back"}
          </button>
        ) : null}
        {error ? <span className="inf-error inf-trunc" role="alert" title={error} data-copy="data">Couldn&apos;t do that</span> : null}
      </span>
      <button type="button" className="inf-link inf-dep-go" aria-label={`Open ${row.app} ${row.env} deploy`}
              data-testid="inf-deploy-row-open" onClick={() => onOpen(row.ref)}>↗</button>
    </li>
  );
}

/** TODAY · YESTERDAY · MONDAY … · Sep 28 — by the local day a deploy started. */
export function dayLabel(iso: string | null, now: number, lt: LocalTime = LOCAL): string {
  if (!iso) return "EARLIER";
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return "EARLIER";
  const start = (t: Date) => new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime();
  const days = Math.round((start(new Date(now)) - start(d)) / 86_400_000);
  if (days <= 0) return "TODAY";
  if (days === 1) return "YESTERDAY";
  if (days < 7) return lt.weekday(d.getTime()).toUpperCase();
  return lt.monthDay(d.getTime()).toUpperCase();
}

/** Rows grouped by day. `grouped={false}` for a short list (the target drawer's 5). */
export function DeployRail({ rows, projectId, actor, onOpen, onChanged, grouped = true, actions = true, testId = "inf-deploy-rail" }: {
  rows: DeployRow[]; projectId: string; actor: string; onOpen: (ref: string) => void; onChanged?: () => void;
  grouped?: boolean; actions?: boolean; testId?: string;
}) {
  const now = useNow(30_000);
  const lt = useLocalTime();
  const groups: { label: string; rows: DeployRow[] }[] = [];
  for (const row of rows) {
    // Day heads need the viewer's clock, which starts after mount (the server render agrees).
    const label = grouped && now !== null ? dayLabel(row.startedAt ?? row.finishedAt, now, lt) : "";
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.rows.push(row); else groups.push({ label, rows: [row] });
  }
  return (
    <div className="inf-rail" data-testid={testId}>
      {groups.map((g, i) => (
        <section key={`${g.label}-${i}`} className="inf-rail-day" aria-label={g.label || "Deploys"}>
          {g.label ? <h4 className="inf-rail-day-head">{g.label}</h4> : null}
          <ul className="inf-dep-rows">
            {g.rows.map((r) => (
              <DeployRowView key={r.ref} row={r} projectId={projectId} actor={actor} onOpen={onOpen}
                             onChanged={onChanged} actions={actions} now={now} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
