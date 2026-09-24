import { NextResponse } from "next/server";

import { fetchInFlightFeed, reconcileRecentWork } from "../../../lib/metrics";
import { fetchRoleFeed } from "../../../lib/roles";
import { buildAgentRoster } from "../../../lib/roster";
import { fetchTaskFeed } from "../../../lib/tasks";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const projectId = new URL(request.url).searchParams.get("project_id")?.trim();
  if (!projectId) return NextResponse.json({ error: "missing project_id" }, { status: 400 });

  const [roles, tasks, inFlight] = await Promise.all([
    fetchRoleFeed(projectId),
    fetchTaskFeed(projectId, 50),
    fetchInFlightFeed(projectId),
  ]);
  if (!roles.apiReachable || !tasks.apiReachable || !inFlight.apiReachable) {
    return NextResponse.json(
      { error: roles.blockedReason ?? tasks.blockedReason ?? "Coordination feed unavailable" },
      { status: 502 },
    );
  }
  return NextResponse.json({
    roster: buildAgentRoster(roles.items, tasks.items),
    inFlight: reconcileRecentWork(inFlight.items, tasks.items),
    observedAt: new Date().toISOString(),
  });
}
