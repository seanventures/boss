/**
 * Extra stub routes for the people screens. Called by stub/server.ts after its core table, with
 * its `on(method, pattern, needs, handler)`. Helpers (stubOk, stubErr, stubEffective, …) are
 * imported from ./server — safe, because they are only called when a request is handled.
 *
 * SEAMS (S1): IDENTITY.md §10 lists `GET /v1/identity/api-keys · POST …/{id}/revoke` for an
 * admin; the core stub had the list, this adds the revoke. `GET /users/{id}/factors` (in core)
 * is also a seam: §10 has /me/passkeys for oneself but no admin read of another's factors.
 */
import type { StubOn } from "./server";
import { stubAudit, stubNotFound, stubOk } from "./server";

export function registerPeopleRoutes(on: StubOn): void {
  on("POST", "/v1/identity/api-keys/([^/]+)/revoke", "api_key:revoke", ({ s, m, who, now }) => {
    for (const k of [...Object.values(s.personal).flat(), ...s.sas.flatMap((x) => x.keys)]) {
      if (k.id !== m[1]) continue;
      k.status = "revoked";
      k.grace_until = null;
      stubAudit(s, now, { event_type: "api_key.revoked", what: k.id, headline: "A key was revoked" }, who);
      return stubOk({ ok: true });
    }
    return stubNotFound();
  });
}
