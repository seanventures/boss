/**
 * A KNOWLEDGE-GRAPH PROJECT'S SETUP (BOSSAPP-768 project flow): the one read and the one write the
 * Knowledge Graph area's stepper and the project home's lifecycle card add. Everything else the
 * steps do goes through the doors that already exist — Factory › Repositories' add
 * (lib/multirepo.ts putProjectRepo), Index now (lib/kg-view.ts indexNow), Scan for rules here
 * (node-panel.tsx ScanForRules) and Rules review — so a press here is the same act as there.
 *
 * GET /v1/knowledge/setup is engine services/knowledge/project_setup.py, the same answer the
 * agent's knowledge_project_setup tool gives.
 */
import type { KgSetupResponse, KgSetupStepKey } from "@boss/api-client";

import { apiErrorText } from "./api-error";
import { apiWrite } from "./api-write";
import { kgRead } from "./kg-view";

export type { KgSetupCandidate, KgSetupResponse, KgSetupStep, KgSetupStepKey, KgSetupStepState } from "@boss/api-client";

type Who = { projectId: string; actor: string };

export function getSetup(w: Who, signal?: AbortSignal) {
  return kgRead<KgSetupResponse>("/v1/knowledge/setup", { project_id: w.projectId, actor: w.actor }, signal);
}

/** The project's team, STATED: exactly these agents (POST /v1/projects/{id}/agents action=set,
 *  through the console's /api/project-agents relay, the same door as the default-team picker). */
export async function setProjectTeam(w: Who, roleKeys: string[]): Promise<{ ok: true } | { ok: false; error: string }> {
  try {
    const r = await apiWrite("/api/project-agents", {
      method: "POST", headers: { "Content-Type": "application/json" }, cache: "no-store",
      body: JSON.stringify({ project_id: w.projectId, app: "*", action: "set", role_keys: roleKeys, actor: w.actor }),
    }, { press: { scope: `kg-team:${w.projectId}`, label: "Save the team" } });
    let body: unknown = null;
    try { body = await r.json(); } catch { body = null; }
    if (!r.ok) return { ok: false, error: apiErrorText(body, r.status) };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

/** The steps' names and one line each, in order: the stepper's rows. */
export const SETUP_STEPS: { key: KgSetupStepKey; label: string; line: string }[] = [
  { key: "import", label: "Import code", line: "Add the repositories to map. BOSS parses them into the code graph: no model is called." },
  { key: "team", label: "Assign the team", line: "Choose the BOSS agents who work on this project: who scans, who changes code, who handles incidents." },
  { key: "scan", label: "First scan", line: "An agent reads one area and proposes the business rules it enforces, each with the lines that prove it." },
  { key: "review", label: "Review rules", line: "A person approves, edits or rejects each proposed rule. Only approved rules guide agents." },
];

/** IS THERE INDEXED CODE? One answer (the API's `has_code`, read once in project_setup.py) for
 *  Import code's Done, the first scan's gate, the graph's Open button and the "continue setup"
 *  strip — so the stepper can never say "1 of 1 repositories indexed" next to "Needs indexed
 *  code" (KG tester round 4). An older API without `has_code`: its graph, or Import code's Done. */
export function hasIndexedCode(setup: Pick<KgSetupResponse, "steps" | "has_code" | "has_graph"> | null | undefined): boolean {
  if (!setup) return false;
  if (typeof setup.has_code === "boolean") return setup.has_code;
  if (typeof setup.has_graph === "boolean") return setup.has_graph;
  return !!setup.steps.find((s) => s.key === "import")?.done;
}

/** What a step really waits for, or null when it can be done now. */
export function blockedBy(key: KgSetupStepKey, setup: Pick<KgSetupResponse, "steps" | "has_code" | "has_graph">): KgSetupStepKey | null {
  const step = setup.steps.find((s) => s.key === key);
  if (!step || step.done || step.state === "working") return null;
  if (step.blocked_by !== undefined) return step.blocked_by ?? null;
  // An older API: the scan needs code, the review needs a scan; nothing else waits.
  if (key === "scan") return hasIndexedCode(setup) ? null : "import";
  if (key === "review") return setup.steps.find((s) => s.key === "scan")?.done ? null : "scan";
  return null;
}

/** The tab the area opens on. A knowledge-graph project whose code is not in yet opens on its
 *  setup (never an empty canvas); once code is in, the graph — with a "continue setup" strip. */
export function setupOpens(setup: Pick<KgSetupResponse, "project_type" | "steps" | "has_code" | "has_graph"> | null): boolean {
  if (!setup || setup.project_type !== "knowledge_graph") return false;
  return !hasIndexedCode(setup);
}

/** "2 of 4 steps done". */
export function stepsLeftWords(setup: Pick<KgSetupResponse, "done_count" | "steps">): string {
  return `${setup.done_count} of ${setup.steps.length} steps done`;
}
