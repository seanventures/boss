import { NextResponse } from "next/server";

import { lastRenderTimings } from "../../../lib/render-timing";
import { slowestFeeds } from "../../../lib/feed-cache";

export const dynamic = "force-dynamic";

export async function GET() {
  return NextResponse.json({ renders: lastRenderTimings(), slowest: slowestFeeds() });
}
