"use client";

/**
 * Agents › Activity: a readable river of agent work, newest first (agents-home spec §4.9).
 *
 * Every line is DATA: the agent's name, the server's work verb (agent_acts.work_verb) and the
 * label the engine wrote from names. No model writes any of it. Children fold under their item
 * (`+n inside`) and tool calls under the work that made them (`n tool calls`) — and an item
 * with none shows no fold control: a control is never a promise.
 *
 * IT NEVER MOVES UNDER THE READER. New work collects behind a `n new ↑` pill at the top until
 * it is pressed; the list only grows downwards (`Load more`). Times are absolute (`09:42`, ISO
 * in the tooltip): this is an audit trail.
 */
import React, { useCallback, useMemo, useState } from "react";
import { ArrowBendDownRight, CaretDown, CaretRight, X } from "@phosphor-icons/react";

import type { Act, AvatarState } from "../../lib/agent-world-types";
import type { ActivityFilter, ActivityItem, ToolCallRow } from "../../lib/agents-live-board";
import { listActivity, listToolCalls } from "../../lib/agents-activity";
import { getAct, identityOf, money } from "../../lib/agent-world";
import { useLogStream } from "../../lib/log-stream";
import { clock as logClock, dayLabel as logDayLabel, utcTitle } from "../../lib/log-time";
import { logHref, type LogScope } from "../../lib/log-url";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { ActCard, consoleHref } from "./act-card";
import { AgState } from "./agent-face";
import { RunFace } from "./run-face";
import type { AvatarVariant } from "../../lib/agent-world-types";

export type ActivityRiverProps = {
  projectId: string;
  /** The header's `Find an agent` text, resolved to a role key when it names one. */
  roleKey?: string | null;
  /** `?run=` focus: chip `<run> ×`. */
  runId?: string | null;
  onClearRun?: () => void;
  /** The console's Live/Paused toggle: no polling while paused. */
  paused?: boolean;
  /** OPTIONAL (S3): the run's name for the `?run=` chip; the run id is shown when absent. */
  runLabel?: string;
  /** OPTIONAL (S3): who is reading, so the work card names a person "you" only for them. */
  viewer?: string;
  /** A link's window (`&days=`; Organization's edges): 1 or 7, the river's own windows. */
  initialDays?: 1 | 7;
  /** A talk's row opens its conversation (Agent Studio's `?conversation=` sheet). */
  onOpenConversation?: (threadId: string) => void;
};

export const RIVER_POLL_MS = 8000;
const TOOL_CAP = 50;

/** "You approved Approved · Notify" says the verb twice: an approval's label starts with it. */
export function withoutVerb(label: string, verb: string): string {
  const prefix = `${verb} · `;
  return verb && label.startsWith(prefix) ? label.slice(prefix.length) : label;
}

const FILTERS: { key: ActivityFilter; label: string; tip: string }[] = [
  { key: "all", label: "All", tip: "Everything agents did" },
  { key: "needs_you", label: "Needs you", tip: "Work waiting on a person" },
  { key: "handoffs", label: "Handoffs", tip: "Work handed from one agent to another" },
  { key: "builds", label: "Builds", tip: "Stages inside a build" },
  { key: "panels", label: "Panels", tip: "Seats on a panel" },
  { key: "talks", label: "Talks", tip: "Conversations with an agent" },
];

/** The face's FINAL state: what the work came to, never a motion. */
export function riverState(status: string, item?: Pick<ActivityItem, "kind" | "verb">): AvatarState {
  // A person's answer is done whichever way it went; a run that ended badly STOPPED (its
  // starter never failed — the step that failed did, and that row says so). Work that errored
  // FAILED; work left open when its run stopped STOPPED. A finished record is never "stuck".
  if (item && (item.verb === "Approved" || item.verb === "Rejected")) return "done";
  if (status === "awaiting_input") return "needs_you";
  if (status === "error") return item?.kind === "run" ? "stopped" : "failed";
  if (status === "stale") return "stopped";
  if (status === "complete" || status === "skipped") return "done";
  return "working";
}

/** The word beside a row: the state's own word, except a run that ended badly, which `Stopped`. */
export function riverStatusWord(state: AvatarState, item: Pick<ActivityItem, "kind">): string | null {
  return item.kind === "run" && state === "stopped" ? "Stopped" : null;
}

/** WHO DID IT, AND WHAT. A gate is the operator's: approving or rejecting is something YOU did,
 *  and a wait on a gate is a wait on you — never the gated step's captain "working" it. */
export function sentenceOf(item: ActivityItem): { who: { roleKey: string | null; variant: AvatarVariant; name: string };
                                                  verb: string } {
  if (item.verb === "Approved" || item.verb === "Rejected") {
    return { who: { roleKey: null, variant: "you", name: "You" }, verb: item.verb.toLowerCase() };
  }
  if (!item.roleKey && (item.verb === "Needs you" || item.status === "awaiting_input")) {
    return { who: { roleKey: null, variant: "you", name: "" }, verb: "Waiting on you:" };
  }
  return { who: whoOf(item), verb: item.verb.toLowerCase() };
}

/** `Today` · `Yesterday` · `Mon 29 Sep`, in the reader's own time zone (lib/log-time: one
 *  clock for every log in the console). */
export function dayLabel(iso: string, now: Date = new Date()): string {
  return logDayLabel(iso, now);
}

/** `09:42`, local; UTC in the tooltip. */
export function clock(iso: string): string {
  return logClock(iso, { seconds: false });
}

/** How long it took, once it ended. Open work shows no duration (no clock read in render). */
export function duration(item: Pick<ActivityItem, "startedAt" | "endedAt">): string {
  if (!item.endedAt) return "";
  const s = Math.max(0, Math.round((Date.parse(item.endedAt) - Date.parse(item.startedAt)) / 1000));
  if (!Number.isFinite(s)) return "";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Who did it: an agent by its callsign, else a person (`You`) or BOSS — never a guessed agent. */
function whoOf(item: ActivityItem): { roleKey: string | null; variant: AvatarVariant; name: string } {
  if (item.roleKey) {
    const id = identityOf(item.roleKey);
    return { roleKey: item.roleKey, variant: "agent", name: id.callsign || id.name };
  }
  if (item.kind === "run" || item.kind === "approval" || item.kind === "chat") return { roleKey: null, variant: "you", name: "You" };
  return { roleKey: null, variant: "boss", name: "BOSS" };
}

/** Agent-to-agent handoffs carry the edge glyph; work a workflow simply named does not. */
const HANDOFF_HOWS = new Set(["captain", "asked_by_agent", "model_pick", "only_holder", "only_candidate"]);
/** Where a river row's `All lines ↗` goes: the act's own lines when it wrote any (turns or
 *  model calls), else its workflow run's — an act scope with no lines was an empty Log. */
export function riverLogScope(item: Pick<ActivityItem, "actId" | "label" | "verb" | "toolCalls" | "costUsd" | "workflowRunId">): LogScope {
  const name = withoutVerb(item.label, item.verb).trim() || item.label;
  const wroteLines = item.toolCalls > 0 || item.costUsd > 0;
  if (!wroteLines && item.workflowRunId) return { wfr: item.workflowRunId, run_name: name || null };
  return { act: item.actId, run_name: name || null };
}

function isHandoff(item: ActivityItem): boolean {
  return item.kind === "handoff" || HANDOFF_HOWS.has(item.how) || item.from?.kind === "agent";
}

export function ActivityRiver({ projectId, roleKey, runId, onClearRun, paused, runLabel, viewer = "",
                               initialDays = 1, onOpenConversation }: ActivityRiverProps) {
  useIdentityVersion();
  const [filter, setFilter] = useState<ActivityFilter>("all");
  const [days, setDays] = useState<1 | 7>(initialDays);
  const query = useMemo(() => ({ projectId, filter, days, role: roleKey || null, run: runId || null }),
                        [projectId, filter, days, roleKey, runId]);
  // THE SHARED STREAM ENGINE (lib/log-stream), the same loop as the Activity log's: cursor
  // paging, an 8s poll that stops while Paused or hidden, and new work that ALWAYS waits behind
  // `n new ↑` here (the river never moves under the reader).
  const stream = useLogStream<ActivityItem>({
    key: JSON.stringify(query),
    idOf: (i) => i.actId,
    pollMs: RIVER_POLL_MS,
    paused: Boolean(paused),
    holding: true,
    first: async () => {
      const r = await listActivity(query);
      return r.ok ? { items: r.data.items, next: r.data.next, newest: null } : { error: r.error };
    },
    older: async (cursor) => {
      const r = await listActivity({ ...query, cursor });
      return r.ok ? { items: r.data.items, next: r.data.next, newest: null } : { error: r.error };
    },
    newer: async () => {
      const r = await listActivity(query);
      return r.ok ? { items: r.data.items, newest: null, etag: null } : { error: r.error };
    },
  });
  const items = stream.items;
  const fresh = stream.fresh;
  const failed = stream.error;
  const next = stream.next;
  const loadingMore = stream.loadingOlder;
  const showFresh = stream.showFresh;
  const loadMore = async () => stream.loadOlder();

  const groups = useMemo(() => {
    const out: { day: string; rows: ActivityItem[] }[] = [];
    for (const item of items ?? []) {
      const day = dayLabel(item.startedAt);
      if (out[out.length - 1]?.day !== day) out.push({ day, rows: [] });
      out[out.length - 1].rows.push(item);
    }
    return out;
  }, [items]);

  return (
    <div className="ag-river" data-testid="ag-river">
      <div className="ag-river-bar">
        <span className="ag-filters ag-river-filters" role="group" aria-label="Show">
          {FILTERS.map((f) => (
            <button key={f.key} type="button" className="cap-btn" data-active={filter === f.key}
                    aria-pressed={filter === f.key} data-testid={`ag-river-filter-${f.key}`} title={f.tip}
                    onClick={() => setFilter(f.key)}>{f.label}</button>
          ))}
        </span>
        <span className="ag-seg" role="group" aria-label="Window">
          {([1, 7] as const).map((d) => (
            <button key={d} type="button" aria-pressed={days === d} data-testid={`ag-river-days-${d}`}
                    title={d === 1 ? "The last 24 hours" : "The last 7 days"}
                    onClick={() => setDays(d)}>{d === 1 ? "24h" : "7d"}</button>
          ))}
        </span>
        {runId ? (
          <button type="button" className="ag-chip ag-river-run" data-testid="ag-river-run" title="Show every run"
                  onClick={() => onClearRun?.()}>
            <span data-user-text>{runLabel || runId}</span> <X size={11} weight="bold" aria-hidden />
          </button>
        ) : null}
      </div>
      {fresh.length ? (
        <button type="button" className="ag-river-new" data-testid="ag-river-new" onClick={showFresh}
                title="Show the newest work">{fresh.length} new ↑</button>
      ) : null}
      {items === null ? (
        <div className="ag-river-loading" aria-busy="true"><span className="ag-sr">Loading</span></div>
      ) : failed ? (
        <div className="ag-empty" role="alert"><span title={failed}>Can&apos;t reach BOSS</span></div>
      ) : items.length === 0 ? (
        <div className="ag-empty" data-testid="ag-river-empty">
          <span>No agent work yet</span>
          <a className="ag-link" href={consoleHref({ area: "delivery_workflows" })}>Start work ↗</a>
        </div>
      ) : (
        <>
          {groups.map((g) => (
            <section key={g.day} className="ag-river-day" aria-label={g.day}>
              <header className="ag-river-dayhead">{g.day}</header>
              <ul className="ag-river-list">
                {g.rows.map((item) => (
                  <RiverRow key={item.actId} item={item} projectId={projectId} viewer={viewer}
                            filter={filter} days={days} onOpenConversation={onOpenConversation} />
                ))}
              </ul>
            </section>
          ))}
          {next ? (
            <button type="button" className="ag-btn ag-river-more" data-testid="ag-river-more" disabled={loadingMore}
                    onClick={() => void loadMore()}>Load more</button>
          ) : null}
        </>
      )}
    </div>
  );
}

function RiverRow({ item, projectId, viewer, filter, days, nested = false, onOpenConversation }: {
  item: ActivityItem; projectId: string; viewer: string; filter: ActivityFilter; days: 1 | 7; nested?: boolean;
  onOpenConversation?: (threadId: string) => void;
}) {
  useIdentityVersion(); // the name is resolved at render
  const { who, verb: sentence } = sentenceOf(item);
  const state = riverState(item.status, item);
  const statusWord = riverStatusWord(state, item);
  const [card, setCard] = useState<Act | null>(null);
  const [kids, setKids] = useState<ActivityItem[] | null>(null);
  const [kidsOpen, setKidsOpen] = useState(false);
  const [tools, setTools] = useState<{ rows: ToolCallRow[]; more: boolean } | null>(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const took = duration(item);

  // A TALK OPENS ITS CONVERSATION: the brief, the answer and what the agent did — never a card
  // naming the console's own actor.
  const conversation = item.kind === "chat" && item.threadId && onOpenConversation ? item.threadId : null;
  const openCard = useCallback(async () => {
    if (conversation) { onOpenConversation?.(conversation); return; }
    if (card) { setCard(null); return; }
    const r = await getAct(projectId, item.actId);
    if (r.ok) setCard(r.data);
  }, [card, projectId, item.actId, conversation, onOpenConversation]);

  const toggleKids = async () => {
    const opening = !kidsOpen;
    setKidsOpen(opening);
    if (!opening || kids) return;
    const r = await listActivity(item.workflowRunId
      ? { projectId, run: item.workflowRunId, days, filter: "all", limit: 200 }
      : { projectId, parent: item.actId, days, filter: "all", limit: 200 });
    setKids(r.ok ? r.data.items.filter((k) => k.parentActId === item.actId) : []);
  };
  const toggleTools = async () => {
    const opening = !toolsOpen;
    setToolsOpen(opening);
    if (!opening || tools) return;
    const r = await listToolCalls(projectId, item.actId, TOOL_CAP);
    setTools(r.ok ? { rows: r.data.items, more: r.data.more } : { rows: [], more: false });
  };

  return (
    <li className="ag-river-item" data-nested={nested || undefined}>
      <div className="ag-river-row" role="button" tabIndex={0} data-testid={`ag-river-row-${item.actId}`}
           data-state={state} aria-expanded={conversation ? undefined : !!card}
           aria-haspopup={conversation ? "dialog" : undefined} data-conversation={conversation ? "true" : undefined}
           title={conversation ? "Open the conversation" : undefined}
           onClick={(e) => { if (!(e.target as Element).closest("button, a")) void openCard(); }}
           onKeyDown={(e) => { if (e.key === "Enter" && e.target === e.currentTarget) void openCard(); }}>
        <time className="ag-river-time" dateTime={item.startedAt} title={utcTitle(item.startedAt)}>{clock(item.startedAt)}</time>
        <span className="ag-river-face" data-still>
          <RunFace roleKey={who.roleKey} variant={who.variant} size={24} state={state} />
        </span>
        <span className="ag-river-text">
          {isHandoff(item) ? (
            <span className="ag-river-edge" title={item.edgeLabel || undefined} aria-label={item.edgeLabel || undefined}>
              <ArrowBendDownRight size={11} weight="bold" aria-hidden />
            </span>
          ) : null}
          {who.name ? <><span className="ag-river-who" data-user-text>{who.name}</span>{" "}</> : null}
          <span className="ag-river-verb">{sentence}</span>{" "}
          <span className="ag-river-label" data-user-text title={item.label}>{withoutVerb(item.label, item.verb)}</span>
        </span>
        <span className="ag-river-status" data-state={state}>
          {statusWord ? <span className="ag-state" data-state={state} data-compact>{statusWord}</span>
            : <AgState state={state} compact />}
        </span>
        <span className="ag-river-took" title={took ? `Took ${took}` : undefined}>{took}</span>
        <span className="ag-river-cost" title={item.costUsd > 0 ? "Metered model spend" : undefined}>
          {item.costUsd > 0 ? money(item.costUsd) : ""}
        </span>
      </div>
      <div className="ag-river-folds">
          {item.children > 0 ? (
            <button type="button" className="ag-link" aria-expanded={kidsOpen} data-testid={`ag-river-inside-${item.actId}`}
                    onClick={() => void toggleKids()}>
              {kidsOpen ? <CaretDown size={10} aria-hidden /> : <CaretRight size={10} aria-hidden />} +{item.children} inside
            </button>
          ) : null}
          {item.toolCalls > 0 ? (
            <button type="button" className="ag-link" aria-expanded={toolsOpen} data-testid={`ag-river-tools-${item.actId}`}
                    onClick={() => void toggleTools()}>
              {item.toolCalls} tool call{item.toolCalls === 1 ? "" : "s"} {toolsOpen ? "▴" : "▾"}
            </button>
          ) : null}
          {/* EVERY ROW reaches its lines (D7): the work's own lines, or its run's when the work
              wrote none of its own; the name rides along so the chip reads before the server. */}
          <a className="ag-link ag-river-log" data-testid={`ag-river-log-${item.actId}`} title="Every log line of this work"
             href={logHref(riverLogScope(item))}>All lines ↗</a>
        </div>
      {card ? (
        <div className="ag-river-card" data-testid={`ag-river-card-${item.actId}`}>
          <ActCard act={card} viewer={viewer} inline onClose={() => setCard(null)} />
        </div>
      ) : null}
      {toolsOpen && tools ? (
        <ul className="ag-river-tools" data-testid={`ag-river-toollist-${item.actId}`}>
          {tools.rows.slice(0, TOOL_CAP).map((t, i) => (
            <li key={`${t.toolName}:${i}`} className="ag-river-tool" data-ok={t.ok === false ? "false" : "true"}>
              <span data-user-text>{t.toolName}</span>
              <span> · {t.ok === false ? "failed" : "ok"}</span>
              {t.latencyMs !== null ? <span> · {t.latencyMs}ms</span> : null}
            </li>
          ))}
          {tools.more || item.toolCalls > TOOL_CAP ? (
            <li className="ag-river-tool">{item.toolCalls - TOOL_CAP > 0 ? `+${item.toolCalls - TOOL_CAP} more in All lines` : "More in All lines"}</li>
          ) : null}
        </ul>
      ) : null}
      {kidsOpen && kids ? (
        <ul className="ag-river-list ag-river-kids">
          {kids.map((k) => (
            <RiverRow key={k.actId} item={k} projectId={projectId} viewer={viewer} filter={filter} days={days} nested
                      onOpenConversation={onOpenConversation} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

