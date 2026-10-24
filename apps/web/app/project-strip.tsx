"use client";

// React is imported by NAME as well as by hook: this app compiles JSX with the classic
// runtime in tests, where `React` must be in scope at call time. Every sibling client
// component in app/ does the same.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { searchForProject } from "./url-intent";
import { appIdentity } from "./app-identity";
import { ArchivedNotice, useArchived } from "./project-archive";

/**
 * Software projects built inside the selected control project, in the header.
 *
 * v1 makes you open Factory and scroll a list of 74 cards to learn what is running. The strip
 * answers "what is happening right now" without leaving the area you are in, and clicking one
 * opens it.
 *
 * AN ARCHIVED PROJECT SAYS SO HERE (BOSSAPP-768 S5, spec §5.8): the strip is in every area's
 * header, so it carries the read-only notice and Restore while the open project is archived.
 * Its builds stay listed (reads keep working); nothing new starts until it is restored.
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
const STRIP_LABEL = "Builds";
type Filter = (typeof FILTERS)[number];
const FILTER_LABELS: Record<Filter, string> = {
  ALL: "All",
  RUNNING: "Building",
  GATED: "Gated",
  DONE: "Shipped",
};

/** Which statuses each filter admits. STUCK belongs under RUNNING: it is work that started and
 *  has not finished, and hiding it under a tab nobody opens is how a stalled project stays
 *  invisible for a day. */
const ADMITS: Record<Filter, (status: string) => boolean> = {
  ALL: () => true,
  RUNNING: (s) => s === "RUNNING" || s === "STUCK",
  GATED: (s) => s === "GATED",
  DONE: (s) => s === "DONE",
};

export function ProjectStrip({ projectId, area }: { projectId: string; area?: string }) {
  // On the Factory the Floor's sections ARE the filter (factory spec §4.1): no second set here.
  const filtersShown = area !== "code_tasks";
  const [projects, setProjects] = useState<StripProject[]>([]);
  // LOADING, FAILED AND EMPTY ARE THREE DIFFERENT THINGS.
  //
  // A strip that renders nothing while it loads and nothing when the API is down tells the
  // operator "there are no projects" in both cases — the same false claim of absence that made
  // this console report "No verified host yet" before it had looked.
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("ALL");
  const [expanded, setExpanded] = useState(false);
  const [query, setQuery] = useState("");
  const pickerRef = useRef<HTMLElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  // What exists versus what arrived. A strip that shows 40 of 59 and says nothing is making
  // the same false claim of absence as one that shows nothing at all.
  const [total, setTotal] = useState(0);
  const [confirmedEmpty, setConfirmedEmpty] = useState(false);
  // True only while a REFRESH is in flight, never during the first load — the first load has
  // its own "loading" state, and showing both makes the header flicker twice.
  const [refreshing, setRefreshing] = useState(false);
  // Read once per project, not on the 30 s poll: archiving is rare and a page reload follows it.
  const archived = useArchived(projectId);
  const notice = archived
    ? <ArchivedNotice state={archived} onRestored={() => window.location.reload()} />
    : null;

  const scopeGeneration = useRef(0);
  // Visibility and polling can overlap the first request; only the newest request may settle it.
  const requestSequence = useRef(0);

  const load = useCallback(async (mode: "first" | "refresh") => {
    const generation = scopeGeneration.current;
    const request = ++requestSequence.current;
    const isCurrent = () => generation === scopeGeneration.current && request === requestSequence.current;
    if (!projectId) return;
    if (mode === "refresh") setRefreshing(true);
    try {
      const r = await fetch(
        `/api/projects-strip?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" });
      const body = (await r.json()) as
        { projects?: StripProject[]; total?: number; error?: string };
      if (!isCurrent()) return;
      if (!r.ok) {
        // A FAILED REFRESH MUST NOT EMPTY A GOOD STRIP.
        //
        // Blanking what is already on screen because a later poll failed is worse than showing
        // slightly stale rows: the operator loses the list they were reading, for a reason they
        // cannot see. Before any result arrives, a failed refresh is still an initial-load failure.
        if (mode === "first") {
          setState("failed");
        } else {
          setState((current) => current === "loading" ? "failed" : current);
        }
        setConfirmedEmpty(false);
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
      setTotal(Number(body.total ?? (body.projects ?? []).length));
      // Omit the empty shortcut only when the server explicitly confirms the whole list is empty.
      setConfirmedEmpty(body.total === 0 && Array.isArray(body.projects) && body.projects.length === 0);
      setState("ready");
      setError(null);
    } catch (e) {
      if (!isCurrent()) return;
      if (mode === "first") setState("failed");
      else setState((current) => current === "loading" ? "failed" : current);
      setConfirmedEmpty(false);
      setError(String((e as Error)?.message ?? e));
    } finally {
      if (isCurrent()) setRefreshing(false);
    }
  }, [projectId]);

  useEffect(() => {
    // ConsoleShell reuses this header across projects. Only the current scope may confirm
    // emptiness, report an error, or finish a refresh; old requests can settle after a switch.
    scopeGeneration.current += 1;
    setState("loading");
    setConfirmedEmpty(false);
    setError(null);
    setRefreshing(false);
    void load("first");
    return () => { scopeGeneration.current += 1; };
  }, [load]);

  // IT HAS TO KEEP PULLING.
  //
  // The strip fetched once on mount and then never again, so a project that started, gated or
  // shipped after the page opened simply never appeared — and the header that is supposed to
  // answer "what is happening right now" was answering "what was happening when you arrived".
  // Thirty seconds is slow enough to be free and fast enough that a gate does not sit unseen.
  useEffect(() => {
    if (!projectId) return;
    const timer = setInterval(() => { void load("refresh"); }, 30_000);
    // Coming back to the tab is the moment the answer is most likely to be stale, and the
    // moment somebody is actually looking at it.
    const onVisible = () => { if (!document.hidden) void load("refresh"); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [projectId, load]);

  const counts = useMemo(() => {
    const out = {} as Record<Filter, number>;
    for (const f of FILTERS) out[f] = projects.filter((p) => ADMITS[f](p.status)).length;
    return out;
  }, [projects]);

  const shown = useMemo(
    () => projects
      .filter((p) => ADMITS[filtersShown ? filter : "ALL"](p.status))
      .filter((p) => p.title.toLowerCase().includes(query.trim().toLowerCase()))
      .sort((a, b) => {
        const priority = (status: string) =>
          status === "STUCK" || status === "GATED" ? 0 : status === "RUNNING" ? 1 : 2;
        return priority(a.status) - priority(b.status);
      }),
    [projects, filter, query, filtersShown],
  );

  useEffect(() => {
    if (!expanded) return;
    searchRef.current?.focus();
    const closeOutside = (event: PointerEvent) => {
      if (!pickerRef.current?.contains(event.target as Node)) setExpanded(false);
    };
    const closeEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeEscape);
    };
  }, [expanded]);

  /** Open a project in the Factory area.
   *
   *  pushState + a popstate event, the same client-side move the rest of this console uses to
   *  switch areas — a real navigation would re-render a tree that takes seconds to paint. */
  // THERE ARE MORE, AND THE STRIP HAS TO SAY SO.
  //
  // Forty projects, four visible, and the only clue was a fade at the edge that reads as a
  // style rather than a control. An operator scanning for a project concluded the rest were
  // not there — the same false absence the console keeps having to be taught not to imply.
  //
  // Buttons rather than scroll-on-hover: hovering to move is unpredictable to aim at, cannot
  // be reached from a keyboard, and fires while the pointer is simply travelling to something
  // else.
  const scroller = useRef<HTMLDivElement | null>(null);
  // The frame of the animation currently running, so a second click can cancel it. Without
  // this, two clicks in quick succession leave two rAF loops writing `scrollLeft` from
  // different starting points, and the strip lands somewhere neither click asked for — it
  // measurably jumped BACKWARDS under fast clicking.
  const animation = useRef<number | null>(null);
  const [edges, setEdges] = useState<{ left: boolean; right: boolean }>({ left: false, right: false });

  const measure = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    // 2px of slack: sub-pixel widths leave scrollLeft a fraction short of the end, which would
    // otherwise keep the right arrow live forever on a fully-scrolled strip.
    const left = el.scrollLeft > 2;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 2;
    // ONLY WHEN IT CHANGED.
    //
    // This runs on every scroll event, and a scroll animation fires them at frame rate. Setting
    // a fresh object each time re-rendered a component inside a page with 74 app cards sixty
    // times a second, which froze the renderer outright — the strip is small, the tree it sits
    // in is not.
    setEdges((current) =>
      current.left === left && current.right === right ? current : { left, right });
  }, []);

  useEffect(() => {
    measure();
    const el = scroller.current;
    if (!el) return;
    el.addEventListener("scroll", measure, { passive: true });
    // Resizing the window changes what fits, so the arrows have to be re-decided.
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    ro?.observe(el);
    return () => { el.removeEventListener("scroll", measure); ro?.disconnect(); };
  }, [measure, shown.length]);

  const page = useCallback((direction: -1 | 1) => {
    const el = scroller.current;
    if (!el) return;
    // 80% of a screenful, so a project at the boundary stays visible instead of being paged
    // past — losing your place is worse than a shorter jump.
    const distance = direction * el.clientWidth * 0.8;
    const from = el.scrollLeft;
    const to = Math.max(0, Math.min(el.scrollWidth - el.clientWidth, from + distance));

    // ANIMATED BY HAND, because `behavior: "smooth"` IS NOT RELIABLE.
    //
    // Measured in the browser this console is used in: `scrollBy({left: 400, behavior: "auto"})`
    // moved it 300 -> 700, and the identical call with `behavior: "smooth"` moved it nothing at
    // all. The pager looked correctly wired and silently did nothing — the worst kind of broken,
    // because every test of its markup passes.
    const reduced = typeof window !== "undefined"
      && window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    // A HIDDEN TAB HAS NO FRAMES.
    //
    // Chrome stops servicing requestAnimationFrame when a tab is not visible, so an animated
    // scroll started in a background tab simply does not happen. Rare from a person and
    // constant from anything automating the page — it is how this very control tested as
    // "does nothing" while being correctly wired. Jump instead; there is nobody watching the
    // motion anyway.
    const unwatched = typeof document !== "undefined" && document.hidden;
    if (reduced || unwatched || from === to) {
      if (animation.current !== null) window.cancelAnimationFrame(animation.current);
      animation.current = null;
      el.scrollLeft = to;
      // MEASURE BECAUSE WE MOVED IT, not because an event told us.
      //
      // The arrows were derived purely from the `scroll` listener, and a hidden tab defers
      // those — so after paging to the far right the strip still offered "next" and refused
      // "previous", both exactly backwards. We know the position changed; say so.
      measure();
      return;
    }
    if (animation.current !== null) window.cancelAnimationFrame(animation.current);
    const started = performance.now();
    const DURATION = 260;
    const step = (now: number) => {
      const t = Math.min(1, (now - started) / DURATION);
      // ease-out: quick off the mark, settles gently, so the eye can follow which way it went.
      el.scrollLeft = from + (to - from) * (1 - Math.pow(1 - t, 3));
      animation.current = t < 1 ? window.requestAnimationFrame(step) : null;
      if (animation.current === null) measure();   // settled: re-decide the arrows
    };
    animation.current = window.requestAnimationFrame(step);
  }, [measure]);

  useEffect(() => () => {
    if (animation.current !== null) window.cancelAnimationFrame(animation.current);
  }, []);

  const open = useCallback((p: StripProject) => {
    // Asking to SEE a project is not asking to start a new one. This used to copy the whole
    // query string, so a `?new=1` left over from the top-bar button rode along and the form
    // reader re-fired on arrival — the project you clicked was replaced by an empty form.
    const search = searchForProject(window.location.search, p.latest_run_id ?? null);
    window.history.pushState({}, "",
      `${window.location.pathname}${search}${window.location.hash}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
    setExpanded(false);
  }, []);

  if (state === "loading") {
    return (
      <nav className="bx-strip" aria-label={STRIP_LABEL} data-state="loading">
        {notice}
        <span className="bx-strip-label">{STRIP_LABEL}</span>
        <span className="bx-strip-note">Loading…</span>
      </nav>
    );
  }

  if (state === "failed") {
    return (
      <nav className="bx-strip" aria-label={STRIP_LABEL} data-state="failed">
        {notice}
        <span className="bx-strip-label">{STRIP_LABEL}</span>
        {/* The reason, not a shrug. "Could not load" sends the operator to the API to find out
            what this line already knows. */}
        <span className="bx-strip-note bx-strip-error">Could not load software projects — {error}</span>
      </nav>
    );
  }

  if (confirmedEmpty) {
    return notice ? <nav className="bx-strip" aria-label={STRIP_LABEL} data-state="archived">{notice}</nav> : null;
  }

  return (
    <nav className="bx-strip" aria-label={STRIP_LABEL} data-state="ready" data-testid="bx-strip" ref={pickerRef}>
      {notice}
      <button type="button" className="bx-project-trigger" aria-expanded={expanded}
              aria-controls="boss-project-picker" onClick={() => setExpanded((open) => !open)}>
        <span>{STRIP_LABEL}</span><strong>{total}</strong>
        {counts.GATED + projects.filter((p) => p.status === "STUCK").length > 0 ?
          <span className="bx-project-attention">
            {counts.GATED + projects.filter((p) => p.status === "STUCK").length} need attention
          </span> : null}
        <span aria-hidden>{expanded ? "⌃" : "⌄"}</span>
      </button>
      {error ? <span className="bx-strip-note bx-strip-error" role="status">Could not refresh — {error}</span> : null}
      <span className="bx-strip-label">
        {/* WHAT IS MISSING, ON THE LABEL THAT CLAIMS TO LIST THEM.
            Shown only when the strip does not have them all — a count beside every list is
            noise, a count beside a truncated one is the whole point. */}
        {total > projects.length ? (
          <span className="bx-strip-of" data-testid="bx-strip-of">
            {projects.length} of {total}
          </span>
        ) : null}
        {/* Refresh is a state of the label, not a banner. A bar that appears at the top of the
            page every thirty seconds is the thing that was called "very annoying", and it was
            right — the strip is already the place you look to see whether this is current. */}
        {refreshing ? (
          <span className="bx-strip-syncing" data-testid="bx-strip-syncing"
                title="Checking for new software projects">Syncing</span>
        ) : null}
      </span>

      {expanded ? <div className="bx-project-picker" id="boss-project-picker"
                       role="region" aria-label="Find a build" data-testid="bx-project-picker">
      <label className="bx-project-search-label" htmlFor="boss-project-search">Find a build</label>
      <input ref={searchRef} id="boss-project-search" type="search" value={query}
             placeholder="Find a build" onChange={(event) => setQuery(event.target.value)} />
      {filtersShown ? <div className="bx-strip-filters" role="group" aria-label="Filter builds by status">
        {FILTERS.map((f) => (
          <button type="button" key={f} className="bx-strip-filter"
            data-active={filter === f} aria-pressed={filter === f}
            onClick={() => setFilter(f)} data-testid={`bx-strip-filter-${f.toLowerCase()}`}>
            {FILTER_LABELS[f]} <span className="bx-strip-filter-n">{counts[f]}</span>
          </button>
        ))}
      </div> : null}
      <div className="bx-strip-scroll" ref={scroller}>
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
            <span className="bx-strip-stage">{p.status === "DONE" ? "Shipped" : p.stage}</span>
            {p.status !== "DONE" ? <span className="bx-strip-pct">{p.pct}%</span> : null}
            {p.status === "GATED" ? <span className="bx-strip-alert">GATED</span> : null}
          </button>
        ))}
        {shown.length === 0 ? (
          <span className="bx-strip-note">
            {projects.length === 0
              ? "No software projects yet"
              /* Says which filter is hiding them. An empty strip under a filter reads as "there
                 is nothing", and the operator forgets they narrowed it two minutes ago. */
              : query.trim() ? "No projects match your search"
              : `No ${FILTER_LABELS[filter].toLowerCase()} — ${projects.length} projects in total`}
          </span>
        ) : null}
      </div>

      {/* Only when there is somewhere to go. Arrows that are always present, and dead half the
          time, teach an operator to ignore them. */}
      {(edges.left || edges.right) ? (
        <div className="bx-strip-pager" data-testid="bx-strip-pager">
          <button type="button" className="bx-strip-page" onClick={() => page(-1)}
                  disabled={!edges.left} aria-label="Show earlier projects"
                  data-testid="bx-strip-prev">‹</button>
          <button type="button" className="bx-strip-page" onClick={() => page(1)}
                  disabled={!edges.right} aria-label="Show more projects"
                  data-testid="bx-strip-next">›</button>
        </div>
      ) : null}

      {/* The filter LAST in the DOM but pinned right: the projects are the content, and a
          keyboard user should reach them before the control that narrows them. */}
      {total > projects.length ? <p className="bx-project-limited">
        Showing {projects.length} of {total}. Open Factory for the full list.
      </p> : null}
      </div> : null}
    </nav>
  );
}
