/**
 * J7 on the real stack: a BOSS group mapped from an IdP group showed bob — synced at his OIDC
 * sign-in — as a plain member with Remove (the API refuses it; the next sign-in would re-add him),
 * and its access roles said "granted by usr_…". A synced membership is read-only with its source
 * named, and a granter is a name or plain words, never a principal id.
 */
import { describe, expect, it } from "vitest";

import { adaptBinding, adaptGroup, grantedByWords } from "../lib/identity/adapt-people";

describe("group members and granters", () => {
  it("keeps each membership's own source", () => {
    const g = adaptGroup({ id: "grp_1", name: "Builders", source: "manual", members: [
      { principal_id: "usr_bob", display_name: "Bob", source: "oidc" },
      { principal_id: "usr_ann", display_name: "Ann", source: "manual" },
    ] });
    expect(g.members.map((m) => [m.id, m.source])).toEqual([["usr_bob", "oidc"], ["usr_ann", "manual"]]);
  });

  it("names the granter, never by id", () => {
    expect(grantedByWords(adaptBinding({ id: "b1", role_key: "operator", granted_by: "usr_x", granted_by_name: "Seed Owner" }))).toBe("Seed Owner");
    expect(grantedByWords({ granted_by: "usr_abcdefgh", granted_by_name: null })).toBe("an admin");
    expect(grantedByWords({ granted_by: "sys_worker", granted_by_name: null })).toBe("BOSS");
    expect(grantedByWords({ granted_by: null, granted_by_name: null })).toBe("BOSS");
  });

  it("the group and person pages say who granted with the helper", async () => {
    const { readFileSync } = await import("node:fs");
    for (const f of ["../app/identity/groups/[id]/group.tsx", "../app/identity/people/[id]/person.tsx"]) {
      const src = readFileSync(new URL(f, import.meta.url), "utf8");
      expect(src).not.toMatch(/granted by \$?\{b\.granted_by\b/);
      expect(src).toMatch(/grantedByWords\(b\)/);
    }
  });
});
