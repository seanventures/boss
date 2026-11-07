/** THE ONE NAMING of the autonomy levels (BOSSAPP-913 13-20), the engine's task_classes.LEVEL_NAMES:
 *  Project › Settings, Govern › Autonomy, badges and tooltips all say these words (the wireframe's
 *  A0 Ask · A1 Propose · A2 Act · A3 Ship). What a level allows is LEVEL_MEANINGS, said beside the
 *  name, never a second name. Enforced by tests/px-autonomy-one-naming.test.ts. */
export const LEVEL_WORDS: Record<string, string> = { A0: "Ask", A1: "Propose", A2: "Act", A3: "Ship" };
export const LEVEL_MEANINGS: Record<string, string> = {
  A0: "Watches and asks; changes nothing", A1: "Proposes the change; a person decides",
  A2: "Acts up to a draft (a branch or draft PR); a person approves", A3: "Acts and ships on its own, then reports",
};
export const levelLabel = (key: string): string => (LEVEL_WORDS[key] ? `${key} ${LEVEL_WORDS[key]}` : key);
