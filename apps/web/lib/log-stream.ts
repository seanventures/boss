"use client";

/**
 * The one client stream engine for every log-shaped list (activity-govern spec §4.3): the
 * Activity log (5s), Agent Studio's river (8s) and a run step's panel (5s).
 *
 *   - cursor paging (`Load older`), and an `after=` tail with ETag/304 for what is new;
 *   - NOTHING MOVES UNDER THE READER: while the reader holds (scrolled away from the top, or a
 *     row open) new rows wait behind `n new ↑`; at the top they are inserted and marked fresh;
 *   - no poll while the console is Paused or the tab is hidden; a new query aborts the old one.
 *
 * Each screen keeps its own rows, filters and words; only the loop is shared.
 */
import { useOnScreen } from "./area-shown";
import { useCallback, useEffect, useRef, useState } from "react";

export type StreamPage<T> = { items: T[]; next: string | null; newest: string | null; etag?: string | null };
export type StreamNewer<T> = { items: T[]; newest: string | null; etag: string | null; notModified?: boolean; gap?: boolean };
type Failed = { error: string };

const failed = (x: unknown): x is Failed => Boolean(x) && typeof (x as Failed).error === "string";

/** Whether the tab is showing AND this panel's area is on screen (lib/area-shown.ts). A hidden
 *  tab, or an area nobody is looking at, polls nothing. */
export function useVisible(): boolean {
  return useOnScreen();
}

export type LogStream<T> = {
  items: T[] | null;
  error: string;
  /** New rows waiting behind `n new ↑`. */
  fresh: T[];
  /** Ids inserted live at the top, for their one-time arrival fade. */
  arrived: ReadonlySet<string>;
  next: string | null;
  loadingOlder: boolean;
  olderError: string;
  loadOlder: () => void;
  showFresh: () => void;
  reload: () => void;
};

export function useLogStream<T>({ key, idOf, first, older, newer, pollMs, paused, holding, enabled = true }: {
  key: string;
  idOf: (item: T) => string;
  first: (signal: AbortSignal) => Promise<StreamPage<T> | Failed>;
  older?: (cursor: string, signal: AbortSignal) => Promise<StreamPage<T> | Failed>;
  newer?: (newest: string | null, etag: string | null, signal: AbortSignal) => Promise<StreamNewer<T> | Failed>;
  pollMs: number;
  paused: boolean;
  holding: boolean;
  enabled?: boolean;
}): LogStream<T> {
  const [items, setItems] = useState<T[] | null>(null);
  const [error, setError] = useState("");
  const [fresh, setFreshState] = useState<T[]>([]);
  const freshRef = useRef<T[]>([]);
  const setFresh = useCallback((next: T[]) => { freshRef.current = next; setFreshState(next); }, []);
  const [arrived, setArrived] = useState<ReadonlySet<string>>(new Set());
  const [next, setNext] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState("");
  const [generation, setGeneration] = useState(0);
  const seen = useRef<Set<string>>(new Set());
  const newest = useRef<string | null>(null);
  const etag = useRef<string | null>(null);
  const holdingRef = useRef(holding);
  holdingRef.current = holding;
  const visible = useVisible();
  // The latest callbacks, so a poll never closes over a stale query.
  const fns = useRef({ first, older, newer, idOf });
  fns.current = { first, older, newer, idOf };

  useEffect(() => {
    if (!enabled) return undefined;
    const ctl = new AbortController();
    setItems(null); setFresh([]); setNext(null); setError(""); setOlderError(""); setArrived(new Set());
    seen.current = new Set(); newest.current = null; etag.current = null;
    void fns.current.first(ctl.signal).then((page) => {
      if (ctl.signal.aborted) return;
      if (failed(page)) { if (page.error !== "aborted") { setError(page.error); setItems([]); } return; }
      seen.current = new Set(page.items.map(fns.current.idOf));
      newest.current = page.newest;
      etag.current = page.etag ?? null;
      setItems(page.items);
      setNext(page.next);
    });
    return () => ctl.abort();
  }, [key, enabled, generation, setFresh]);

  useEffect(() => {
    if (!enabled || paused || !visible || items === null || !fns.current.newer) return undefined;
    let ctl: AbortController | null = null;
    const tick = async () => {
      ctl?.abort();
      ctl = new AbortController();
      const got = await fns.current.newer!(newest.current, etag.current, ctl.signal);
      if (failed(got) || ctl.signal.aborted) return;
      etag.current = got.etag ?? etag.current;
      if (got.notModified) return;
      if (got.gap) { setGeneration((g) => g + 1); return; }
      const unseen = got.items.filter((i) => !seen.current.has(fns.current.idOf(i)));
      if (got.newest) newest.current = got.newest;
      if (!unseen.length) return;
      for (const u of unseen) seen.current.add(fns.current.idOf(u));
      if (holdingRef.current) {
        setFresh([...unseen, ...freshRef.current]);
      } else {
        setItems((cur) => [...unseen, ...(cur ?? [])]);
        setArrived(new Set(unseen.map(fns.current.idOf)));
      }
    };
    const timer = setInterval(() => void tick(), pollMs);
    return () => { clearInterval(timer); ctl?.abort(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed by the query, not by rows
  }, [key, enabled, paused, visible, items === null, pollMs, setFresh]);

  const showFresh = useCallback(() => {
    const waiting = freshRef.current;
    if (!waiting.length) return;
    setItems((cur) => [...waiting, ...(cur ?? [])]);
    setArrived(new Set(waiting.map(fns.current.idOf)));
    setFresh([]);
  }, [setFresh]);

  const loadOlder = useCallback(() => {
    if (!next || !fns.current.older || loadingOlder) return;
    setLoadingOlder(true);
    setOlderError("");
    const ctl = new AbortController();
    void fns.current.older(next, ctl.signal).then((page) => {
      setLoadingOlder(false);
      if (failed(page)) { setOlderError(page.error); return; }
      const add = page.items.filter((i) => !seen.current.has(fns.current.idOf(i)));
      for (const a of add) seen.current.add(fns.current.idOf(a));
      setItems((cur) => [...(cur ?? []), ...add]);
      setNext(page.next);
    });
  }, [next, loadingOlder]);

  const reload = useCallback(() => setGeneration((g) => g + 1), []);

  return { items, error, fresh, arrived, next, loadingOlder, olderError, loadOlder, showFresh, reload };
}

/**
 * One read, re-asked every `pollMs` with its ETag (a 304 changes nothing): the step panel's
 * summary, the pulse, Govern's board. Stops while Paused or hidden; a new key aborts the old.
 */
export function usePolledRead<T>({ key, read, pollMs, paused, enabled = true }: {
  key: string;
  read: (etag: string | null, signal: AbortSignal) => Promise<
    { ok: true; data?: T; notModified?: boolean; etag: string | null } | { ok: false; error: string; status?: number }>;
  pollMs: number | null;
  paused: boolean;
  enabled?: boolean;
}): { data: T | null; error: string; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [generation, setGeneration] = useState(0);
  const etag = useRef<string | null>(null);
  const readRef = useRef(read);
  readRef.current = read;
  const visible = useVisible();

  useEffect(() => {
    if (!enabled) return undefined;
    const ctl = new AbortController();
    etag.current = null;
    setLoading(true);
    void readRef.current(null, ctl.signal).then((got) => {
      if (ctl.signal.aborted) return;
      setLoading(false);
      if (!got.ok) { if (got.error !== "aborted") setError(got.error); return; }
      setError("");
      etag.current = got.etag;
      if (got.data !== undefined) setData(got.data);
    });
    return () => ctl.abort();
  }, [key, enabled, generation]);

  useEffect(() => {
    if (!enabled || paused || !visible || !pollMs) return undefined;
    let ctl: AbortController | null = null;
    const timer = setInterval(() => {
      ctl?.abort();
      ctl = new AbortController();
      const mine = ctl;
      void readRef.current(etag.current, mine.signal).then((got) => {
        if (mine.signal.aborted || !got.ok) return;
        etag.current = got.etag ?? etag.current;
        if (got.notModified || got.data === undefined) return;
        setError("");
        setData(got.data);
      });
    }, pollMs);
    return () => { clearInterval(timer); ctl?.abort(); };
  }, [key, enabled, paused, visible, pollMs]);

  return { data, error, loading, reload: useCallback(() => setGeneration((g) => g + 1), []) };
}
