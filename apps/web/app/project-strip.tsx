"use client";

// React is imported by NAME as well as by hook: this app compiles JSX with the classic
// runtime in tests, where `React` must be in scope at call time. Every sibling client
// component in app/ does the same.
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { appIdentity } from "./app-identity";

/**
 * Every project across BOSS, in the header — the v2 strip, brought to v1.
 *
 * v1 makes you open Factory and scroll a list of 74 cards to learn what is running. The strip
 * answers "what is happening right now" without leaving the area you are in, and clicking one
 * opens it.
 */

type StripProject = {
  id: string;
  title: string;
  status: string;
  stage: string;
  pct: number;
  latest_run_id: string;
};

/** ALL first, because most of the time you want the whole picture; the others narrow it. */
const FILTERS = ["ALL", "RUNNING", "GATED", "DONE"] as const;
type Filter = (typeof FILTERS)[number];

/** Which statuses each filter admits. STUCK belongs under RUNNING: it is work that started and
 *  has not finished, and hiding it under a tab nobody opens is how a stalled project stays
 *  invisible for a day. */
const ADMITS: Record<Filter, (status: string) => boolean> = {
  ALL: () => true,
  RUNNING: (s) => s === "RUNNING" || s === "STUCK",
  GATED: (s) => s === "GATED",
  DONE: (s) => s === "DONE",
};

export function ProjectStrip({ projectId }: { projectId: string }) {
  const [projects, setProjects] = useState<StripProject[]>([]);
  // LOADING, FAILED AND EMPTY ARE THREE DIFFERENT THINGS.
  //
  // A strip that renders nothing while it loads and nothing when the API is down tells the
  // operator "there are no projects" in both cases — the same false claim of absence that made
  // this console report "No verified host yet" before it had looked.
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("ALL");

  useEffect(() => {
    if (!projectId) return;
    let live = true;
    void fetch(`/api/projects-strip?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" })
      .then(async (r) => {
        const body = (await r.json()) as { projects?: StripProject[]; error?: string };
        if (!live) return;
        if (!r.ok) {
          setState("failed");
          setError(body.error ?? `API returned ${r.status}`);
          return;
        }
        // SAME NAME AS THE CARD IT OPENS.
        //
        // The overview returns the raw run title — "PRD — Pull‐Request Review‐Time Tracker",
        // with a U+2010 hyphen the operator cannot type. Reading a strip that says one thing
        // and a card that says another is worse than either alone, so both go through the one
        // function that decides what an app is called.
        setProjects((body.projects ?? []).map((p) => ({
          ...p, title: appIdentity({ title: p.title }).name,
        })));
        setState("ready");
      })
      .catch((e) => {
        if (!live) return;
        setState("failed");
        setError(String((e as Error)?.message ?? e));
      });
    return () => { live = false; };
  }, [projectId]);

  const counts = useMemo(() => {
    const out = {} as Record<Filter, number>;
    for (const f of FILTERS) out[f] = projects.filter((p) => ADMITS[f](p.status)).length;
    return out;
  }, [projects]);

  const shown = useMemo(
    () => projects.filter((p) => ADMITS[filter](p.status)),
    [projects, filter],
  );

  /** Open a project in the Factory area.
   *
   *  pushState + a popstate event, the same client-side move the rest of this console uses to
   *  switch areas — a real navigation would re-render a tree that takes seconds to paint. */
  const open = useCallback((p: StripProject) => {
    const url = new URL(window.location.href);
    url.searchParams.set("area", "code_tasks");
    if (p.latest_run_id) url.searchParams.set("run", p.latest_run_id);
    else url.searchParams.delete("run");
    window.history.pushState({}, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, []);

  if (state === "loading") {
    return (
      <nav className="bx-strip" aria-label="Projects" data-state="loading">
        <span className="bx-strip-label">Projects</span>
        <span className="bx-strip-note">Loading…</span>
      </nav>
    );
  }

  if (state === "failed") {
    return (
      <nav className="bx-strip" aria-label="Projects" data-state="failed">
        <span className="bx-strip-label">Projects</span>
        {/* The reason, not a shrug. "Could not load" sends the operator to the API to find out
            what this line already knows. */}
        <span className="bx-strip-note bx-strip-error">Could not load projects — {error}</span>
      </nav>
    );
  }

  return (
    <nav className="bx-strip" aria-label="Projects" data-state="ready" data-testid="bx-strip">
      <span className="bx-strip-label">Projects</span>

      <div className="bx-strip-scroll">
        {shown.map((p) => (
          <button
            type="button"
            key={p.id}
            className="bx-strip-item"
            onClick={() => open(p)}
            title={`${p.title} — ${p.status.toLowerCase()}, ${p.stage.toLowerCase()} ${p.pct}%`}
            data-testid={`bx-strip-${p.id}`}
          >
            <span className="bx-strip-dot" data-status={p.status} aria-hidden />
            <span className="bx-strip-name">{p.title}</span>
            <span className="bx-strip-stage">{p.stage}</span>
            <span className="bx-strip-pct">{p.pct}%</span>
            {p.status === "GATED" ? <span className="bx-strip-alert">GATED</span> : null}
          </button>
        ))}
        {shown.length === 0 ? (
          <span className="bx-strip-note">
            {projects.length === 0
              ? "No projects yet"
              /* Says which filter is hiding them. An empty strip under a filter reads as "there
                 is nothing", and the operator forgets they narrowed it two minutes ago. */
              : `No ${filter.toLowerCase()} projects — ${projects.length} in total`}
          </span>
        ) : null}
      </div>

      {/* The filter LAST in the DOM but pinned right: the projects are the content, and a
          keyboard user should reach them before the control that narrows them. */}
      <div className="bx-strip-filters" role="group" aria-label="Filter projects by status">
        {FILTERS.map((f) => (
          <button
            type="button"
            key={f}
            className="bx-strip-filter"
            data-active={filter === f}
            aria-pressed={filter === f}
            onClick={() => setFilter(f)}
            data-testid={`bx-strip-filter-${f.toLowerCase()}`}
          >
            {f}
            {/* The count is the point: "GATED" alone does not say whether clicking it is worth
                doing. Zero is shown too — a filter that leads nowhere should say so first. */}
            <span className="bx-strip-filter-n">{counts[f]}</span>
          </button>
        ))}
      </div>
    </nav>
  );
}
