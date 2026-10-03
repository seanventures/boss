/**
 * A run, as the shared lane draws it.
 *
 * The Runs view used to have its own rail, which wrapped onto a second row and named steps
 * differently from the designer. It now draws the same FlowLane in run mode, from THE SAME
 * DEFINITION the designer draws: the version the run actually ran (`workflow_key` + `version`),
 * with the same palette. So a step is titled, credited to an agent and placed in its column
 * exactly as the editor shows it, and a branched workflow is drawn with its branches rather than
 * as a chain that suggests its steps ran one after another.
 *
 * What a run reports about itself (its steps, in order, without titles or custom-step bindings)
 * is only the stand-in while that definition loads, or if it cannot be read.
 */
"use client";

import { useEffect, useState } from "react";

import { fetchPalette, fetchSpec } from "../../../lib/workflow-api";
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

/**
 * The spec a run's lane draws: the definition it ran when that is known and names every step the
 * run reports, otherwise the run's own steps as a line.
 */
export function runLaneSpec(steps: RunLaneStep[], definition: Spec | null): Spec {
  if (definition && Array.isArray(definition.steps)) {
    const ids = new Set(definition.steps.map((s) => s.id));
    if (steps.every((s) => ids.has(s.step_id))) return definition;
  }
  return runSpec(steps);
}

/* A published version never changes, so its spec is read once per page. The palette (custom
   steps, the roster) can change, so it is re-read when it is older than PALETTE_TTL_MS. */
const PALETTE_TTL_MS = 5 * 60_000;
const specs = new Map<string, Promise<Spec | null>>();
const palettes = new Map<string, { at: number; value: Promise<Palette | null> }>();

function definitionOf(projectId: string, key: string, version: number): Promise<Spec | null> {
  const id = `${projectId}:${key}:${version}`;
  let got = specs.get(id);
  if (!got) {
    got = fetchSpec(projectId, key, version).then((r) => r.spec, () => {
      specs.delete(id); // not cached: the next run card asks again
      return null;
    });
    specs.set(id, got);
  }
  return got;
}

function paletteOf(projectId: string): Promise<Palette | null> {
  // A monotonic clock: this is an age, not a time anyone reads.
  const now = performance.now();
  const held = palettes.get(projectId);
  if (held && now - held.at < PALETTE_TTL_MS) return held.value;
  const value = fetchPalette(projectId).catch(() => {
    palettes.delete(projectId);
    return null;
  });
  palettes.set(projectId, { at: now, value });
  return value;
}

/** For tests: forget what has been read. */
export function forgetRunDefinitions(): void {
  specs.clear();
  palettes.clear();
}

/**
 * The definition version a run ran, and the palette the designer names its steps with. Each is
 * null until it loads (or when it cannot be read), when the caller draws from the run itself.
 */
export function useRunDefinition(projectId: string, key: string | null | undefined,
                                 version: number | null | undefined): { spec: Spec | null; palette: Palette | null } {
  const [spec, setSpec] = useState<{ id: string; spec: Spec | null } | null>(null);
  const [palette, setPalette] = useState<Palette | null>(null);
  const id = key && version ? `${projectId}:${key}:${version}` : null;
  useEffect(() => {
    if (!id || !key || !version) return;
    let live = true;
    void definitionOf(projectId, key, version).then((got) => { if (live) setSpec({ id, spec: got }); });
    return () => { live = false; };
  }, [id, projectId, key, version]);
  useEffect(() => {
    let live = true;
    void paletteOf(projectId).then((got) => { if (live) setPalette(got); });
    return () => { live = false; };
  }, [projectId]);
  return { spec: spec && spec.id === id ? spec.spec : null, palette };
}
