/**
 * What the operator asked for, without the prompt scaffolding a workflow's panel wraps it in.
 *
 * request_meeting puts "--- WHAT THIS PANEL IS FOR --- You are the panel inside BOSS's …
 * workflow" in front of the ask so every seat reads it first. The task was titled from the
 * first 90 characters, so on boss-1 the title read "PRD meeting: --- WHAT THIS PANEL IS FOR ---
 * You are the pane…", and the Factory's panel cards (leadLine cut at the first ". ", giving the
 * boilerplate sentence) and the Approvals evidence chip showed that. The engine titles new tasks
 * by the ask now (meeting_service.directive_subject); this cleans rows already written.
 *
 * The markers mirror engine/boss_engine/services/meeting_service.py; a test compares them.
 */
export const PANEL_PURPOSE_MARK = "--- WHAT THIS PANEL IS FOR ---";
export const PANEL_ASK_MARK = "--- WHAT YOU ARE BEING ASKED ABOUT ---";
export const PANEL_RESEARCH_MARK = "--- RESEARCH ALREADY DONE FOR THIS ---";

export function directiveSubject(raw: string | null | undefined): string {
  let text = String(raw ?? "");
  const prefix = /^\s*PRD meeting:\s*/.exec(text)?.[0] ?? "";
  text = text.slice(prefix.length);
  const ask = text.indexOf(PANEL_ASK_MARK);
  if (ask >= 0) {
    text = text.slice(ask + PANEL_ASK_MARK.length);
  } else if (text.trimStart().startsWith(PANEL_PURPOSE_MARK)) {
    // A title cut at 90 characters ends inside the purpose block: the ask is not in it at all.
    text = "";
  }
  const research = text.indexOf(PANEL_RESEARCH_MARK);
  if (research >= 0) text = text.slice(0, research);
  return text.replace(/\s+/g, " ").trim();
}

/** A title for display: the subject, or — when a truncated title held only scaffolding — a
 *  plain fallback instead of the scaffolding. */
export function displayTitle(raw: string | null | undefined, fallback = "Untitled panel"): string {
  const subject = directiveSubject(raw);
  if (subject) return subject;
  const text = String(raw ?? "").trim();
  return text.includes(PANEL_PURPOSE_MARK) || !text ? fallback : text;
}
