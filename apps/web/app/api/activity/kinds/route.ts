import { forwardLogRead } from "../../../../lib/activity-proxy";

/** `kind:` completion by prefix over the window (GET /v1/activity/kinds), counted in SQL. */
export const dynamic = "force-dynamic";

export function GET(req: Request) {
  return forwardLogRead(req, "/v1/activity/kinds");
}
