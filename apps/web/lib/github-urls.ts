/**
 * EVERY github.com WEB LINK THE CONSOLE BUILDS ITSELF, in one place — the twin of
 * engine/boss_engine/identity/github_urls.py.
 *
 * Links into an account's settings (an App's permissions, an installation) need the account TYPE:
 * a person's are under /settings/…, an organisation's under /organizations/<org>/settings/…. The
 * console never guesses it: those links come from the server (GET /v1/identity/github → app.links),
 * built from the type GitHub reported. What is left here does not depend on the account: the
 * signed-in person's token pages, a new OAuth app, a repository.
 *
 * tests/github-urls.test.ts checks each kind and that no other console file spells a github.com URL.
 */

export const GITHUB_WEB = "https://github.com";

export type AccountType = "User" | "Organization";

/** An account's settings tree, or null when its type is unknown (never guessed). */
export function settingsRoot(login: string | null | undefined, type: AccountType | null | undefined): string | null {
  if (type === "User") return `${GITHUB_WEB}/settings`;
  if (type === "Organization" && login) return `${GITHUB_WEB}/organizations/${encodeURIComponent(login)}/settings`;
  return null;
}

/** GitHub's new fine-grained token page, prefilled (tokens always belong to a person). */
export function newFineGrainedTokenUrl(params: Record<string, string>): string {
  return `${GITHUB_WEB}/settings/personal-access-tokens/new?${new URLSearchParams(params).toString()}`;
}

/** Register an OAuth app: under the person's settings, or an organisation's. */
export function newOAuthAppUrl(org?: string | null): string {
  const root = org ? settingsRoot(org, "Organization") : settingsRoot(null, "User");
  return `${root}/applications/new`;
}

/** A repository's page ("owner/name"). */
export function repoUrl(fullName: string): string {
  return `${GITHUB_WEB}/${fullName.replace(/^\/+|\/+$/g, "")}`;
}

/** Whether a URL is on github.com (a PR link from GitHub, say). */
export function isGitHubUrl(url: string | null | undefined): boolean {
  return (url ?? "").startsWith(`${GITHUB_WEB}/`);
}
