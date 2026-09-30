/**
 * Has THIS turn's answer landed in the thread?
 *
 * A chat turn writes the operator's line and the reply in one transaction. While
 * it is still running the messages endpoint still ends on the previous
 * assistant bubble. Treating that leftover as arrival drops the new question
 * from the dock and paints the old paragraph as the new answer on the
 * Command Center.
 */
export function landingAnswerArrived(
  serverMessages: Array<{ role?: string; content?: string | null }>,
  sentText: string,
): boolean {
  const sent = sentText.trim();
  if (!sent) return false;
  // The operator may send the same text twice. Only the newest matching turn
  // can satisfy the answer currently being awaited.
  let idx = -1;
  for (let i = 0; i < serverMessages.length; i += 1) {
    const message = serverMessages[i];
    if (message.role === "user" && (message.content ?? "").includes(sent)) idx = i;
  }
  if (idx < 0) return false;
  return serverMessages.slice(idx + 1).some((m) => m.role === "assistant");
}
