/**
 * The incident card's wire (GET /v1/reliability/incidents/{id}/response, engine
 * incident_response.response_view), with every section filled: an app alert answered by a
 * running Incident Response, customer reports from a second app, a root cause, a verification
 * and a stored report. `over` replaces top-level keys.
 */
export const INCIDENT_NOW = "2026-10-09T12:00:00Z";

export function incidentResponseWire(over: Record<string, unknown> = {}) {
  return {
    incident: { id: "inc_1", title: "helpdesk: 12.5% of requests failing", service: "helpdesk", severity: "high",
                status: "open", confidence: 0.9, first_seen: "2026-10-09T11:48:00Z", last_seen: "2026-10-09T11:59:00Z" },
    app: { target_id: "t_down", app_name: "helpdesk", environment: "prod" },
    deploy: { commit_sha: "a1b2c3d4e5f6a7b8", previous_commit_sha: "9f8e7d6c5b4a3210", deployed_at: "2026-10-09T11:40:00Z",
              ref: "main", repository: "acme/helpdesk", extends_run_id: "wfr_build_1" },
    alert: { fired_at: "2026-10-09T11:48:00Z", rule: { error_rate_percent: 5, window_minutes: 5, min_requests: 20 },
             error_rate: 0.125, requests: 220, errors: 27,
             routes: [{ route: "GET /health", requests: 40, errors: 0, error_rate: 0 },
                      { route: "POST /tickets", requests: 90, errors: 20, error_rate: 0.222 },
                      { route: "GET /tickets/{id}", requests: 60, errors: 6, error_rate: 0.1 },
                      { route: "GET /search", requests: 30, errors: 1, error_rate: 0.033 }] },
    verification: { state: "recovered", summary: "Recovered: 0.4% of 260 requests failed over the last 10 min",
                    at: "2026-10-09T12:30:00Z", independent: "BOSS's own scrapes of the app's /metrics" },
    reported_only: false,
    evidence: [
      { kind: "alert", ref: "5", at: "2026-10-09T11:48:00Z", payload: {} },
      { kind: "customer_reports", ref: "t_desk", at: "2026-10-09T11:50:00Z",
        payload: { reporter: { target_id: "t_desk", app_name: "support-desk" } } },
      { kind: "rca_hypothesis", ref: "release_changed_failing_frame", at: "2026-10-09T11:52:00Z",
        payload: { root_cause: "The release a1b2c3d changed tickets.create, where every failing request raised KeyError.",
                   basis: "release_changed_failing_frame", confidence: 0.9, suspect_commit: "a1b2c3d", suspect_file: "app/tickets.py" } },
      { kind: "note", ref: "wfr_ir_1", at: "2026-10-09T11:55:00Z",
        payload: { pull_request_url: "https://github.com/acme/helpdesk/pull/42" } },
    ],
    reports: [
      { reporter_target_id: "t_desk", via: "push", at: "2026-10-09T11:50:00Z", id: "s1", title: "Tickets fail to save",
        summary: "", affects: "helpdesk", refs: ["T-1042", "T-1043"], count: 2 },
      { reporter_target_id: "t_desk", via: "push", at: "2026-10-09T11:51:00Z", id: "s2", title: "Tickets fail to save",
        summary: "", affects: "helpdesk", refs: ["T-1043", "T-1044"], count: 1 },
    ],
    response: { id: "wfr_ir_1", status: "running", started_at: "2026-10-09T11:49:00Z",
                steps: [{ step_id: "investigate", status: "succeeded" }, { step_id: "build", status: "succeeded" },
                        { step_id: "ci", status: "succeeded" }, { step_id: "report", status: "succeeded" },
                        { step_id: "deploy", status: "running" }] },
    auto_respond_rules: [],
    report_ready: true,
    respond_input: { incident_id: "inc_1", extends_run_id: "wfr_build_1", title: "helpdesk: 12.5% of requests failing",
                     directive: "Respond to the incident: helpdesk: 12.5% of requests failing." },
    ...over,
  };
}

export const INCIDENT_REPORT_WIRE = {
  kind: "incident_report",
  markdown: "# Incident report\n\n- **Faulty release:** a1b2c3d4e5f6\n\n## Root cause\n\nThe release changed tickets.create.",
};
