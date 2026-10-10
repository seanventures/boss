/**
 * Repo and models' one way to the API. Every mutation answers with the whole `SetupView`, and
 * the board REPLACES its state with it — nothing is patched or judged in the browser.
 *
 * One press, one effect: each write goes through `apiWrite` with a press scope, so a double
 * click sends one request; its Idempotency-Key travels on to the API (route.ts).
 */
import { apiWrite } from "./api-write";
import { askConsent, consentFromBody } from "./identity/consent";
import type { SetupRefusal, SetupView, TokenFound } from "./setup-types";

export class SetupFailure extends Error {
  status: number;
  refusal: SetupRefusal | null;

  constructor(status: number, message: string, refusal: SetupRefusal | null) {
    super(message);
    this.status = status;
    this.refusal = refusal;
  }
}

async function read<T>(r: Response): Promise<T> {
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const refusal = (data as { refusal?: SetupRefusal }).refusal ?? null;
    throw new SetupFailure(r.status, refusal?.headline
      || String((data as { error?: string }).error || `HTTP ${r.status}`), refusal);
  }
  return data as T;
}

export async function fetchSetup(projectId: string): Promise<SetupView> {
  const r = await fetch(`/api/setup?project_id=${encodeURIComponent(projectId)}`,
                        { cache: "no-store" });
  return read<SetupView>(r);
}

export async function fetchRepos(projectId: string, connectionId: string): Promise<TokenFound> {
  const r = await fetch(`/api/setup?project_id=${encodeURIComponent(projectId)}`
                        + `&repos=${encodeURIComponent(connectionId)}`, { cache: "no-store" });
  return read<TokenFound>(r);
}

export async function fetchModelList(projectId: string, provider: string,
                                     sourceId?: string): Promise<string[]> {
  const r = await fetch(`/api/setup?project_id=${encodeURIComponent(projectId)}`
                        + `&catalogue=${encodeURIComponent(provider)}`
                        + (sourceId ? `&source_id=${encodeURIComponent(sourceId)}` : ""),
                        { cache: "no-store" });
  return (await read<{ models: string[] }>(r)).models ?? [];
}

/** "Builds may change CI workflows" for one bound repository (services/ci_gate.py). */
export type CiWorkflows = {
  binding_id: string; repository: string; label: string; help: string; enabled: boolean;
  requires: string; permissions_url?: string | null; installation_url?: string | null;
  note?: string;
  /** What GitHub last said about `workflows` on the installation — never what BOSS asked for. */
  workflows?: { state: "accepted" | "waiting" | "app_lacks" | "unknown"; fix_url: string | null;
                checked_at?: number | null };
};

export async function fetchCiWorkflows(projectId: string, bindingId: string,
                                       recheck = false): Promise<CiWorkflows> {
  const r = await fetch(`/api/setup?project_id=${encodeURIComponent(projectId)}`
                        + `&ci_workflows=${encodeURIComponent(bindingId)}${recheck ? "&recheck=1" : ""}`,
                        { cache: "no-store" });
  return read<CiWorkflows>(r);
}

/** A write. `label` is the button's own words (the press registry quotes it on a refusal);
 *  a background write (an automatic re-check) passes `background` and takes no press scope. */
export async function setupWrite<T = SetupView>(
  projectId: string, op: string, body: Record<string, unknown>,
  { label, background = false }: { label: string; background?: boolean },
): Promise<T> {
  const send = (fresh: boolean) => apiWrite("/api/setup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // No per-call key in the body: two clicks of one press must be byte-identical so the press
    // registry joins them. apiWrite sends the Idempotency-Key header; the route forwards it.
    body: JSON.stringify({ op, project_id: projectId, ...body }),
  }, background || fresh ? {} : { press: { scope: `setup:${projectId}:${op}`, label } });
  const r = await send(false);
  // NEEDS YOUR CONSENT (409 consent_required: Try it starts a build that acts for you): ask, and
  // once granted re-send the same write with a fresh key. It used to end on "Not started".
  if (r.status === 409 && !background) {
    const asked = consentFromBody(await r.clone().json().catch(() => null));
    if (asked) {
      if (await askConsent(asked)) return read<T>(await send(true));
      throw new SetupFailure(409, "Not started: consent was not given", null);
    }
  }
  return read<T>(r);
}
