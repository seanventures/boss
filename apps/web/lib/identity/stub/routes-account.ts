/**
 * Stub routes for service accounts, Passports and My account extras (edit own name and avatar,
 * personal key rotate, GitHub disconnect, assign a sponsor, delete a service account). Dev-only
 * double of apps/api (identity_me.py, identity_people.py, identity_agents.py); registered by
 * stub/server.ts after its core table.
 */
import type { ApiKey, IssuedSecret } from "../types";
import type { StubOn } from "./server";
import { stubAudit, stubErr, stubNotFound, stubOk } from "./server";

const profiles = new Map<string, string | null>();
const rand = () => Math.random().toString(36).slice(2, 10);

export function registerAccountRoutes(on: StubOn): void {
  on("GET", "/v1/identity/me/profile", "self", ({ s, who }) => {
    const p = s.people.find((x) => x.id === who);
    if (!p) return stubErr(403, "people_only");
    return stubOk({ display_name: p.display_name, email: p.email, avatar_seed: profiles.get(who) ?? null });
  });

  on("PATCH", "/v1/identity/me", "self", ({ s, who, req, now }) => {
    const p = s.people.find((x) => x.id === who);
    if (!p) return stubErr(403, "people_only");
    const b = (req.body ?? {}) as Record<string, unknown>;
    if (Object.keys(b).some((k) => k !== "display_name" && k !== "avatar_seed")) return stubErr(422, "invalid_fields");
    if ("display_name" in b) {
      const name = String(b.display_name ?? "").trim();
      if (!name) return stubErr(422, "name_required");
      p.display_name = name.slice(0, 200);
    }
    if ("avatar_seed" in b) {
      const seed = b.avatar_seed === null ? null : String(b.avatar_seed);
      if (seed !== null && !/^[0-7]$/.test(seed)) return stubErr(422, "invalid_fields");
      profiles.set(who, seed);
      if (seed !== null) p.hue = Number(seed);
    }
    stubAudit(s, now, { event_type: "user.updated", what: p.display_name, headline: `${p.display_name} updated their profile` }, who);
    return stubOk({ display_name: p.display_name, email: p.email, avatar_seed: profiles.get(who) ?? null });
  });

  on("POST", "/v1/identity/me/api-keys/([^/]+)/rotate", "api_key:manage_own", ({ s, who, m, req, now }) => {
    const old = (s.personal[who] ?? []).find((k) => k.id === m[1] && k.status === "active");
    if (!old) return stubNotFound();
    const grace = String((req.body as { grace?: unknown } | null)?.grace ?? "1h");
    const hours = ({ "0": 0, "1h": 1, "24h": 24, "7d": 168 } as Record<string, number>)[grace];
    if (hours === undefined) return stubErr(422, "invalid_fields");
    const key: ApiKey = { ...old, id: `key_${rand().slice(0, 6)}`, created_at: new Date(now).toISOString(), last_used_at: null,
      last_used_ip: null, last_used_client: null, rotated_from: old.id, status: "active", grace_until: null };
    old.status = hours ? "grace" : "revoked";
    old.grace_until = hours ? new Date(now + hours * 3_600_000).toISOString() : null;
    s.personal[who] = [key, ...(s.personal[who] ?? [])];
    stubAudit(s, now, { event_type: "api_key.rotated", what: key.name, headline: "A personal key was rotated" }, who);
    const issued: IssuedSecret = { key, secret: `boss_pat_${rand()}${rand()}${rand()}${rand()}${rand()}`, replaces: { id: old.id, stops_at: old.grace_until } };
    return stubOk(issued, 201);
  });

  on("DELETE", "/v1/identity/me/connections/([^/]+)", "self", ({ m }) => stubOk({ revoked: m[1] }));

  // The real API answers PATCH /v1/identity/agents/{id} and …/passport alike; the console sets
  // the sponsor through this one.
  on("PATCH", "/v1/identity/agents/([^/]+)", "agent_passport:manage", ({ s, m, req, who, now }) => {
    const a = s.passports.find((x) => x.id === m[1]);
    if (!a) return stubNotFound();
    const b = (req.body ?? {}) as { sponsor?: unknown };
    if (Object.keys(b).some((k) => k !== "sponsor")) return stubErr(422, "unknown_fields");
    const p = s.people.find((x) => x.id === b.sponsor && x.status === "active");
    if (!p) return stubErr(422, "invalid_sponsor");
    a.sponsor = { id: p.id, name: p.display_name, hue: p.hue };
    a.lineage.push({ at: new Date(now).toISOString(), event: "sponsor_changed", detail: p.display_name });
    stubAudit(s, now, { event_type: "agent.passport.updated", what: a.name, headline: `${p.display_name} now sponsors ${a.name}` }, who);
    return stubOk(a);
  });

  on("DELETE", "/v1/identity/service-accounts/([^/]+)", "service_account:manage", ({ s, m, who, now }) => {
    const sa = s.sas.find((x) => x.id === m[1]);
    if (!sa) return stubNotFound();
    sa.status = "revoked";
    for (const k of sa.keys) { k.status = "revoked"; k.grace_until = null; }
    stubAudit(s, now, { event_type: "service_account.deleted", what: sa.name, headline: `${sa.name} was deleted` }, who);
    return stubOk({ status: "revoked" });
  });

  on("POST", "/v1/identity/service-accounts/([^/]+)/enable", "service_account:manage", ({ s, m }) => {
    const sa = s.sas.find((x) => x.id === m[1]);
    if (!sa) return stubNotFound();
    sa.status = "active";
    return stubOk({ status: "active" });
  });
}
