"use client";

import React, { useEffect, useMemo, useState, useTransition } from "react";

import { formatDateTime } from "../lib/datetime";
import { fetchActivityLog } from "../lib/activity-log-client";

/**
 * Local types matching GET /v1/activity.
 * Prefer apps/web/lib/activity-log.ts when that sibling module is present.
 */
const ACTIVITY_STATUSES = [
  "ok",
  "failed",
  "waiting",
  "stuck",
  "info",
] as const;

const ACTIVITY_KINDS = [
  "event",
  "process_step",
  "job",
  "deadletter",
  "receipt",
  "llm_call",
  "agent_turn",
] as const;

/** How loudly a row speaks. A different question from `status`, which says what happened to a
 *  piece of work — and the one a log viewer is organised around. */
const LEVELS = ["error", "warn", "info", "verbose"] as const;

/** The streams, in the words an operator uses. "Infra" is three sources at once because
 *  "is BOSS healthy" and "is my project healthy" are different questions. */
const SOURCE_CHOICES: Array<{ value: string; label: string; hint: string }> = [
  { value: "", label: "Everything", hint: "all streams" },
  { value: "agent_turn", label: "Agent conversation", hint: "what the agents said and ran" },
  { value: "llm_call", label: "Model calls", hint: "which agent used which model" },
  { value: "event", label: "Events", hint: "the audit stream" },
  { value: "process_step", label: "Process steps", hint: "the PRD pipeline" },
  { value: "infra", label: "BOSS infra", hint: "jobs, deadletters, receipts" },
];

const API_SORTS = new Set(["created_at", "status", "kind"]);

type ActivityStatus = (typeof ACTIVITY_STATUSES)[number];
type ActivityKind = (typeof ACTIVITY_KINDS)[number];
type SortKey = "created_at" | "status" | "kind" | "title" | "actor" | "entity";
type SortOrder = "asc" | "desc";

type ActivityLogItem = {
  id: string;
  source: string;
  kind: string;
  status: ActivityStatus | string;
  level: string;
  title: string;
  actor: string;
  entityType: string;
  entityId: string;
  stage: string;
  runId: string;
  agent: string;
  body: string;
  createdAt: string;
  data: Record<string, unknown>;
};

type ActivityLogFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: ActivityLogItem[];
  total?: number;
  limit?: number;
  offset?: number;
};

const EMPTY_FEED: ActivityLogFeed = {
  projectId: "",
  apiReachable: false,
  items: [],
};

const KIND_LABELS: Record<ActivityKind, string> = {
  event: "Event",
  process_step: "Process step",
  job: "Job",
  deadletter: "Deadletter",
  receipt: "Receipt",
  llm_call: "Model call",
  agent_turn: "Agent turn",
};

/** The first meaningful line of a turn, for the row that summarises it.
 *
 *  A transcript row whose title is "Turn Completion" tells you a turn happened; the first line
 *  of what was actually said tells you what the build is doing. Clipped hard — this sits inside
 *  a table cell, and a wrapped paragraph per row makes the sequence unreadable.
 */
function firstLine(body: string): string {
  const line = (body || "")
    .split("\n")
    .map((part) => part.trim())
    .find((part) => part.length > 0);
  if (!line) return "";
  return line.length > 140 ? `${line.slice(0, 140)}…` : line;
}

function labelize(value: string): string {
  return value.replaceAll("_", " ");
}

function statusTone(status: string): "ok" | "bad" | "live" | "idle" {
  if (status === "ok") return "ok";
  if (status === "failed" || status === "stuck") return "bad";
  if (status === "waiting") return "live";
  return "idle";
}

function entityLabel(item: ActivityLogItem): string {
  if (item.entityType && item.entityId) return `${item.entityType} · ${item.entityId}`;
  return item.entityId || item.entityType || "—";
}

function displayText(value: unknown, empty = "—"): string {
  if (typeof value !== "string") return empty;
  const trimmed = value.trim();
  return trimmed || empty;
}

function compareText(left: string, right: string): number {
  return left.localeCompare(right, "en", { sensitivity: "base", numeric: true });
}

function sortKeyValue(item: ActivityLogItem, sort: SortKey): string {
  if (sort === "entity") return entityLabel(item);
  if (sort === "created_at") return item.createdAt;
  if (sort === "status") return item.status;
  if (sort === "kind") return item.kind;
  if (sort === "title") return item.title;
  return item.actor;
}

function sortItems(
  items: ActivityLogItem[],
  sort: SortKey,
  order: SortOrder,
): ActivityLogItem[] {
  if (API_SORTS.has(sort)) return items;
  const direction = order === "asc" ? 1 : -1;
  return [...items].sort(
    (left, right) =>
      compareText(sortKeyValue(left, sort), sortKeyValue(right, sort)) * direction,
  );
}

function redactPayload(data: Record<string, unknown>): string {
  if (Object.keys(data).length === 0) return "";
  try {
    return JSON.stringify(data, null, 2);
  } catch {
    return "";
  }
}

export function ActivityLog({
  projectId,
  actor,
  runId,
  runTitles = {},
}: {
  projectId: string;
  actor: string;
  /** run id -> the app's name. The scope banner said only "crun_2f0a15b2…", which is true and
   *  tells the reader nothing about which app they are looking at. */
  runTitles?: Record<string, string>;
  /** Scope every query to ONE work task. Normally arrives as `?run=` instead — see below —
   *  but a caller that already knows the run can pass it directly. */
  runId?: string;
}) {
  const [feed, setFeed] = useState<ActivityLogFeed>({
    ...EMPTY_FEED,
    projectId,
  });
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const [actorFilter, setActorFilter] = useState("");
  // Level defaults to hiding VERBOSE.
  //
  // Per-turn capture writes hundreds of rows per build and most of them are a model thinking
  // out loud. Opening the log on all of them would bury the events people come here to find —
  // the levels exist precisely so the default view is readable, and the chip says plainly that
  // something is being hidden.
  const [level, setLevel] = useState("");
  const [hideVerbose, setHideVerbose] = useState(true);
  const [source, setSource] = useState("");
  const [agentFilter, setAgentFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // SCOPE FROM THE URL, READ ON THE CLIENT.
  //
  // The Factory detail page links here with `?run=` via pushState, which never re-runs the
  // server component that renders this one — so a prop threaded from there would work on a
  // reload and do nothing on the click that matters.
  const [urlRun, setUrlRun] = useState("");
  useEffect(() => {
    const read = () => setUrlRun(new URLSearchParams(window.location.search).get("run") ?? "");
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  const scopedRun = runId || urlRun;
  const [sort, setSort] = useState<SortKey>("created_at");
  const [order, setOrder] = useState<SortOrder>("desc");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    setLoading(true);
    setSelectedId(null);
    setFeed({ ...EMPTY_FEED, projectId });
  }, [projectId]);

  const serverSort = API_SORTS.has(sort) ? sort : undefined;
  const serverOrder = API_SORTS.has(sort) ? order : undefined;

  useEffect(() => {
    let alive = true;
    startTransition(async () => {
      // A ROUTE HANDLER, NOT A SERVER ACTION. This call took 25.3 seconds on the live
      // console -- about 14s of query and the rest a full re-render of the route, which is
      // what a Server Action ships alongside its return value. See lib/activity-log-client.ts.
      const result = await fetchActivityLog(projectId, {
        q: debouncedSearch || undefined,
        kind: kind || undefined,
        status: status || undefined,
        actor: actorFilter || undefined,
        level: level || undefined,
        source: source || undefined,
        agent: agentFilter || undefined,
        run_id: scopedRun || undefined,
        sort: serverSort,
        order: serverOrder,
        // A transcript is dense. Fifty rows is barely a minute of a build.
        limit: 200,
      });
      if (!alive) return;
      setFeed(result);
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [projectId, debouncedSearch, kind, status, actorFilter, level, source, agentFilter,
      scopedRun, serverSort, serverOrder]);

  const items = useMemo(() => {
    const rows = feed.items ?? [];
    // Client-side, because it is a VIEW preference rather than a query: toggling it must not
    // cost a round trip, and the counts below have to be able to say how many are hidden.
    const shown = hideVerbose && !level
      ? rows.filter((item) => item.level !== "verbose")
      : rows;
    return sortItems(shown, sort, order);
  }, [feed.items, sort, order, hideVerbose, level]);

  /** How many rows sit at each level on this page. A level chip with no count is a guess about
   *  whether clicking it is worth doing. */
  const levelCounts = useMemo(() => {
    const counts: Record<string, number> = { error: 0, warn: 0, info: 0, verbose: 0 };
    for (const item of feed.items ?? []) {
      counts[item.level] = (counts[item.level] ?? 0) + 1;
    }
    return counts;
  }, [feed.items]);

  const agentOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const item of feed.items ?? []) {
      if (item.agent) seen.add(item.agent);
    }
    return Array.from(seen).sort();
  }, [feed.items]);

  const toggleRow = (id: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const kindOptions = useMemo(() => {
    const seen = new Set<string>(ACTIVITY_KINDS);
    const extras = items
      .map((item) => item.kind.trim())
      .filter((value) => value && !seen.has(value));
    return [...ACTIVITY_KINDS, ...Array.from(new Set(extras))];
  }, [items]);

  const selected = items.find((item) => item.id === selectedId) ?? null;
  const selectedPayload = selected ? redactPayload(selected.data) : "";
  const hasFilters = Boolean(debouncedSearch || status || kind || actorFilter);
  const stuckCount = items.filter((item) => item.status === "stuck").length;
  const unavailable = !loading && !feed.apiReachable;
  const empty = !loading && feed.apiReachable && items.length === 0;
  const blocked =
    feed.blockedReason ||
    "Activity log is unavailable. No events, steps, jobs, receipts, or deadletters were invented.";

  const toggleSort = (next: SortKey) => {
    if (sort === next) {
      setOrder((current) => (current === "desc" ? "asc" : "desc"));
      return;
    }
    setSort(next);
    setOrder(next === "created_at" ? "desc" : "asc");
  };

  const selectRow = (id: string) => {
    setSelectedId((current) => (current === id ? null : id));
  };

  return (
    <article
      className="stack-card"
      data-testid="activity-log"
      data-actor={actor}
      aria-label="Activity log"
      aria-busy={loading || pending}
    >
      <div className="section-head">
        <div>
          <span className="eyebrow">Ops timeline</span>
          <h2>Activity log</h2>
        </div>
      </div>
      <p className="detail">
        Central log of events, steps, jobs, receipts, and deadletters.
      </p>

      {unavailable ? (
        <p className="error-banner" role="alert" data-testid="activity-log-error">
          {blocked}
        </p>
      ) : null}

      {scopedRun ? (
        // A FILTER THE OPERATOR DID NOT SET HERE MUST ANNOUNCE ITSELF.
        // They arrived from a run page, so this view is already narrowed to one build. Without
        // saying so, the log reads as though the whole project only ever did this.
        <p className="alog-scope" data-testid="activity-scope">
          <span>Showing only</span>
          {/* The NAME first, because that is what the operator came here about. The id stays —
              it is what they would paste into a query or a bug report — but it is no longer
              the only thing on the line. */}
          {runTitles[scopedRun] ? (
            <strong className="alog-scope-name" data-testid="activity-scope-name">
              {runTitles[scopedRun]}
            </strong>
          ) : null}
          <code className="mono">{scopedRun}</code>
          <button
            type="button"
            className="cap-btn"
            onClick={() => {
              const url = new URL(window.location.href);
              url.searchParams.delete("run");
              window.history.pushState({}, "", url.toString());
              window.dispatchEvent(new PopStateEvent("popstate"));
            }}
            data-testid="activity-scope-clear"
          >
            Show the whole project
          </button>
        </p>
      ) : null}

      {/* LEVELS FIRST, AS CHIPS.
          Everything else here narrows by identity — which actor, which kind. Level is the one
          filter an operator reaches for before they know what they are looking for: show me
          what broke. It gets the top row, with counts, so "ERROR 12" answers the question
          without a click. */}
      <div className="alog-levels" role="group" aria-label="Filter by log level">
        <button
          type="button"
          className="alog-level"
          data-active={!level}
          aria-pressed={!level}
          onClick={() => setLevel("")}
          data-testid="activity-level-all"
        >
          ALL
        </button>
        {LEVELS.map((value) => (
          <button
            type="button"
            key={value}
            className="alog-level"
            data-level={value}
            data-active={level === value}
            aria-pressed={level === value}
            onClick={() => setLevel(level === value ? "" : value)}
            data-testid={`activity-level-${value}`}
          >
            {value.toUpperCase()}
            <span className="alog-level-n">{levelCounts[value] ?? 0}</span>
          </button>
        ))}
        {/* Says plainly that rows are being hidden. A viewer that quietly drops most of what it
            loaded reads as a viewer that found nothing. */}
        {!level ? (
          <label className="alog-verbose-toggle">
            <input
              type="checkbox"
              checked={hideVerbose}
              onChange={(event) => setHideVerbose(event.target.checked)}
              data-testid="activity-hide-verbose"
            />
            <span>
              Hide verbose
              {hideVerbose && levelCounts.verbose
                ? ` (${levelCounts.verbose} hidden)`
                : ""}
            </span>
          </label>
        ) : null}
      </div>

      <form
        className="composer compact compact-grid"
        data-testid="activity-log-filters"
        onSubmit={(event) => {
          event.preventDefault();
          setDebouncedSearch(search.trim());
        }}
      >
        <label className="field">
          <span>Search</span>
          <input
            className="input"
            type="search"
            name="q"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search title, actor, entity…"
            autoComplete="off"
            data-testid="activity-log-search"
          />
        </label>
        <label className="field">
          <span>Status</span>
          <select
            className="input"
            name="status"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            data-testid="activity-log-status-filter"
          >
            <option value="">All statuses</option>
            {ACTIVITY_STATUSES.map((value) => (
              <option key={value} value={value}>
                {labelize(value)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Stream</span>
          <select
            className="input"
            name="source"
            value={source}
            onChange={(event) => setSource(event.target.value)}
            data-testid="activity-log-source-filter"
          >
            {SOURCE_CHOICES.map((choice) => (
              <option key={choice.value || "all"} value={choice.value}>
                {choice.label} — {choice.hint}
              </option>
            ))}
          </select>
        </label>
        {/* Only offers agents this page actually contains. A dropdown of every role BOSS knows
            about would mostly lead to empty results. */}
        {agentOptions.length ? (
          <label className="field">
            <span>AI agent</span>
            <select
              className="input"
              name="agent"
              value={agentFilter}
              onChange={(event) => setAgentFilter(event.target.value)}
              data-testid="activity-log-agent-filter"
            >
              <option value="">Every agent</option>
              {agentOptions.map((value) => (
                <option key={value} value={value}>
                  {labelize(value)}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <label className="field">
          <span>Actor</span>
          <input
            className="input"
            type="search"
            name="actor"
            value={actorFilter}
            onChange={(event) => setActorFilter(event.target.value)}
            placeholder="Filter by actor…"
            autoComplete="off"
            data-testid="activity-log-actor-filter"
          />
        </label>
        <label className="field">
          <span>Kind</span>
          <select
            className="input"
            name="kind"
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            data-testid="activity-log-kind-filter"
          >
            <option value="">All kinds</option>
            {kindOptions.map((value) => (
              <option key={value} value={value}>
                {KIND_LABELS[value as ActivityKind] ?? labelize(value)}
              </option>
            ))}
          </select>
        </label>
      </form>

      {stuckCount > 0 ? (
        <p className="error-banner" role="status" data-testid="activity-log-stuck">
          {stuckCount} stuck {stuckCount === 1 ? "item" : "items"} in this view.
        </p>
      ) : null}

      {loading ? (
        <p className="loading" role="status" data-testid="activity-log-loading">
          Loading activity…
        </p>
      ) : null}

      {unavailable ? (
        <p className="empty-state" data-testid="activity-log-unavailable">
          {blocked}
        </p>
      ) : null}

      {empty ? (
        <p className="empty-state" data-testid="activity-log-empty">
          {hasFilters
            ? "No events, steps, jobs, receipts, or deadletters match these filters."
            : "No events, steps, jobs, receipts, or deadletters recorded yet. Nothing was invented."}
        </p>
      ) : null}

      {!loading && feed.apiReachable && items.length > 0 ? (
        <div className="table-wrap">
          <table className="ra-runs" data-testid="activity-log-table">
            <caption className="sr-only">
              Searchable activity log. Sort by a column header. Open a row for
              redacted detail.
            </caption>
            <thead>
              <tr>
                <SortHeader
                  label="Time"
                  column="created_at"
                  sort={sort}
                  order={order}
                  onSort={toggleSort}
                />
                <SortHeader
                  label="Status"
                  column="status"
                  sort={sort}
                  order={order}
                  onSort={toggleSort}
                />
                <SortHeader
                  label="Kind"
                  column="kind"
                  sort={sort}
                  order={order}
                  onSort={toggleSort}
                />
                <SortHeader
                  label="Title"
                  column="title"
                  sort={sort}
                  order={order}
                  onSort={toggleSort}
                />
                <SortHeader
                  label="Actor"
                  column="actor"
                  sort={sort}
                  order={order}
                  onSort={toggleSort}
                />
                <SortHeader
                  label="Entity"
                  column="entity"
                  sort={sort}
                  order={order}
                  onSort={toggleSort}
                />
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const selectedRow = selectedId === item.id;
                const open = () => {
                  selectRow(item.id);
                  // A row with a body expands in place; one without opens the detail panel.
                  // Two behaviours because there are two kinds of row, and pretending
                  // otherwise means half the log has a control that does nothing.
                  if (item.body) toggleRow(item.id);
                };
                return [
                  <tr
                    key={item.id}
                    data-testid="activity-log-row"
                    data-level={item.level}
                    data-expanded={expanded.has(item.id) || undefined}
                    tabIndex={0}
                    aria-selected={selectedRow}
                    aria-expanded={item.body ? expanded.has(item.id) : undefined}
                    onClick={open}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        open();
                      }
                    }}
                    style={
                      selectedRow
                        ? { background: "rgba(47, 111, 224, 0.08)", cursor: "pointer" }
                        : { cursor: "pointer" }
                    }
                  >
                    <td className="mono">
                      {formatDateTime(item.createdAt, "—")}
                    </td>
                    <td>
                      {/* The level in words as well as colour, because this column is the one
                          people scan and colour alone does not survive a screenshot, a
                          colour-blind reader, or a printout. */}
                      <span className="alog-tag" data-level={item.level}>
                        {(item.level || "info").toUpperCase()}
                      </span>
                    </td>
                    <td>{displayText(item.kind || item.source)}</td>
                    <td className="ra-run-title" title={item.title || undefined}>
                      {displayText(item.title)}
                      {/* The first line of a turn, inline. Without it every transcript row
                          reads "Turn Completion" and the log is a list of shapes. */}
                      {item.body ? (
                        <span className="alog-preview">{firstLine(item.body)}</span>
                      ) : null}
                    </td>
                    <td>{displayText(item.agent || item.actor)}</td>
                    <td className="mono">{entityLabel(item)}</td>
                  </tr>,
                  // A SECOND ROW, not a modal. Reading a build means reading turns in order,
                  // and a dialog that covers the list breaks the sequence you came for.
                  item.body && expanded.has(item.id) ? (
                    <tr key={`${item.id}-body`} className="alog-body-row">
                      <td colSpan={6}>
                        <pre className="alog-body" data-testid="activity-turn-body">
                          {item.body}
                        </pre>
                        {typeof item.data?.truncated === "boolean" && item.data.truncated ? (
                          <p className="detail alog-truncated">
                            Clipped for storage — {String(item.data.content_bytes ?? "?")} bytes
                            were captured. A clipped turn must never read as a short one.
                          </p>
                        ) : null}
                      </td>
                    </tr>
                  ) : null,
                ];
              }).flat()}
            </tbody>
          </table>
          <p className="detail">
            Showing {items.length}
            {typeof feed.total === "number" ? ` of ${feed.total}` : ""}
            {pending ? " · refreshing…" : ""}
          </p>
        </div>
      ) : null}

      {selected ? (
        <aside
          className="stack-card"
          data-testid="activity-log-detail"
          aria-label="Activity detail"
        >
          <div className="section-head">
            <div>
              <span className="eyebrow">Redacted row</span>
              <h3>{displayText(selected.title, "Activity")}</h3>
            </div>
          </div>
          <dl className="detail">
            <div>
              <dt>Time</dt>
              <dd>{formatDateTime(selected.createdAt, "—")}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{displayText(selected.status)}</dd>
            </div>
            <div>
              <dt>Kind</dt>
              <dd>{displayText(selected.kind)}</dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd>{displayText(selected.source)}</dd>
            </div>
            <div>
              <dt>Actor</dt>
              <dd>{displayText(selected.actor)}</dd>
            </div>
            <div>
              <dt>Entity</dt>
              <dd>{entityLabel(selected)}</dd>
            </div>
            <div>
              <dt>Stage</dt>
              <dd>{displayText(selected.stage)}</dd>
            </div>
          </dl>
          <h4>Redacted data</h4>
          {selectedPayload ? (
            <pre className="mono">{selectedPayload}</pre>
          ) : (
            <p className="detail">No redacted payload on this row.</p>
          )}
        </aside>
      ) : null}
    </article>
  );
}

function SortHeader({
  label,
  column,
  sort,
  order,
  onSort,
}: {
  label: string;
  column: SortKey;
  sort: SortKey;
  order: SortOrder;
  onSort: (column: SortKey) => void;
}) {
  const active = sort === column;
  return (
    <th scope="col" aria-sort={active ? (order === "asc" ? "ascending" : "descending") : "none"}>
      <button
        type="button"
        onClick={() => onSort(column)}
        aria-label={`Sort by ${label}`}
        style={{
          background: "none",
          border: 0,
          padding: 0,
          color: "inherit",
          font: "inherit",
          letterSpacing: "inherit",
          textTransform: "inherit",
          cursor: "pointer",
        }}
      >
        {label}
        {active ? (order === "asc" ? " ↑" : " ↓") : ""}
      </button>
    </th>
  );
}
