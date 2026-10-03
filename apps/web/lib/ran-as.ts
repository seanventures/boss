/**
 * Who ran a workflow step, as the engine records it (`output.ran_as`, see
 * engine/boss_engine/services/step_agents.py). Pure and server-safe, so the run view's chip, the
 * run lane and the Credits image route all read one rule.
 */

/** `ran_as.how` values that mean no agent ran the step: whoever started the run, the workflow
 *  itself, or BOSS's decision model. */
export const NOT_AN_AGENT = ["run_actor", "workflow_runner", "legacy_workflow_runner",
                             "decision_model"];

/** The `how` values where BOSS's own machinery did the step, not a person. */
export const BOSS_RAN_IT = ["workflow_runner", "legacy_workflow_runner", "decision_model"];

/** The captain an approval gate names when a person answers it (step_agents.HUMAN). */
export const HUMAN = "human";
