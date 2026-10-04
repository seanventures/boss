/**
 * The doors into a conversation: open a 1:1 talk, a plan, a room, or the picker. Each one only
 * dispatches an event the ConverseHost (app/agents/converse-host.tsx, mounted once by the
 * shell) hears, so this module has no React and no fetch: any surface may import it, a server
 * route included, without pulling the conversation client in with it.
 */
import type { ConversationContext } from "./agent-world-types";

/** What a door asks the converse host to open. Mounted once by the console shell. */
export type ConverseOpening =
  | { kind: "talk" | "plan"; roleKey?: string; context?: ConversationContext; fresh?: boolean;
      /** A question to start from, left in the composer for the operator to send or edit. */
      draft?: string }
  | { kind: "room"; seats?: string[]; preset?: string; question?: string; context?: ConversationContext }
  | { kind: "resume"; conversationId: string }
  | { kind: "pick"; mode: "talk" | "plan" | "room" };

export const CONVERSE_EVENT = "boss:converse";

function dispatch(detail: ConverseOpening): void {
  try { window.dispatchEvent(new CustomEvent(CONVERSE_EVENT, { detail })); } catch { /* no window */ }
}

/** Talk to one agent, 1:1, from its Passport, its chip, a run step or the palette. Resumes the
 *  last open talk with it unless `fresh`. */
export function openTalk(roleKey: string, context?: ConversationContext,
                         o: { fresh?: boolean; draft?: string } = {}): void {
  dispatch({ kind: "talk", roleKey, context, fresh: o.fresh, draft: o.draft });
}

/** "Plan it with <agent>": scope a piece of work, end in a card one press starts. Default: the
 *  Product Manager. */
export function openPlan(roleKey = "pm", context?: ConversationContext): void {
  dispatch({ kind: "plan", roleKey, context, fresh: true });
}

/** The room. With seats (or a preset) it opens straight away; without, it asks who to seat. */
export function openRoom(o: { seats?: string[]; preset?: string; question?: string; context?: ConversationContext } = {}): void {
  dispatch({ kind: "room", ...o });
}

export function openConversation(conversationId: string): void {
  dispatch({ kind: "resume", conversationId });
}

/** Pick who to talk to first (the palette's doors). */
export function openPicker(mode: "talk" | "plan" | "room"): void {
  dispatch({ kind: "pick", mode });
}

/** The palette's converse commands. Labels obey the copy budget. */
export const CONVERSE_COMMANDS: { key: string; label: string; sub: string; run: () => void }[] = [
  { key: "talk", label: "Talk to an agent", sub: "One agent, 1:1", run: () => openPicker("talk") },
  { key: "plan", label: "Plan it with an agent", sub: "Discuss, then start", run: () => openPicker("plan") },
  { key: "room", label: "Open a room", sub: "2-5 agents at once", run: () => openPicker("room") },
];
