"use client";

import React, { useEffect, useMemo, useState, useTransition } from "react";

import { formatDateTime } from "../lib/datetime";
import { fetchActivityFacets, fetchActivityLog } from "../lib/activity-log-client";
import { activityPagesByCursor, facetChipLabel, type ActivityFacets } from "../lib/activity-log";

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
  "app_log",
  "workflow_step",
  "deployment",
] as const;

/** How loudly a row speaks. A different question from `status`, which says what happened to a
 *  piece of work — and the one a log viewer is organised around. */
const LEVELS = ["error", "warn", "info", "verbose"] as const;

/** EVERY STREAM, EACH ONE INDEPENDENTLY SWITCHABLE.
 *
 *  This was a one-of dropdown, so the log could show you one stream or all of them and nothing
 *  in between. "The workflow and the deploys, but not the model chatter" is an ordinary
 *  question and it could only be answered by looking twice.
 *
 *  All ticked is the default. The API still accepts the `queue` and `platform` GROUPS, but the
 *  console no longer needs them: a set of toggles says the same thing and says it visibly.
 */
const SOURCE_FILTERS: Array<{ value: string; label: string; hint: string }> = [
  { value: "event", label: "Events", hint: "the audit stream" },
  // THE WORKFLOW, at step resolution. Workflow lifecycle was always in "Events", which says a
  // step became ready and never how long it took or why it failed. This is the source that
  // carries the duration, the attempt count and the error text.
  { value: "workflow_step", label: "Workflow steps", hint: "each step, its duration and error" },
  { value: "process_step", label: "Process steps", hint: "the PRD pipeline" },
  { value: "agent_turn", label: "Agent conversation", hint: "what the agents said and ran" },
  { value: "llm_call", label: "Model calls", hint: "which agent used which model" },
  // BOSS's own machinery AND the container output of the apps it builds. A phase reading
  // "27m46s — no model call" says the model was not the cost without saying what was; this is
  // where what-was lives, now including the app's own stdout.
  { value: "app_log", label: "App & BOSS output", hint: "container output, commands, timings" },
  // THE APP'S INFRASTRUCTURE, not BOSS's queue. These were one filter called "BOSS infra",
  // which meant jobs+deadletters+receipts — so an operator asking "did the deploy fail" was
  // shown the work queue and concluded the log knew nothing about deploys.
  { value: "deployment", label: "Deployments", hint: "what shipped where, and if it came up" },
  { value: "job", label: "Jobs", hint: "BOSS's work queue" },
  { value: "deadletter", label: "Deadletters", hint: "work that gave up" },
  { value: "receipt", label: "Receipts", hint: "recorded effects" },
];

/** The value sent when every stream is ticked: nothing at all. Spelling out all ten names
 *  would mean the same thing and cost the request its unfiltered fast path. */
const ALL_SOURCES = SOURCE_FILTERS.map((choice) => choice.value);

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
  nextCursor?: string;
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
  app_log: "BOSS internals",
  workflow_step: "Workflow step",
  deployment: "Deployment",
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
  workflowRunId,
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
  /** Scope every query to ONE workflow run (`wfr_…`). Normally arrives as `?wfr=`. */
  workflowRunId?: string;
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
  // EVERY STREAM ON, until the operator says otherwise. The set holds what is SHOWN, so the
  // default is all of them and unticking one is the act of filtering.
  const [enabledSources, setEnabledSources] = useState<Set<string>>(
    () => new Set(ALL_SOURCES),
  );
  const [agentFilter, setAgentFilter] = useState("");
  const [serviceFilter, setServiceFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // SCOPE FROM THE URL, READ ON THE CLIENT.
  //
  // The Factory detail page links here with `?run=` via pushState, which never re-runs the
  // server component that renders this one — so a prop threaded from there would work on a
  // reload and do nothing on the click that matters.
  const [urlRun, setUrlRun] = useState("");
  // `wfr=` is the WORKFLOW run, read the same way and for the same reason. The console links
  // here carrying both (`?run=crun_…&wfr=wfr_…`) and they scope different things: one work
  // task, versus the whole governed run that task was a step of. Arriving from a workflow run
  // used to drop that scope on the floor and show the project's entire log.
  const [urlWorkflowRun, setUrlWorkflowRun] = useState("");
  // `step=` narrows the workflow run to ONE of its steps: the step's own rows and the work task
  // it started. A run step's "Step log" link lands here.
  const [urlStep, setUrlStep] = useState("");
  // The names the step panel knew, for the banner; the ids are the fallback and the tooltip.
  const [urlNames, setUrlNames] = useState<{ step: string; run: string }>({ step: "", run: "" });
  useEffect(() => {
    const read = () => {
      const params = new URLSearchParams(window.location.search);
      setUrlRun(params.get("run") ?? "");
      setUrlWorkflowRun(params.get("wfr") ?? "");
      setUrlStep(params.get("step") ?? "");
      setUrlNames({ step: params.get("step_name") ?? "", run: params.get("run_name") ?? "" });
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  const scopedRun = runId || urlRun;
  const scopedWorkflowRun = workflowRunId || urlWorkflowRun;
  const scopedStep = scopedWorkflowRun ? urlStep : "";
  const [sort, setSort] = useState<SortKey>("created_at");
  const [order, setOrder] = useState<SortOrder>("desc");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  // THE LOG'S COUNTS, not the page's. null until they arrive, or when they could not be read.
  const [facets, setFacets] = useState<ActivityFacets | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState("");

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
  const pagesByCursor = activityPagesByCursor(serverSort, serverOrder);

  const allSourcesOn = enabledSources.size === ALL_SOURCES.length;
  const noSourcesOn = enabledSources.size === 0;
  // Ticked-everything sends NOTHING. Listing all ten names would mean the same thing to the
  // API and would cost the request its unfiltered fast path, which is what keeps the first
  // page of a large project quick.
  const sourceParam = allSourcesOn
    ? undefined
    : ALL_SOURCES.filter((value) => enabledSources.has(value)).join(",");

  const listQuery = useMemo(() => ({
    q: debouncedSearch || undefined,
    kind: kind || undefined,
    status: status || undefined,
    actor: actorFilter || undefined,
    level: level || undefined,
    source: sourceParam,
    agent: agentFilter || undefined,
    service: serviceFilter || undefined,
    run_id: scopedRun || undefined,
    workflow_run_id: scopedWorkflowRun || undefined,
    step_id: scopedStep || undefined,
    sort: serverSort,
    order: serverOrder,
    // A transcript is dense. Fifty rows is barely a minute of a build.
    limit: 200,
  }), [debouncedSearch, kind, status, actorFilter, level, sourceParam, agentFilter,
       serviceFilter, scopedRun, scopedWorkflowRun, scopedStep, serverSort, serverOrder]);

  // THE CHIP COUNTS, FROM THE LOG. Fetched on their own so the first page never waits for the
  // classification they need; scoped like the list, but not by level or stream, because a chip
  // says how many rows switching it on would show.
  useEffect(() => {
    let alive = true;
    setFacets(null);
    void fetchActivityFacets(projectId, {
      run_id: scopedRun || undefined,
      workflow_run_id: scopedWorkflowRun || undefined,
      step_id: scopedStep || undefined,
      agent: agentFilter || undefined,
      service: serviceFilter || undefined,
    }).then((result) => {
      if (alive) setFacets(result);
    });
    return () => {
      alive = false;
    };
  }, [projectId, scopedRun, scopedWorkflowRun, scopedStep, agentFilter, serviceFilter]);

  /** "Load older": the next page behind the last row shown, with the same filters. */
  const loadOlder = async () => {
    if (!feed.nextCursor || loadingOlder) return;
    setLoadingOlder(true);
    setOlderError("");
    const older = await fetchActivityLog(projectId, { ...listQuery, cursor: feed.nextCursor });
    setLoadingOlder(false);
    if (!older.apiReachable) {
      setOlderError(
        `Could not load older rows (${older.blockedReason ?? "the API did not answer"}). ` +
        "The rows above are unchanged; press Load older again to retry.",
      );
      return;
    }
    setFeed((current) => {
      const seen = new Set(current.items.map((item) => item.id));
      return {
        ...current,
        items: [...current.items, ...older.items.filter((item) => !seen.has(item.id))],
        nextCursor: older.nextCursor,
      };
    });
  };

  useEffect(() => {
    let alive = true;
    // NOTHING TICKED IS NOT "EVERYTHING". An empty `source` means every stream to the API, so
    // sending one here would answer "show me none of them" with the whole log. The request is
    // skipped instead and the panel says what happened.
    if (noSourcesOn) {
      setFeed({ ...EMPTY_FEED, projectId, apiReachable: true });
      setLoading(false);
      return;
    }
    startTransition(async () => {
      // A ROUTE HANDLER, NOT A SERVER ACTION. This call took 25.3 seconds on the live
      // console -- about 14s of query and the rest a full re-render of the route, which is
      // what a Server Action ships alongside its return value. See lib/activity-log-client.ts.
      const result = await fetchActivityLog(projectId, listQuery);
      if (!alive) return;
      setFeed(result);
      setOlderError("");
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [projectId, listQuery, noSourcesOn]);

  const items = useMemo(() => {
    const rows = feed.items ?? [];
    // Client-side, because it is a VIEW preference rather than a query: toggling it must not
    // cost a round trip, and the counts below have to be able to say how many are hidden.
    const shown = hideVerbose && !level
      ? rows.filter((item) => item.level !== "verbose")
      : rows;
    return sortItems(shown, sort, order);
  }, [feed.items, sort, order, hideVerbose, level]);

  /** How many rows the LOG holds at each level and in each stream — server-side counts.
   *
   *  These were counted over the 200 rows on the page and read as totals: on boss-1 the chips
   *  said ERROR 0 and Deadletters 0 while Operations reported 14 dead-letters. A chip is read
   *  as a fact about the log, so it comes from the log (GET /v1/activity/facets). Until the
   *  counts arrive, or when they cannot be read, a chip shows "…" or "?" — never a zero that
   *  was not counted. */
  const chipCount = (kind: "levels" | "sources", key: string): string =>
    facetChipLabel(facets, kind, key);
  /** Verbose rows hidden on THIS page — a fact about the page, and labelled as one. */
  const pageVerbose = useMemo(
    () => (feed.items ?? []).filter((item) => item.level === "verbose").length,
    [feed.items],
  );

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
  // EVERY narrowing facet, not just the first four. This decides between "nothing matches
  // these filters" and "nothing was ever recorded", and those are opposite conclusions. The
  // level/source/agent/run/process filters were missing, so narrowing to an empty result
  // announced that BOSS had recorded nothing at all — the Process filter especially, since it
  // excludes every source that does not record a process.
  const hasFilters = Boolean(
    debouncedSearch || status || kind || actorFilter ||
    // `!allSourcesOn` rather than a truthy string: the streams filter is a SET now, and it is
    // active precisely when something has been switched off.
    level || !allSourcesOn || agentFilter || serviceFilter || scopedRun || scopedWorkflowRun,
  );
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

      {scopedWorkflowRun && !scopedRun ? (
        // The same announcement for the workflow scope. Shown only when it is the ONLY scope:
        // with both set, the work-task banner above already says the view is narrowed, and two
        // stacked banners saying "showing only" read as a bug rather than as more precision.
        <p className="alog-scope" data-testid="activity-scope-workflow">
          <span>{scopedStep ? "Showing only" : "Showing only this workflow run"}</span>
          {scopedStep ? (
            <strong className="alog-scope-name" data-testid="activity-scope-step"
                    data-tip={`${scopedStep} · ${scopedWorkflowRun}`}>
              {[urlNames.step || scopedStep, urlNames.run].filter(Boolean).join(" · ")}
            </strong>
          ) : (
            <code className="mono">{scopedWorkflowRun}</code>
          )}
          {scopedStep ? (
            <button
              type="button"
              className="cap-btn"
              onClick={() => {
                const url = new URL(window.location.href);
                url.searchParams.delete("step");
                url.searchParams.delete("step_name");
                url.searchParams.delete("run_name");
                window.history.pushState({}, "", url.toString());
                window.dispatchEvent(new PopStateEvent("popstate"));
              }}
              data-testid="activity-scope-step-clear"
            >
              Whole run
            </button>
          ) : null}
          <button
            type="button"
            className="cap-btn"
            onClick={() => {
              const url = new URL(window.location.href);
              url.searchParams.delete("wfr");
              url.searchParams.delete("step");
                url.searchParams.delete("step_name");
                url.searchParams.delete("run_name");
              window.history.pushState({}, "", url.toString());
              window.dispatchEvent(new PopStateEvent("popstate"));
            }}
            data-testid="activity-scope-workflow-clear"
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
            <span className="alog-level-n">{chipCount("levels", value)}</span>
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
              {hideVerbose && pageVerbose
                ? ` (${pageVerbose} hidden on this page)`
                : ""}
            </span>
          </label>
        ) : null}
      </div>

      {/* THE STREAMS, EACH ONE SWITCHABLE.
          Everything is on by default, because a log's job is to show what happened and a
          viewer that starts filtered hides things nobody asked it to hide. Unticking is the
          act of filtering, and each toggle carries the count it would remove so the operator
          can see whether it is worth doing. */}
      <div className="alog-sources" role="group" aria-label="Filter by log source">
        <button
          type="button"
          className="alog-source"
          data-active={allSourcesOn}
          aria-pressed={allSourcesOn}
          onClick={() => setEnabledSources(new Set(ALL_SOURCES))}
          data-testid="activity-source-all"
        >
          ALL STREAMS
        </button>
        {SOURCE_FILTERS.map((choice) => {
          const on = enabledSources.has(choice.value);
          return (
            <button
              type="button"
              key={choice.value}
              className="alog-source"
              title={choice.hint}
              data-source={choice.value}
              data-active={on}
              aria-pressed={on}
              onClick={() =>
                setEnabledSources((current) => {
                  const next = new Set(current);
                  // ISOLATE, don't start from nothing. Clicking one stream while every stream
                  // is on means "just this one" — the alternative, unticking it, is nine more
                  // clicks to reach the view they obviously wanted.
                  if (current.size === ALL_SOURCES.length) return new Set([choice.value]);
                  if (next.has(choice.value)) next.delete(choice.value);
                  else next.add(choice.value);
                  return next;
                })
              }
              data-testid={`activity-source-${choice.value}`}
            >
              {choice.label}
              <span className="alog-source-n">{chipCount("sources", choice.value)}</span>
            </button>
          );
        })}
      </div>

      {/* Says plainly that the operator switched everything off, rather than rendering an
          empty table that reads as "this project has done nothing". */}
      {noSourcesOn ? (
        <p className="empty-state" data-testid="activity-no-sources">
          No streams selected. Pick at least one above, or choose ALL STREAMS.
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
        {/* A FIXED LIST, unlike the agent filter above. Deriving the options from the feed
            would be self-defeating for a two-value facet: once you pick "api" the feed holds
            only api rows, so "worker" would vanish from the very control you would use to
            switch to it. */}
        <label className="field">
          <span>Process</span>
          <select
            className="input"
            name="service"
            value={serviceFilter}
            onChange={(event) => setServiceFilter(event.target.value)}
            data-testid="activity-log-service-filter"
            title="Which BOSS process emitted the row. Sources that do not record a process
                   (events, receipts, jobs) are excluded rather than guessed at."
          >
            <option value="">Every process</option>
            <option value="api">API</option>
            <option value="worker">Worker</option>
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
          {facets?.note ? <p className="detail" data-testid="activity-facets-note">{facets.note}</p> : null}
          {/* THE ROWS PAST THE FIRST PAGE. There was no way to them: the log fetched the newest
              200 and anything older was unreachable without guessing a filter. */}
          {feed.nextCursor ? (
            <button type="button" className="cap-btn" onClick={() => void loadOlder()}
                    disabled={loadingOlder} data-testid="activity-load-older">
              {loadingOlder ? "Loading older rows…" : "Load older"}
            </button>
          ) : items.length > 0 && !pending ? (
            // THE END OF THE LOG IS ONLY KNOWN NEWEST-FIRST. The API pages by cursor only when
            // sorted by time, newest first, and sends no cursor for any other order. This line
            // used to say "That is the oldest row" whenever the cursor was absent, so sorting
            // 33k rows by kind showed 200 and claimed there were no more (review of
            // milestone/console-b, 2026-10-02).
            pagesByCursor || (typeof feed.total === "number" && items.length >= feed.total) ? (
              <p className="detail" data-testid="activity-end-of-log">That is the oldest row for these filters.</p>
            ) : (
              <p className="detail" data-testid="activity-paging-newest-first">
                These are the first {items.length} rows in this order. Older rows load only when
                the log is sorted by time, newest first.{" "}
                <button type="button" className="cap-btn" onClick={() => { setSort("created_at"); setOrder("desc"); }}
                        data-testid="activity-sort-newest-first">
                  Sort newest first
                </button>
              </p>
            )
          ) : null}
          {olderError ? <p className="error-banner" role="alert">{olderError}</p> : null}
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
