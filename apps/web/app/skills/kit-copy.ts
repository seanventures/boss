/**
 * Skills & MCP's visible words (skills-mcp spec §8), in one place so every surface says the same
 * thing and the copy-budget test can hold them to it. Labels are 3 words or fewer; explanations
 * live in tooltips, one line each.
 */
import { agentIdentity } from "../../lib/agent-identity";
import type { AbilityStatus, ToolPolicy } from "../../lib/kit-types";

export const PILL: Record<AbilityStatus, string> = {
  ready: "Ready", unheld: "No one holds", setup: "Set up", needs_repo: "Needs repo",
  testing: "Testing…", failing: "Failing", review: "Review", off: "Off",
};

export const PILL_TONE: Record<AbilityStatus, "green" | "muted" | "accent" | "red" | "amber" | "faint"> = {
  ready: "green", unheld: "muted", setup: "muted", needs_repo: "muted",
  testing: "accent", failing: "red", review: "amber", off: "faint",
};

export const BUTTON: Record<AbilityStatus, string> = {
  ready: "Test", unheld: "Give to agents", setup: "Connect", needs_repo: "Connect repo ↗",
  testing: "Testing…", failing: "Fix", review: "Review", off: "Turn on",
};

export const TIP = {
  progress: "Abilities set up and passing their test",
  testAll: "Re-run every connection test now",
  applyRecommended: "Give every agent its role's default abilities",
  essentials: "The four abilities a software crew needs first",
  testCrew: "Checks each essential works for at least one agent",
  unheld: "Works, but no agent may use it yet",
  setup: "Needs an account or key first",
  needsRepo: "Connect your GitHub repo in Setup first",
  review: "Unreviewed: every call waits for a person",
  off: "No agent or step can use it",
  builtIn: "Ships with BOSS. Nothing to install.",
  official: "Published by the service's own vendor",
  custom: "Added by your team",
  allow: "Runs without asking",
  ask: "Waits for a person each time",
  offTool: "Never offered to agents",
  locked: "Changes the world · always asks a person",
  askInCode: "Ask tools are skipped in unattended code tasks",
  reads: "Reads only",
  writes: "Changes something recoverable",
  public: "Public or irreversible",
  actsAs: "Whose account the agents use",
  tryIt: "Runs one read-only call through BOSS",
  markReviewed: "You checked this server and vouch for it",
  usedIn: "Workflow steps that use this ability",
  gaps: "Recommended for a role but not held",
  showInternal: "Skills BOSS uses itself; agents can't hold them",
  describe: "Describe a skill; BOSS drafts it",
  dataBoundary: "Where this server may send your data",
  allAbilities: "See and change every agent's abilities",
  studioSetUp: "Open this ability in Skills & MCP",
};

export const PROVENANCE: Record<string, { label: string; tip: string }> = {
  built_in: { label: "Built in", tip: TIP.builtIn },
  official: { label: "Official", tip: TIP.official },
  custom: { label: "Custom", tip: TIP.custom },
};

export const GROUP_LABEL: Record<string, string> = {
  code: "Code", research: "Research", coordinate: "Coordinate", ship: "Ship", media: "Media",
};

export const POLICY_LABEL: Record<ToolPolicy, string> = { allow: "Allow", ask: "Ask", off: "Off" };
export const POLICY_TIP: Record<ToolPolicy, string> = { allow: TIP.allow, ask: TIP.ask, off: TIP.offTool };

/** The risk words for a tier: never the number on screen. */
export function riskOf(tier: number): { label: "Reads" | "Writes" | "Public"; tip: string; tone: string } {
  if (tier <= 1) return { label: "Reads", tip: TIP.reads, tone: "green" };
  if (tier === 2) return { label: "Writes", tip: TIP.writes, tone: "amber" };
  return { label: "Public", tip: TIP.public, tone: "red" };
}

/** At most six words, for a red line on a card; the whole message goes in its tooltip. */
export function shortError(message: string | null | undefined): string {
  const said = String(message ?? "").trim();
  const low = said.toLowerCase();
  if (/(401|403|bad credentials|unauthori[sz]ed|forbidden|invalid api key|token rejected|invalid_auth)/.test(low)) return "Token rejected";
  if (/(connecterror|connection refused|unreachable|timed out|can't reach|no answer|name or service|nodename|gaierror|urlopen error)/.test(low)) return "Can't reach server";
  const words = said.split(/\s+/).filter(Boolean);
  return words.length ? words.slice(0, 6).join(" ") : "Test failed";
}

/** What the console calls an agent: its callsign (Juno), else its role's name. */
export function agentName(roleKey: string): string {
  const id = agentIdentity(roleKey);
  return id.callsign || id.name;
}
