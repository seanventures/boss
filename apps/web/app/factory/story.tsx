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
import { logHref } from "../../lib/log-url";
import { CaretDown, CaretRight } from "@phosphor-icons/react";
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { ReviewComment, StoryAct, StoryBoss, StoryRow, StoryYou, NowLine } from "../../lib/cockpit";
import { plural, type Station, type StationKey } from "../../lib/factory-floor";
import type { ToolCalls } from "../../lib/agents-live-board";
import { nameOf, RunFace } from "../agents/run-face";
import { useNow } from "../agents/live-util";
import { fetchTools } from "./cockpit-actions";
import { areaHref, clockOf, costWord, secondsWord, spanOf, whoName } from "./cockpit-util";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { buildIndex, type LogLine, type ToolLog } from "../../lib/build-detail";
import { ToolList } from "./tool-log";
import { SearchBox, SearchResults, useLogSearch } from "./log-search";



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
  /** Every tool call of the build, one line each (GET …/tool-log); null until read. */
  log?: ToolLog | null;
};

/** A search match chosen: open this act's tool calls at this call. */
type Jump = { actId: string; call: number; n: number };

const ACT_STATE: Record<string, "working" | "needs_you" | "done" | "failed" | "stopped" | "idle"> = {
  active: "working", pending: "idle", awaiting_input: "needs_you", complete: "done", error: "failed", stale: "stopped", skipped: "idle",
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

function ToolsFold({ projectId, runId, act, log, jump }: { projectId: string; runId: string; act: StoryAct; log?: ToolLog | null; jump?: Jump | null }) {
  const [open, setOpen] = useState(false);
  const mine = useMemo(() => (log ? log.items.filter((t) => t.actId === act.actId) : null), [log, act.actId]);
  const focus = jump && jump.actId === act.actId ? jump.call : null;
  useEffect(() => { if (focus !== null) setOpen(true); }, [focus, jump?.n]);
  if (mine && mine.length) {
    const n = Math.max(act.toolCalls, mine.length);
    const bad = mine.filter((t) => t.ok === false).length;
    return (
      <span className="fx-fold fx-fold-wide">
        <button type="button" className="ag-link fx-fold-btn" aria-expanded={open} data-testid={`fx-tools-${act.actId}`}
                onClick={() => setOpen((v) => !v)}>
          {n} tool {n === 1 ? "call" : "calls"}
          {bad ? <span className="fx-fold-bad"> · {bad} failed</span> : null}
          {" "}{open ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />}
        </button>
        {open ? (
          <span className="fx-fold-body fx-fold-tools">
            <ToolList key={jump?.n ?? 0} calls={mine} focus={focus} testId={`fx-tools-body-${act.actId}`} />
            <a className="ag-link" href={logHref({ run: runId })} data-testid="fx-tools-log">Open in Activity log ↗</a>
          </span>
        ) : null}
      </span>
    );
  }
  return <ToolsCount projectId={projectId} runId={runId} act={act} />;
}

/** An API without the tool log: names and outcomes only (the earlier fold). */
function ToolsCount({ projectId, runId, act }: { projectId: string; runId: string; act: StoryAct }) {
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
          <a className="ag-link" href={logHref({ run: runId })} data-testid="fx-tools-log">
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

function ActRow({ row, props, live, enter, jump }: { row: StoryAct; props: StoryProps; live: NowLine | null; enter?: boolean; jump?: Jump | null }) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const face = useRef<HTMLButtonElement>(null);
  // A running act's span ticks from a clock that exists only after mount (hydration-safe).
  const now = useNow(1000);
  const state = ACT_STATE[row.status] ?? "idle";
  const still = props.still || state === "idle";
  // `round n` only on a build that went round (walk D15): a stage re-opened for another reason
  // on a build with no repair rounds is not a round.
  const looped = props.stations.some((st) => st.rounds > 0);
  const title = [row.title, row.seat ? `seat ${row.seat}` : "", looped && row.round > 1 ? `round ${row.round}` : ""].filter(Boolean).join(" · ");
  const right = [row.word, spanOf(row.startedAt, row.endedAt ?? null, row.endedAt ? null : now),
    costWord(row.costUsd)].filter(Boolean);
  const authored = row.commentsAuthored > 0
    ? props.comments.filter((c) => c.authorRoleKey && c.authorRoleKey === row.roleKey && (!row.round || c.round === row.round))
    : [];
  const fixed = row.commentsFixed > 0 ? props.comments.filter((c) => c.status === "addressed" && c.round < row.round) : [];
  return (
    <li className="fx-ck-row" data-enter={enter || undefined} data-type="act" data-status={row.status} data-testid={`fx-story-${row.id}`} data-role={row.roleKey ?? undefined}
        data-act={row.actId}>
      <time className="fx-ck-time" dateTime={row.startedAt} title={row.startedAt}>{clockOf(row.startedAt)}</time>
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
          <Fold label={plural(row.files?.length ?? 0, "file")} count={row.files?.length ?? 0} testId={`fx-files-${row.id}`}>
            {() => <span className="fx-files">{(row.files ?? []).map((f) => <span key={f} className="fx-mono" data-user-text>{f}</span>)}</span>}
          </Fold>
          <ToolsFold projectId={props.projectId} runId={props.runId} act={row} log={props.log} jump={jump} />
          <Fold label={plural(authored.length || row.commentsAuthored, "comment")} count={row.commentsAuthored} testId={`fx-authored-${row.id}`}>
            {() => <CommentList items={authored} />}
          </Fold>
          <Fold label={`Fixing ${plural(row.commentsFixed, "comment")}`} count={row.commentsFixed} testId={`fx-fixing-${row.id}`}>
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
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const mine = !row.actor || row.actor === viewer;
  const word = row.what === "waived" ? (row.count ? `Waived ${plural(row.count, "check")}` : "Waived checks") : YOU_WORDS[row.what] ?? row.what;
  return (
    <li className="fx-ck-row" data-enter={enter || undefined} data-type="you" data-what={row.what} data-testid={`fx-story-${row.id}`}>
      <time className="fx-ck-time" dateTime={row.at} title={row.at}>{clockOf(row.at)}</time>
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
          row.word && !row.word.startsWith("Queued") && !row.word.startsWith("Read by")
            // The server's last word on it: `Not read · build ended` (walk D11).
            ? <span data-testid="fx-steer-unread" title="The build ended before the next station">{row.word}</span>
            : row.appliedAt || row.word?.startsWith("Read by")
              ? <span data-testid="fx-steer-read">{row.word ?? `Read by ${whoName(row.appliedRole)}`}</span>
              : <span data-testid="fx-steer-queued" title="Agents read guidance between stations">Queued for next station</span>
        ) : row.word ? (
          // Any other row the server has a last word on: a Stop the build outran (walk D1).
          <span data-testid={`fx-you-word-${row.id}`}>{row.word}</span>
        ) : null}
      </span>
    </li>
  );
}

function BossRow({ row, enter }: { row: StoryBoss; enter?: boolean }) {
  const cmds = Array.isArray(row.detail?.commands) ? (row.detail?.commands as Record<string, unknown>[]) : [];
  const steps = Array.isArray(row.detail?.steps) ? (row.detail?.steps as Record<string, unknown>[]) : [];
  return (
    <li className="fx-ck-row" data-enter={enter || undefined} data-type="boss" data-what={row.what} data-testid={`fx-story-${row.id}`}>
      <time className="fx-ck-time" dateTime={row.at} title={row.at}>{clockOf(row.at)}</time>
      <span className="fx-row-face" title="BOSS machinery. No agent at this station.">
        <RunFace roleKey={null} variant="boss" size={24} state="idle" />
      </span>
      <span className="fx-row-main">
        <span className="fx-row-what"><span className="fx-verb">{row.text}</span></span>
        <span className="fx-row-folds">
          <Fold label={plural(cmds.length, "command")} count={cmds.length} testId={`fx-cmds-${row.id}`}>
            {() => <span className="fx-files">{cmds.map((c, i) => (
              <span key={i} className="fx-mono" data-user-text>{String(c.command ?? "")} · exit {String(c.exit_code ?? "?")}</span>
            ))}</span>}
          </Fold>
          <Fold label={plural(steps.length, "step")} count={steps.length} testId={`fx-steps-${row.id}`}>
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
      .map((p): StoryYou => ({ type: "you", id: p.id, station: currentStation(stations), what: "steered", actor: props.viewer, text: p.text,
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

  const label = (key: StationKey | null) => (key ? stations.find((s) => s.key === key)?.label ?? key : "");
  const groups = groupByStation(shown, stations);

  // SEARCH: one index over the Story, the tool calls and the verify output.
  const actTitle = useMemo(() => {
    const by = new Map(rows.filter((r): r is StoryAct => r.type === "act").map((r) => [r.actId, r.title]));
    return (id: string | null) => (id ? by.get(id) ?? "" : "");
  }, [rows]);
  const index = useMemo(() => buildIndex(rows, props.log ?? null,
    (k) => (k ? stations.find((s) => s.key === k)?.label ?? "" : ""), actTitle), [rows, props.log, stations, actTitle]);
  const search = useLogSearch(index);
  const input = useRef<HTMLInputElement>(null);
  const [jump, setJump] = useState<Jump | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.altKey || e.metaKey || e.ctrlKey || e.defaultPrevented) return;
      const t = e.target as HTMLElement | null;
      if (t?.closest?.("input, textarea, select, [contenteditable='true']")) return;
      e.preventDefault();
      input.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  const pick = (hit: LogLine, i: number) => {
    search.setCurrent(i);
    if (hit.call === undefined) return;
    const call = props.log?.items[hit.call];
    if (!call?.actId) return;
    // Show it where it happened: leave the search, open that act's calls at this one.
    search.setState({ query: "", filter: null });
    if (filter) props.onClearFilter();
    setJump((j) => ({ actId: call.actId as string, call: hit.call as number, n: (j?.n ?? 0) + 1 }));
    window.requestAnimationFrame(() => {
      list.current?.querySelector(`[data-act="${call.actId}"]`)?.scrollIntoView({ block: "start" });
    });
  };

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
        <SearchBox value={search.state} onChange={search.setState} count={search.hits.length} current={search.current}
                   onStep={search.step} inputRef={input} />
      </header>
      {search.active ? (
        <div className="fx-story-list fx-hits-wrap" data-testid="fx-story-hits">
          <SearchResults hits={search.hits} query={search.state.query} current={search.current} onPick={pick} />
        </div>
      ) : null}
      <div ref={list} className="fx-story-list" onScroll={onScroll} data-testid="fx-story-list" hidden={search.active}>
        {groups.map((g, gi) => (
          <div key={`${g.key}-${gi}`} className="fx-story-group" data-station={g.key ?? undefined}>
            {g.key ? <h3 className="fx-story-station">{label(g.key)}</h3> : null}
            <ol className="fx-story-rows">
              {g.rows.map((r) => r.type === "act" ? <ActRow key={r.id} row={r} props={props} live={props.now} enter={entering(r.id)} jump={jump} />
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

/** The station the build is at: where guidance sent now is filed. */
export function currentStation(stations: Station[]): StationKey | null {
  for (const want of ["needs_you", "failed", "stuck", "stopped", "working", "waiting"]) {
    const hit = stations.find((st) => st.status === want);
    if (hit) return hit.key;
  }
  const done = stations.filter((st) => st.status === "done");
  return done.length ? done[done.length - 1].key : null;
}

/**
 * The Story under ONE head per station, in the LINE'S order (walk D8): `Check` once, not again
 * after `PR` because a test re-ran later. Inside a station, rows keep their time order. A row the
 * server filed under no station goes with the row before it in time.
 */
export function groupByStation(rows: StoryRow[], stations: Station[]): { key: StationKey | null; rows: StoryRow[] }[] {
  const order = new Map<string, number>(stations.map((st, i) => [st.key, i]));
  const byKey = new Map<string, { key: StationKey | null; rows: StoryRow[]; first: number }>();
  let prev: StationKey | null = null;
  rows.forEach((r, i) => {
    const key = r.station ?? prev;
    prev = key;
    const k = key ?? "";
    const g = byKey.get(k) ?? { key, rows: [], first: i };
    g.rows.push(r);
    byKey.set(k, g);
  });
  const rank = (g: { key: StationKey | null; first: number }) =>
    g.key === null ? -1 : order.get(g.key) ?? stations.length + g.first;
  return [...byKey.values()].sort((a, b) => rank(a) - rank(b) || a.first - b.first)
    .map(({ key, rows: rs }) => ({ key, rows: rs }));
}

/** Who a station's work belongs to, for an aria label: `Build, Forge, coding`. */
export function bayLabel(s: Station): string {
  const who = s.face === "agent" && s.roleKey ? nameOf(s.roleKey).name : s.face === "boss" ? "BOSS" : s.face === "you" ? "You" : "";
  return [s.label, who, s.word.toLowerCase()].filter(Boolean).join(", ");
}
