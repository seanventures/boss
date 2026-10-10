import type { CommandCenterView } from "./command-center-view";

export type BriefRecord = {
  id: string; text: string; at: string;
  patch: Record<string, string | null>;
};

/** A timestamp only: no tasks, messages, credentials or results are stored in the browser. */
export function lastSeenKey(org: string | undefined, project: string, viewer: string): string {
  return `boss.last-seen.v1:${JSON.stringify([org ?? "", project, viewer])}`;
}

export function returningBrief(view: CommandCenterView | null, project: string, since: string | null): BriefRecord[] {
  const start = Date.parse(since ?? "");
  const end = Date.parse(view?.generatedAt ?? "");
  if (!view || view.projectId !== project || !Number.isFinite(start) || !Number.isFinite(end) || start > end) return [];
  const after = (at: string | null | undefined) => {
    const time = Date.parse(at ?? "");
    return Number.isFinite(time) && time > start && time <= end;
  };
  const rows: BriefRecord[] = [];
  for (const event of view.pulse?.latest ?? []) {
    if (!event.runId || !after(event.at)) continue;
    rows.push({ id: `${event.kind}:${event.runId}`, text: `${event.verb} ${event.subject}`.trim(), at: event.at,
      patch: event.kind === "build" ? { project_id: project, area: "code_tasks", view: "apps", build: event.runId, role: null }
        : { project_id: project, area: "delivery_workflows", wfr: event.runId, view: null, role: null } });
  }
  for (const run of view.recent) {
    if (!run.runId || !after(run.finishedAt)) continue;
    rows.push({ id: `run:${run.runId}`, text: `${run.name}: ${run.outcome || "outcome not recorded"}`,
      at: run.finishedAt!, patch: { project_id: project, area: "delivery_workflows", wfr: run.runId, view: null, role: null } });
  }
  // The read is deliberately a bounded history window, not a complete change ledger or
  // a claim about a build's current revision, technical checks or human acceptance.
  const seen = new Set<string>();
  return rows.sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).filter((row) => {
    if (seen.has(row.id)) return false;
    seen.add(row.id);
    return true;
  }).slice(0, 3);
}
