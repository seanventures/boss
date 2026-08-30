/**
 * Collapse many per-row lookups into one request.
 *
 * The console renders lists of cards, and each card wants a little state of its own. Written
 * the obvious way — a `useEffect` per card calling a Server Action — that is catastrophic
 * here, because a Server Action re-renders the whole route to reply. Measured on the live
 * Factory area, loading it with no interaction at all: 111 server actions, 64.5s of action
 * time, and the page was not quiet for 74 seconds.
 *
 * Cards mount independently and cannot coordinate with each other, so the coordination lives
 * here: asks arriving in the same tick become one call to a route handler, which returns JSON
 * and nothing else.
 *
 * Two invariants everything below is built to keep:
 *
 *   1. NEVER REJECTS. A rejection a caller forgot to catch leaves a card asserting something
 *      false — "not deployed", "no gate" — because a lookup failed. Results carry `error`.
 *   2. SILENCE IS NOT ABSENCE. A key the server did not mention resolves as an error, not as
 *      an empty result. Conflating the two is how a deployed app once displayed as never
 *      deployed with an armed Deploy button beside it.
 */

export type BatchResult = { error?: string };

export type BatcherOptions<T extends BatchResult> = {
  /** Build the request URL for one chunk. `extras` is positional against `keys`. */
  url: (keys: string[], extras: string[]) => string;
  /** Pull one key's value out of the response body, or undefined if it is not there. */
  pick: (body: unknown, key: string) => T | undefined;
  /** How long a resolved answer may be reused. Must stay well under any caller's poll
   *  interval, or polling silently stalls on a cached reply. */
  ttlMs?: number;
  /** Must match the route handler's own cap; larger batches are chunked, never truncated. */
  maxPerRequest?: number;
  /** How long to hold the door open for other cards. One frame: long enough for a list to
   *  finish mounting, short enough that the first card is not visibly waiting. */
  windowMs?: number;
  /** Always settle. A fetch with no timeout can hang forever, and every card in the batch is
   *  waiting on this one. */
  timeoutMs?: number;
};

type Pending<T> = { key: string; extra: string; resolve: (v: T) => void };

export type Batcher<T extends BatchResult> = {
  load: (key: string, extra?: string) => Promise<T>;
  forget: (key: string) => void;
  reset: () => void;
};

export function createBatcher<T extends BatchResult>(opts: BatcherOptions<T>): Batcher<T> {
  const ttlMs = opts.ttlMs ?? 2_500;
  const maxPerRequest = opts.maxPerRequest ?? 60;
  const windowMs = opts.windowMs ?? 16;
  const timeoutMs = opts.timeoutMs ?? 20_000;

  let queue: Pending<T>[] = [];
  let timer: ReturnType<typeof setTimeout> | null = null;
  const cache = new Map<string, { at: number; promise: Promise<T> }>();

  async function send(batch: Pending<T>[]): Promise<void> {
    const url = opts.url(batch.map((b) => b.key), batch.map((b) => b.extra));
    try {
      const res = await Promise.race([
        fetch(url, { cache: "no-store" }),
        new Promise<never>((_, reject) =>
          setTimeout(() => reject(new Error(`lookup timed out after ${timeoutMs}ms`)), timeoutMs),
        ),
      ]);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      for (const b of batch) {
        b.resolve(opts.pick(body, b.key) ?? ({ error: "no answer for this item" } as T));
      }
    } catch (e) {
      const error = String((e as Error)?.message ?? e);
      for (const b of batch) b.resolve({ error } as T);
    }
  }

  function flush(): void {
    const batch = queue;
    queue = [];
    timer = null;
    for (let i = 0; i < batch.length; i += maxPerRequest) {
      void send(batch.slice(i, i + maxPerRequest));
    }
  }

  return {
    load(key: string, extra = ""): Promise<T> {
      const k = (key ?? "").trim();
      if (!k) return Promise.resolve({ error: "missing id" } as T);

      const cacheKey = `${k}|${extra}`;
      const hit = cache.get(cacheKey);
      const now = Date.now();
      if (hit && now - hit.at < ttlMs) return hit.promise;

      const promise = new Promise<T>((resolve) => {
        queue.push({ key: k, extra, resolve });
        if (timer === null) timer = setTimeout(flush, windowMs);
      });
      cache.set(cacheKey, { at: now, promise });
      return promise;
    },

    /** Drop a key's cached answer so the next ask is a real one — used after an action that
     *  changes the thing being read, where a stale reply would show the pre-action world.
     *  Only that key: evicting the whole cache would re-fetch the other thirty-nine cards. */
    forget(key: string): void {
      for (const cacheKey of Array.from(cache.keys())) {
        if (cacheKey.slice(0, cacheKey.lastIndexOf("|")) === key) cache.delete(cacheKey);
      }
    },

    reset(): void {
      cache.clear();
      queue = [];
      if (timer !== null) clearTimeout(timer);
      timer = null;
    },
  };
}
