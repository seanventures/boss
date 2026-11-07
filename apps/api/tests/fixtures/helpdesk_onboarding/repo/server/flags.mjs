// Server-exposed feature flags. On by default (this is the first ship, not a rollout onto
// live traffic); set ENABLE_THEME_V2=false / ENABLE_DASHBOARD=false to roll back each
// independently, same convention as TICKET_TAGS_ENABLED.
export function themeV2Enabled(env = process.env) {
  return env.ENABLE_THEME_V2 !== "false";
}

export function dashboardEnabled(env = process.env) {
  return env.ENABLE_DASHBOARD !== "false";
}

export function incidentDetectionEnabled(env = process.env) {
  return env.INCIDENT_DETECTION_ENABLED !== "false";
}
