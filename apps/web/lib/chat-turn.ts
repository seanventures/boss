/**
 * A chat turn the console can lose sight of and find again (services/chat_turns.py).
 *
 * The API commits the operator's message and a turn record (`turn_id`) before any model is
 * called. A background send answers 202 `{thread_id, turn_id, status: "running"}` at once; the
 * console polls GET /v1/agent/turns/{turn_id} until the turn is `done` (its response, exactly
 * what the old synchronous call returned) or `failed` (a sentence). A console that reloaded
 * mid-turn reads the thread, finds `turn: {turn_id, status: "running"}` on it, and resumes
 * waiting instead of showing a question with no answer coming.
 *
 * Pure functions, so the resume logic is unit-tested without a browser.
 */

export type TurnRead = {
  turn_id?: string;
  thread_id?: string;
  status?: string;
  error?: string | null;
  result?: Record<string, unknown> | null;
  failure?: { sentence?: string; technical?: string } | null;
};

export type TurnOutcome =
  | { ok: true; data: Record<string, unknown> }
  | { ok: false; error: string; data?: { technical?: string } };

const LIVE = new Set(["queued", "running"]);

/** The turn a background send started, when the send answered "running" rather than a reply. */
export function startedTurnOf(data: unknown): { turnId: string; threadId: string | null } | null {
  if (!data || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  if (typeof d.turn_id !== "string" || !d.turn_id) return null;
  if (typeof d.reply === "string") return null;              // it already answered
  if (!LIVE.has(String(d.status ?? ""))) return null;
  return { turnId: d.turn_id, threadId: typeof d.thread_id === "string" ? d.thread_id : null };
}

/** The turn still running on a thread the console just read (GET …/messages `turn`), with the
 *  question it is answering — what the landing watch waits on. Null when nothing is in flight. */
export function resumeFrom(data: unknown): { turnId: string; waitingFor: string } | null {
  if (!data || typeof data !== "object") return null;
  const d = data as { turn?: { turn_id?: unknown; status?: unknown } | null;
                      messages?: Array<{ role?: string; content?: string | null }> };
  const turn = d.turn;
  if (!turn || typeof turn.turn_id !== "string" || !turn.turn_id) return null;
  if (!LIVE.has(String(turn.status ?? ""))) return null;
  const messages = Array.isArray(d.messages) ? d.messages : [];
  let waitingFor = "";
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.role === "user") { waitingFor = String(messages[i].content ?? ""); break; }
  }
  return { turnId: turn.turn_id, waitingFor };
}

/** What one read of a turn means: still going (null), or how it ended. */
export function outcomeOf(read: TurnRead | null): TurnOutcome | null {
  if (!read || LIVE.has(String(read.status ?? ""))) return null;
  if (read.status === "done" && read.result && typeof read.result === "object") {
    return { ok: true, data: read.result };
  }
  if (read.status === "failed") {
    return { ok: false, error: read.failure?.sentence || read.error || "That turn did not finish.",
             data: read.failure?.technical ? { technical: read.failure.technical } : undefined };
  }
  return null;
}

/**
 * Wait for a turn to end, reading it every `intervalMs`. A read that fails (offline, a proxy
 * hiccup) is not an outcome — the turn is still running server-side — so it keeps reading.
 * `shouldStop` lets the caller give up (the panel closed); then the promise resolves null.
 */
export async function awaitTurn(
  readTurn: (turnId: string) => Promise<TurnRead | null>,
  turnId: string,
  { intervalMs = 1500, sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms)),
    shouldStop = () => false }: {
    intervalMs?: number; sleep?: (ms: number) => Promise<void>; shouldStop?: () => boolean;
  } = {},
): Promise<TurnOutcome | null> {
  for (;;) {
    if (shouldStop()) return null;
    const ended = outcomeOf(await readTurn(turnId));
    if (ended) return ended;
    await sleep(intervalMs);
  }
}
