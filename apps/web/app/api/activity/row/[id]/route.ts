import { forwardLogRead } from "../../../../../lib/activity-proxy";

/** One line by id: the target of a `row=` permalink. */
export const dynamic = "force-dynamic";

export async function GET(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  return forwardLogRead(req, `/v1/activity/row/${encodeURIComponent(id)}`);
}
