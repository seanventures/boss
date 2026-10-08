/**
 * AN AGENT'S GITHUB LIMIT (its Passport's `external_ceiling.github`), in words, with its rules.
 *
 * What a run may do on GitHub is the person's grant ∩ this limit ∩ what the step needs, minted as
 * a short-lived token by the BOSS GitHub App. The App never asks GitHub for more than
 * APP_PERMISSIONS, so a limit above it is a promise no token can keep: the API refuses a raise
 * past it (engine identity/passports.above_connection) and the console offers nothing above it.
 * tests/identity-passport-github-limit.test.tsx keeps this copy equal to the engine's.
 */
export type GithubLevel = "none" | "read" | "write";

/** engine/boss_engine/identity/github_setup.py APP_PERMISSIONS (metadata: read is always on). */
export const APP_PERMISSIONS: Readonly<Record<string, Exclude<GithubLevel, "none">>> = {
  contents: "write", pull_requests: "write", checks: "read", statuses: "read", metadata: "read", issues: "read", actions: "read",
};

/** The keys a limit may name, in the order the Passport shows them, with what each one is. */
export const GITHUB_KEYS: readonly { key: string; label: string; tip: string }[] = [
  { key: "contents", label: "Code", tip: "Read the repository; write pushes branches (only boss/*)" },
  { key: "pull_requests", label: "Pull requests", tip: "Read pull requests; write opens them and posts review comments" },
  { key: "issues", label: "Issues", tip: "Read issues; write opens and comments on them" },
  { key: "checks", label: "Checks", tip: "Read check runs and their results" },
  { key: "statuses", label: "Commit statuses", tip: "Read commit statuses" },
  { key: "actions", label: "Actions", tip: "Read GitHub Actions runs" },
];

const RANK: Record<GithubLevel, number> = { none: 0, read: 1, write: 2 };

/** The highest level the App can ever get for a key. */
export function appMax(key: string): GithubLevel {
  return APP_PERMISSIONS[key] ?? "none";
}

/** Whether `level` is offered for `key` now: never above the App, except a level already held. */
export function levelOffered(key: string, level: GithubLevel, current: GithubLevel): boolean {
  return RANK[level] <= RANK[appMax(key)] || level === current;
}

/** The change a save would make, in words: ["Pull requests: read → write"]. */
export function limitChanges(before: Record<string, string>, after: Record<string, string>): string[] {
  const out: string[] = [];
  for (const { key, label } of GITHUB_KEYS) {
    const a = (before[key] ?? "none") as GithubLevel;
    const b = (after[key] ?? "none") as GithubLevel;
    if (a !== b) out.push(`${label}: ${a === "none" ? "no access" : a} → ${b === "none" ? "no access" : b}`);
  }
  return out;
}

/** The body PATCH /v1/identity/agents/{id}/passport takes: "none" keys dropped, push_refs kept. */
export function limitBody(before: Record<string, string>, levels: Record<string, string>): { github: Record<string, string> } {
  const github: Record<string, string> = {};
  for (const [k, v] of Object.entries(before)) if (!GITHUB_KEYS.some((g) => g.key === k)) github[k] = v;
  for (const { key } of GITHUB_KEYS) if (levels[key] && levels[key] !== "none") github[key] = levels[key];
  return { github };
}
