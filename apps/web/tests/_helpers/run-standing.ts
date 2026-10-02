import { readFileSync } from "node:fs";

/**
 * Where a run stands, computed the way the ENGINE computes it — read from
 * engine/boss_engine/services/workflow_run_actions.py, not written out here.
 *
 * The console no longer holds a list of run states; the server sends `standing`. A test
 * fixture that builds a run by hand still needs one, and a hand-written table here would be the
 * private copy the review of 0fd6c45 found drifting. So fixtures take it from the engine's own
 * source.
 */
const ENGINE = readFileSync("../../engine/boss_engine/services/workflow_run_actions.py", "utf8");

function engineSet(name: string): Set<string> {
  const found = new RegExp(`^${name} = frozenset\\(\\{([^}]*)\\}\\)`, "m").exec(ENGINE);
  if (!found) throw new Error(`${name} is not in workflow_run_actions.py`);
  return new Set([...found[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]));
}

export const ENGINE_STOPPED = engineSet("STOPPED_RUN_STATES");
export const ENGINE_CLOSED = engineSet("CLOSED_RUN_STATES");
export const ENGINE_ACTIVE = engineSet("ACTIVE_RUN_STATES");
export const ENGINE_COMPENSATING = /^COMPENSATING = "([a-z_]+)"/m.exec(ENGINE)?.[1] ?? "";

export function standingOf(status: string): "active" | "stopped" | "closed" | "compensating" {
  if (ENGINE_STOPPED.has(status)) return "stopped";
  if (ENGINE_CLOSED.has(status)) return "closed";
  if (status === ENGINE_COMPENSATING) return "compensating";
  return "active";
}

/** Every run status the database allows (db/schema.sql's CHECK on workflow_runs.status). */
export function schemaRunStatuses(): string[] {
  const schema = readFileSync("../../db/schema.sql", "utf8");
  const block = schema.slice(schema.indexOf("CREATE TABLE IF NOT EXISTS workflow_runs"));
  const check = block.slice(block.indexOf("CHECK (status IN ("), block.indexOf("))"));
  return [...check.matchAll(/'([a-z_]+)'/g)].map((m) => m[1]);
}
