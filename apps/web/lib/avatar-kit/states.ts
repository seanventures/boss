/**
 * What an avatar is doing, and where that comes from.
 *
 * Motion carries state (spec §2.5, §7): an avatar animates only for a real status, and every
 * state also has a glyph and a word so it is never colour or movement alone. These mappers are
 * the ONLY way a surface turns a status into a state, so the same act reads the same on the
 * roster, the run lane and the dock.
 */
import type { ActKind, ActStatus, AvatarState, Lifecycle, Presence } from "../agent-world-types";

export const AVATAR_STATES: readonly AvatarState[] =
  ["idle", "thinking", "speaking", "working", "needs_you", "done", "stuck", "paused", "retired", "failed", "stopped"];

/** The word for each state (copy guide §9). */
export const STATE_WORDS: Record<AvatarState, string> = {
  idle: "Idle", thinking: "Thinking", speaking: "Speaking", working: "Working", needs_you: "Needs you",
  done: "Done", stuck: "Stuck", paused: "Paused", retired: "Retired", failed: "Failed", stopped: "Stopped",
};

/** States that loop. Everything else is still or plays once. Idle loops only on the Passport hero. */
export const LIVE_STATES: ReadonlySet<AvatarState> = new Set(["thinking", "speaking", "working", "needs_you"]);

/** An act's status, as a face. `streaming` = tokens are arriving for this act right now. */
export function stateFromAct(status: ActStatus | string, o: { kind?: ActKind | string; streaming?: boolean; tokens?: boolean } = {}): AvatarState {
  switch (status) {
    case "awaiting_input": return "needs_you";
    case "active":
      if (o.streaming) return "speaking";
      // A chat turn with nothing back yet is thinking; any other open act is working.
      if (o.kind === "chat" && !o.tokens) return "thinking";
      return "working";
    case "complete": return "done";
    // An act that errored FAILED; one closed stale was left open when its run stopped. Neither
    // is "stuck", which is live work that has gone quiet.
    case "error": return "failed";
    case "stale": return "stopped";
    default: return "idle"; // pending, skipped
  }
}

/** Presence from the API (§4.5). `in_panel` is a live seat: working, not speaking, until its tokens arrive. */
export function stateFromPresence(p: Pick<Presence, "state"> | { state: string } | null | undefined): AvatarState {
  const s = p?.state as string | undefined;
  if (s === "in_panel") return "working";
  return (AVATAR_STATES as readonly string[]).includes(s ?? "") ? (s as AvatarState) : "idle";
}

/** A workflow step's phase on the run lane (StepNode's RunPhase). */
export function stateFromRunPhase(phase: string | undefined): AvatarState {
  switch (phase) {
    case "moving": return "working";
    case "needs_you": return "needs_you";
    case "done": return "done";
    case "failed": return "failed";
    default: return "idle"; // future, skipped
  }
}

/** A raw step / task status string, for surfaces that have not got an act yet. */
export function stateFromStatus(status: string | null | undefined): AvatarState {
  const s = (status ?? "").toLowerCase();
  if (/^(awaiting|waiting|needs|gated|pending_approval|blocked_on_human)/.test(s) || s === "at_gate") return "needs_you";
  if (/^(running|in_progress|active|started|building|reviewing|executing)$/.test(s)) return "working";
  if (/^(completed?|succeeded|success|done|passed|approved|merged)$/.test(s)) return "done";
  if (/^(failed|failure|error|errored|timed_out|rejected)$/.test(s)) return "failed";
  if (/^(stale|stopped|cancelled|canceled)$/.test(s)) return "stopped";
  if (/^(stuck|blocked)$/.test(s)) return "stuck";
  return "idle";
}

/** A dock / room turn: thinking while in flight, speaking while text arrives. */
export function stateFromTurn(t: { inFlight: boolean; streaming: boolean; error?: boolean }): AvatarState {
  if (t.error) return "failed";
  if (t.streaming) return "speaking";
  return t.inFlight ? "thinking" : "idle";
}

/** Lifecycle wins over activity: a paused agent shows paused even with an old act still open. */
export function withLifecycle(state: AvatarState, lifecycle: Lifecycle | undefined): AvatarState {
  if (lifecycle === "retired") return "retired";
  if (lifecycle === "paused") return "paused";
  return state;
}
