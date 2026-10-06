/**
 * Extra stub routes for the agents, delegation and audit screens. Called by stub/server.ts after
 * its core table, with its `on(method, pattern, needs, handler)`. Helpers are imported from
 * ./server — safe, because they are only called when a request is handled.
 *
 * SEAMS (S3): IDENTITY.md §10 lists revoke but not extend or narrow for a grant. The console
 * offers both on the delegation view (§9.6 "Extend · Narrow"); these are the shapes it sends:
 *   POST /v1/identity/delegations/{id}/extend {days}          only the person who granted it
 *   POST /v1/identity/delegations/{id}/narrow {permissions[]} the new, smaller set
 */
import { isPermission } from "../permissions.gen";
import type { StubOn } from "./server";
import { stubAudit, stubErr, stubNotFound, stubOk } from "./server";

export function registerAgentRoutes(on: StubOn): void {
  on("POST", "/v1/identity/delegations/([^/]+)/extend", "delegation:grant", ({ s, m, req, who, now }) => {
    const d = s.delegations.find((x) => x.id === m[1]);
    if (!d) return stubNotFound();
    if (d.user.id !== who) return stubErr(403, "forbidden", { permission: "delegation:grant" });
    if (d.status !== "active") return stubErr(409, "revoked_is_terminal");
    const days = Math.min(90, Math.max(1, Number((req.body as { days?: number } | null)?.days) || 30));
    d.expires_at = new Date(now + days * 86_400_000).toISOString();
    stubAudit(s, now, { event_type: "delegation.extended", what: `${d.id} · ${days} days`, headline: "A grant was extended", grant_id: d.id }, who);
    return stubOk(d);
  });
  on("POST", "/v1/identity/delegations/([^/]+)/narrow", "delegation:revoke_any", ({ s, m, req, who, now }) => {
    const d = s.delegations.find((x) => x.id === m[1]);
    if (!d) return stubNotFound();
    if (d.status !== "active") return stubErr(409, "revoked_is_terminal");
    const asked = ((req.body as { permissions?: string[] } | null)?.permissions ?? []).filter(isPermission);
    const widened = asked.find((p) => !d.permissions.includes(p));
    if (widened) return stubErr(422, "narrowing_only", { permission: widened });
    const dropped = d.permissions.filter((p) => !asked.includes(p));
    d.permissions = asked;
    stubAudit(s, now, { event_type: "delegation.narrowed", what: `${d.id} · −${dropped.length}`, headline: "A grant was narrowed", grant_id: d.id }, who);
    return stubOk(d);
  });
}
