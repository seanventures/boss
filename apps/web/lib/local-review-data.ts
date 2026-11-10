/**
 * LOCAL REVIEW'S DATA (BOSSAPP-948): the shapes GET …/local-changes/{n} carries for the review
 * (where the code lives, the comment threads) and their parsers. No imports, so the change-set
 * parser (lib/repo-links.ts) and the review's calls (lib/local-review.ts) both read it.
 */
export type Severity = "blocker" | "major" | "minor" | "nit";
export type ThreadState = "open" | "addressed" | "disputed" | "resolved";

export type ReviewComment = {
  id: number; path: string | null; line: number | null; end_line: number | null; side: "new" | "old";
  commit_sha: string | null; author: string; author_kind: "human" | "agent"; severity: Severity;
  severity_words: string; body: string; state: ThreadState; reply_to: number | null; origin: string;
  outdated: boolean; line_text: string | null; run_round: string | null; response_commit: string | null;
  resolved_by: string | null; resolved_at: string | null; created_at: string | null;
  /** BOSSAPP-954: the commit the agent's fix landed in; a GitHub comment still waiting to be
   *  posted (GitHub did not answer); a GitHub conversation comment (not on a line). */
  fixed_in?: string | null; queued?: boolean; kind?: "line" | "conversation";
  /** A local comment of a change later published to GitHub that is not a line comment there. */
  kept?: boolean; in_review_body?: boolean;
  /** The comment's code (2 lines either side, as it was at `commit`), and the commit it was made
   *  on (an outdated comment links it). GitHub's come from the comment's diff_hunk. */
  snippet?: Snippet | null; made_on?: string | null;
};
export type Snippet = { start: number | null; line: number | null; lines: string[]; commit: string | null; marks?: string[] };
export type Thread = ReviewComment & { replies: ReviewComment[] };
export type CommentSummary = { threads: number; unresolved: number; human_open: number; human_unreplied: number };
export type WorkingCopy = { path: string; branch: string | null; machine: string; recorded_by: string; updated_at: string | null };
export type Where = {
  storage_path: string; volume: string; branch: string; head_sha: string; ahead: number | null;
  remote_path: string; remote_url: string; reach: string; working_copies: WorkingCopy[];
};
export type TreeItem = { path: string; status: "M" | "A" | "D" | "R" | null; comments: number; open_comments: number };
export type Tree = { ref: string; sha: string; head_sha: string; base_sha: string; items: TreeItem[] };
export type FileView = {
  path: string; ref: string; sha: string; language: string; size: number | null; binary: boolean;
  too_large: boolean; text: string | null; lines?: number; message?: string;
};

const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : d);
const numOrNull = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function parseComment(raw: unknown): ReviewComment {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const sev = str(r.severity, "major");
  const state = str(r.state, "open");
  return {
    id: Number(r.id) || 0, path: str(r.path) || null, line: numOrNull(r.line), end_line: numOrNull(r.end_line),
    side: r.side === "old" ? "old" : "new", commit_sha: str(r.commit_sha) || null, author: str(r.author, "someone"),
    author_kind: r.author_kind === "agent" ? "agent" : "human",
    severity: (["blocker", "major", "minor", "nit"].includes(sev) ? sev : "major") as Severity,
    severity_words: str(r.severity_words, "must fix"), body: str(r.body),
    state: (["open", "addressed", "disputed", "resolved"].includes(state) ? state : "open") as ThreadState,
    reply_to: numOrNull(r.reply_to), origin: str(r.origin, "console"), outdated: r.outdated === true,
    line_text: str(r.line_text) || null, run_round: str(r.run_round) || null,
    response_commit: str(r.response_commit) || null, resolved_by: str(r.resolved_by) || null,
    resolved_at: str(r.resolved_at) || null, created_at: str(r.created_at) || null,
    fixed_in: str(r.fixed_in) || null, queued: r.queued === true, kind: r.kind === "conversation" ? "conversation" : "line",
    kept: r.kept === true, in_review_body: r.in_review_body === true,
    snippet: parseSnippet(r.snippet), made_on: str(r.made_on) || str(r.commit_sha) || null,
  };
}

export function parseSnippet(raw: unknown): Snippet | null {
  const r = (raw && typeof raw === "object" ? raw : null) as Record<string, unknown> | null;
  if (!r || !Array.isArray(r.lines) || !r.lines.length) return null;
  return { start: numOrNull(r.start), line: numOrNull(r.line), lines: r.lines.map((x) => str(x)), commit: str(r.commit) || null,
           ...(Array.isArray(r.marks) ? { marks: r.marks.map((x) => str(x)) } : {}) };
}

/** Which snippet row is the commented line: by number, else the last (a GitHub hunk ends there). */
export function snippetFocus(s: Snippet): number {
  if (s.start != null && s.line != null) {
    const i = s.line - s.start;
    if (i >= 0 && i < s.lines.length) return i;
  }
  return s.lines.length - 1;
}

/** Where a comment sits, in words: "src/app.js:39", "src/app.js", or "on the whole change". */
export function anchorWords(t: { path: string | null; line: number | null }): string {
  if (!t.path) return "on the whole change";
  return t.line ? `${t.path}:${t.line}` : t.path;
}

export function parseThreads(raw: unknown): Thread[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((t) => ({ ...parseComment(t), replies: Array.isArray((t as { replies?: unknown }).replies)
    ? ((t as { replies: unknown[] }).replies).map(parseComment) : [] }));
}

export function parseSummary(raw: unknown): CommentSummary {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return { threads: Number(r.threads) || 0, unresolved: Number(r.unresolved) || 0,
           human_open: Number(r.human_open) || 0, human_unreplied: Number(r.human_unreplied) || 0 };
}

export function parseWhere(raw: unknown): Where | null {
  const r = (raw && typeof raw === "object" ? raw : null) as Record<string, unknown> | null;
  if (!r || !str(r.storage_path)) return null;
  return {
    storage_path: str(r.storage_path), volume: str(r.volume), branch: str(r.branch), head_sha: str(r.head_sha),
    ahead: numOrNull(r.ahead), remote_path: str(r.remote_path), remote_url: str(r.remote_url), reach: str(r.reach, "local"),
    working_copies: Array.isArray(r.working_copies) ? (r.working_copies as Record<string, unknown>[]).map((w) => ({
      path: str(w.path), branch: str(w.branch) || null, machine: str(w.machine), recorded_by: str(w.recorded_by),
      updated_at: str(w.updated_at) || null })) : [],
  };
}

/** The clone URL as THIS browser reaches BOSS (the console's own origin), else the API's. */
export function cloneUrl(where: Where): string {
  if (typeof window !== "undefined" && where.remote_path) return `${window.location.origin}${where.remote_path}`;
  return where.remote_url;
}

export function cloneCommand(projectId: string, folder: string, branch: string | null): string {
  const q = (s: string) => (/^[A-Za-z0-9._/~:@-]+$/.test(s) ? s : `'${s.replace(/'/g, "'\\''")}'`);
  return `boss clone ${q(projectId)} ${q(folder)}${branch ? ` --branch ${q(branch)}` : ""}`;
}

