/**
 * A run, as the shared lane draws it.
 *
 * The Runs view used to have its own rail, which wrapped onto a second row and named steps
 * differently from the designer. It now draws the same FlowLane in run mode, so a step looks
 * the same in the editor, on a library card and on a running workflow. This turns what a run
 * reports (its steps, in order) into the spec and palette the lane reads.
 */
import type { Capability, Palette, Spec } from "../../../lib/workflow-editor-types";

export type RunLaneStep = {
  step_id: string; kind: string; capability: string | null; captain?: string | null;
  /** The step's own title, when the run reports one. */
  title?: string | null;
};

/** The run's steps as a linear spec, in the order the run reports them. */
export function runSpec(steps: RunLaneStep[]): Spec {
  return {
    entry_step: steps[0]?.step_id,
    steps: steps.map((s, i) => ({
      id: s.step_id,
      kind: s.kind || "capability",
      ...(s.title ? { title: s.title } : {}),
      ...(s.captain ? { captain: s.captain } : {}),
      binding: s.capability ? { capability: s.capability } : {},
      ...(i + 1 < steps.length ? { next: [steps[i + 1].step_id] } : {}),
    })),
  };
}

/** Enough of a palette to name each step and its agent: the built-ins and the roster. */
export function runPalette(capabilities: Capability[],
                           roles: { role_key: string; display_name?: string | null }[]): Palette {
  return {
    capabilities,
    customSteps: [],
    blocks: [],
    agents: roles.map((r) => ({ role_key: r.role_key, name: r.display_name || r.role_key, skills: [], mcp: [] })),
    humanRole: "human",
    riskFloor: 0,
    bindable: { skills: [], mcp_servers: [] },
  };
}
