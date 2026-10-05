import { forwardLogRead } from "../../../../lib/activity-proxy";

/** Rows per time bucket, stacked by level (the pulse above the Log). */
export const dynamic = "force-dynamic";

export function GET(req: Request) {
  return forwardLogRead(req, "/v1/activity/histogram");
}
