import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { rawRequestUrl } from "../../../lib/raw-request-url";

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
// One page can ask about sixty runs, and several page chunks can arrive together. Letting
// each chunk open sixty API requests at once can saturate the API's database connection pool.
const MAX_CONCURRENT_LOOKUPS = 4;
// Shared by every invocation of this route in one web process. The per-request cap alone still
// opened 36 API reads when nine browser batches arrived in the same second.
const MAX_PROCESS_LOOKUPS = 8;
const LOOKUP_TIMEOUT_MS = 3_000;
const BATCH_BUDGET_MS = 12_000;

type RunResult = { error: string } | Record<string, unknown>;
type RunEntry = readonly [string, RunResult];
type SlotWaiter = {
  deadline: number;
  timer: ReturnType<typeof setTimeout>;
  resolve: (release: (() => void) | null) => void;
};

let activeProcessLookups = 0;
const slotWaiters: SlotWaiter[] = [];

function releaseProcessSlot(): void {
  activeProcessLookups -= 1;
  while (slotWaiters.length > 0) {
    const waiter = slotWaiters.shift()!;
    clearTimeout(waiter.timer);
    if (Date.now() >= waiter.deadline) {
      waiter.resolve(null);
      continue;
    }
    activeProcessLookups += 1;
    waiter.resolve(releaseProcessSlot);
    return;
  }
}

function acquireProcessSlot(deadline: number): Promise<(() => void) | null> {
  const remainingMs = deadline - Date.now();
  if (remainingMs <= 0) return Promise.resolve(null);
  if (activeProcessLookups < MAX_PROCESS_LOOKUPS) {
    activeProcessLookups += 1;
    return Promise.resolve(releaseProcessSlot);
  }
  return new Promise((resolve) => {
    const waiter: SlotWaiter = {
      deadline,
      timer: setTimeout(() => {
        const index = slotWaiters.indexOf(waiter);
        if (index >= 0) slotWaiters.splice(index, 1);
        resolve(null);
      }, remainingMs),
      resolve,
    };
    slotWaiters.push(waiter);
  });
}

function lookupDeployment(
  id: string, name: string, remainingMs: number,
): { result: Promise<RunEntry>; done: Promise<void> } {
  const controller = new AbortController();
  const timeoutError = { error: "deployment lookup timed out" };
  let timer: ReturnType<typeof setTimeout> | undefined;
  // Race the response (including JSON parsing) against the deadline. Aborting fetch alone is
  // insufficient if a transport or test double does not settle when it receives the signal.
  const timedOut = new Promise<RunEntry>((resolve) => {
    timer = setTimeout(() => {
      controller.abort();
      resolve([id, timeoutError]);
    }, Math.min(LOOKUP_TIMEOUT_MS, remainingMs));
  });
  const request = (async (): Promise<RunEntry> => {
    const query = name ? `?app_name=${encodeURIComponent(name)}` : "";
    try {
      const response = await fetch(
        `${API_URL}/v1/code-tasks/${encodeURIComponent(id)}/deployments${query}`,
        { headers: authHeaders({}), cache: "no-store", signal: controller.signal },
      );
      if (!response.ok) return [id, { error: `HTTP ${response.status}` }];
      return [id, await response.json()];
    } catch (error) {
      if (controller.signal.aborted) return [id, timeoutError];
      return [id, { error: String((error as Error)?.message ?? error) }];
    }
  })();
  const result = Promise.race([request, timedOut]).finally(() => {
    if (timer !== undefined) clearTimeout(timer);
  });
  const done = request.then(() => undefined, () => undefined);
  return { result, done };
}

export async function GET(req: Request) {
  const url = rawRequestUrl(req);
  const raw = (url.searchParams.get("run_ids") ?? "").trim();
  const ids = raw.split(",").map((s) => s.trim()).filter(Boolean);
  // Names are repeated parameters, positional against `run_ids`. A comma is valid in an app
  // name, so splitting a joined name list can point one run at a different app's deployment.
  const pairedNames = url.searchParams.getAll("app_name");
  // Older browser tabs still send the joined form during a rolling console upgrade.
  const legacyNames = url.searchParams.get("app_names");
  const names = pairedNames.length > 0 ? pairedNames.map((s) => s.trim())
    : ids.length === 1 ? [(legacyNames ?? "").trim()] : [];

  if (ids.length === 0) {
    return NextResponse.json({ error: "missing run_ids" }, { status: 400 });
  }
  if (ids.length > MAX_IDS) {
    return NextResponse.json(
      { error: `too many run_ids (${ids.length} > ${MAX_IDS}); request them in chunks` },
      { status: 400 },
    );
  }
  if (pairedNames.length > 0 && pairedNames.length !== ids.length) {
    return NextResponse.json({ error: "app_name count must match run_ids" }, { status: 400 });
  }
  if (legacyNames !== null && ids.length > 1 && pairedNames.length === 0) {
    // A joined list cannot distinguish two app names from one name containing a comma.
    // Fail closed for an older tab rather than pairing a run with another app's deployment.
    return NextResponse.json({ error: "use one app_name parameter per run" }, { status: 400 });
  }

  const deadline = Date.now() + BATCH_BUDGET_MS;
  // Pre-fill every position: work that never starts before the deadline must still tell its
  // card "couldn't check". An omitted key would otherwise look like no deployment.
  const results: RunEntry[] = ids.map((id) => [id, { error: "deployment lookup budget exhausted" }]);
  let next = 0;
  async function worker() {
    while (next < ids.length && Date.now() < deadline) {
      const index = next++;
      const release = await acquireProcessSlot(deadline);
      if (!release) return;
      const remainingMs = deadline - Date.now();
      if (remainingMs <= 0) {
        release();
        return;
      }
      const lookup = lookupDeployment(ids[index], names[index], remainingMs);
      try {
        results[index] = await lookup.result;
      } finally {
        // Abort does not release the API connection. The next id waits until this
        // read settles so a hang cannot overlap another upstream read on the same slot.
        if (next >= ids.length || Date.now() >= deadline) {
          void lookup.done.finally(release);
          return;
        }
        await lookup.done;
        release();
      }
    }
  }
  const workers = Array.from(
    { length: Math.min(MAX_CONCURRENT_LOOKUPS, ids.length) },
    () => worker(),
  );
  // A read that ignores abort must not hold the HTTP response open until the API's
  // own 30s failure. The slot stays occupied until that read settles.
  await new Promise<void>((resolve) => {
    const timer = setTimeout(resolve, Math.max(0, deadline - Date.now()));
    void Promise.all(workers).then(() => {
      clearTimeout(timer);
      resolve();
    }, () => {
      clearTimeout(timer);
      resolve();
    });
  });

  return NextResponse.json({ runs: Object.fromEntries(results) });
}
