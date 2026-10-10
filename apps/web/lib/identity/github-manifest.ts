/**
 * THE GITHUB APP MANIFEST HAND-OFF (round 2, R2-WEB). GitHub's manifest flow is a form POST to
 * GitHub, and the console's CSP is `form-action 'self'` everywhere, so Setup › GitHub access ›
 * "Create the App on GitHub" did nothing. The form is not posted from Setup any more: Setup keeps
 * the API's {post_url, manifest} for this tab and opens HANDOFF_PATH, the one console page whose
 * CSP also allows GitHub as a form target (middleware.ts). That page posts the form, and only to a
 * GitHub web origin.
 *
 * The GitHub web origin is https://github.com, or BOSS_GITHUB_WEB_URL (a GitHub Enterprise host),
 * the same variable the API builds post_url from (engine/boss_engine/identity/github_setup.py).
 */
import { GITHUB_WEB } from "../github-urls";

export const HANDOFF_PATH = "/github-app";
export const HANDOFF_KEY = "boss:github-app-manifest";

export function githubWebOrigins(env: string | undefined = process.env.BOSS_GITHUB_WEB_URL): string[] {
  const out = [GITHUB_WEB];
  try {
    if (env && env.trim()) {
      const u = new URL(env.trim());
      if ((u.protocol === "https:" || u.protocol === "http:") && !out.includes(u.origin)) out.push(u.origin);
    }
  } catch { /* not a URL: github.com only */ }
  return out;
}

export type ManifestHandoff = { post_url: string; manifest: unknown };

/** The hand-off when its post_url is a GitHub web URL; null otherwise. */
export function readHandoff(raw: string | null, origins: string[]): ManifestHandoff | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<ManifestHandoff>;
    if (typeof value.post_url !== "string" || value.manifest == null) return null;
    const url = new URL(value.post_url);
    if (!origins.includes(url.origin)) return null;
    return { post_url: url.toString(), manifest: value.manifest };
  } catch {
    return null;
  }
}
