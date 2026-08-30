import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * Deployment state for MANY runs in one call.
 *
 * Every app card asked for its own deployment state through `runDeploymentsAction`, from a
 * `useEffect` that fires on mount. Two costs, and the second is the one that hurt:
 *
 *   1. N calls where one would do.
 *   2. Each was a Server Action, and a Server Action re-renders the whole route to reply.
 *
 * Measured on the live console, loading the Factory area with no interaction at all:
 * 111 server actions, 82 of them this one, 64.5s of action time, and the page was not quiet
 * for 74 seconds. That is the "takes 30s to a minute to render fully" complaint, in full.
 *
 * The host list was moved to a route handler for exactly this reason and the comment on it
 * says so — this is the same fix applied to the per-card path, which was missed because it was
 * cheap-looking in isolation. It is not cheap forty times.
 *
 * Batching is done here rather than client-side alone so a card that mounts late still costs
 * one small request, not one route render.
 */
export const dynamic = "force-dynamic";

/** Cap per request. A project with a thousand runs must not become a thousand upstream calls
 *  in a single handler — the client batcher chunks, and this refuses the rest rather than
 *  quietly truncating, which would report "not deployed" for the runs it dropped. */
const MAX_IDS = 60;

export async function GET(req: Request) {
  const url = new URL(req.url);
  const raw = (url.searchParams.get("run_ids") ?? "").trim();
  const ids = raw.split(",").map((s) => s.trim()).filter(Boolean);
  // Titles are positional, matching `run_ids`, because the upstream endpoint disambiguates
  // apps by name — several apps share one repo, and without the name a deploy once reported
  // success for a different app entirely.
  const names = (url.searchParams.get("app_names") ?? "").split(",").map((s) => s.trim());

  if (ids.length === 0) {
    return NextResponse.json({ error: "missing run_ids" }, { status: 400 });
  }
  if (ids.length > MAX_IDS) {
    return NextResponse.json(
      { error: `too many run_ids (${ids.length} > ${MAX_IDS}); request them in chunks` },
      { status: 400 },
    );
  }

  const results = await Promise.all(
    ids.map(async (id, i) => {
      const name = names[i] ? `?app_name=${encodeURIComponent(names[i])}` : "";
      try {
        const r = await fetch(
          `${API_URL}/v1/code-tasks/${encodeURIComponent(id)}/deployments${name}`,
          { headers: authHeaders({}), cache: "no-store" },
        );
        if (!r.ok) return [id, { error: `HTTP ${r.status}` }] as const;
        return [id, await r.json()] as const;
      } catch (e) {
        // One card's failure must not blank the other fifty-nine. The error rides along on
        // that entry so the card can say "couldn't check" instead of "not deployed" — those
        // are different facts and the second one is a lie.
        return [id, { error: String((e as Error)?.message ?? e) }] as const;
      }
    }),
  );

  return NextResponse.json({ runs: Object.fromEntries(results) });
}
