/**
 * ONE place builds github.com links in the console (lib/github-urls.ts, the twin of
 * engine/boss_engine/identity/github_urls.py). Account-settings links need the account TYPE — a
 * person's are /settings/…, an organisation's /organizations/<org>/settings/… — and the console
 * never guesses it: those come from the server. No other console file spells a github.com URL.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

import { isGitHubUrl, newFineGrainedTokenUrl, newOAuthAppUrl, repoUrl, settingsRoot } from "../lib/github-urls";

const ROOT = join(__dirname, "..");

describe("github-urls", () => {
  it("puts a person's settings under /settings and an organisation's under /organizations/<org>", () => {
    expect(settingsRoot("paramg", "User")).toBe("https://github.com/settings");
    expect(settingsRoot("acme", "Organization")).toBe("https://github.com/organizations/acme/settings");
    expect(settingsRoot("paramg", null)).toBeNull();
    expect(settingsRoot(null, "Organization")).toBeNull();
    expect(settingsRoot("a/b", "Organization")).toBe("https://github.com/organizations/a%2Fb/settings");
  });

  it("builds the token, OAuth-app and repository pages", () => {
    expect(newFineGrainedTokenUrl({ name: "BOSS (x)", contents: "write" }))
      .toBe("https://github.com/settings/personal-access-tokens/new?name=BOSS+%28x%29&contents=write");
    expect(newOAuthAppUrl()).toBe("https://github.com/settings/applications/new");
    expect(newOAuthAppUrl("acme")).toBe("https://github.com/organizations/acme/settings/applications/new");
    expect(repoUrl("paramg/boss-sandbox")).toBe("https://github.com/paramg/boss-sandbox");
    expect(isGitHubUrl("https://github.com/o/r/pull/1")).toBe(true);
    expect(isGitHubUrl("https://github.company.test/o")).toBe(false);
    expect(isGitHubUrl(null)).toBe(false);
  });

  it("no other console file spells a github.com URL", () => {
    const allowed = new Set(["lib/github-urls.ts"]);
    const skip = /(^|\/)(node_modules|\.next|tests|stub)(\/|$)/;
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const full = join(dir, name);
        const rel = relative(ROOT, full);
        if (skip.test(rel)) continue;
        if (statSync(full).isDirectory()) { walk(full); continue; }
        if (!/\.(ts|tsx)$/.test(name) || allowed.has(rel)) continue;
        readFileSync(full, "utf8").split("\n").forEach((line, i) => {
          // Prose in a comment may name GitHub; code may not build a link to it.
          const code = line.replace(/\/\/.*$/, "").replace(/^\s*\*.*$/, "");
          if (/https:\/\/github\.com(\/|["'`])/.test(code)) offenders.push(`${rel}:${i + 1}: ${line.trim().slice(0, 100)}`);
        });
      }
    };
    for (const top of ["app", "lib", "components"]) walk(join(ROOT, top));
    expect(offenders, "build github.com links with lib/github-urls.ts").toEqual([]);
  });
});
