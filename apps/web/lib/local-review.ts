/**
 * LOCAL REVIEW (BOSSAPP-948; docs/product/wireframes/agentic-factory/5-local-change-review.html):
 * the data the change page's review reads and writes. The engine is services/local_review.py —
 * ONE comment store, whoever writes (this page, `boss review`, git notes, MCP).
 *
 *   GET  …/local-changes/{n}/tree?ref=head|base|<sha>    the Code tab's tree
 *   GET  …/local-changes/{n}/file?path=&ref=             one whole file (1 MB cap)
 *   POST …/local-changes/{n}/comments                    a comment on a line
 *   POST …/local-changes/{n}/comments/{id}/reply         a reply in its thread
 *   POST …/local-changes/{n}/comments/{id}/resolve       a person resolves / reopens
 *   POST …/local-changes/{n}/request-changes             the fix round (a person's decision)
 *   POST …/local-changes/{n}/review                      a pushed change's review, now
 */
import { idGet, idPost } from "./identity/api";
import { withConsent } from "./identity/consent";
import { changeBase, type ChangeAt } from "./change-review";

export * from "./local-review-data";
import type { FileView, ReviewComment, Tree } from "./local-review-data";

/** BOSSAPP-954: every call names its change (ChangeAt): a local change set, or a GitHub pull
 *  request of the project — the same calls, the same shapes (lib/change-review.ts). */
export type { ChangeAt } from "./change-review";

/** `head`, `base`, or a commit of the change. */
export type Ref = string;
export const getTree = (at: ChangeAt, ref: Ref) => idGet<Tree>(`${changeBase(at)}/tree?ref=${encodeURIComponent(ref)}`);
export const getFile = (at: ChangeAt, path: string, ref: Ref) =>
  idGet<FileView>(`${changeBase(at)}/file?path=${encodeURIComponent(path)}&ref=${encodeURIComponent(ref)}`);

export type NewComment = { body: string; path?: string | null; line?: number | null; side?: "new" | "old";
                           commit_sha?: string | null; severity?: string };
export const addComment = (at: ChangeAt, c: NewComment) => idPost<ReviewComment>(`${changeBase(at)}/comments`, c);
export const replyTo = (at: ChangeAt, id: number, body: string) =>
  idPost<ReviewComment>(`${changeBase(at)}/comments/${id}/reply`, { body });
export const resolveThread = (at: ChangeAt, id: number, reopen = false) =>
  idPost<ReviewComment>(`${changeBase(at)}/comments/${id}/resolve`, { reopen });
/** Request changes starts a build: the consent sheet first when no grant covers it. */
export const requestChanges = (at: ChangeAt, note: string) =>
  withConsent(() => idPost<{ status: string; run_id: string | null }>(`${changeBase(at)}/request-changes`, { note }));
export const runReview = (at: ChangeAt) =>
  withConsent(() => idPost<{ run_id: string | null }>(`${changeBase(at)}/review`, {}));

// ── syntax colour: Shiki, loaded only when a whole file is shown ───────────────────────────

export type Tok = { content: string; light?: string; dark?: string };
const SHIKI_LANG: Record<string, string> = {
  python: "python", javascript: "javascript", jsx: "jsx", typescript: "typescript", tsx: "tsx", json: "json",
  markdown: "markdown", css: "css", scss: "scss", html: "html", yaml: "yaml", toml: "toml", bash: "bash",
  go: "go", rust: "rust", java: "java", kotlin: "kotlin", ruby: "ruby", php: "php", sql: "sql", c: "c",
  cpp: "cpp", csharp: "csharp", swift: "swift", vue: "vue", svelte: "svelte", xml: "xml",
  dockerfile: "dockerfile", makefile: "makefile",
};

/** Lines of coloured tokens, or null (no grammar, too long, or Shiki could not load: plain). */
export async function colourise(text: string, language: string): Promise<Tok[][] | null> {
  const lang = SHIKI_LANG[language];
  if (!lang || text.length > 400_000) return null;
  try {
    const shiki = await import("shiki");
    const out = await shiki.codeToTokens(text, { lang: lang as never, themes: { light: "github-light", dark: "github-dark" } });
    return out.tokens.map((line) => line.map((t) => {
      const style = (t.htmlStyle ?? {}) as Record<string, string>;
      return { content: t.content, light: style.color, dark: style["--shiki-dark"] };
    }));
  } catch {
    return null;
  }
}
