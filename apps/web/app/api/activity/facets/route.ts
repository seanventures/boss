import { forwardLogRead } from "../../../../lib/activity-proxy";

/** Counts per level, stream, agent, run, process and status for the current filter
 *  (GET /v1/activity/facets), counted in SQL over the window — never from a page. */
export const dynamic = "force-dynamic";

export function GET(req: Request) {
  return forwardLogRead(req, "/v1/activity/facets");
}
