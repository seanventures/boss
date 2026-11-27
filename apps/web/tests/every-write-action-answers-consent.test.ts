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
 * consent_required. An action that reached the API must answer with the consent request. One that
 * stops on its own input check first sends nothing and is not judged. Reads never ask for consent.
 */
import { afterAll, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn(), revalidateTag: vi.fn() }));
vi.mock("next/headers", () => ({ headers: async () => new Headers(), cookies: async () => ({ get: () => undefined, getAll: () => [] }) }));
vi.mock("next/navigation", () => ({ redirect: vi.fn(), notFound: vi.fn() }));

const CONSENT = { error: "consent_required", task: "Retry the build", task_run_id: "pending:crun_x", agents: [],
                  permissions: ["code_task:run"], projects: ["p"] };
const READ = /^(get|list|load|fetch|search|read)[A-Z]/;
/** Every field any action reads, so as many as possible get past their own input checks. */
const FIELDS = ["project_id", "run_id", "thread_id", "message", "actor", "directive", "name", "text", "decision_id",
  "task_id", "id", "workflow_key", "prompt", "title", "comment", "answer", "reason", "option", "kind", "key", "value",
  "url", "repo", "branch", "target_id", "host_id", "escalation_id", "goal", "command", "feedback", "choice",
  "extends_run_id", "preview_id", "deployment_id", "app_name", "skill_key", "agent_key", "role_key", "email", "query",
  "note", "body", "label", "status", "mode", "path", "content", "scope", "permission", "severity", "finding_id",
  "meeting_run_id", "outcome_id"];

const fetchMock = vi.fn(async () => new Response(JSON.stringify(CONSENT), { status: 409, headers: { "content-type": "application/json" } }));
vi.stubGlobal("fetch", fetchMock);
afterAll(() => { vi.unstubAllGlobals(); });

const asksConsent = (out: unknown) => !!out && typeof out === "object"
  && (out as { ok?: unknown }).ok === false
  && (out as { data?: { consent?: { error?: string } } }).data?.consent?.error === "consent_required";

type Call = (...args: unknown[]) => Promise<unknown>;
async function press(fn: Call): Promise<{ reached: boolean; answers: unknown[] }> {
  const form = new FormData();
  for (const k of FIELDS) form.set(k, "x1");
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
    const swallowed: string[] = [];
    for (const [name, fn] of actions) {
      const { reached: hit, answers } = await press(fn as Call);
      if (!hit) continue;
      reached.push(name);
      if (!answers.some(asksConsent)) swallowed.push(`${name}: ${JSON.stringify(answers.at(-1)).slice(0, 120)}`);
    }
    expect(swallowed).toEqual([]);
    // Not vacuous: most write actions were driven all the way to the API.
    expect(reached.length).toBeGreaterThanOrEqual(70);
    for (const name of ["confirmCodeTaskFeedbackAction", "submitCodeTaskFeedbackAction", "redeployAction",
                        "executeDeploymentAction", "updateTargetAction", "reactivateTargetAction", "createPreviewFixtureAction",
                        "shepherdSweepAction", "shepherdAnswerAction", "previewGateAction", "runDeploymentsAction"]) {
      expect(reached, name).toContain(name);
    }
  }, 120_000);
});
