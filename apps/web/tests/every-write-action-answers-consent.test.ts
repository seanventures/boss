/**
 * NEEDS YOUR CONSENT IS A QUESTION, NOT A FAILURE, from every server action (behavioural).
 *
 * `act()` and callAction turn a 409 consent_required into `{ok: false, data: {consent}}`, and the
 * console opens the consent sheet and sends again. A write action with its own error handling
 * swallowed it: a thrown BossApiError into `error`, and a Response (apiWrite and fetch never throw
 * on a 409) into "409" or the raw body. Change it's Start, Redeploy, the deploy-target actions and
 * the Shepherd's answers all did (enterprise validation, Change it sheet).
 *
 * Here every exported *Action is called while the API answers EVERY request with 409
 * consent_required, with every field it reads filled in, and must answer with the consent request.
 * An action that still stops on its own input check (sends nothing) must be on NOT_REACHED with the
 * reason, so a new one fails here instead of going unjudged. Reads never ask for consent.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn(), revalidateTag: vi.fn() }));
vi.mock("next/headers", () => ({ headers: async () => new Headers(), cookies: async () => ({ get: () => undefined, getAll: () => [] }) }));
vi.mock("next/navigation", () => ({ redirect: vi.fn(), notFound: vi.fn() }));

const CONSENT = { error: "consent_required", task: "Retry the build", task_run_id: "pending:crun_x", agents: [],
                  permissions: ["code_task:run"], projects: ["p"] };
const READ = /^(get|list|load|fetch|search|read)[A-Z]/;
/** Every field any action reads (scanned from app/actions.ts), filled so as many actions as
 *  possible get past their own input checks: numbers for amounts and counts, JSON for *_json. */
const SRC = readFileSync(join(__dirname, "../app/actions.ts"), "utf8");
const FIELDS = [...new Set([
  ...[...SRC.matchAll(/formData\.get(?:All)?\("([a-z_0-9]+)"\)/g)].map((m) => m[1]),
  ...[...SRC.matchAll(/\(formData, "([a-z_0-9]+)"/g)].map((m) => m[1]),
  "credential_ref__api_key",          // a replacement SecretRef slot (parseProviderCredentialRefs)
])];
const NUMBER = /(^approve$|^reject$|^abstain$|_value|_usd_micros|_minutes|_workers|_depth|_hour|limit|quorum|depth|port|max_|min_|outcome_index|required_approvals|tier_)/;
/** Fields an action checks against a fixed set of words. */
const CHOSEN: Record<string, string> = { auth_method: "personal_access_token", role: "primary", operation: "enable",
  backend: "local_runner", secret_value: "x1-long-enough-secret", action: "approve", remediation_action: "retry" };
function valueOf(field: string): string {
  if (CHOSEN[field]) return CHOSEN[field];
  if (field.endsWith("_json")) return /operations|points|checks|resources/.test(field) ? "[{}]" : "{}";
  if (NUMBER.test(field)) return "1";
  return "x1";
}

const fetchMock = vi.fn(async () => new Response(JSON.stringify(CONSENT), { status: 409, headers: { "content-type": "application/json" } }));
vi.stubGlobal("fetch", fetchMock);
afterAll(() => { vi.unstubAllGlobals(); });

const asksConsent = (out: unknown) => !!out && typeof out === "object"
  && (out as { ok?: unknown }).ok === false
  && (out as { data?: { consent?: { error?: string } } }).data?.consent?.error === "consent_required";

/** Write actions this test cannot drive to the API, each with why. Reviewed; keep it short. */
const NOT_REACHED: Record<string, string> = {};

type Call = (...args: unknown[]) => Promise<unknown>;
async function press(fn: Call): Promise<{ reached: boolean; answers: unknown[] }> {
  const form = new FormData();
  for (const k of FIELDS) form.set(k, valueOf(k));
  const answers: unknown[] = [];
  let reached = false;
  // A form action ((prev, form) or (form)), then a plain one (ids as strings).
  for (const args of [fn.length >= 2 ? [null, form] : [form], ["x1", "x1", "x1"]]) {
    fetchMock.mockClear();
    let out: unknown;
    try { out = await fn(...args); } catch (e) { out = { threw: String(e) }; }
    if (fetchMock.mock.calls.length) { reached = true; answers.push(out); }
    if (asksConsent(out)) break;
  }
  return { reached, answers };
}

describe("every write action answers a consent request, not an error", () => {
  it("each action that reached the API answers with the consent request", async () => {
    const mod = (await import("../app/actions")) as Record<string, unknown>;
    const actions = Object.entries(mod).filter(([name, fn]) => typeof fn === "function" && /Action$/.test(name) && !READ.test(name));
    const reached: string[] = [];
    const unreached: string[] = [];
    const swallowed: string[] = [];
    for (const [name, fn] of actions) {
      const { reached: hit, answers } = await press(fn as Call);
      if (!hit) { unreached.push(name); continue; }
      reached.push(name);
      if (!answers.some(asksConsent)) swallowed.push(`${name}: ${JSON.stringify(answers.at(-1)).slice(0, 120)}`);
    }
    expect(swallowed).toEqual([]);
    // Every write action was driven to the API, or is on the reviewed list with its reason.
    expect(unreached.sort()).toEqual(Object.keys(NOT_REACHED).sort());
    expect(reached.length).toBeGreaterThanOrEqual(140);
    for (const name of ["confirmCodeTaskFeedbackAction", "submitCodeTaskFeedbackAction", "redeployAction",
                        "executeDeploymentAction", "updateTargetAction", "reactivateTargetAction", "createPreviewFixtureAction",
                        "shepherdSweepAction", "shepherdAnswerAction", "previewGateAction", "runDeploymentsAction"]) {
      expect(reached, name).toContain(name);
    }
  }, 120_000);
});
