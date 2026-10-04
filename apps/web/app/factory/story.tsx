"use client";

/**
 * The Story (factory spec §4.5.4): what each agent did on this build, oldest first, grouped
 * under the same station heads as the Line. Every sentence is built from fields — `<Verb> ·
 * <stage title>`, `Steered`, the server's BOSS line — never by a model; the only agent text is
 * the trimmed `Said` excerpt the server sends.
 *
 * A fold control is never a promise: `n files ▾`, `n tool calls ▾`, `Said ▾` appear only when
 * there is something under them. The `Now:` line is one node, replaced in place on each poll.
 * New rows append at the bottom; a reader who scrolled up gets `n new ↓`, and the list does
 * not move under them.
 */
import { CaretDown, CaretRight } from "@phosphor-icons/react";
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { ReviewComment, StoryAct, StoryBoss, StoryRow, StoryYou, NowLine } from "../../lib/cockpit";
import type { Station, StationKey } from "../../lib/factory-floor";
import type { ToolCalls } from "../../lib/agents-live-board";
import { nameOf, RunFace } from "../agents/run-face";
import { fetchTools } from "./cockpit-actions";
import { clockOf, money, secondsWord, spanOf, whoName } from "./cockpit-util";

export type StoryProps = {
  projectId: string;
  runId: string;
  viewer: string;
  rows: StoryRow[];
  stations: Station[];
  now: NowLine | null;
  comments: ReviewComment[];
  /** The station the Line filtered to, or null for All. */
  filter: StationKey | null;
  onClearFilter: () => void;
  onFace: (actId: string, anchor: HTMLElement) => void;
  onLogs: () => void;
  /** Guidance sent from this page that the server has not echoed yet. */
  pending?: { id: string; text: string; at: string }[];
  /** Live updates are paused: rows still render, nothing moves. */
  still?: boolean;
};

const ACT_STATE: Record<string, "working" | "needs_you" | "done" | "stuck" | "idle"> = {
  active: "working", pending: "idle", awaiting_input: "needs_you", complete: "done", error: "stuck", stale: "stuck", skipped: "idle",
};

function Fold({ label, testId, children, count }: { label: string; testId: string; count: number; children: () => React.ReactNode }) {
  const [open, setOpen] = useState(false);
  if (count <= 0) return null;
  return (
    <span className="fx-fold">
      <button type="button" className="ag-link fx-fold-btn" aria-expanded={open} data-testid={testId}
              onClick={() => setOpen((v) => !v)}>
        {label} {open ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />}
      </button>
      {open ? <span className="fx-fold-body">{children()}</span> : null}
    </span>
  );
}

function ToolsFold({ projectId, runId, act }: { projectId: string; runId: string; act: StoryAct }) {
  const [open, setOpen] = useState(false);
  const [tools, setTools] = useState<ToolCalls | null>(null);
  const [failed, setFailed] = useState(false);
  if (act.toolCalls <= 0) return null;
  const toggle = async () => {
    const opening = !open;
    setOpen(opening);
    if (!opening || tools) return;
    const got = await fetchTools(projectId, act.actId);
    if (got) setTools(got); else setFailed(true);
  };
  const n = act.toolCalls;
  return (
    <span className="fx-fold">
      <button type="button" className="ag-link fx-fold-btn" aria-expanded={open} data-testid={`fx-tools-${act.actId}`}
              onClick={() => void toggle()}>
        {n} tool {n === 1 ? "call" : "calls"} {open ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />}
      </button>
      {open ? (
        <span className="fx-fold-body fx-tools" data-testid={`fx-tools-body-${act.actId}`}>
          {tools ? tools.items.slice(0, 50).map((t, i) => (
            <span key={i} className="fx-tool" data-ok={t.ok === null ? undefined : String(t.ok)}>
              <span className="fx-mono">{t.toolName}</span>
              {t.ok === null ? null : <span> · {t.ok ? "ok" : "failed"}</span>}
              {t.latencyMs !== null ? <span> · {t.latencyMs}ms</span> : null}
            </span>
          )) : failed ? <span className="fx-muted">Not available</span> : <span className="fx-muted">Loading</span>}
          <a className="ag-link" href={`?area=activity&run=${encodeURIComponent(runId)}`} data-testid="fx-tools-log">
            Open in Activity log ↗
          </a>
        </span>
      ) : null}
    </span>
  );
}

function CommentList({ items }: { items: ReviewComment[] }) {
  return (
    <span className="fx-cmts">
      {items.map((c) => (
        <span key={c.id} className="fx-cmt" data-severity={c.severity}>
          {c.path ? <span className="fx-mono">{c.path}{c.line ? `:${c.line}` : ""}</span> : null}
          <span className="fx-clamp" title={c.body} data-user-text>{c.body}</span>
        </span>
      ))}
    </span>
  );
}

function ActRow({ row, props, live, enter }: { row: StoryAct; props: StoryProps; live: NowLine | null; enter?: boolean }) {
  const face = useRef<HTMLButtonElement>(null);
  const state = ACT_STATE[row.status] ?? "idle";
  const still = props.still || state === "idle";
  const title = [row.title, row.seat ? `seat ${row.seat}` : "", row.round > 1 ? `round ${row.round}` : ""].filter(Boolean).join(" · ");
  const right = [row.word, spanOf(row.startedAt, row.endedAt ?? null, row.endedAt ? null : Date.now()),
    row.costUsd > 0 ? money(row.costUsd) : ""].filter(Boolean);
  const authored = row.commentsAuthored > 0
    ? props.comments.filter((c) => c.authorRoleKey && c.authorRoleKey === row.roleKey && (!row.round || c.round === row.round))
    : [];
  const fixed = row.commentsFixed > 0 ? props.comments.filter((c) => c.status === "addressed" && c.round < row.round) : [];
  return (
    <li className="fx-row" data-enter={enter || undefined} data-type="act" data-status={row.status} data-testid={`fx-story-${row.id}`} data-role={row.roleKey ?? undefined}>
      <time className="fx-time" dateTime={row.startedAt} title={row.startedAt}>{clockOf(row.startedAt)}</time>
      <button ref={face} type="button" className="fx-row-face" aria-label={`${whoName(row.roleKey)}: who did this`}
              data-testid={`fx-story-face-${row.id}`} onClick={() => face.current && props.onFace(row.actId, face.current)}>
        <RunFace roleKey={row.roleKey} variant={row.roleKey ? "agent" : "boss"} size={24} state={still && state === "working" ? "idle" : state} />
      </button>
      <span className="fx-row-main">
        <span className="fx-row-what">
          <span className="fx-verb">{row.verb}</span>
          <span> · </span>
          <span data-user-text title={row.label}>{title}</span>
        </span>
        <span className="fx-row-folds">
          <Fold label={`${row.files?.length ?? 0} files`} count={row.files?.length ?? 0} testId={`fx-files-${row.id}`}>
            {() => <span className="fx-files">{(row.files ?? []).map((f) => <span key={f} className="fx-mono" data-user-text>{f}</span>)}</span>}
          </Fold>
          <ToolsFold projectId={props.projectId} runId={props.runId} act={row} />
          <Fold label={`${authored.length || row.commentsAuthored} comments`} count={row.commentsAuthored} testId={`fx-authored-${row.id}`}>
            {() => <CommentList items={authored} />}
          </Fold>
          <Fold label={`Fixing ${row.commentsFixed} comments`} count={row.commentsFixed} testId={`fx-fixing-${row.id}`}>
            {() => <CommentList items={fixed} />}
          </Fold>
          <Fold label="Said" count={row.said ? 1 : 0} testId={`fx-said-${row.id}`}>
            {() => <span className="fx-said" data-user-text>{row.said}</span>}
          </Fold>
        </span>
        {live && live.actId === row.actId ? (
          <span className="fx-now" data-testid="fx-now" title="What the agent is doing right now" aria-live="off">
            <span className="fx-now-key">Now:</span>{" "}
            <span className="fx-mono" data-user-text>{[live.tool, live.arg].filter(Boolean).join(" ")}</span>
          </span>
        ) : null}
        {row.failure ? (
          <span className="fx-fail" data-testid="fx-story-failure">
            <span title={row.failure.tip || undefined}>{row.failure.line}</span>
            <button type="button" className="ag-link" onClick={props.onLogs}>Logs ▾</button>
          </span>
        ) : null}
      </span>
      <span className="fx-row-right">{right.join(" · ")}</span>
    </li>
  );
}

const YOU_WORDS: Record<string, string> = {
  approved_plan: "Approved the plan", retried: "Retried", stopped: "Stopped", steered: "Steered",
};

function YouRow({ row, viewer, enter }: { row: StoryYou; viewer: string; enter?: boolean }) {
  const mine = !row.actor || row.actor === viewer;
  const word = row.what === "waived" ? `Waived ${row.count ?? ""} checks`.replace("  ", " ") : YOU_WORDS[row.what] ?? row.what;
  return (
    <li className="fx-row" data-enter={enter || undefined} data-type="you" data-what={row.what} data-testid={`fx-story-${row.id}`}>
      <time className="fx-time" dateTime={row.at} title={row.at}>{clockOf(row.at)}</time>
      <span className="fx-row-face">
        <RunFace roleKey={null} variant={mine ? "you" : "person"} personId={row.actor} size={24} state="idle" />
      </span>
      <span className="fx-row-main">
        <span className="fx-row-what">
          <span className="fx-verb">{word}</span>
          {row.what === "steered" && row.text ? (
            <><span> · </span><span className="fx-quote" title={row.text} data-user-text>“{row.text}”</span></>
          ) : null}
        </span>
        {row.what === "waived" && row.text ? (
          <span className="fx-row-folds">
            <Fold label="Reason" count={1} testId={`fx-reason-${row.id}`}>{() => <span data-user-text>{row.text}</span>}</Fold>
          </span>
        ) : null}
      </span>
      <span className="fx-row-right">
        {row.what === "steered" ? (
          row.appliedAt
            ? <span data-testid="fx-steer-read">Read by {whoName(row.appliedRole)}</span>
            : <span data-testid="fx-steer-queued" title="Agents read guidance between stations">Queued for next station</span>
        ) : null}
      </span>
    </li>
  );
}

function BossRow({ row, enter }: { row: StoryBoss; enter?: boolean }) {
  const cmds = Array.isArray(row.detail?.commands) ? (row.detail?.commands as Record<string, unknown>[]) : [];
  const steps = Array.isArray(row.detail?.steps) ? (row.detail?.steps as Record<string, unknown>[]) : [];
  return (
    <li className="fx-row" data-enter={enter || undefined} data-type="boss" data-what={row.what} data-testid={`fx-story-${row.id}`}>
      <time className="fx-time" dateTime={row.at} title={row.at}>{clockOf(row.at)}</time>
      <span className="fx-row-face" title="BOSS machinery. No agent at this station.">
        <RunFace roleKey={null} variant="boss" size={24} state="idle" />
      </span>
      <span className="fx-row-main">
        <span className="fx-row-what"><span className="fx-verb">{row.text}</span></span>
        <span className="fx-row-folds">
          <Fold label={`${cmds.length} commands`} count={cmds.length} testId={`fx-cmds-${row.id}`}>
            {() => <span className="fx-files">{cmds.map((c, i) => (
              <span key={i} className="fx-mono" data-user-text>{String(c.command ?? "")} · exit {String(c.exit_code ?? "?")}</span>
            ))}</span>}
          </Fold>
          <Fold label={`${steps.length} steps`} count={steps.length} testId={`fx-steps-${row.id}`}>
            {() => <span className="fx-files">{steps.map((s, i) => (
              <span key={i} data-user-text>{String(s.name ?? s.label ?? s.step ?? "")}</span>
            ))}</span>}
          </Fold>
        </span>
      </span>
      <span className="fx-row-right">{typeof row.ms === "number" ? secondsWord(Math.round(row.ms / 1000)) : ""}</span>
    </li>
  );
}

export function Story(props: StoryProps) {
  const { rows, stations, filter, pending = [] } = props;
  const list = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const seen = useRef(rows.length);
  const [unseen, setUnseen] = useState(0);
  // Rows present at first paint never animate (page load is not a state change); a row that
  // arrives later rises in once, and only while the reader is at the bottom.
  const initial = useRef<Set<string> | null>(null);
  if (initial.current === null) initial.current = new Set(rows.map((r) => r.id));
  const entering = (id: string) => !initial.current?.has(id) && atBottom.current;

  const all: StoryRow[] = [
    ...rows,
    // Guidance sent here and not echoed yet: shown at once, as queued (spec §4.5.6).
    ...pending.filter((p) => !rows.some((r) => r.type === "you" && r.what === "steered" && r.text === p.text))
      .map((p): StoryYou => ({ type: "you", id: p.id, station: null, what: "steered", actor: props.viewer, text: p.text,
                               at: p.at, appliedRole: null })),
  ];
  const shown = filter ? all.filter((r) => r.station === filter) : all;

  useLayoutEffect(() => {
    const el = list.current;
    const grew = all.length - seen.current;
    seen.current = all.length;
    if (!el || grew <= 0) return;
    if (atBottom.current) el.scrollTop = el.scrollHeight;
    else setUnseen((n) => n + grew);
  }, [all.length]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const el = list.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  const onScroll = () => {
    const el = list.current;
    if (!el) return;
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (atBottom.current) setUnseen(0);
  };

  // Group under station heads, in the Line's order of first appearance.
  const label = (key: StationKey | null) => (key ? stations.find((s) => s.key === key)?.label ?? key : "");
  const groups: { key: StationKey | null; rows: StoryRow[] }[] = [];
  for (const r of shown) {
    const last = groups[groups.length - 1];
    const key = r.station ?? last?.key ?? null;
    if (last && last.key === key) last.rows.push(r); else groups.push({ key, rows: [r] });
  }

  return (
    <section className="fx-story" aria-labelledby="fx-story-h" data-testid="fx-story">
      <header className="fx-sec-head">
        <h2 id="fx-story-h" className="fx-sec-title">STORY</h2>
        <button type="button" className="fx-chip" aria-pressed={!filter} onClick={props.onClearFilter} data-testid="fx-story-all">All</button>
        {filter ? (
          <button type="button" className="fx-chip" aria-pressed data-testid="fx-story-filter" onClick={props.onClearFilter}
                  title="Show every station">
            {label(filter)} ×
          </button>
        ) : null}
      </header>
      <div ref={list} className="fx-story-list" onScroll={onScroll} data-testid="fx-story-list">
        {groups.map((g, gi) => (
          <div key={`${g.key}-${gi}`} className="fx-story-group" data-station={g.key ?? undefined}>
            {g.key ? <h3 className="fx-story-station">{label(g.key)}</h3> : null}
            <ol className="fx-story-rows">
              {g.rows.map((r) => r.type === "act" ? <ActRow key={r.id} row={r} props={props} live={props.now} enter={entering(r.id)} />
                : r.type === "you" ? <YouRow key={r.id} row={r} viewer={props.viewer} enter={entering(r.id)} />
                : <BossRow key={r.id} row={r} enter={entering(r.id)} />)}
            </ol>
          </div>
        ))}
        {!shown.length ? <span className="fx-muted" data-testid="fx-story-empty">Nothing yet</span> : null}
      </div>
      {unseen > 0 ? (
        <button type="button" className="fx-new" data-testid="fx-story-new"
                onClick={() => { const el = list.current; if (el) el.scrollTop = el.scrollHeight; atBottom.current = true; setUnseen(0); }}>
          {unseen} new ↓
        </button>
      ) : null}
    </section>
  );
}

/** Who a station's work belongs to, for an aria label: `Build, Forge, coding`. */
export function bayLabel(s: Station): string {
  const who = s.face === "agent" && s.roleKey ? nameOf(s.roleKey).name : s.face === "boss" ? "BOSS" : s.face === "you" ? "You" : "";
  return [s.label, who, s.word.toLowerCase()].filter(Boolean).join(", ");
}
