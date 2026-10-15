/**
 * TEST RUNS WITH PINNED DATA, AND RUN AGAIN FROM A STEP (#512) — the console's half.
 *
 * The rules are the engine's (boss_engine/services/workflow_test_runs.py): a pinned step does not
 * run and returns its pinned output; a step that could spend or reach outside BOSS is refused in
 * a test unless pinned or allowed to run live; control steps decide from the run and cannot be
 * pinned. This file only shapes what a person typed into the request, and says in advance which
 * steps a test would refuse, so the designer can show it before anything starts. The server is
 * the authority: a hint that is wrong here is still refused there.
 */
import type { Capability, SpecStep } from "./workflow-editor-types";

/** Steps the runtime decides itself; never pinned (`workflows.CONTROL_KINDS`). */
export const CONTROL_KINDS = new Set([
  "condition", "parallel", "join", "bounded_loop", "decision", "timer", "subworkflow",
]);

/** A pin is a step's output, not a data store (`MAX_PIN_BYTES`). */
export const MAX_PIN_BYTES = 64_000;

export type PinDraft = { text: string; live: boolean };

export type PinParse =
  | { ok: true; value: Record<string, unknown> | null }
  | { ok: false; error: string };

/** Read one step's pin box. Empty means "no pin"; anything else must be a JSON object. */
export function parsePin(text: string): PinParse {
  const raw = text.trim();
  if (!raw) return { ok: true, value: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return { ok: false, error: `Not valid JSON: ${(e as Error).message}` };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, error: "A pinned output must be a JSON object, like {\"answer\": 42}" };
  }
  if (new TextEncoder().encode(JSON.stringify(parsed)).length > MAX_PIN_BYTES) {
    return { ok: false, error: `Keep a pinned output under ${MAX_PIN_BYTES} bytes` };
  }
  return { ok: true, value: parsed as Record<string, unknown> };
}

export type TestRunRow = {
  id: string;
  title: string;
  kind: string;
  /** False for control steps: they decide from the run and cannot be pinned. */
  pinnable: boolean;
  /** Why a test would refuse to run this step for real, or null when it may run. */
  refusedBecause: string | null;
};

/** What a test does with each step, before anything is pinned (mirrors `effect_of`). */
export function testRunRows(steps: SpecStep[], capabilities: Capability[]): TestRunRow[] {
  const byKey = new Map(capabilities.map((c) => [c.key, c]));
  return steps.map((step) => {
    const kind = String(step.kind || "");
    const title = String(step.title || step.id);
    if (CONTROL_KINDS.has(kind)) {
      return { id: step.id, title, kind, pinnable: false,
               refusedBecause: kind === "subworkflow"
                 ? "starts another workflow run, which would run for real" : null };
    }
    if (kind === "capability") {
      const key = String((step.binding as { capability?: unknown } | undefined)?.capability ?? "");
      const capability = byKey.get(key);
      if (!capability) {
        return { id: step.id, title, kind, pinnable: true,
                 refusedBecause: "is a step BOSS cannot classify" };
      }
      if (capability.is_gate) return { id: step.id, title, kind, pinnable: true, refusedBecause: null };
      if (capability.risk_tier >= 1) {
        return { id: step.id, title, kind, pinnable: true,
                 refusedBecause: `spends or changes something outside BOSS (${capability.label})` };
      }
      return { id: step.id, title, kind, pinnable: true, refusedBecause: null };
    }
    return { id: step.id, title, kind, pinnable: true,
             refusedBecause: `is a '${kind}' step, which may spend or change something outside BOSS` };
  });
}

export type TestRunRequest = { pins: Record<string, Record<string, unknown>>; allow_live: string[] };

/** The request a set of drafts makes, or the errors that stop it, by step id. */
export function testRunRequest(
  rows: TestRunRow[], drafts: Record<string, PinDraft | undefined>,
): { ok: true; request: TestRunRequest } | { ok: false; errors: Record<string, string> } {
  const pins: Record<string, Record<string, unknown>> = {};
  const allow_live: string[] = [];
  const errors: Record<string, string> = {};
  for (const row of rows) {
    const draft = drafts[row.id];
    if (!draft) continue;
    if (row.pinnable) {
      const parsed = parsePin(draft.text);
      if (!parsed.ok) errors[row.id] = parsed.error;
      else if (parsed.value) pins[row.id] = parsed.value;
    }
    if (draft.live && !pins[row.id]) allow_live.push(row.id);
  }
  return Object.keys(errors).length ? { ok: false, errors } : { ok: true, request: { pins, allow_live } };
}

/** The steps a test of these drafts would refuse — said before it starts. */
export function wouldRefuse(rows: TestRunRow[], request: TestRunRequest): TestRunRow[] {
  return rows.filter((row) => row.refusedBecause
    && !request.pins[row.id] && !request.allow_live.includes(row.id));
}

type RunLike = { mode?: string | null; rerun_of?: { run_id?: string; from_step?: string } | null };

export const isTestRun = (run: RunLike): boolean => run.mode === "test";

/** "Run again from build of wfr_…", for a run started from a step of another. */
export function rerunNote(run: RunLike): string | null {
  const of = run.rerun_of;
  if (!of?.run_id || !of.from_step) return null;
  return `Run again from ${of.from_step} of ${of.run_id}`;
}

/** Is this step output a pin rather than work? (`output.pinned`, set by the engine). */
export function pinnedOutput(output: unknown): boolean {
  return !!output && typeof output === "object" && (output as { pinned?: unknown }).pinned === true;
}
