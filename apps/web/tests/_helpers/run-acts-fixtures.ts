/**
 * Signed work as the API sends it (snake_case `agent_acts` columns + `edge_label`), for the run
 * surfaces' tests. The labels are the engine's own vocabulary, read from
 * engine/boss_engine/services/agent_world_types.py, so a test can never agree with a label the
 * console made up.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

export type WireAct = Record<string, unknown>;

/** EDGE_LABELS from the engine, as written there (templates keep their {placeholders}). */
export function engineEdgeLabels(): Record<string, string> {
  const py = readFileSync(resolve(__dirname, "../../../../engine/boss_engine/services/agent_world_types.py"), "utf8");
  const block = py.match(/EDGE_LABELS[^=]*=\s*\{([\s\S]*?)\n\}/)?.[1] ?? "";
  const out: Record<string, string> = {};
  for (const m of block.matchAll(/"([a-z_]+)":\s*"([^"]+)"/g)) out[m[1]] = m[2];
  return out;
}

/** A label as the server would fill it. */
export function filled(template: string): string {
  return template.replace("{skill}", "deploy_app").replace("{n}", "3").replace("{captain}", "Engineering Captain")
    .replace("{actor}", "you").replace("{agent}", "Atlas");
}

let seq = 0;
export function wireAct(p: Partial<WireAct> & { kind: string }): WireAct {
  seq += 1;
  const id = (p.act_id as string) ?? `act_${String(seq).padStart(4, "0")}`;
  return {
    project_id: "p1", role_key: null, status: "complete",
    parent_act_id: null, root_act_id: "act_root", handed_by_kind: "boss", handed_by_id: null,
    how: "named", why: null, considered: [], scope: { skills: [], mcp: [] }, scope_ok: null,
    accountable: "ceo", label: "Step · Ship it #182", workflow_run_id: "wfr_1", step_id: null,
    artifacts: [], tokens_in: 1200, tokens_out: 300, cost_usd: 0.042, reconstructed: false,
    started_at: `2026-10-03T12:${String(seq % 60).padStart(2, "0")}:00Z`,
    ended_at: `2026-10-03T12:${String(seq % 60).padStart(2, "0")}:40Z`, edge_label: "Named in workflow",
    ...p, act_id: id,
  };
}

/**
 * Ship it, signed: You start it → Juno (pm, named) writes the PRD → Atlas (picked by BOSS over 3)
 * plans → a Build step by the Engineering Captain with five stages inside (implement ↔ review ×3
 * among them) → Vera and Rook review in parallel → You approve → Pilot (release_manager) ships.
 */
export function shipItChain(): Record<string, unknown> {
  seq = 0;
  const L = engineEdgeLabels();
  const root = wireAct({ act_id: "act_root", kind: "run", how: "started", handed_by_kind: "person",
    handed_by_id: "ceo", edge_label: filled(L.started), label: "Run · Ship it #182", status: "active" });
  const prd = wireAct({ act_id: "act_prd", kind: "step", role_key: "pm", parent_act_id: "act_root", step_id: "prd",
    how: "named", edge_label: filled(L.named), label: "PRD · Ship it #182" });
  const plan = wireAct({ act_id: "act_plan", kind: "step", role_key: "chief_architect", parent_act_id: "act_prd",
    step_id: "plan", how: "model_pick", considered: ["chief_architect", "software_engineer", "pm"],
    why: "Holds the planning skill", edge_label: filled(L.model_pick), label: "Plan · Ship it #182" });
  const build = wireAct({ act_id: "act_build", kind: "step", role_key: "engineering_captain", parent_act_id: "act_plan",
    step_id: "build", how: "named", edge_label: filled(L.named), label: "Build · Ship it #182",
    code_task_run_id: "crun_1", scope: { skills: ["git", "tests"], mcp: ["github"] }, scope_ok: false });
  const stages = [
    ["act_s1", "backend_engineer", "implement"], ["act_s2", "adversarial_reviewer", "review_code"],
    ["act_s3", "backend_engineer", "repair"], ["act_s4", "adversarial_reviewer", "review_code"],
    ["act_s5", "backend_engineer", "repair"], ["act_s6", "adversarial_reviewer", "review_code"],
  ].map(([id, role, stage], i, all) => wireAct({ act_id: id, kind: "stage", role_key: role,
    parent_act_id: i === 0 ? "act_build" : all[i - 1][0], how: "next_stage", edge_label: filled(L.next_stage),
    label: `Build · ${stage}`, stage, code_task_run_id: "crun_1", step_id: "build" }));
  const qa = wireAct({ act_id: "act_qa", kind: "step", role_key: "qa", parent_act_id: "act_build", step_id: "test",
    how: "named", edge_label: filled(L.named), label: "Test · Ship it #182" });
  const review = wireAct({ act_id: "act_review", kind: "step", role_key: "adversarial_reviewer", parent_act_id: "act_build",
    step_id: "review", how: "model_pick", considered: ["adversarial_reviewer", "qa"], edge_label: filled(L.model_pick),
    label: "Review · Ship it #182" });
  const gate = wireAct({ act_id: "act_gate", kind: "step", role_key: null, parent_act_id: "act_review", step_id: "gate",
    how: "run_actor", handed_by_kind: "boss", edge_label: filled(L.run_actor), label: "Approve · Ship it #182",
    status: "complete" });
  const approved = wireAct({ act_id: "act_ok", kind: "approval", role_key: null, parent_act_id: "act_gate",
    how: "approved", handed_by_kind: "person", handed_by_id: "ceo", edge_label: filled(L.approved),
    label: "Approval · Ship it #182" });
  const ship = wireAct({ act_id: "act_ship", kind: "step", role_key: "release_manager", parent_act_id: "act_gate",
    step_id: "deploy", how: "named", edge_label: filled(L.named), label: "Deploy · Ship it #182", status: "active",
    ended_at: null });
  return {
    acts: [root, prd, plan, build, ...stages, qa, review, gate, approved, ship],
    accountable: "ceo",
    totals: { steps: 7, minutes: 42, cost: 1.37 },
    credits: [
      { role_key: "pm", line: "Wrote the PRD" }, { role_key: "chief_architect", line: "Planned it" },
      { role_key: "engineering_captain", line: "Captained the build" },
      { role_key: "backend_engineer", line: "Built · 3 rounds" }, { role_key: "adversarial_reviewer", line: "Reviewed · 3 rounds" },
      { role_key: "qa", line: "Tested it" }, { role_key: null, line: "Approved deploy" },
      { role_key: "release_manager", line: "Shipping it" },
    ],
  };
}
