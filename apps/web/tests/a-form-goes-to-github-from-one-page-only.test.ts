/**
 * A FORM GOES TO GITHUB FROM ONE PAGE ONLY (round 2, R2-WEB).
 *
 * GitHub's App manifest flow is a form POST to github.com, and the console's CSP is
 * `form-action 'self'`, so "Create the App on GitHub" did nothing. Setup now hands the manifest
 * to /github-app, whose CSP alone adds the GitHub web origin; every other page keeps 'self' only,
 * and the hand-off posts only to a GitHub web URL.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { middleware } from "../middleware";
import { githubWebOrigins, HANDOFF_PATH, readHandoff } from "../lib/identity/github-manifest";

const SESSION = "__Host-boss_session=bss_a";
const page = (path: string) => (vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example"), new NextRequest(`https://boss.example${path}`, { headers: { cookie: SESSION, host: "boss.example" } }));
const formAction = (csp: string) => /form-action ([^;]*)/.exec(csp)?.[1] ?? "";

describe("the form-action directive", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("the hand-off page may post to GitHub", async () => {
    const r = await middleware(page(HANDOFF_PATH));
    expect(formAction(r.headers.get("content-security-policy") ?? "")).toBe("'self' https://github.com");
  });

  it("a GitHub Enterprise host named by BOSS_GITHUB_WEB_URL is allowed there too", async () => {
    vi.stubEnv("BOSS_GITHUB_WEB_URL", "https://ghe.acme.dev/");
    const r = await middleware(page(HANDOFF_PATH));
    expect(formAction(r.headers.get("content-security-policy") ?? "")).toBe("'self' https://github.com https://ghe.acme.dev");
  });

  it.each(["/", "/identity", "/account", "/v2/settings", "/github-app/x", "/login"])("%s keeps form-action 'self'", async (p) => {
    const r = await middleware(page(p));
    const csp = r.headers.get("content-security-policy") ?? "";
    expect(csp, p).toContain("form-action");
    expect(formAction(csp)).toBe("'self'");
  });
});

describe("the hand-off", () => {
  const origins = githubWebOrigins(undefined);
  it("posts only to a GitHub web URL", () => {
    const ok = readHandoff(JSON.stringify({ post_url: "https://github.com/organizations/acme/settings/apps/new?state=s", manifest: { name: "BOSS" } }), origins);
    expect(ok?.post_url).toBe("https://github.com/organizations/acme/settings/apps/new?state=s");
    for (const post_url of ["https://evil.example/x", "javascript:alert(1)", "https://github.com.evil.example/", "/v1/x"]) {
      expect(readHandoff(JSON.stringify({ post_url, manifest: {} }), origins), post_url).toBeNull();
    }
    expect(readHandoff(null, origins)).toBeNull();
    expect(readHandoff("{not json", origins)).toBeNull();
  });

  it("Setup no longer submits a form itself; it opens the hand-off page", () => {
    const src = readFileSync(join(process.cwd(), "app/setup/github-access-row.tsx"), "utf8");
    expect(src).not.toMatch(/\.submit\(\)/);
    expect(src).toMatch(/window\.location\.assign\(HANDOFF_PATH\)/);
  });
});
