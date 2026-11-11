"use client";

/**
 * GIT ACTIONS ON THE CHANGE PAGE (BOSSAPP-996). The operator: "provide few important git
 * commands in the ui, to pull, resolve, showing history etc.. we dont have to go to CLI to
 * execute these things."
 *
 * The header's third line, for an OPEN change, local or GitHub alike:
 *   - where the branch stands against its base (ahead · behind), from GET …/sync-preview;
 *   - Sync — merge the base into the branch (GitHub's "Update branch"), one press, recorded as
 *     an approved decision (POST …/sync), never ambient;
 *   - Restack — offered when the branch's last change merged as a squash (PR #31's machinery):
 *     syncing such a branch only entrenches the conflict, so Restack comes first;
 *   - the guided resolve: the conflicting files, named (a local repository knows them; GitHub
 *     only says CLEAN or DIRTY, and the line says so), behind an expander — and the honest next
 *     step when git alone cannot fix it: Request changes sends an agent;
 *   - History › — the repository's log on the project page.
 */
import React, { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise, CaretDown, CircleNotch, GitCommit, StackSimple, Warning } from "@phosphor-icons/react";

import { BoxPop } from "../../../../components/box-pop";
import { useGuardedCall } from "../../../../components/identity/identity-provider";
import { refusalSentence } from "../../../../lib/identity/api";
import type { ChangeSet } from "../../../../lib/repo-links";
import { getSyncPreview, postSync, type ChangeAt, type SyncPreview } from "../../../../lib/change-review";

export function SyncPanel({ cs, at, reread }: { cs: ChangeSet; at: ChangeAt; reread: () => Promise<unknown> }) {
  const [p, setP] = useState<SyncPreview | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const [busy, setBusy] = useState<"update" | "restack" | null>(null);
  const [open, setOpen] = useState(false);
  const guarded = useGuardedCall();
  const headSha = cs.head_sha;
  const look = useCallback(() => getSyncPreview(at)
    .then((v) => setP(v.unreachable || v.state !== "open" ? null : v))
    .catch(() => setP(null)), [at]);
  useEffect(() => {
    if (cs.state !== "open") { setP(null); return; }
    void look();
  }, [cs.state, headSha, look]);
  if (cs.state !== "open" || !p) return null;
  const word = cs.source === "github" ? "PR" : "change";
  const act = async (mode: "update" | "restack") => {
    if (busy) return;
    setBusy(mode); setSaid(null);
    try {
      await guarded(mode === "restack"
        ? `Restack this ${word}'s branch onto ${p.base}: replay only the commits made after its last merge.`
        : `Sync this ${word}'s branch with ${p.base}: merge ${p.base} in.`,
        () => postSync(at, mode));
      await reread();
      await look();
    } catch (e) {
      setSaid(refusalSentence(e));
      await look();
    } finally { setBusy(null); }
  };
  const conflicted = p.mergeable === false;
  return (
    <div className="gr-sync" data-testid="gr-sync" data-conflicted={conflicted || undefined}>
      <span className="cs-faint" data-testid="gr-sync-stand" title={`${p.branch} against ${p.base}`}>
        {p.ahead} ahead{p.behind ? <> · <b>{p.behind} behind</b></> : " · up to date with"} {p.base}</span>
      {/* Restack first: after a squash merge, syncing only entrenches the conflict (PR #31). */}
      {p.restack && (conflicted || p.behind > 0) ? (
        <button type="button" className="lr-btn" data-kind="ghost" disabled={!!busy} data-testid="gr-restack"
                title={`Its last change (#${p.restack.number}) merged as one commit, so the branch re-proposes merged work. Restack replays only what came after — recorded as a decision.`}
                onClick={() => void act("restack")}>
          {busy === "restack" ? <CircleNotch size={13} className="cs-spin" aria-hidden /> : <StackSimple size={13} aria-hidden />}Restack onto {p.base}</button>
      ) : null}
      {p.behind > 0 ? (
        <button type="button" className="lr-btn" data-kind="ghost" disabled={!!busy} data-testid="gr-sync-run"
                title={`Merge ${p.base} into the branch (pull) — recorded as a decision, run by BOSS`}
                onClick={() => void act("update")}>
          {busy === "update" ? <CircleNotch size={13} className="cs-spin" aria-hidden /> : <ArrowsClockwise size={13} aria-hidden />}Sync</button>
      ) : null}
      {conflicted ? (
        <span className="gr-anchor">
          <button type="button" className="cs-pill gr-conflicts" data-tone="amber" aria-expanded={open} data-testid="gr-conflicts"
                  title={p.conflictsKnown ? "The files that do not merge cleanly" : "GitHub reports a conflict; it does not name the files"}
                  onClick={() => setOpen((v) => !v)}>
            <Warning size={12} aria-hidden />{p.conflictsKnown ? `${p.conflicts.length} conflicting file${p.conflicts.length === 1 ? "" : "s"}` : "Conflicts with base"}
            <CaretDown size={10} aria-hidden /></button>
          {open ? (
            <BoxPop className="gr-conflicts-list" aria-label="Conflicting files" data-testid="gr-conflicts-list"
                    onEscape={() => setOpen(false)}>
              {p.conflictsKnown && p.conflicts.length
                ? <ul>{p.conflicts.map((f) => <li key={f}><code>{f}</code></li>)}</ul>
                : <p className="cs-faint">GitHub says this {word} conflicts with {p.base}; it does not name the files.</p>}
              <p className="cs-faint cs-small">{p.restack
                ? `Restack first: its last change merged as one commit, so these may clear on their own.`
                : `Git alone cannot resolve these: Request changes sends an agent to fix them on this branch.`}</p>
            </BoxPop>) : null}
        </span>
      ) : null}
      <a className="cs-link" data-testid="gr-history-link"
         href={`/?project_id=${encodeURIComponent(at.projectId)}&area=project&tab=knowledge`}
         title="The repository's commit history">
        <GitCommit size={12} aria-hidden />History ›</a>
      {said ? <span className="cs-error" role="alert" data-testid="gr-sync-said">{said}</span> : null}
    </div>
  );
}
