/**
 * ADD A GITHUB ACCOUNT (BOSSAPP-945): the console's side of /v1/identity/github/accounts/*
 * (engine: identity/github_accounts.py). Shapes, the calls, and the pure rules the sheet draws
 * from: the steps for a path, how often to ask GitHub again, the token page's link.
 */
import { idFetch, idGet, idPost, IdentityError } from "./identity/api";
import { newFineGrainedTokenUrl } from "./github-urls";

export type AccountPath = "pick" | "make_public" | "install" | "no_app";

export type FoundAccount = {
  login: string; type: "User" | "Organization" | null; id: string; name?: string | null;
  avatar_url?: string | null; html_url?: string | null; public_repos?: number | null;
};

export type AccountApp = {
  slug: string; owner_login: string | null; owner_type: string | null; public: boolean | null;
  page_url: string | null; settings_url: string | null; install_url?: string | null; is_owner?: boolean;
  /** The install page behind GitHub's own sign-in (github.com/login?return_to=…): a personal
   *  account's install page only answers to that very person's session (a wrong one is a 404). */
  signin_install_url?: string | null;
};

export type AccountPlan = {
  path: AccountPath;
  app: AccountApp | null;
  installation: { installation_id: string; new?: boolean; repository_selection?: string | null;
                  settings_url?: string | null } | null;
  line: string;
};

export type AccountLookup = AccountPlan & {
  account: FoundAccount;
  connection: { covered: boolean; how: string | null; words: string };
};

export type InstallationRepo = {
  full_name: string; private: boolean; default_branch?: string | null; used_by: string[];
  allowed: boolean; state: string; words: string;
};

export type AllowResult = {
  full_name: string; id?: string; state: string; allowed: boolean; words: string;
  checks: Record<string, { ok: boolean | null; state?: string | null }>;
};

export const lookupAccount = (account: string) =>
  idPost<AccountLookup>("/v1/identity/github/accounts/lookup", { account });

/** The poll: in the background (never a step-up sheet in the middle of watching). The type the
 *  lookup reported rides along so a personal target keeps the private-app rule between polls. */
export const watchAccount = (login: string, accountId: string, accountType?: string | null) =>
  idFetch<AccountPlan & { account: { login: string; id: string } }>(
    "POST", "/v1/identity/github/accounts/watch",
    { login, account_id: accountId, account_type: accountType ?? "" }, { background: true });

export const installationRepos = (installationId: string) =>
  idGet<{ installation_id: string; account: string | null; account_type: string | null; repos: InstallationRepo[] }>(
    `/v1/identity/github/accounts/installations/${encodeURIComponent(installationId)}/repos`);

export const allowRepos = (projectId: string, installationId: string, repos: string[]) =>
  idPost<{ results: AllowResult[]; working: number; line: string }>(
    "/v1/identity/github/accounts/allow", { project_id: projectId, installation_id: installationId, repos });

/** The API's own sentence for a refusal, never JSON. */
export function refusalWords(error: unknown): string {
  if (error instanceof IdentityError) {
    const said = error.body?.message;
    return typeof said === "string" && said ? said : error.message;
  }
  return String((error as Error)?.message ?? error);
}

// ---- the steps the Agent actions card draws -------------------------------------------------------

export type StepState = "done" | "run" | "todo";
export type Step = { key: string; label: string; sub: string; state: StepState };

/** The steps for a path. `sawGate`: the account hit the private-app wall before the app route
 *  opened (the one-time switch stays on the list, done, once the app is public). `allowed`:
 *  Allow was pressed. The make_public path leads with the direct connection — a private app
 *  installs only on its owner, so "Install on <account>" would be a dead button there. */
export function stepsFor(path: AccountPath, login: string, kind: FoundAccount["type"], opts: {
  sawGate?: boolean; allowing?: boolean; allowed?: boolean;
} = {}): Step[] {
  const who = kind === "Organization" ? "organisation" : kind === "User" ? "person" : "account";
  const found: Step = { key: "found", label: "Found it", sub: `${login} · ${who}`, state: "done" };
  const check: Step = { key: "check", label: "Check access", sub: "BOSS",
                        state: opts.allowed ? "done" : opts.allowing ? "run" : "todo" };
  const pick: Step = { key: "pick", label: "Pick repos", sub: "here",
                       state: path === "pick" ? (opts.allowed || opts.allowing ? "done" : "run") : "todo" };
  if (path === "no_app") {
    return [found, { key: "create", label: "Create the BOSS app", sub: "on GitHub · you", state: "run" },
            { key: "install", label: `Install on ${login}`, sub: "on GitHub · you", state: "todo" }, pick, check];
  }
  if (path === "make_public") {
    return [found, { key: "connect", label: `Connect ${login}`, sub: "sign in or token · you", state: "run" },
            pick, check];
  }
  const steps: Step[] = [found];
  if (opts.sawGate) {
    steps.push({ key: "gate", label: "Let it install", sub: "one time · you", state: "done" });
  }
  steps.push({ key: "install", label: `Install on ${login}`, sub: "on GitHub · you",
               state: path === "install" ? "run" : path === "pick" ? "done" : "todo" });
  return [...steps, pick, check];
}

/** How long to wait before asking GitHub again: every 5 s for the first minute, then 10 s, then
 *  20 s (never more often than every 5 s). */
export function pollDelay(attempt: number): number {
  if (attempt < 12) return 5000;
  if (attempt < 24) return 10000;
  return 20000;
}
export const MIN_POLL_MS = 5000;

/** The permissions a pasted token needs, exactly as GitHub's token page names them. */
export const TOKEN_PERMISSIONS = [
  ["Contents", "Read and write"], ["Pull requests", "Read and write"], ["Metadata", "Read-only"],
] as const;

/** GitHub's new fine-grained token page, filled in: the name, the account as its owner, the
 *  longest expiry and the three permissions. */
export function tokenPageFor(login: string | null, host: string): string {
  return newFineGrainedTokenUrl({
    name: `BOSS (${host})`.slice(0, 40),
    description: "BOSS writes branches and opens pull requests",
    ...(login ? { target_name: login } : {}),
    expires_in: "366",
    contents: "write",
    pull_requests: "write",
    metadata: "read",
  });
}

/** "14 public repositories", "1 public repository". */
export function publicRepos(n: number | null | undefined): string | null {
  if (n === null || n === undefined) return null;
  return `${n} public ${n === 1 ? "repository" : "repositories"}`;
}

/** What a person typed that is worth looking up yet (a name, @name, or a github.com link). */
export function lookable(text: string): boolean {
  const t = text.trim();
  if (!t) return false;
  if (/github\.com\/[A-Za-z0-9]/i.test(t)) return true;
  return /^@?[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(t);
}
