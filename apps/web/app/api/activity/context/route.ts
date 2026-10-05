import { forwardLogRead } from "../../../../lib/activity-proxy";

/** The lines just before and after one line, in the same run (`Show around this`). */
export const dynamic = "force-dynamic";

export function GET(req: Request) {
  return forwardLogRead(req, "/v1/activity/context");
}
