/**
 * EVERY IDENTITY REFUSAL HAS WORDS (operator, 2026-10-24). "Let an agent act for me" posted
 * projects ["NorthPeak","Bank"]; the API refused it 403 exceeds_your_access, naming the project and
 * the permissions, and the sheet said only "That didn't work" — errorSentence had no words for the
 * code, and each screen's own fallback dropped the body. Now:
 *   - every code the identity API's grants, authentication and agents router can refuse with has an
 *     entry in REFUSAL_WORDS (read from the engine and router source, so a new code fails here);
 *   - the words read the refusal's body (the project, the permissions, the repo);
 *   - an unworded code still says which refusal it was (the API's sentence, or the code humanised);
 *   - the bare "That didn't work" lives in ONE function (refusalSentence), which every identity
 *     screen's fallback ends in.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { IdentityError, REFUSAL_WORDS, errorSentence, refusalSentence } from "../lib/identity/api";
import { codeOnly } from "./_helpers/source";

const REPO = join(process.cwd(), "..", "..");
const read = (p: string) => readFileSync(join(REPO, p), "utf8");
const codes = (src: string, re: RegExp) => [...src.matchAll(re)].map((m) => m[1]);

const RAISED = [...new Set([
  ...codes(read("engine/boss_engine/identity/grants.py"), /GrantRefused\(\s*"([a-z_]+)"/g),
  ...codes(read("engine/boss_engine/identity/authn.py"), /AuthnError\(\s*"([a-z_]+)"/g),
  ...codes(read("apps/api/app/routers/identity_agents.py"), /AuthnError\(\s*"([a-z_]+)"/g),
  ...codes(read("apps/api/app/routers/identity_agents.py"), /_err\(\s*"([a-z_]+)"/g),
])];

describe("every identity refusal has words", () => {
  it("finds the codes it checks", () => {
    expect(RAISED).toEqual(expect.arrayContaining(["exceeds_your_access", "no_projects", "bad_repos", "github_access_lacking", "people_only", "unauthenticated"]));
  });

  it("has an entry for every code the grants, authentication and agents router raise", () => {
    const missing = RAISED.filter((c) => c !== "forbidden" && !(c in REFUSAL_WORDS));
    expect(missing, "add words to REFUSAL_WORDS in lib/identity/api.ts").toEqual([]);
    for (const c of RAISED) expect(errorSentence(403, c)).not.toMatch(/didn't work|_/);
  });

  it("names the project and the permissions you lack", () => {
    const e = new IdentityError(403, "exceeds_your_access", { body: { error: "exceeds_your_access", project: "northpeak-bank", permissions: ["github:sync", "code_task:steer"] } });
    expect(e.message).toContain("northpeak-bank");
    expect(e.message).toContain("Steer builds");
    expect(e.message).not.toMatch(/didn't work|github:sync|exceeds/);
    expect(errorSentence(403, "exceeds_your_access", undefined, { project: "prj_1", project_name: "NorthPeak Bank", permissions: ["code_task:steer"] }))
      .toContain("in NorthPeak Bank");
  });

  it("says what to change for the grant form's own mistakes", () => {
    expect(new IdentityError(422, "no_projects", { body: { error: "no_projects" } }).message).toBe("Pick at least one project");
    expect(new IdentityError(422, "bad_repos", { body: { error: "bad_repos" } }).message).toContain("owner/name");
    expect(new IdentityError(403, "people_only", { body: { error: "people_only" } }).message).toContain("person");
    expect(new IdentityError(403, "github_access_lacking", { body: { error: "github_access_lacking", repo: "paramg/northpeak-fineract-b" } }).message)
      .toContain("paramg/northpeak-fineract-b");
    expect(new IdentityError(401, "step_up_cancelled").message).toContain("confirm it's you");
  });

  it("an unworded refusal still says which one it was", () => {
    expect(errorSentence(409, "some_new_rule")).toBe("Some new rule");
    expect(errorSentence(403, "x_y", undefined, { detail: "Only Owners may do this." })).toBe("Only Owners may do this.");
    expect(refusalSentence(new IdentityError(403, "policy_refused", { body: { error: "policy_refused", detail: "Longer than your org allows." } })))
      .toBe("Longer than your org allows.");
    expect(refusalSentence(new Error("socket hang up"))).toBe("That didn't work · socket hang up");
  });

  it("keeps the bare words in refusalSentence alone across the identity screens", () => {
    const roots = ["app/identity", "app/account", "components/identity", "lib/identity"];
    const files: string[] = [];
    const walk = (d: string) => {
      for (const n of readdirSync(d)) {
        const p = join(d, n);
        if (statSync(p).isDirectory()) { if (n !== "stub") walk(p); } else if (/\.tsx?$/.test(n)) files.push(p);
      }
    };
    roots.forEach(walk);
    const hits = files.filter((f) => codeOnly(f).includes("That didn't work"));
    expect(hits).toEqual(["lib/identity/api.ts"]);
    const api = codeOnly("lib/identity/api.ts");
    const fn = api.slice(api.indexOf("export function refusalSentence"));
    expect(api.split("That didn't work").length - 1).toBe(1);
    expect(fn.slice(0, fn.indexOf("\n}"))).toContain("That didn't work");
  });
});
