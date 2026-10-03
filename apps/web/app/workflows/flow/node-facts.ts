/**
 * The view a node draws, and the names behind its badges.
 *
 * `stepView` (lib/step-view.ts) is the one display rule for a step, used here as is. Before the
 * palette has loaded it is applied to an empty one, so a node still follows the same rule (a
 * title from the step's own words, never a raw id) rather than a second, local one. Badge
 * tooltips list names that StepView only counts, so those are read here.
 */
import type { Palette, SpecStep, StepView } from "../../../lib/workflow-editor-types";
import { stepView } from "../../../lib/step-view";
import type { NodeDetail } from "./step-node";

const asObj = (v: unknown): Record<string, unknown> =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
const strings = (v: unknown): string[] =>
  Array.isArray(v) ? v.map((x) => String(x).trim()).filter(Boolean) : [];

const NO_PALETTE: Palette = {
  capabilities: [], customSteps: [], blocks: [], agents: [], humanRole: "human", riskFloor: 2,
  bindable: { skills: [], mcp_servers: [] },
};

/** The view a node draws, from the palette when it has loaded. */
export function viewOf(step: SpecStep, palette: Palette | null): StepView {
  return stepView(step, palette ?? NO_PALETTE);
}

/** "7d", "2h", "30m": how long a Wait step waits. */
export function formatWait(seconds: unknown): string {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return "";
  if (s % 86400 === 0) return `${s / 86400}d`;
  if (s % 3600 === 0) return `${s / 3600}h`;
  if (s >= 60) return `${Math.round(s / 60)}m`;
  return `${s}s`;
}

/** The risk, skill names, MCP server names and wait a node's badges describe. */
export function nodeDetail(step: SpecStep, palette: Palette | null): NodeDetail {
  const binding = asObj(step.binding);
  const capability = palette?.capabilities.find((c) => c.key === binding.capability);
  const custom = palette?.customSteps.find((c) => c.step_key === binding.custom_step);
  const skillName = new Map((palette?.bindable.skills ?? []).map((s) => [s.id, s.name]));
  const serverName = new Map((palette?.bindable.mcp_servers ?? []).map((s) => [s.id, s.name]));
  return {
    risk: capability?.risk_tier ?? custom?.risk_tier,
    skills: strings(binding.skills).map((s) => skillName.get(s) ?? s),
    mcp: strings(binding.mcp ?? binding.mcp_servers).map((s) => serverName.get(s) ?? s),
    duration: step.kind === "timer" ? formatWait(step.wait_seconds) : undefined,
  };
}
