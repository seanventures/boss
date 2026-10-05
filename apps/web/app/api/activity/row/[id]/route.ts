import { NextResponse } from "next/server";

import { forwardLogRead } from "../../../../../lib/activity-proxy";

/** One line by id: the target of a `row=` permalink.
 *
 *  A line that is gone (pruned, another project's) is an ANSWER, not a failure: it comes back
 *  as `200 {item: null, gone: true}` so the drawer says "This line is gone" and the browser
 *  console stays clean — a 404 here was logged as a failed resource on every old permalink. */
export const dynamic = "force-dynamic";

export async function GET(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  const got = await forwardLogRead(req, `/v1/activity/row/${encodeURIComponent(id)}`);
  if (got.status === 404) {
    return NextResponse.json({ item: null, gone: true, labels: {} }, { headers: { "Cache-Control": "no-cache" } });
  }
  return got;
}
