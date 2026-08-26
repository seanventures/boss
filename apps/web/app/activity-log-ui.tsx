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
] as const;

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
  title: string;
  actor: string;
  entityType: string;
  entityId: string;
  stage: string;
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
};

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
}: {
  projectId: string;
  actor: string;
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
        sort: serverSort,
        order: serverOrder,
        limit: 50,
      });
      if (!alive) return;
      setFeed(result);
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [projectId, debouncedSearch, kind, status, actorFilter, serverSort, serverOrder]);

  const items = useMemo(
    () => sortItems(feed.items ?? [], sort, order),
    [feed.items, sort, order],
  );

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
                return (
                  <tr
                    key={item.id}
                    data-testid="activity-log-row"
                    tabIndex={0}
                    aria-selected={selectedRow}
                    onClick={() => selectRow(item.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        selectRow(item.id);
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
                      <span className="ra-dot" data-tone={statusTone(item.status)} />
                      {displayText(item.status)}
                    </td>
                    <td>{displayText(item.kind || item.source)}</td>
                    <td className="ra-run-title" title={item.title || undefined}>
                      {displayText(item.title)}
                    </td>
                    <td>{displayText(item.actor)}</td>
                    <td className="mono">{entityLabel(item)}</td>
                  </tr>
                );
              })}
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
