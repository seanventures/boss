/**
 * THE CHANGE SET'S DIFF, READ AND COLOURED (BOSSAPP-919). Pure functions, no DOM:
 *
 *   parseUnifiedDiff(diff)  `git diff` text → one entry per file: its path, status, +/− counts and
 *                           hunks of lines, each with its old and new line number
 *   langOf(path)            the language a file's extension names ("ts", "py", …, "" for others)
 *   highlight(text, lang)   a line as spans: keyword · string · comment · number · plain
 *
 * The tokenizer is deliberately light — one line at a time, no state across lines — so a long
 * diff colours fast and a strange file never breaks the page (it just stays plain).
 */

export type DiffLineKind = "add" | "del" | "ctx" | "note";
export type DiffLine = { kind: DiffLineKind; text: string; oldNo: number | null; newNo: number | null };
export type DiffHunk = { header: string; section: string; lines: DiffLine[] };
export type DiffFile = {
  path: string; oldPath: string | null; status: "added" | "deleted" | "renamed" | "modified" | "binary";
  additions: number; deletions: number; hunks: DiffHunk[];
};

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$/;

function stripPrefix(p: string): string {
  const t = p.trim().replace(/^"(.*)"$/, "$1");
  return t.replace(/^[ab]\//, "");
}

/** `git diff` (unified) → files. Unknown lines are kept as notes, never dropped silently. */
export function parseUnifiedDiff(diff: string | null | undefined): DiffFile[] {
  const files: DiffFile[] = [];
  if (!diff) return files;
  let file: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;
  const start = (path: string, oldPath: string | null = null): DiffFile => {
    const f: DiffFile = { path, oldPath, status: "modified", additions: 0, deletions: 0, hunks: [] };
    files.push(f);
    hunk = null;
    return f;
  };
  for (const raw of diff.replace(/\r/g, "").split("\n")) {
    if (raw.startsWith("diff --git ")) {
      const m = /^diff --git (?:"?a\/(.+?)"?) (?:"?b\/(.+?)"?)$/.exec(raw);
      file = start(m ? m[2] : raw.slice(11).split(" ").pop() ?? "", null);
      if (m && m[1] !== m[2]) file.oldPath = m[1];
      continue;
    }
    if (!file && (raw.startsWith("--- ") || raw.startsWith("+++ "))) file = start("");
    if (!file) continue;
    if (!hunk) {
      if (raw.startsWith("new file mode")) { file.status = "added"; continue; }
      if (raw.startsWith("deleted file mode")) { file.status = "deleted"; continue; }
      if (raw.startsWith("rename from ")) { file.oldPath = raw.slice(12); file.status = "renamed"; continue; }
      if (raw.startsWith("rename to ")) { file.path = raw.slice(10); file.status = "renamed"; continue; }
      if (raw.startsWith("Binary files ")) { file.status = "binary"; continue; }
      if (raw.startsWith("--- ")) {
        const p = raw.slice(4);
        if (p.trim() === "/dev/null") file.status = "added";
        else if (!file.path) file.path = stripPrefix(p);
        continue;
      }
      if (raw.startsWith("+++ ")) {
        const p = raw.slice(4);
        if (p.trim() === "/dev/null") file.status = "deleted";
        else file.path = stripPrefix(p);
        continue;
      }
    }
    const h = HUNK.exec(raw);
    if (h) {
      oldNo = Number(h[1]);
      newNo = Number(h[2]);
      hunk = { header: raw, section: h[3] ?? "", lines: [] };
      file.hunks.push(hunk);
      continue;
    }
    if (!hunk) continue;
    const cur: DiffHunk = hunk;
    if (raw.startsWith("+")) { cur.lines.push({ kind: "add", text: raw.slice(1), oldNo: null, newNo: newNo++ }); file.additions++; }
    else if (raw.startsWith("-")) { cur.lines.push({ kind: "del", text: raw.slice(1), oldNo: oldNo++, newNo: null }); file.deletions++; }
    else if (raw.startsWith(" ")) cur.lines.push({ kind: "ctx", text: raw.slice(1), oldNo: oldNo++, newNo: newNo++ });
    else if (raw.startsWith("\\")) cur.lines.push({ kind: "note", text: raw.slice(1).trim(), oldNo: null, newNo: null });
    else if (raw === "") { /* the trailing newline */ }
  }
  return files.filter((f) => f.path);
}

const EXT: Record<string, string> = {
  ts: "ts", tsx: "ts", js: "ts", jsx: "ts", mjs: "ts", cjs: "ts", py: "py", css: "css", scss: "css",
  html: "html", htm: "html", json: "json", md: "md", markdown: "md", sh: "sh", yml: "yaml", yaml: "yaml",
  sql: "sql", go: "ts", java: "ts", rs: "ts",
};

export function langOf(path: string): string {
  const name = path.split("/").pop() ?? "";
  if (/^Dockerfile/i.test(name)) return "sh";
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return EXT[ext] ?? "";
}

const KEYWORDS: Record<string, Set<string>> = {
  ts: new Set(("import export from as default function return const let var if else for while do switch case break continue new "
    + "class extends interface type enum async await try catch finally throw typeof instanceof in of null undefined true false this "
    + "void yield public private protected readonly static implements package func struct fn pub mut use impl").split(" ")),
  py: new Set(("import from as def return if elif else for while in not and or is None True False class with try except finally "
    + "raise lambda yield pass break continue global nonlocal async await assert del self").split(" ")),
  sh: new Set("if then else fi for do done case esac function export local return in echo cd RUN FROM COPY CMD ENV WORKDIR EXPOSE".split(" ")),
  sql: new Set("select from where insert into update delete create table index and or not null join on group by order limit values set as".split(" ")),
  yaml: new Set(["true", "false", "null"]),
  json: new Set(["true", "false", "null"]),
  css: new Set(["important"]),
};

export type Span = { t: "kw" | "str" | "com" | "num" | "tag" | "attr" | "plain"; v: string };

function push(out: Span[], t: Span["t"], v: string): void {
  if (!v) return;
  const last = out[out.length - 1];
  if (last && last.t === t) last.v += v; else out.push({ t, v });
}

/** One line as coloured spans. Plain text for a language it does not know. */
export function highlight(text: string, lang: string): Span[] {
  const out: Span[] = [];
  if (!lang || !text) { push(out, "plain", text); return out; }
  if (lang === "md") {
    if (/^\s*#/.test(text)) push(out, "kw", text);
    else if (/^\s*(```|[-*] )/.test(text)) { const m = /^(\s*(?:```|[-*] ))(.*)$/.exec(text)!; push(out, "tag", m[1]); push(out, "plain", m[2]); }
    else push(out, "plain", text);
    return out;
  }
  if (lang === "html") {
    const re = /(<!--.*?-->)|(<\/?[\w!-]+)|(\s[\w:-]+=)|("[^"]*"|'[^']*')|(\/?>)/g;
    let at = 0;
    for (let m = re.exec(text); m; m = re.exec(text)) {
      push(out, "plain", text.slice(at, m.index));
      push(out, m[1] ? "com" : m[2] || m[5] ? "tag" : m[3] ? "attr" : "str", m[0]);
      at = m.index + m[0].length;
    }
    push(out, "plain", text.slice(at));
    return out;
  }
  const lineComment = lang === "py" || lang === "sh" || lang === "yaml" ? "#" : lang === "sql" ? "--" : lang === "css" || lang === "json" ? null : "//";
  const kws = KEYWORDS[lang] ?? new Set<string>();
  let i = 0;
  while (i < text.length) {
    const rest = text.slice(i);
    if (lineComment && rest.startsWith(lineComment)) { push(out, "com", rest); break; }
    if (rest.startsWith("/*")) {
      const end = rest.indexOf("*/", 2);
      const v = end < 0 ? rest : rest.slice(0, end + 2);
      push(out, "com", v); i += v.length; continue;
    }
    const q = rest[0];
    if (q === '"' || q === "'" || q === "`") {
      let j = 1;
      while (j < rest.length && rest[j] !== q) j += rest[j] === "\\" ? 2 : 1;
      const v = rest.slice(0, Math.min(rest.length, j + 1));
      push(out, lang === "json" && /^\s*:/.test(rest.slice(v.length)) ? "attr" : "str", v); i += v.length; continue;
    }
    const num = /^\d[\d_.]*(?:e[+-]?\d+)?/i.exec(rest);
    if (num && !/\w/.test(text[i - 1] ?? "")) { push(out, "num", num[0]); i += num[0].length; continue; }
    const word = /^[A-Za-z_$][\w$]*/.exec(rest);
    if (word) {
      const w = word[0];
      push(out, kws.has(lang === "sql" ? w.toLowerCase() : w) ? "kw" : "plain", w); i += w.length; continue;
    }
    if (lang === "css" && rest[0] === "#" && /^#[0-9a-f]{3,8}\b/i.test(rest)) {
      const v = /^#[0-9a-f]{3,8}/i.exec(rest)![0]; push(out, "num", v); i += v.length; continue;
    }
    push(out, "plain", rest[0]); i += 1;
  }
  return out;
}

/** Inline review comments keyed "path:line" (new-side line numbers), for drawing under a diff line. */
export function commentsByLine<T extends { path?: string; line?: number }>(comments: T[]): Map<string, T[]> {
  const map = new Map<string, T[]>();
  for (const c of comments) {
    if (!c.path || !c.line) continue;
    const k = `${c.path}:${c.line}`;
    map.set(k, [...(map.get(k) ?? []), c]);
  }
  return map;
}
