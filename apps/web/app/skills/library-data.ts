/**
 * The Library's reads: authored skills, the skills BOSS ships, who holds which, MCP servers.
 * The skill shapes and the two helpers below moved here verbatim from skills-ui.tsx (spec S-library).
 */
import { agentIdentity } from "../../lib/agent-identity";
import type { AbilityStatus } from "../../lib/kit-types";

export type Skill = {
  skill_key: string; label: string; summary: string; behaviour_kind: string;
  knowledge: string; inputs: Array<{ name: string; required?: boolean }>;
  produces: Array<{ name: string }>; config: Record<string, unknown>;
  risk_tier: number; trusted: boolean; enabled: boolean; requires_approval: boolean;
  references?: { custom_steps: Array<{ step_key: string; label?: string }>; agents: string[];
                 workflows?: Array<{ workflow_key: string; name?: string }> };
  catalogue?: { risk_tier: number; usable: boolean; reason?: string | null; runs_through?: string;
                requires_person?: string | null };
};

export type BuiltIn = { id: string; kind?: string; source?: string; risk_tier?: number;
                        enabled?: boolean; description?: string; system?: boolean;
                        /** From the skill catalogue: whether it can run here, and why not. */
                        usable?: boolean; reason?: string | null;
                        permissions?: { allowed_callers?: string[] } };

export const BLANK = {
  skill_key: "", label: "", summary: "", behaviour_kind: "sandboxed_command",
  knowledge: "", inputs: "", produces: "",
  image: "", command: "", timeout_seconds: "300", network: false,
  mcp_server_id: "", tool_name: "", executor_id: "",
  language: "python", source: "", program_timeout: "60",
  risk_tier: "1", trusted: false, enabled: true,
};

export type Draft = typeof BLANK;

/** An existing skill, as the form's fields — what Edit loads. */
export function draftFromSkill(s: Skill): Draft {
  const c = (s.config ?? {}) as Record<string, unknown>;
  const lines = (xs: Array<{ name: string }> | undefined) => (xs ?? []).map((x) => x.name).join("\n");
  return {
    ...BLANK,
    skill_key: s.skill_key, label: s.label, summary: s.summary ?? "",
    behaviour_kind: s.behaviour_kind, knowledge: s.knowledge ?? "",
    inputs: lines(s.inputs), produces: lines(s.produces),
    image: String(c.image ?? ""), command: String(c.command ?? ""),
    timeout_seconds: String(c.timeout_seconds ?? "300"), network: Boolean(c.network),
    mcp_server_id: String(c.mcp_server_id ?? ""), tool_name: String(c.tool_name ?? ""),
    executor_id: String(c.executor_id ?? ""),
    language: String(c.language ?? "python"), source: String(c.source ?? ""),
    program_timeout: String(c.timeout_seconds ?? "60"),
    risk_tier: String(s.risk_tier), trusted: Boolean(s.trusted), enabled: s.enabled !== false,
  };
}

/** What deleting would do, said before the press. A step that runs the skill blocks it; agents
 *  that hold it lose it, which the operator confirms. */
export function deleteConsequence(s: Skill): { blocked: string | null; confirm: string } {
  const steps = s.references?.custom_steps ?? [];
  const agents = s.references?.agents ?? [];
  const flows = s.references?.workflows ?? [];
  if (steps.length > 0) {
    return {
      blocked: `${s.skill_key} is run by ${steps.map((x) => x.label || x.step_key).join(", ")}. `
        + "Delete or re-point those workflow steps first — or turn the skill off instead.",
      confirm: "",
    };
  }
  if (flows.length > 0) {
    return {
      blocked: `${s.skill_key} is a step in ${flows.map((w) => w.name || w.workflow_key)
        .join(", ")}. Remove it from those workflows first — or turn the skill off instead.`,
      confirm: "",
    };
  }
  return {
    blocked: null,
    confirm: agents.length > 0
      ? `Delete ${s.skill_key}? ${agents.map(roleLabel).join(", ")} will no longer hold it. `
        + "This cannot be undone."
      : `Delete ${s.skill_key}? This cannot be undone.`,
  };
}

export function roleLabel(roleKey: string): string {
  return agentIdentity(roleKey).name;
}

/** The one status vocabulary, for a skill row. */
export function skillStatus(enabled: boolean, usable: boolean, needsReview: boolean, holders: number): AbilityStatus {
  if (!usable) return "failing";
  if (needsReview) return "review";
  if (!enabled) return "off";
  if (holders === 0) return "unheld";
  return "ready";
}
