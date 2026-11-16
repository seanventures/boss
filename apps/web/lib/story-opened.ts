/**
 * Which story lines are newer than the last time this build was opened.
 * No previous stamp marks nothing. The stamp to store is the newest line time.
 */

export type StoryOpenLine = { id: string; at?: string | null };

export type StoryOpenMark = {
  ids: string[];
  stamp: string | null;
  note: string | null;
  count: number;
};

const NOTE = "New since you opened this build.";

function ms(at: string | null | undefined): number | null {
  if (!at) return null;
  const n = Date.parse(at);
  return Number.isFinite(n) ? n : null;
}

/** sessionStorage key. Includes the run id so two builds do not share a stamp. */
export function storyOpenedKey(runId: string): string {
  return `boss.story.opened.${runId}`;
}

/**
 * Lines strictly after `previous`. A missing or unreadable stamp marks nothing.
 * `stamp` is the newest line time, unless `previous` is already newer.
 */
export function sinceOpen(lines: StoryOpenLine[], previous: string | null): StoryOpenMark {
  let newestMs = Number.NEGATIVE_INFINITY;
  let newestAt: string | null = null;
  for (const line of lines) {
    const t = ms(line.at);
    if (t === null || t <= newestMs) continue;
    newestMs = t;
    newestAt = line.at ?? null;
  }
  const prevMs = ms(previous);
  const stamp = prevMs === null ? newestAt : newestAt === null || newestMs <= prevMs ? previous : newestAt;
  if (prevMs === null) return { ids: [], stamp, note: null, count: 0 };
  const ids = lines.filter((line) => {
    const t = ms(line.at);
    return t !== null && t > prevMs;
  }).map((line) => line.id);
  return { ids, stamp, note: ids.length > 0 ? NOTE : null, count: ids.length };
}
