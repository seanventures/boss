/**
 * SERVER-SIDE IDENTITY READS, for layouts and pages (never imported by a client component).
 *
 * Every Identity and My-account page is guarded HERE, before it renders: the layout reads
 * /v1/identity/me/permissions with the person's own cookie and renders <AccessDenied> when the
 * page's permission is missing, so a page never loads and then 403s. The API re-checks every
 * call the page then makes; this only decides what to draw.
 */
import { API_URL } from "../client";
import type { MePermissionsResponse, MeResponse } from "./contract";
import { ME_PATH, ME_PERMISSIONS_PATH } from "./contract";
import { withForwardedIdentity } from "./forward";
import { answerFromStub } from "./server-fetch";
import { identityStubEnabled } from "./stub/enabled";
import { refusalCode } from "./refusal-code";

export type ServerRead<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; code: string; permission: string | null };

export async function serverGet<T>(path: string): Promise<ServerRead<T>> {
  try {
    let res: Response;
    if (identityStubEnabled()) {
      res = await answerFromStub("GET", `http://stub${path}`, await withForwardedIdentity("GET", undefined), null);
    } else if (!API_URL) {
      return { ok: false, status: 503, code: "identity_unavailable", permission: null };
    } else {
      res = await fetch(`${API_URL}${path}`, { cache: "no-store", headers: { Accept: "application/json" } });
    }
    const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
    if (!res.ok) {
      return { ok: false, status: res.status, code: refusalCode(res.status, data),
        permission: typeof data?.permission === "string" ? data.permission : null };
    }
    return { ok: true, data: data as T };
  } catch {
    return { ok: false, status: 503, code: "identity_unavailable", permission: null };
  }
}

export type IdentityBoot = {
  me: MeResponse | null;
  org: MePermissionsResponse | null;
  project: MePermissionsResponse | null;
  /** Why there is no answer: "unauthenticated" sends the visitor to sign in. */
  problem: string | null;
};

/** Who is signed in and what they hold, at the org and (when given) one project. */
export async function loadIdentity(projectId?: string | null): Promise<IdentityBoot> {
  const [me, org, project] = await Promise.all([
    serverGet<MeResponse>(ME_PATH),
    serverGet<MePermissionsResponse>(ME_PERMISSIONS_PATH),
    projectId ? serverGet<MePermissionsResponse>(`${ME_PERMISSIONS_PATH}?project_id=${encodeURIComponent(projectId)}`) : Promise.resolve(null),
  ]);
  const problem = !me.ok ? me.code : !org.ok ? org.code : null;
  return {
    me: me.ok ? me.data : null,
    org: org.ok ? org.data : null,
    project: project && project.ok ? project.data : null,
    problem,
  };
}
