"use client";

/**
 * T6 — THE WORK DRAWER'S "LINKED TICKET" (BOSSAPP-1047 Phase C, wireframe TicketRun.dc.html), and
 * the small ticket badge the PR/change page shows.
 *
 *   Linked ticket LEND-142 ↗ · Jira: In Progress
 *   Started ✓ → Pull request → Merged → Deployed     (what BOSS writes at each)
 *   [The ticket changed while BOSS was building]  Include it · Next ticket
 *   Written to Jira: 14:02 Moved To Do → In Progress · 14:02 Commented … · Retry
 *
 * The work row is a RUN (its id is the workflow run's), so the work item is read from the run's
 * input (`input.work_item_id`, which start-from-ticket sets); a caller that holds the work item
 * id passes it. The write-back log is GET /v1/work-items/{id}/ticket-sync; Retry resends only the
 * failed ones. The "ticket changed" card is the project's pending decision whose
 * recommendation.action is `ticket_changed_mid_run` for this work item or run.
 */
import React, { useCallback, useEffect, useState } from "react";
import { ArrowClockwise, ArrowSquareOut, Check, Warning } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import {
  loadTicketChangedCard, type ChangedCard,
  answerTicketChanged, readSync, retryTicketSync, syncWords, ticketRead, type Read, type SyncTimeline,
} from "../../lib/tickets";
import { ProviderMono, TicketChip, TkReadState } from "../tickets/ticket-parts";
import { localWith } from "../../lib/local-time";
import "../tickets/ticket-run.css";

type R = Record<string, unknown>;
const o = (v: unknown): R => (v && typeof v === "object" && !Array.isArray(v) ? (v as R) : {});
const s = (v: unknown) => (typeof v === "string" ? v : "");

const LABEL: Record<string, string> = { jira: "Jira", plane: "Plane", github_issues: "GitHub Issues", github: "GitHub", linear: "Linear", azure_devops: "Azure DevOps" };
export const providerLabel = (p: string) => LABEL[p] ?? (p ? p.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : "the ticket system");

/** The four moments BOSS writes to the ticket, and what it writes there. */
export const SYNC_STEPS = [
  { action: "started", when: "Started", what: "Move to In Progress · comment “BOSS is on it”" },
  { action: "pr_opened", when: "Pull request", what: "Link the PR · move to In Review" },
  { action: "merged", when: "Merged", what: "Move to Done" },
  { action: "deployed", when: "Deployed", what: "Comment with the preview link" },
] as const;

/** The run → its work item (start-from-ticket puts `work_item_id` in the run's input). */
export function useWorkItemOfRun(runId: string | null | undefined, workItemId?: string | null, projectId?: string | null): { id: string | null; done: boolean } {
  const [found, setFound] = useState<{ id: string | null; done: boolean }>({ id: workItemId ?? null, done: Boolean(workItemId) || !runId });
  useEffect(() => {
    if (workItemId || !runId) { setFound({ id: workItemId ?? null, done: true }); return; }
    let live = true;
    setFound({ id: null, done: false });
    // The run route is project-scoped: without ?project_id the API answers 422.
    fetch(`/v1/workflow-runs/${encodeURIComponent(runId)}${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null)).catch(() => null)
      .then((body) => { if (live) setFound({ id: s(o(o(body).input).work_item_id) || null, done: true }); });
    return () => { live = false; };
  }, [runId, workItemId, projectId]);
  return found;
}

/** The sync timeline of one work item (null while loading; re-read with `reload`). */
export function useTicketSync(workItemId: string | null): { read: Read<SyncTimeline> | null; reload: () => void } {
  const [read, setRead] = useState<Read<SyncTimeline> | null>(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!workItemId) { setRead(null); return; }
    let live = true;
    setRead(null);
    void readSync(workItemId).then((r) => { if (live) setRead(r); });
    return () => { live = false; };
  }, [workItemId, n]);
  return { read, reload: useCallback(() => setN((x) => x + 1), []) };
}

type Changed = ChangedCard;

/** The pending "ticket changed while BOSS was building" card for this work item or run. */
function useChanged(projectId: string, workItemId: string | null, runId: string | null | undefined): [Changed | null, () => void] {
  const [card, setCard] = useState<Changed | null>(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!projectId || (!workItemId && !runId)) return;
    let live = true;
    void loadTicketChangedCard(projectId, workItemId, runId).then((r) => { if (live) setCard(r.ok ? r.data : null); });
    return () => { live = false; };
  }, [projectId, workItemId, runId, n]);
  return [card, () => setN((x) => x + 1)];
}

const at = (iso: string) => {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? localWith(t, { hour: "2-digit", minute: "2-digit" }) : "";
};

/** T6: the drawer block. Renders nothing for work that did not come from a ticket. */
export function LinkedTicket({ projectId, runId, workItemId }: { projectId: string; runId?: string | null; workItemId?: string | null }) {
  const item = useWorkItemOfRun(runId, workItemId, projectId);
  const { read, reload } = useTicketSync(item.id);
  const [changed, rereadChanged] = useChanged(projectId, item.id, runId);
  const [said, setSaid] = useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const [busy, setBusy] = useState("");

  if (!item.done) return <div data-testid="tk-linked-loading"><TkReadState read={null} what="The linked ticket" rows={1} testid="tk-linked" /></div>;
  if (!item.id) return null;
  if (!read || !read.ok) {
    // A work item with no ticket answers an empty timeline, not an error; a refusal is said.
    if (read && !read.ok && read.missing) return null;
    return <section className="tk-run" aria-label="Linked ticket" data-testid="tk-linked">
      <TkReadState read={read} what="The linked ticket" onRetry={reload} rows={2} testid="tk-linked" /></section>;
  }
  const tl = read.data;
  if (!tl.ticket) return null;
  const label = providerLabel(tl.ticket.provider);
  const ok = new Set(tl.entries.filter((e) => e.status === "succeeded").map((e) => e.action));
  const nowAt = SYNC_STEPS.findIndex((st) => !ok.has(st.action));
  const lastMove = [...tl.entries].reverse().find((e) => e.op === "transition" && e.status === "succeeded");
  const status = lastMove ? lastMove.summary.replace(/^Moved to\s*/i, "") : "";

  const retry = async () => {
    setBusy("retry"); setSaid({ text: "" });
    const r = await retryTicketSync(item.id!);
    setBusy("");
    const b = o(r.body);
    setSaid(r.ok ? (b.ok === false ? { text: "Some writes failed again. The reason is on each line.", tone: "red" } : { text: "Sent again.", tone: "green" })
                 : { text: r.error ?? "Not retried.", tone: "red" });
    reload();
  };
  const answer = async (include: boolean) => {
    if (!changed) return;
    setBusy(include ? "include" : "next"); setSaid({ text: "" });
    const r = await answerTicketChanged(changed.id, include);
    setBusy("");
    setSaid(r.ok ? { text: include ? "This build will include the new criteria." : "Left for the next ticket.", tone: "green" } : { text: r.error ?? "Not answered.", tone: "red" });
    if (r.ok) rereadChanged();
  };

  return (
    <section className="tk-run" aria-label="Linked ticket" data-testid="tk-linked">
      <div className="tk-run-head">
        <ProviderMono provider={tl.ticket.provider} size={22} />
        <h3 className="tk-run-h">Linked ticket{" "}
          {tl.ticket.url ? <a href={tl.ticket.url} target="_blank" rel="noreferrer noopener" data-testid="tk-linked-key">{tl.ticket.key}<ArrowSquareOut size={11} aria-hidden /></a>
            : <b data-testid="tk-linked-key">{tl.ticket.key}</b>}</h3>
        <span className="tk-spacer" />
        <span className="tk-tag" data-tone={tl.sync_status === "failed" ? "red" : tl.sync_status === "paused" ? "amber" : "teal"} data-testid="tk-linked-status"
              title={tl.sync_error || (tl.last_synced_at ? `Last written ${at(tl.last_synced_at)}` : undefined)}>
          {tl.sync_status === "failed" ? "Write-back failed" : tl.sync_status === "paused" ? "Sync paused" : `${label}${status ? `: ${status}` : ""}`}</span>
      </div>

      <ol className="tk-steps" data-testid="tk-linked-steps">
        {SYNC_STEPS.map((st, i) => {
          const done = ok.has(st.action);
          return (
            <li key={st.action} data-state={done ? "done" : i === nowAt ? "now" : "next"} data-testid={`tk-step-${st.action}`}>
              <span className="tk-step-dot" aria-hidden>{done ? <Check size={11} weight="bold" /> : i + 1}</span>
              <span><b>{st.when}</b><small>{st.what}</small></span>
            </li>
          );
        })}
      </ol>

      {changed ? (
        <div className="tk-changed" role="alert" data-testid="tk-changed">
          <Warning size={16} aria-hidden />
          <div className="tk-changed-body">
            <b>The ticket changed while BOSS was building</b>
            <p>{changed.added.length ? `New in ${label}: ${changed.added.map((a) => `“${a}”`).join(", ")}.` : changed.question}</p>
            <div className="tk-row">
              <GatedButton permission="decision:approve" projectId={projectId} className="tk-btn tk-btn-sm" data-kind="primary"
                           disabled={Boolean(busy)} onClick={() => void answer(true)} data-testid="tk-changed-include"
                           title="Give this build the new acceptance criteria">Include it</GatedButton>
              <GatedButton permission="decision:approve" projectId={projectId} className="tk-btn tk-btn-sm"
                           disabled={Boolean(busy)} onClick={() => void answer(false)} data-testid="tk-changed-next"
                           title="Finish as planned; the change waits for the next ticket">Next ticket</GatedButton>
            </div>
          </div>
        </div>
      ) : null}

      <div className="tk-log" data-testid="tk-linked-log">
        <div className="tk-row"><span className="tk-lbl">Written to {label}</span><span className="tk-spacer" />
          {tl.failed > 0 ? (
            <GatedButton permission="work_tracker:write" projectId={projectId} className="tk-btn tk-btn-sm" disabled={Boolean(busy)}
                         onClick={() => void retry()} data-testid="tk-linked-retry" title="Send the failed writes again">
              <ArrowClockwise size={12} aria-hidden /> {busy === "retry" ? "Retrying…" : `Retry ${tl.failed}`}</GatedButton>
          ) : null}
        </div>
        {tl.entries.length ? (
          <ul>
            {tl.entries.map((e) => (
              <li key={e.receipt_id} data-status={e.status} data-testid="tk-log-line">
                <span className="tk-mono-text tk-faint">{at(e.at)}</span>
                <span>{e.label && !e.summary.toLowerCase().startsWith(e.label.toLowerCase()) ? <><b>{e.label}</b> </> : null}{e.summary}
                  {e.status === "failed" ? <small className="tk-log-why"> · failed{e.reason ? `: ${e.reason}` : ""}{e.retryable ? "" : " (retry won't help)"}</small> : null}
                  {e.status === "in_progress" ? <small className="tk-faint"> · sending</small> : null}
                  {e.acted_as ? <small className="tk-faint"> · as {e.acted_as}</small> : null}</span>
              </li>
            ))}
          </ul>
        ) : <p className="tk-faint" data-testid="tk-log-empty">Nothing written yet. BOSS writes when the work starts.</p>}
      </div>
      {said.text ? <p role="status" className={said.tone === "red" ? "tk-error" : "tk-ok"} data-testid="tk-linked-said">{said.text}</p> : null}
    </section>
  );
}

/** The PR/change page's ticket badge: "J LEND-142 · Jira: In Progress ✓". Nothing without a ticket. */
export function TicketSyncBadge({ runId, workItemId, projectId }: { runId?: string | null; workItemId?: string | null; projectId?: string | null }) {
  const item = useWorkItemOfRun(runId, workItemId, projectId);
  const { read } = useTicketSync(item.id);
  if (!read || !read.ok || !read.data.ticket) return null;
  const tl = read.data;
  const lastMove = [...tl.entries].reverse().find((e) => e.op === "transition" && e.status === "succeeded");
  const status = lastMove ? lastMove.summary.replace(/^Moved to\s*/i, "") : "";
  return <TicketChip provider={tl.ticket!.provider} ticketKey={tl.ticket!.key} url={tl.ticket!.url} testid="tk-pr-badge"
                     sync={syncWords(providerLabel(tl.ticket!.provider), status, tl.sync_status)}
                     tone={tl.sync_status === "failed" ? "red" : tl.sync_status === "paused" ? "amber" : "green"} />;
}
