/**
 * CHANGE REVIEW (BOSSAPP-954; docs/product/wireframes/agentic-factory/6-git-review.html): the
 * change page's data, one shape for a LOCAL change set and a GITHUB pull request.
 *
 *   local    /v1/projects/{p}/local-changes/{n}/…
 *   github   /v1/projects/{p}/github-changes/{owner}/{repo}/{n}/…
 *
 * Both answer: the change, files?base=&head=, compare, tree?ref=, file?path=&ref=, commits,
 * commits/{sha}, checks, comments (+ reply, resolve), viewed, agent, agent/messages, merge,
 * request-changes. The pure helpers below (the file tree, the compare range, the agent's counts)
 * are what the page's tree, Commits tab and agent pane render.
 */
import { idGet, idPost } from "./identity/api";
import { withConsent } from "./identity/consent";

/** Which change: a local change set (no repo) or a GitHub PR of the project (`owner/repo`). */
export type ChangeAt = { projectId: string; number: string | number; repo?: string | null };

export function changeBase(at: ChangeAt): string {
  const p = encodeURIComponent(at.projectId);
  const n = encodeURIComponent(String(at.number));
  const repo = String(at.repo ?? "").trim();
  if (repo && repo.includes("/")) {
    const [owner, name] = repo.split("/", 2);
    return `/v1/projects/${p}/github-changes/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/${n}`;
  }
  return `/v1/projects/${p}/local-changes/${n}`;
}

const q = (params: Record<string, string | null | undefined>): string => {
  const parts = Object.entries(params).filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
};

// ── shapes ───────────────────────────────────────────────────────────────────────────────────

export type FileStat = { path: string; status: string; additions: number; deletions: number };
export type FilesView = { base: string; head: string; files: FileStat[]; diff: string; stale?: boolean };
export type Commit = { sha: string; message: string; author: string; date: string | null; fixes: number[];
                       /** +/− and files of this commit alone (null when GitHub was not asked). */
                       additions: number | null; deletions: number | null; files: number | null };
export type CommitView = FilesView & { sha: string; parent: string | null; author: string; date: string | null;
                                       message: string; fixes: number[] };
export type CheckItem = { name: string; status: string | null; conclusion: string | null; app: string | null };
export type Viewed = { person: string; paths: Record<string, string>; reviewed_sha: string | null };
export type AgentRun = { run_id: string; title: string; status: string; phase: string; running: boolean; builder: string;
                         cost_usd: number; updated_at: string | null; created_at: string | null };
export type AgentComment = { id: number; path: string | null; line: number | null; body: string; state: string;
                             fixed_in: string | null; author: string; queued: boolean };
export type AgentMessage = { id: number | string; author: string; author_kind: "human" | "agent"; body: string;
                             delivery: string; run_id: string | null; created_at: string | null;
                             about?: { path: string | null; line: number | null; thread: number } };
export type AgentPane = { run: AgentRun | null; builder: AgentRun | null; agent: string | null; comments: AgentComment[];
                          open: number; fixable: number; messages: AgentMessage[]; viewed: Viewed };

const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : d);
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : Number(v) || 0);
const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? v.filter((x): x is Record<string, unknown> => !!x && typeof x === "object") : [];

export function parseFiles(raw: unknown): FilesView {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return { base: str(r.base), head: str(r.head), diff: str(r.diff), stale: r.stale === true,
           files: arr(r.files).map((f) => ({ path: str(f.path ?? f.filename), status: str(f.status, "modified"),
                                             additions: num(f.additions), deletions: num(f.deletions) })) };
}

export function parseCommits(raw: unknown): Commit[] {
  const items = Array.isArray(raw) ? raw : (raw && typeof raw === "object" ? (raw as { items?: unknown }).items : []);
  const n = (v: unknown) => (v == null || v === "" ? null : num(v));
  return arr(items).map((c) => ({ sha: str(c.sha), message: str(c.message), author: str(c.author),
                                  date: str(c.date) || null, fixes: Array.isArray(c.fixes) ? c.fixes.map(num) : [],
                                  additions: n(c.additions), deletions: n(c.deletions), files: n(c.files) }));
}

function parseRun(raw: unknown): AgentRun | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (!str(r.run_id)) return null;
  return { run_id: str(r.run_id), title: str(r.title), status: str(r.status), phase: str(r.phase), running: r.running === true,
           builder: str(r.builder, "builder"), cost_usd: num(r.cost_usd), updated_at: str(r.updated_at) || null,
           created_at: str(r.created_at) || null };
}

export function parseViewed(raw: unknown): Viewed {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const paths: Record<string, string> = {};
  if (r.paths && typeof r.paths === "object") for (const [k, v] of Object.entries(r.paths as object)) paths[k] = str(v);
  return { person: str(r.person), paths, reviewed_sha: str(r.reviewed_sha) || null };
}

export function parseAgent(raw: unknown): AgentPane {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    run: parseRun(r.run), builder: parseRun(r.builder), agent: str(r.agent) || null,
    comments: arr(r.comments).map((c) => ({ id: num(c.id), path: str(c.path) || null, line: c.line == null ? null : num(c.line),
                                            body: str(c.body), state: str(c.state, "open"), fixed_in: str(c.fixed_in) || null,
                                            author: str(c.author), queued: c.queued === true })),
    open: num(r.open), fixable: num(r.fixable),
    messages: arr(r.messages).map((m) => ({ id: typeof m.id === "number" ? m.id : str(m.id), author: str(m.author),
      author_kind: m.author_kind === "agent" ? "agent" : "human", body: str(m.body), delivery: str(m.delivery),
      run_id: str(m.run_id) || null, created_at: str(m.created_at) || null,
      ...(m.about && typeof m.about === "object" ? { about: m.about as AgentMessage["about"] } : {}) })),
    viewed: parseViewed(r.viewed),
  };
}

// ── calls ────────────────────────────────────────────────────────────────────────────────────

export const getFiles = (at: ChangeAt, base?: string | null, head?: string | null) =>
  idGet<unknown>(`${changeBase(at)}/files${q({ base, head })}`).then(parseFiles);
export const getCommits = (at: ChangeAt) => idGet<unknown>(`${changeBase(at)}/commits`).then(parseCommits);
export const getCommit = (at: ChangeAt, sha: string) =>
  idGet<Record<string, unknown>>(`${changeBase(at)}/commits/${encodeURIComponent(sha)}`).then((r) => ({
    ...parseFiles({ ...r, base: r.parent, head: r.sha }), sha: str(r.sha), parent: str(r.parent) || null, author: str(r.author),
    date: str(r.date) || null, message: str(r.message), fixes: Array.isArray(r.fixes) ? r.fixes.map(num) : [] }) as CommitView);
export const getViewed = (at: ChangeAt) => idGet<unknown>(`${changeBase(at)}/viewed`).then(parseViewed);
export const setViewed = (at: ChangeAt, path: string, sha: string | null, viewed = true) =>
  idPost<unknown>(`${changeBase(at)}/viewed`, { path, sha, viewed }).then(parseViewed);
export const getAgent = (at: ChangeAt) => idGet<unknown>(`${changeBase(at)}/agent`).then(parseAgent);
export const sendToAgent = (at: ChangeAt, body: string) =>
  idPost<{ delivery: string; message: string }>(`${changeBase(at)}/agent/messages`, { body });
/** "Fix N open comments" / "Ask agent to fix": the fix run on the change's own branch. */
export const fixComments = (at: ChangeAt, note = "") =>
  withConsent(() => idPost<{ status: string; run_id: string | null }>(`${changeBase(at)}/request-changes`, { note }));

// ── the file tree (the left pane) ────────────────────────────────────────────────────────────

export type TreeFile = { path: string; name: string; status: string | null; additions: number; deletions: number;
                         comments: number; open: number; changed: boolean };
export type TreeNode = { kind: "dir"; name: string; path: string; children: TreeNode[] } | ({ kind: "file" } & TreeFile);

/** Paths → nested folders, folders first, then files, each by name. A folder with one folder
 *  inside is merged into one row ("src/inbox"), as GitHub shows it. */
export function buildTree(files: TreeFile[]): TreeNode[] {
  type Dir = { kind: "dir"; name: string; path: string; children: TreeNode[]; dirs: Map<string, Dir> };
  const root: Dir = { kind: "dir", name: "", path: "", children: [], dirs: new Map() };
  for (const f of files) {
    const parts = f.path.split("/");
    let at = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const name = parts[i];
      let next = at.dirs.get(name);
      if (!next) {
        next = { kind: "dir", name, path: parts.slice(0, i + 1).join("/"), children: [], dirs: new Map() };
        at.dirs.set(name, next);
        at.children.push(next);
      }
      at = next;
    }
    at.children.push({ kind: "file", ...f, name: parts[parts.length - 1] });
  }
  const finish = (d: Dir): TreeNode[] => {
    const out = d.children.map((c): TreeNode => {
      if (c.kind !== "dir") return c;
      let dir = c as Dir;
      let name = dir.name;
      while (dir.children.length === 1 && dir.children[0].kind === "dir") {
        dir = dir.children[0] as Dir;
        name = `${name}/${dir.name}`;
      }
      return { kind: "dir", name, path: dir.path, children: finish(dir) };
    });
    return out.sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "dir" ? -1 : 1));
  };
  return finish(root);
}

/** The files a filter keeps: every word of it in the path (case-insensitive). */
export function filterFiles<T extends { path: string }>(files: T[], filter: string): T[] {
  const words = filter.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return files;
  return files.filter((f) => words.every((w) => f.path.toLowerCase().includes(w)));
}

/** The tree's rows: the change's files (Changed) or every path at the head (All files), each
 *  with its +/− and open-comment dot. */
export function treeFiles(changed: FileStat[], all: { path: string; status: string | null }[] | null,
                          threads: { path: string | null; state: string }[], mode: "changed" | "all"): TreeFile[] {
  const stat = new Map(changed.map((f) => [f.path, f]));
  const counts = new Map<string, { n: number; open: number }>();
  for (const t of threads) {
    if (!t.path) continue;
    const c = counts.get(t.path) ?? { n: 0, open: 0 };
    c.n += 1;
    if (t.state !== "resolved") c.open += 1;
    counts.set(t.path, c);
  }
  const paths = mode === "all" && all ? all.map((a) => a.path) : changed.map((f) => f.path);
  return paths.map((p) => {
    const s = stat.get(p);
    return { path: p, name: p.split("/").pop() ?? p, status: s ? s.status : null, additions: s?.additions ?? 0,
             deletions: s?.deletions ?? 0, comments: counts.get(p)?.n ?? 0, open: counts.get(p)?.open ?? 0, changed: !!s };
  });
}

// ── compare: base…head ───────────────────────────────────────────────────────────────────────

export type Range = { base: string | null; head: string | null };
export const WHOLE_CHANGE: Range = { base: null, head: null };

/** Two ticked commits → the range between them, older first (commits are listed newest first).
 *  The base is the older commit's PARENT side: comparing c1…c3 shows c2 and c3's changes, as
 *  `git diff c1 c3` does. */
export function rangeOfTicked(commits: Commit[], ticked: string[]): Range | null {
  if (ticked.length !== 2) return null;
  const idx = ticked.map((s) => commits.findIndex((c) => c.sha === s));
  if (idx.some((i) => i < 0) || idx[0] === idx[1]) return null;
  const [newer, older] = idx[0] < idx[1] ? [ticked[0], ticked[1]] : [ticked[1], ticked[0]];
  return { base: older, head: newer };
}

/** Tick a commit (at most two: a third replaces the oldest tick). */
export function tick(ticked: string[], sha: string): string[] {
  if (ticked.includes(sha)) return ticked.filter((s) => s !== sha);
  return [...ticked, sha].slice(-2);
}

/** "Since my review": from the head the person last reviewed to the latest, or null when they
 *  have not reviewed it or nothing came after. */
export function sinceMyReview(viewed: Viewed | null, head: string | null): Range | null {
  const at = viewed?.reviewed_sha;
  if (!at || !head || at === head) return null;
  return { base: at, head };
}

/** How the range reads in the picker: "main … 9e4f1c2 · latest". */
export function rangeLabel(range: Range, baseBranch: string, commits: Commit[]): string {
  const short = (s: string | null) => (s ? s.slice(0, 7) : "");
  const base = range.base ? short(range.base) : baseBranch;
  const head = range.head ? short(range.head) : short(commits[0]?.sha ?? null);
  const latest = !range.head || range.head === commits[0]?.sha;
  return `${base} … ${head || "head"}${latest ? " · latest" : ""}`;
}

/** The files a person has ticked Viewed at the current head, of the ones shown. */
export function viewedCount(files: { path: string }[], viewed: Viewed | null, head: string | null): number {
  if (!viewed || !head) return 0;
  return files.filter((f) => viewed.paths[f.path] && viewed.paths[f.path] === head).length;
}

// ── the agent pane ───────────────────────────────────────────────────────────────────────────

export function agentStatus(pane: AgentPane | null, now = Date.now()): { label: string; tone: "green" | "amber" | "grey" } {
  const run = pane?.run;
  if (!run) return { label: "No task built this change", tone: "grey" };
  if (run.running) return { label: "Working", tone: "amber" };
  const at = run.updated_at ? new Date(run.updated_at).getTime() : NaN;
  const ago = Number.isFinite(at) ? agoWords(now - at) : "";
  return { label: `Idle${ago ? ` · last run ${ago}` : ""}`, tone: "green" };
}

export function agoWords(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

/** "2 open · 3 fixed" for the pane's comment list. */
export function commentCounts(comments: AgentComment[]): { open: number; fixed: number; resolved: number } {
  return {
    open: comments.filter((c) => c.state === "open" || c.state === "disputed").length,
    fixed: comments.filter((c) => c.state === "addressed" || (!!c.fixed_in && c.state !== "resolved")).length,
    resolved: comments.filter((c) => c.state === "resolved").length,
  };
}

// ── links: a GitHub PR opens BOSS's page for it ──────────────────────────────────────────────

const GITHUB_PR = /^https?:\/\/(?:www\.)?github\.com\/([^/\s]+)\/([^/\s]+)\/pull\/(\d+)(\/[^?#]*)?/;

export type GithubPr = { owner: string; repo: string; number: number; rest: string };

export function parseGithubPr(url: string | null | undefined): GithubPr | null {
  const m = GITHUB_PR.exec(String(url ?? ""));
  return m ? { owner: m[1], repo: m[2], number: Number(m[3]), rest: (m[4] ?? "").replace(/^\/+/, "") } : null;
}

/** BOSS's page for a GitHub PR: /changes/github/<owner>/<repo>/<n>, which opens the project's
 *  change page (or GitHub, when the repository is none of the person's projects). */
export function githubChangePath(pr: GithubPr, suffix = ""): string {
  const base = `/changes/github/${encodeURIComponent(pr.owner)}/${encodeURIComponent(pr.repo)}/${pr.number}`;
  const rest = suffix.replace(/^\/+/, "");
  if (!rest) return base;
  if (rest === "files") return `${base}?tab=files`;
  const commit = /^commits?\/([0-9a-f]{7,40})/.exec(rest);
  if (commit) return `${base}?tab=commits&commit=${commit[1]}`;
  if (rest === "commits") return `${base}?tab=commits`;
  if (rest === "checks") return `${base}?tab=checks`;
  return base;
}

// ── one source for every count: the diff of the current base…head ────────────────────────────

/** The files the diff shows, with their +/−: the parsed diff is the truth for lines; a file the
 *  diff has no lines for (binary, too large) still counts, from the file list. Every count on the
 *  page — the tab badge, the tree, the toolbar's +/−, "n of m viewed", the summary — reads this. */
export function changedFiles(stats: FileStat[], parsed: { path: string; status: string; additions: number; deletions: number }[]): FileStat[] {
  const byDiff = new Map(parsed.filter((d) => d.path).map((d) => [d.path, d]));
  const out: FileStat[] = [];
  const seen = new Set<string>();
  for (const f of stats) {
    if (!f.path || seen.has(f.path)) continue;
    seen.add(f.path);
    const d = byDiff.get(f.path);
    out.push(d ? { path: f.path, status: f.status || d.status, additions: d.additions, deletions: d.deletions } : f);
  }
  for (const d of byDiff.values()) {
    if (seen.has(d.path)) continue;
    seen.add(d.path);
    out.push({ path: d.path, status: d.status, additions: d.additions, deletions: d.deletions });
  }
  return out;
}

export function totals(files: FileStat[]): { files: number; additions: number; deletions: number } {
  return { files: files.length, additions: files.reduce((a, f) => a + f.additions, 0),
           deletions: files.reduce((a, f) => a + f.deletions, 0) };
}

/** The description without its own "What changed" section: the page says what changed from the
 *  diff (one source), so a hand-written count can never disagree with the tree. */
export function withoutWhatChanged(body: string): string {
  const lines = body.split("\n");
  const out: string[] = [];
  let skip = 0;
  for (const line of lines) {
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      if (skip && h[1].length <= skip) skip = 0;
      if (!skip && /^what changed\b/i.test(h[2].trim())) { skip = h[1].length; continue; }
    }
    if (!skip) out.push(line);
  }
  return out.join("\n").trim();
}

/** THE CHANGE PAGE'S URLS: /changes/<project>/<n>[/commits/<sha>][?repo=owner/name][&tab=…].
 *  A commit is a real link of its own; the other tabs keep ?tab=, so links and Back work. */
export function changeUrl(at: ChangeAt, tab: string, commit: string | null = null): string {
  const base = `/changes/${encodeURIComponent(at.projectId)}/${encodeURIComponent(String(at.number))}`;
  const q = new URLSearchParams();
  if (at.repo) q.set("repo", String(at.repo));
  if (tab === "commits" && commit) return `${base}/commits/${encodeURIComponent(commit)}${q.toString() ? `?${q}` : ""}`;
  if (tab !== "files") q.set("tab", tab);
  return `${base}${q.toString() ? `?${q}` : ""}`;
}

/** Read the tab and commit back from a change page URL (Back, a pasted link). */
export function readChangeUrl(pathname: string, search: string): { tab: string; commit: string | null } {
  const m = /\/commits\/([0-9a-fA-F]{7,40})\/?$/.exec(pathname);
  if (m) return { tab: "commits", commit: m[1] };
  const q = new URLSearchParams(search);
  return { tab: q.get("tab") || "files", commit: q.get("commit") };
}

/** A project commit's page (the default branch's history). */
export function projectCommitUrl(projectId: string, sha: string, repo?: string | null): string {
  return `/projects/${encodeURIComponent(projectId)}/commits/${encodeURIComponent(sha)}${repo ? `?repo=${encodeURIComponent(repo)}` : ""}`;
}

export type HistoryItem = { sha: string; parents: string[]; merge: boolean; author: string; date: string | null; message: string;
                            change: { source: string; number: number; href: string } | null };
export type History = { repository: string; source: string; branch: string; items: HistoryItem[] };
export function parseHistory(raw: unknown): History {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return { repository: str(r.repository), source: str(r.source), branch: str(r.branch, "main"),
           items: arr(r.items).map((c) => ({ sha: str(c.sha), parents: Array.isArray(c.parents) ? c.parents.map((p) => str(p)) : [],
             merge: c.merge === true, author: str(c.author), date: str(c.date) || null, message: str(c.message),
             change: c.change && typeof c.change === "object" ? { source: str((c.change as Record<string, unknown>).source),
               number: num((c.change as Record<string, unknown>).number), href: str((c.change as Record<string, unknown>).href) } : null })) };
}
export const getHistory = (projectId: string, repo?: string | null) =>
  idGet<unknown>(`/v1/projects/${encodeURIComponent(projectId)}/history${q({ repo })}`).then(parseHistory);
export const getHistoryCommit = (projectId: string, sha: string, repo?: string | null) =>
  idGet<Record<string, unknown>>(`/v1/projects/${encodeURIComponent(projectId)}/history/${encodeURIComponent(sha)}${q({ repo })}`);

/** The page's URL for a tab and a commit, keeping the rest of the query (?repo=…). */
export function pageUrl(current: string, tab: string, commit: string | null): string {
  const u = new URL(current, "http://x");
  u.searchParams.set("tab", tab);
  if (commit) u.searchParams.set("commit", commit); else u.searchParams.delete("commit");
  return `${u.pathname}?${u.searchParams.toString()}${u.hash && !/^#(conversation|commits|files|code|checks)$/.test(u.hash) ? u.hash : ""}`;
}

/** The commit before and after one in the list (the list is newest first: Previous is older). */
export function neighbours(commits: Commit[], sha: string | null): { prev: string | null; next: string | null; index: number } {
  const i = sha ? commits.findIndex((c) => c.sha === sha || c.sha.startsWith(sha)) : -1;
  if (i < 0) return { prev: null, next: null, index: -1 };
  return { prev: commits[i + 1]?.sha ?? null, next: i > 0 ? commits[i - 1].sha : null, index: i };
}

// ── the tree's behaviour: what opens, go to file, names that fit ─────────────────────────────

/** The folders (tree node paths) that start open: those holding the open file or a changed
 *  file. A compacted row ("a/b") opens when its deepest folder does. */
export function defaultExpanded(nodes: TreeNode[], open: string | null): Set<string> {
  const out = new Set<string>();
  const walk = (list: TreeNode[]): boolean => {
    let any = false;
    for (const n of list) {
      if (n.kind === "dir") {
        if (walk(n.children)) { out.add(n.path); any = true; }
      } else if (n.changed || n.path === open) any = true;
    }
    return any;
  };
  walk(nodes);
  return out;
}

/** The tree rows to open so `path` (a file or a folder) is visible: every folder above it. */
export function ancestorsOf(nodes: TreeNode[], path: string): string[] {
  const out: string[] = [];
  const walk = (list: TreeNode[]): boolean => {
    for (const n of list) {
      if (n.kind === "dir") {
        if (path === n.path || path.startsWith(`${n.path}/`)) { out.push(n.path); walk(n.children); return true; }
      }
    }
    return false;
  };
  walk(nodes);
  return out;
}

/** Fuzzy score of a path for a query: every query character in order (a subsequence), with
 *  points for matches in the file name, at word starts and in runs; null when it does not match. */
export function fuzzyScore(path: string, query: string): number | null {
  const p = path.toLowerCase();
  const qy = query.toLowerCase().replace(/\s+/g, "");
  if (!qy) return 0;
  const nameAt = p.lastIndexOf("/") + 1;
  let score = 0;
  let last = -2;
  let at = 0;
  for (const ch of qy) {
    const i = p.indexOf(ch, at);
    if (i < 0) return null;
    score += 1;
    if (i === last + 1) score += 3;                                     // a run
    if (i === 0 || "/._-".includes(p[i - 1])) score += 2;               // a word start
    if (i >= nameAt) score += 2;                                        // in the name
    last = i;
    at = i + 1;
  }
  return score - p.length / 100;                                        // shorter paths first on a tie
}

export function fuzzyFiles(paths: string[], query: string, limit = 12): { path: string; name: string; folder: string }[] {
  return paths.map((path) => ({ path, s: fuzzyScore(path, query) }))
    .filter((x): x is { path: string; s: number } => x.s != null)
    .sort((a, b) => b.s - a.s || a.path.localeCompare(b.path))
    .slice(0, limit)
    .map(({ path }) => { const i = path.lastIndexOf("/"); return { path, name: path.slice(i + 1), folder: i > 0 ? path.slice(0, i) : "" }; });
}

/** A long name cut in the MIDDLE ("helpdesk-…-laptop.ts"), so its start and its extension both
 *  show; never cut at the right edge. */
export function middleEllipsis(name: string, max: number): string {
  if (max < 5 || name.length <= max) return name;
  const keep = max - 1;
  const tail = Math.ceil(keep / 2);
  return `${name.slice(0, keep - tail)}…${name.slice(name.length - tail)}`;
}

/** A path's breadcrumb: each folder with the path it opens, then the file. */
export function crumbs(path: string): { name: string; path: string; file: boolean }[] {
  const parts = path.split("/").filter(Boolean);
  return parts.map((name, i) => ({ name, path: parts.slice(0, i + 1).join("/"), file: i === parts.length - 1 }));
}
