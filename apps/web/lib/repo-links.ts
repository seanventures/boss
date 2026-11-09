/**
 * REPOSITORY AND PULL-REQUEST LINKS, for a project on GitHub or on THIS machine (BOSSAPP-915).
 *
 * A local project's repository is named `local_<project_id>/<name>` (GitHub owners cannot hold
 * "_"), has no web page anywhere but BOSS, and its "pull requests" are BOSS change sets whose URL
 * is `http://boss-local/local_<project_id>/<name>/pull/<n>` — a name, not a reachable address.
 * Every console link to a repository or a PR goes through here, so a local one opens BOSS's own
 * change-set view (/changes/<project_id>/<n>) and never a dead github.com or boss-local link.
 */

import { repoUrl } from "./github-urls";
import { parseCiHold, type CiHold } from "./ci-hold";

const LOCAL_OWNER_PREFIX = "local_";
const LOCAL_PR_HOST = "boss-local";

export type LocalChange = { projectId: string; repo: string; number: number };

/** `local_<project>/<name>` (or the bare owner): a repository BOSS keeps on this machine. */
export function isLocalRepo(fullName: string | null | undefined): boolean {
  return String(fullName ?? "").trim().startsWith(LOCAL_OWNER_PREFIX);
}

/** A local change set's URL (`http://boss-local/...`). */
export function isLocalChangeUrl(url: string | null | undefined): boolean {
  return String(url ?? "").includes(`://${LOCAL_PR_HOST}/`);
}

/** The project, repository and number a local change-set URL names; null for any other URL. */
export function parseLocalChange(url: string | null | undefined): LocalChange | null {
  if (!isLocalChangeUrl(url)) return null;
  const m = /:\/\/boss-local\/(local_([^/]+))\/([^/]+)\/pull\/(\d+)/.exec(String(url));
  if (!m) return null;
  return { projectId: m[2], repo: `${m[1]}/${m[3]}`, number: Number(m[4]) };
}

/** BOSS's change-set view of a local change set. */
export function changeViewPath(projectId: string, number: number | string): string {
  return `/changes/${encodeURIComponent(projectId)}/${encodeURIComponent(String(number))}`;
}

/** Where a PR link goes: BOSS's change-set view for a local change set, else the URL itself. */
export function changeHref(url: string): string;
export function changeHref(url: string | null | undefined): string | null;
export function changeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  const local = parseLocalChange(url);
  return local ? changeViewPath(local.projectId, local.number) : url;
}

/** A link to one of the PR's sub-pages (`files`, `commits/<sha>`): GitHub only. A local change
 *  set's view already shows its files and commits, so it is the change set itself. */
export function changeSubHref(url: string | null | undefined, suffix: string): string | null {
  if (!url) return null;
  if (isLocalChangeUrl(url)) return changeHref(url);
  return `${url.replace(/\/+$/, "")}/${suffix.replace(/^\/+/, "")}`;
}

/** A local link opens in this tab; GitHub opens in a new one. */
export function linkTarget(url: string | null | undefined): { target?: string; rel?: string } {
  return isLocalChangeUrl(url) || isLocalRepo(url ?? "") || String(url ?? "").startsWith("/")
    ? {}
    : { target: "_blank", rel: "noreferrer" };
}

/** Where a repository link goes: its page on GitHub, or the project's Repo & knowledge tab for a
 *  local one (never github.com). Null when there is nothing to open. */
export function repoHref(fullName: string | null | undefined, externalUrl?: string | null,
                         projectId?: string | null): string | null {
  if (isLocalRepo(fullName)) {
    const owner = String(fullName).split("/", 1)[0];
    const pid = projectId || owner.slice(LOCAL_OWNER_PREFIX.length);
    return pid ? `/?project_id=${encodeURIComponent(pid)}&area=project&tab=knowledge` : null;
  }
  if (externalUrl) return externalUrl;
  return fullName ? repoUrl(fullName) : null;
}

/** A file in a repository at a ref: GitHub's blob view, or null for a local repository. */
export function blobHref(fullName: string | null | undefined, path: string, ref = "HEAD"): string | null {
  if (!fullName || isLocalRepo(fullName)) return null;
  return `${repoUrl(fullName)}/blob/${ref}/${path}`;
}

/** "Local repo" or "GitHub", from a repository name or a binding's host. */
export function repoHostLabel(nameOrHost: string | null | undefined): "Local repo" | "GitHub" {
  const v = String(nameOrHost ?? "").trim();
  return v === "local" || isLocalRepo(v) ? "Local repo" : "GitHub";
}

/** "Review change #3 ›" — the words on every link to a LOCAL change set (the build page, Work,
 *  Needs you); null for a GitHub pull request, whose surfaces keep their own words. */
export function reviewChangeWords(url: string | null | undefined): string | null {
  const local = parseLocalChange(url);
  return local ? `Review change #${local.number} ›` : null;
}

/** The words on a PR link. */
export function prLinkLabel(url: string | null | undefined): string {
  return isLocalChangeUrl(url) ? "Open change set" : "Open PR on GitHub";
}

// ── the change-set view's data (GET /v1/projects/{project_id}/local-changes/{number}) ──────────

export type ChangeSetFile = { path: string; status: string; additions: number; deletions: number };
export type ChangeSetNote = { id: string | number; kind: string; event: string; body: string; author: string;
                              comments: { path?: string; line?: number; body: string }[] };
export type ChangeSet = {
  number: number; title: string; state: string; repository: string; branch: string; base_branch: string;
  url: string; body: string; github_pr_url: string | null; merged_by: string | null; merged_sha: string | null;
  files: ChangeSetFile[]; diff: string; diff_error: string | null; commits: { sha: string; message: string }[];
  review: ChangeSetNote[];
  checks: { where: string; run_status: string | null; tests_passed: string | null; gates?: unknown } | null;
  merge: { how: string; decision_id: string; held?: CiHold | null } | null;
  /** The review decision whose approval merges it (pending while it is open). */
  decision: { id: string; status: string; question: string } | null;
  draft: boolean; labels: string[]; pending_push: boolean; run_id: string | null; head_sha: string | null;
  created_at: string | null; updated_at: string | null; merged_at: string | null;
};

export function changeSetPath(projectId: string, number: number | string): string {
  return `/v1/projects/${encodeURIComponent(projectId)}/local-changes/${encodeURIComponent(String(number))}`;
}

const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : d);
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : Number(v) || 0);
const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? v.filter((x): x is Record<string, unknown> => !!x && typeof x === "object") : [];

/** The API's answer, made safe to render (missing fields become empty). */
export function parseChangeSet(raw: unknown): ChangeSet {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const checks = r.checks && typeof r.checks === "object" ? r.checks as Record<string, unknown> : null;
  const merge = r.merge && typeof r.merge === "object" ? r.merge as Record<string, unknown> : null;
  const decision = r.decision && typeof r.decision === "object" ? r.decision as Record<string, unknown> : null;
  return {
    number: num(r.number), title: str(r.title), state: str(r.state, "open"), repository: str(r.repository),
    branch: str(r.branch), base_branch: str(r.base_branch, "main"), url: str(r.url), body: str(r.body),
    github_pr_url: str(r.github_pr_url) || null, merged_by: str(r.merged_by) || null, merged_sha: str(r.merged_sha) || null,
    files: arr(r.files).map((f) => ({ path: str(f.path ?? f.filename), status: str(f.status, "modified"),
                                       additions: num(f.additions), deletions: num(f.deletions) })),
    diff: str(r.diff), diff_error: str(r.diff_error) || null,
    commits: arr(r.commits).map((c) => ({ sha: str(c.sha), message: str(c.message) })),
    review: arr(r.review).map((n) => ({
      id: str(n.id), kind: str(n.kind), event: str(n.event), body: str(n.body), author: str(n.author),
      comments: arr(n.comments).map((c) => ({ path: str(c.path) || undefined, line: c.line == null ? undefined : num(c.line), body: str(c.body) })),
    })),
    checks: checks ? { where: str(checks.where, "BOSS's sandbox"), run_status: str(checks.run_status) || null,
                       tests_passed: checks.tests_passed == null ? null : str(checks.tests_passed),
                       ...(checks.gates !== undefined ? { gates: checks.gates } : {}) } : null,
    merge: merge && str(merge.decision_id) ? { how: str(merge.how), decision_id: str(merge.decision_id), held: parseCiHold(merge.held) } : null,
    decision: decision && str(decision.id) ? { id: str(decision.id), status: str(decision.status, "pending"), question: str(decision.question) } : null,
    draft: r.draft === true, labels: Array.isArray(r.labels) ? r.labels.map((l) => str(l)).filter(Boolean) : [],
    pending_push: r.pending_push === true, run_id: str(r.run_id) || null, head_sha: str(r.head_sha) || null,
    created_at: str(r.created_at) || null, updated_at: str(r.updated_at) || null, merged_at: str(r.merged_at) || null,
  };
}

/** "on GitHub" / "in BOSS" — for sentences like "review it on GitHub". */
export function prWhere(url: string | null | undefined): string {
  return isLocalChangeUrl(url) ? "in BOSS" : "on GitHub";
}
