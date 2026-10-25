/** Who reads code for a business-rules scan, named as BOSS's agent is — never a placeholder.
 *
 *  The scan step's captain is the Chief Architect (engine workflow_catalogue
 *  DEFAULT_STEP_AGENTS "scan_business_rules" → "chief_architect"; a test pins the two). The
 *  console said "Scout reads it", a name no BOSS agent has (brec_aeaa99f4fedf4823a4e4). */
import { roleWords } from "../app/agents/chain-model";

export const SCAN_CAPTAIN_ROLE = "chief_architect";

/** "the Chief Architect" — the agent the scan runs as, by its role. */
export function scanReader(): string {
  return `the ${roleWords(SCAN_CAPTAIN_ROLE)}`;
}

/** The same words at the start of a sentence ("The Chief Architect reads …"). */
export function sentenceStart(words: string): string {
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : words;
}
