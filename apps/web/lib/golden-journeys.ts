import { apiWrite } from "./api-write";

/**
 * Govern › Journeys (BOSSAPP-1284/1285): the preview's golden journeys, today's result per
 * journey and host, and the consecutive green days the feature freeze waits for (5).
 * Reads GET /v1/golden-journeys; "Run now" is POST /v1/golden-journeys/run (assurance:run).
 */
export type JourneyStatus = "green" | "red" | "pending" | "not_run";

export type JourneyToday = {
  status: JourneyStatus; why?: string; verdict?: string | null; seconds?: number; runs?: number;
  at?: string | null;
};

export type Journey = {
  id: string; title: string; ticket: string; modes: string[];
  pending: { waits_for: string; why: string } | null;
  today: Record<string, JourneyToday>;
};

export type JourneysView = {
  today: string; hosts: string[]; green_days: number; needed: number; freeze_lifted: boolean;
  journeys: Journey[]; pending: { id: string; waits_for: string; why: string }[];
  history: { day: string; green: boolean; short: string[] }[];
  schedule: { at_time: string; timezone: string; next_wake_at: string | null } | null;
  running: boolean;
};

export const STATUS_WORDS: Record<JourneyStatus, string> = {
  green: "Green", red: "Red", pending: "Pending", not_run: "Not run",
};

/** The counter in a few words: "3 of 5 green days", "Freeze lifted". */
export function counterWords(v: Pick<JourneysView, "green_days" | "needed" | "freeze_lifted">): string {
  if (v.freeze_lifted) return "Freeze lifted";
  return `${v.green_days} of ${v.needed} green day${v.needed === 1 ? "" : "s"}`;
}

/** Why today is not green yet, shortest first: pending journeys, then reds, then not run. */
export function todayShort(v: JourneysView): string {
  const rank: Record<JourneyStatus, number> = { pending: 0, red: 1, not_run: 2, green: 3 };
  const bad = v.journeys.flatMap((j) => Object.values(j.today).map((t) => ({ id: j.id, s: t.status })))
    .filter((x) => x.s !== "green");
  if (!bad.length) return "Every journey green today";
  const count = (s: JourneyStatus) => new Set(bad.filter((x) => x.s === s).map((x) => x.id)).size;
  return (["pending", "red", "not_run"] as JourneyStatus[])
    .sort((a, b) => rank[a] - rank[b])
    .map((s) => (count(s) ? `${count(s)} ${STATUS_WORDS[s].toLowerCase()}` : ""))
    .filter(Boolean).join(" · ");
}

export function journeysPath(projectId: string): string {
  return `/v1/golden-journeys?project_id=${encodeURIComponent(projectId)}`;
}

/** For usePolledRead. */
export function polledJourneys(projectId: string) {
  return async (_etag: string | null, signal: AbortSignal) => {
    try {
      const r = await fetch(journeysPath(projectId), { cache: "no-store", signal });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        return { ok: false as const, status: r.status,
                 error: String((body as { message?: string }).message ?? `BOSS answered ${r.status}`) };
      }
      return { ok: true as const, data: body as JourneysView, etag: null };
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return { ok: false as const, error: "aborted" };
      return { ok: false as const, error: "Can't reach BOSS" };
    }
  };
}

export type RunResult = { ok: true; started: string[]; note?: string } | { ok: false; error: string };

/** Start today's run now (all journeys). One press, one run (lib/api-write.ts). */
export async function runJourneysNow(projectId: string): Promise<RunResult> {
  try {
    const r = await apiWrite("/v1/golden-journeys/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId }),
    }, { press: { scope: "golden-journeys", label: "Run now" } });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: String((body as { message?: string }).message ?? `BOSS answered ${r.status}`) };
    return { ok: true, started: (body as { started?: string[] }).started ?? [], note: (body as { note?: string }).note };
  } catch {
    return { ok: false, error: "Can't reach BOSS" };
  }
}
