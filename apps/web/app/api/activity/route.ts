import { forwardLogRead } from "../../../lib/activity-proxy";

/**
 * One page of the Activity log (GET /v1/activity), as a route handler: JSON out, same origin,
 * no Server Action reply (that path once cost a full re-render of the console). Forwards the
 * ETag both ways so the live tail's quiet polls are 304s.
 */
export const dynamic = "force-dynamic";

export function GET(req: Request) {
  return forwardLogRead(req, "/v1/activity");
}
