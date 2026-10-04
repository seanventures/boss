/**
 * Agents › Live fixtures, in the WIRE shape /v1/agents/live-board answers (agents-home spec
 * §6.2), so the tests go through the same parser the screen does.
 */
type Wire = Record<string, unknown>;

export const gate = (over: Wire = {}): Wire => ({
  id: "dec_gate", kind: "approval", decision_kind: "workflow_gate", decision_id: "dec_gate",
  title: "“Publish” is waiting for you on Weekly video", detail: "The video is cut and captioned.",
  what_to_do: null, role_key: "media_publisher",
  run: { id: "wfr_video", subject: "Weekly video", step_id: "publish", step_label: "Publish", step_index: 5, step_count: 7 },
  actions: [
    { action: "approve", label: "Publish it", tone: "approve", effect: "Releases the step and the workflow continues from it.", needs: [] },
    { action: "reject", label: "Reject", tone: "reject", effect: "Fails the step; the run stops there.", needs: [] },
  ],
  answers: [], decide_inline: true, content_hash: "h1", preview: "Weekly video · 4:12", risk_tier: 2,
  at: "2026-10-04T09:34:00Z", ...over,
});

export const failed = (over: Wire = {}): Wire => ({
  id: "run:wfr_ship:build", kind: "failed_step", title: "“Build” failed on Ship it #180",
  detail: "The sandbox ran out of disk.", role_key: "software_engineer",
  run: { id: "wfr_ship", subject: "Ship it #180", step_id: "build", step_label: "Build", step_index: 3, step_count: 7 },
  actions: [], decide_inline: false,
  answers: [
    { key: "retry_step", label: "Retry", enabled: true, reason: null, step_id: "build" },
    { key: "skip_step", label: "Skip", enabled: false, reason: "A required step cannot be skipped.", step_id: "build" },
  ],
  at: "2026-10-04T09:39:00Z", ...over,
});

export const prReview = (over: Wire = {}): Wire => ({
  id: "dec_pr", kind: "approval", decision_kind: "pull_request", decision_id: "dec_pr",
  title: "Merge PR #842?", detail: "Tests pass.", role_key: null,
  actions: [{ action: "approve", label: "Approve and merge", tone: "approve", effect: "Merges the PR.", needs: [] }],
  answers: [], decide_inline: false, risk_tier: 3, at: "2026-10-04T08:00:00Z", ...over,
});

export const mission = (over: Wire = {}): Wire => ({
  run_id: "wfr_182", subject: "Ship it #182", workflow_key: "ship_it", workflow_name: "Ship it", version: 3,
  actor: "ceo", state: "running", standing: "active", step_index: 4, step_count: 7, step_label: "Build",
  verb: "Coding", waiting_on: null, blocked_reason: null, error: null, attention_id: null,
  started_at: "2026-10-04T09:00:00Z", step_since: "2026-10-04T09:30:00Z", elapsed_seconds: 2520,
  step_elapsed_seconds: 720, cost_usd: 0.42,
  hops: [
    { act_id: "act_1", role_key: null, person: "ceo", state: "done", label: "Started", edge_label: "Started by you" },
    { act_id: "act_2", role_key: "pm", state: "done", label: "PRD", edge_label: "Named in workflow" },
    { act_id: "act_3", role_key: "software_engineer", state: "working", label: "Build", edge_label: "Picked by BOSS over 3" },
  ],
  next: [{ role_key: "qa", step_label: "Test", named: true }],
  finished_at: null, ...over,
});

export const board = (over: Wire = {}): Wire => ({
  project_id: "p1", etag: "\"e1\"", generated_at: "2026-10-04T09:42:00Z",
  counts: { agents: 34, working: 2, needs_you: 2, running: 2, stopped: 1 },
  attention: [gate(), failed()],
  missions: [
    mission(),
    mission({ run_id: "wfr_video", subject: "Weekly video", workflow_key: "video", workflow_name: "Video",
              state: "waiting_you", step_index: 5, verb: "Needs you", attention_id: "dec_gate", cost_usd: 0,
              hops: [{ act_id: "act_9", role_key: "media_publisher", state: "done", label: "Cut", edge_label: "Named in workflow" }],
              next: [] }),
    mission({ run_id: "wfr_ship", subject: "Ship it #180", state: "stopped", step_index: 3, verb: "Stuck",
              cost_usd: 0, error: "The sandbox ran out of disk.",
              hops: [{ act_id: "act_7", role_key: "software_engineer", state: "stuck", label: "Build", edge_label: "Named in workflow" }],
              next: [] }),
  ],
  outcomes: [
    { act_id: "act_o1", role_key: "software_engineer", kind: "pr", text: "PR #842 opened", url: "https://example.test/pr/842",
      run_id: "wfr_182", label: "Build · Ship it #182", at: "2026-10-04T09:30:00Z" },
    { act_id: "act_o2", role_key: "pm", kind: "minutes", text: "Minutes ready", run_id: "wfr_182", at: "2026-10-04T08:42:00Z" },
  ],
  teams: [
    { key: "engineering", label: "Engineering", members: ["software_engineer"], working: 1, needs_you: 0, in_panel: 0 },
  ],
  agents: [
    { role_key: "software_engineer", name: "Software Engineer", callsign: "Forge", team: "engineering", state: "working",
      line: "Now: Coding · Build · Ship it #182" },
  ],
  ...over,
});

/** A fetch that answers the live board, the presence poll and nothing else. */
export function liveFetch(body: () => Wire, live: () => Wire = () => ({ items: [] })) {
  return async (input: unknown) => {
    const url = String(input);
    if (url.startsWith("/v1/agents/live-board")) return Response.json(body(), { headers: { etag: "\"e1\"" } });
    if (url.startsWith("/v1/agents/live")) return Response.json(live());
    return Response.json({});
  };
}
