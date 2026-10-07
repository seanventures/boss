import { NextResponse } from "next/server";

import { PROJECT_ID } from "../../../lib/client";
import { checkWith } from "../../../lib/identity/check";
import { snapshotFrom } from "../../../lib/identity/contract";
import type { Permission } from "../../../lib/identity/permissions.gen";
import { loadIdentity } from "../../../lib/identity/server";
import { lastRenderTimings } from "../../../lib/render-timing";
import { slowestFeeds } from "../../../lib/feed-cache";

export const dynamic = "force-dynamic";

/** The process's own diagnostics: render phases and the slowest feeds. */
const PERMISSION: Permission = "system:read";
const NO_STORE = { "cache-control": "no-store" };

/**
 * Render timings and the slowest feeds of THIS server process. It makes no API call, so the API
 * cannot gate it: the viewer's own permissions are read here (with their cookie) and the answer
 * needs `system:read` ("See health, status, diagnostics…") at the console's project. Diagnostics
 * fail CLOSED: with no session it is 401, and when the identity API cannot answer it is 503 —
 * never the timings.
 */
export async function GET() {
  const boot = await loadIdentity(PROJECT_ID);
  if (boot.problem === "unauthenticated") {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401, headers: NO_STORE });
  }
  if (!boot.org) {
    return NextResponse.json({ error: "identity_unavailable" }, { status: 503, headers: NO_STORE });
  }
  const allowed = checkWith(snapshotFrom(boot.org), boot.project ? snapshotFrom(boot.project) : null, PROJECT_ID,
                            PERMISSION).allowed;
  if (!allowed) {
    return NextResponse.json({ error: "forbidden", permission: PERMISSION }, { status: 403, headers: NO_STORE });
  }
  return NextResponse.json({ renders: lastRenderTimings(), slowest: slowestFeeds() }, { headers: NO_STORE });
}
