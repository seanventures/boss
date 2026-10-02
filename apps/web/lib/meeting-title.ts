/**
 * A meeting's title as a card or a picker shows it: its subject, on one line.
 *
 * The API keeps workflow framing out of titles (engine meeting_service.meeting_subject); before
 * it did, every workflow-convened meeting on boss-1 read "PRD meeting: --- WHAT THIS PANEL IS
 * FOR ---" and the cards and the consensus picker were four identical rows. This only drops the
 * shared "PRD meeting:" prefix that every card would otherwise repeat, so the Meetings cards and
 * the consensus picker read the same words.
 */
export function meetingCardTitle(raw: string | null | undefined): string {
  return (raw || "").replace(/^PRD meeting:\s*/, "").replace(/\s+/g, " ").trim() || "Untitled meeting";
}
