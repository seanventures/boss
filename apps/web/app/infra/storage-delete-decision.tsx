"use client";

/**
 * S4 · ONE CARD FOR THE ONE THING THAT CANNOT BE UNDONE (BOSSAPP-1156). The Infra Agent cleaned
 * everything rebuildable by itself; a deletion with no rule yet (backups, a database, a data
 * volume, anything unlabelled) is a decision whose `recommendation.action = storage_delete`
 * (docs/infra/INFRA_AGENT_API.md §7), drawn here wherever decisions are drawn: Needs you,
 * Approvals, the Command Center card's Review.
 *
 * The card says exactly what goes and exactly what stays, the dry-run count, the typed tool it
 * will run (never a shell string), and carries the rule proposal: one tick and the same cause is
 * tier 2, automatic, next time. One tap: Delete N · free X GB (red; step-up as any
 * decision:approve), the alternatives the agent offers ("Keep 20 instead"), Not now. Approve
 * binds to the dry run's plan hash.
 */
import React, { useState } from "react";
import { Archive, Database, HardDrives, Lock, Pause, Prohibit, Sparkle, Trash } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { CheckRow } from "../../components/form-kit";
import { REPLAN_PERMISSION, STORAGE_DELETE_PERMISSION, answerStorageDelete, clock, gb, parseAsk, replanStorageStep, useStorageAsk, type StorageAsk } from "../../lib/infra-storage";
import { AgentAvatar } from "../agent-avatar";
import { useNow } from "../agents/live-util";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";

/** "Delete 348 · free 161 GB". */
export function deleteLabel(ask: StorageAsk): string {
  return `Delete ${ask.count.toLocaleString("en-US")} · free ${gb(ask.gb)}`;
}
/** "2 min", "40 s", "" when unknown. */
export function waitedWord(askedAt: string | null, now: number | null): string {
  if (!askedAt || now === null) return "";
  const t = Date.parse(askedAt);
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 90) return `${Math.max(1, s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${Math.round(s / 3600)} h`;
}

/** What goes, in a kind's words: "348 pre-upgrade backups". */
const KIND_WORD: Record<string, string> = {
  "storage.remove_backups": "pre-upgrade backups", "storage.drop_databases": "databases", "storage.remove_volumes": "data volumes", "storage.remove_unlabelled": "unlabelled items",
};
export function whatGoes(ask: StorageAsk): string {
  return KIND_WORD[ask.tool] ?? (ask.rule?.kind ? ask.rule.kind.replace(/_/g, " ") : "items");
}
/** "the newest 10 and everything from the last 7 days", from the typed arguments. */
export function whatStays(ask: StorageAsk): string {
  const keep = ask.keeps ?? (typeof ask.args.keep_newest === "number" ? ask.args.keep_newest : null);
  const days = typeof ask.args.keep_days === "number" ? ask.args.keep_days : null;
  const parts: string[] = [];
  if (keep !== null) parts.push(`the newest ${keep}`);
  if (days !== null) parts.push(`everything from the last ${days} days`);
  return parts.join(" and ");
}

/** The CSV of what goes (the full list the API sent), handed to the browser as a file. */
export function itemsCsv(items: string[]): string {
  return ["name", ...items.map((i) => `"${i.replace(/"/g, "\"\"")}"`)].join("\n");
}

function download(name: string, text: string, type: string) {
  try {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch { /* a browser without blobs keeps the list on screen */ }
}

const NEVER_ICON: Record<string, React.ReactNode> = {
  backups: <Archive size={11} aria-hidden />, databases: <Database size={11} aria-hidden />, "non-scratch databases": <Database size={11} aria-hidden />,
  "data volumes": <HardDrives size={11} aria-hidden />, unlabelled: <Prohibit size={11} aria-hidden />, "anything unlabelled": <Prohibit size={11} aria-hidden />,
};
const NEVER_DEFAULT = ["backups", "databases", "data volumes", "anything unlabelled"];
const LEFT_ALONE_DEFAULT = ["unlabelled items", "data volumes", "non-scratch databases"];

export function StorageDeleteDecision({ ask, projectId, actor, why, onAnswered, density = "page" }: {
  ask: StorageAsk; projectId: string; actor: string;
  /** The incident's cause in plain words (the card's Why); the decision's question otherwise. */
  why?: string | null;
  onAnswered?: (what: "approved" | "rejected") => void; density?: "page" | "card";
}) {
  const [makeRule, setMakeRule] = useState<boolean>(!!ask.rule && ask.rule.preticked);
  const now = useNow(30_000);
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<"approved" | "rejected" | "replanned" | null>(null);
  const [error, setError] = useState<string | null>(null);

  // An alternative re-plans the step (a new dry run, a NEW card to approve); it never approves.
  const replan = async (i: number) => {
    if (busy || !ask.incidentId) return;
    const alt = ask.alternatives[i];
    setBusy(`alt-${i}`);
    setError(null);
    const r = await replanStorageStep(ask.incidentId, ask.tool, { ...ask.args, ...alt.args });
    setBusy(null);
    if (!r.ok) { setError(r.error ?? "Not re-planned."); return; }
    setDone("replanned");
    showToast("success", `${alt.label}: the Infra Agent is counting again and will ask with the new list`);
    notifyChanged("decisions", { decision_id: ask.decisionId });
  };

  const send = async (which: "delete" | "later") => {
    if (busy) return;
    setBusy(which);
    setError(null);
    const r = which === "later"
      ? await answerStorageDelete(ask.decisionId, actor, ask, { action: "reject", note: "Not now · ask again in 1 h" })
      : await answerStorageDelete(ask.decisionId, actor, ask, { action: "approve", makeRule });
    setBusy(null);
    if (!r.ok) { setError(r.error ?? "Not sent."); return; }
    const what = which === "later" ? "rejected" : "approved";
    setDone(what);
    showToast("success", which === "later" ? "The Infra Agent will ask again in an hour" : `Deleting · ${gb(ask.gb)} back soon`);
    notifyChanged("decisions", { decision_id: ask.decisionId });
    onAnswered?.(what);
  };

  if (done) {
    return (
      <div className="sdd-said" data-testid="sdd-said" role="status" data-done={done}>
        {done === "approved" ? `Approved${makeRule ? " · the rule is now Layer 1, automatic from now on" : ""}. Work resumes when free space is back above ${ask.targetPct} %.`
          : done === "replanned" ? "Re-planning. The Infra Agent runs the dry run again with your numbers and asks once more, with the new list; runs stay paused until then."
          : "Not now. Runs stay paused; the Infra Agent asks again in 1 h or if space falls further."}
      </div>
    );
  }

  const freeNow = ask.freePctNow;
  const progress = freeNow !== null && ask.targetPct > 0 ? Math.max(0, Math.min(1, freeNow / ask.targetPct)) : 0;
  const shownItems = ask.toDelete.slice(0, 4);
  const moreItems = Math.max(0, ask.count - shownItems.length);
  const keptCount = ask.keeps ?? ask.kept.length;
  const shownKept = ask.kept.slice(0, 2);
  const moreKept = Math.max(0, keptCount - shownKept.length);
  const stays = whatStays(ask);
  const waited = waitedWord(ask.askedAt, now);
  const never = ask.neverAutomatic.length ? ask.neverAutomatic : NEVER_DEFAULT;
  const leftAlone = ask.leftAlone.length ? ask.leftAlone : LEFT_ALONE_DEFAULT;

  return (
    <div className="sdd" data-density={density} data-testid="storage-delete-decision" data-decision={ask.decisionId}>
      <section className="sdd-main" aria-labelledby={`sdd-${ask.decisionId}-h`}>
        <header className="sdd-head">
          <span className="sdd-face" aria-hidden><AgentAvatar agent="infra_steward" size={32} aiBadge={false} state="needs_you" /></span>
          <span className="sdd-who">
            <b id={`sdd-${ask.decisionId}-h`}>Infra Agent asks</b>
            <span className="sdd-meta">Storage incident on <code>{ask.hostId}</code>{ask.askedAt ? ` · ${clock(ask.askedAt)}` : ""}</span>
          </span>
          <span className="sdd-spacer" />
          {waited ? <span className="ia-chip" data-tone="amber" data-testid="sdd-waiting">Waiting {waited}</span> : null}
          <span className="ia-chip" data-tone="red" title="Deleting these cannot be undone, so a person decides"><Trash size={11} weight="bold" aria-hidden />Not reversible</span>
        </header>

        <div className="sdd-sentence" data-testid="sdd-sentence">
          Delete <b className="sdd-red">{ask.count.toLocaleString("en-US")} {whatGoes(ask)}</b>, freeing <b className="sdd-green">{gb(ask.gb)}</b>.
          {stays ? <> Keep <b>{stays}</b>.</> : null}
        </div>

        <div className="sdd-two">
          <div className="sdd-box">
            <span className="sdd-label">Why</span>
            <div className="sdd-p">{why || ask.question || "No rule says how many to keep, and deleting is irreversible: a person decides once."}</div>
          </div>
          <div className="sdd-box">
            <span className="sdd-label">Already done without asking</span>
            <div className="sdd-p"><b>{gb(ask.alreadyDoneGb)}</b> of rebuildable things: caches, old images, temp files, orphan volumes, logs.{freeNow !== null ? <> Free space is now <b>{Math.round(freeNow)} %</b>, target {ask.targetPct} %.</> : null}</div>
          </div>
        </div>

        <div className="sdd-two">
          <details className="sdd-list" data-testid="sdd-to-delete">
            <summary>{ask.count.toLocaleString("en-US")} to delete · {gb(ask.gb)}</summary>
            <ul>
              {shownItems.map((name) => <li key={name}><code>{name}</code></li>)}
              {moreItems ? <li className="sdd-more">… {moreItems.toLocaleString("en-US")} more</li> : null}
            </ul>
            {ask.toDelete.length ? <button type="button" className="linklike" data-testid="sdd-csv"
                    onClick={() => download(`${ask.hostId}-to-delete.csv`, itemsCsv(ask.toDelete), "text/csv")}>Full list (CSV)</button> : null}
          </details>
          <details className="sdd-list" open data-testid="sdd-kept">
            <summary>{keptCount.toLocaleString("en-US")} kept</summary>
            <ul>
              {shownKept.map((name) => <li key={name}><code>{name}</code></li>)}
              {moreKept ? <li className="sdd-more">… {moreKept} more</li> : null}
            </ul>
          </details>
        </div>

        {ask.rule ? (
          <div className="sdd-rule" data-testid="sdd-rule">
            <CheckRow testId="sdd-make-rule" checked={makeRule} onChange={() => setMakeRule((v) => !v)}
                      tip="Next time this cause is tier 2: automatic, no card"
                      label={<span className="sdd-rule-words">
                        <b><Sparkle size={12} weight="bold" aria-hidden /> Make this a permanent rule</b>
                        <span>{ask.rule.kind.replace(/_/g, " ")}: {ask.rule.words}.{ask.rule.preticked ? " Seen before, so the Infra Agent suggests it." : ""} Change it any time under Govern › Infrastructure.</span>
                      </span>} />
            <span className="ia-chip" data-tone="violet">Layer 1 rule</span>
          </div>
        ) : null}

        <div className="sdd-actions">
          <GatedButton permission={STORAGE_DELETE_PERMISSION} projectId={projectId} className="sdd-btn sdd-btn-danger" data-testid="sdd-delete"
                       disabled={busy !== null} onClick={() => void send("delete")} title="Deletes exactly the list above. Logged with your name.">
            <Trash size={14} weight="bold" aria-hidden /> {deleteLabel(ask)}
          </GatedButton>
          {/* Alternatives need the incident to re-plan against; a card without one offers none. */}
          {ask.incidentId ? ask.alternatives.map((alt, i) => (
            <GatedButton key={alt.label} permission={REPLAN_PERMISSION} projectId={projectId} className="sdd-btn" data-testid="sdd-alternative"
                         disabled={busy !== null} onClick={() => void replan(i)}
                         title={`Re-plans with ${Object.entries(alt.args).map(([k, v]) => `${k} = ${String(v)}`).join(", ")}: a new dry run, then a new card to approve`}>
              {alt.label}
            </GatedButton>
          )) : null}
          <GatedButton permission={STORAGE_DELETE_PERMISSION} projectId={projectId} className="sdd-btn" data-testid="sdd-later" disabled={busy !== null} onClick={() => void send("later")}
                       title="Runs stay paused. The Infra Agent asks again in 1 h or if space falls further.">Not now</GatedButton>
          <span className="sdd-foot"><Lock size={11} aria-hidden /> Logged with your name · passkey step-up</span>
        </div>
        {error ? <div className="fk-err" role="alert" data-testid="sdd-error">{error}</div> : null}
      </section>

      <aside className="sdd-side">
        <div className="sdd-box">
          <span className="sdd-label">While you decide</span>
          <b className="sdd-paused"><Pause size={13} weight="bold" aria-hidden /> {ask.pausedRuns} run{ask.pausedRuns === 1 ? "" : "s"} paused</b>
          <div className="sdd-p">They wait at a checkpoint. Approving frees <b>{gb(ask.gb)}</b> and they resume within a minute.</div>
          {freeNow !== null ? (
            <span className="sdd-bar" role="img" aria-label={`${Math.round(freeNow)} % free of a ${ask.targetPct} % target`}>
              <span className="sdd-bar-l">{Math.round(freeNow)} %</span>
              <svg className="sdd-bar-track" viewBox="0 0 100 1" preserveAspectRatio="none" aria-hidden><rect className="sdd-bar-fill" x="0" y="0" width={Math.round(progress * 100)} height="1" /></svg>
              <span className="sdd-bar-r">{ask.targetPct} %</span>
            </span>
          ) : null}
        </div>
        <div className="sdd-box">
          <span className="sdd-label">What it will run</span>
          <pre className="sdd-tool" data-testid="sdd-tool">{[
            `tool: ${ask.tool}`,
            `host: ${ask.hostId}`,
            ...Object.entries(ask.args).map(([k, v]) => `${k}: ${String(v)}`),
            `dry_run: done · ${ask.count.toLocaleString("en-US")} matched`,
            "receipt: on approve",
          ].join("\n")}</pre>
          <div className="sdd-faint">A tool with typed arguments, never a shell command. The dry run matched exactly the list on the left.</div>
        </div>
        <div className="sdd-box">
          <span className="sdd-label">Never automatic</span>
          <span className="sdd-chips">
            {never.map((w) => <span key={w} className="ia-chip" data-tone="red">{NEVER_ICON[w] ?? <Prohibit size={11} aria-hidden />}{w}</span>)}
          </span>
        </div>
        <div className="sdd-box" data-testid="sdd-left-alone">
          <span className="sdd-label">Left alone</span>
          <span className="sdd-chips">
            {leftAlone.map((w) => <span key={w} className="ia-chip">{w}</span>)}
          </span>
        </div>
      </aside>
    </div>
  );
}

/** The card from a decision id (the Command Center card's Review, Needs you): one read of
 *  GET /v1/decisions/{id}. */
export function StorageDeleteByDecision({ decisionId, projectId, actor, why, density = "page", onAnswered }: {
  decisionId: string; projectId: string; actor: string; why?: string | null; density?: "page" | "card"; onAnswered?: (what: "approved" | "rejected") => void;
}) {
  const { ask, loading, error } = useStorageAsk(projectId, decisionId);
  if (!ask) {
    return (
      <div className="sdd-faint" data-testid="sdd-pending" role="status">
        {loading ? "Reading the Infra Agent's question…" : error ? `Can't read the question: ${error}` : "The Infra Agent's question is no longer open: the incident moved on, or the dry run is being redone."}
      </div>
    );
  }
  return <StorageDeleteDecision ask={ask} projectId={projectId} actor={actor} why={why} density={density} onAnswered={onAnswered} />;
}

/** The card from the inbox's own row (Approvals already holds the decision). */
export function StorageDeleteFromDecision({ decision, projectId, actor, onAnswered }: {
  decision: { id: string; status: string; question: string; recommendation: Record<string, unknown>; createdAt?: string };
  projectId: string; actor: string; onAnswered?: (what: "approved" | "rejected") => void;
}) {
  const ask = parseAsk({ id: decision.id, status: decision.status, question: decision.question, recommendation: decision.recommendation, created_at: decision.createdAt ?? null });
  if (!ask) return <StorageDeleteByDecision decisionId={decision.id} projectId={projectId} actor={actor} onAnswered={onAnswered} />;
  return <StorageDeleteDecision ask={ask} projectId={projectId} actor={actor} onAnswered={onAnswered} />;
}
