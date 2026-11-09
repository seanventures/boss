/**
 * THE CHANGE SET'S DIFF (BOSSAPP-919): a local project's "pull request" shows its diff per file,
 * line numbers old/new, added and removed lines told apart, and light syntax colours.
 */
import { describe, expect, it } from "vitest";

import { commentsByLine, highlight, langOf, parseUnifiedDiff } from "../lib/diff-view";

const DIFF = [
  "diff --git a/index.html b/index.html",
  "new file mode 100644",
  "index 0000000..1111111",
  "--- /dev/null",
  "+++ b/index.html",
  "@@ -0,0 +1,2 @@",
  "+<!doctype html><h1 class=\"t\">Corner shop</h1>",
  "+<p>open</p>",
  "diff --git a/src/app.ts b/src/app.ts",
  "index 2222222..3333333 100644",
  "--- a/src/app.ts",
  "+++ b/src/app.ts",
  "@@ -3,4 +3,4 @@ export function open() {",
  " const a = 1;",
  "-return \"closed\"; // old",
  "+return \"open\"; // new",
  " }",
  "\\ No newline at end of file",
  "diff --git a/old.py b/old.py",
  "deleted file mode 100644",
  "--- a/old.py",
  "+++ /dev/null",
  "@@ -1 +0,0 @@",
  "-def x(): return None",
  "",
].join("\n");

describe("parseUnifiedDiff", () => {
  const files = parseUnifiedDiff(DIFF);

  it("reads one entry per file with its status and counts", () => {
    expect(files.map((f) => [f.path, f.status, f.additions, f.deletions])).toEqual([
      ["index.html", "added", 2, 0], ["src/app.ts", "modified", 1, 1], ["old.py", "deleted", 0, 1],
    ]);
  });

  it("numbers lines on both sides and keeps the hunk's section", () => {
    const h = files[1].hunks[0];
    expect(h.section).toBe("export function open() {");
    expect(h.lines.map((l) => [l.kind, l.oldNo, l.newNo])).toEqual([
      ["ctx", 3, 3], ["del", 4, null], ["add", null, 4], ["ctx", 5, 5], ["note", null, null],
    ]);
  });

  it("answers nothing for nothing", () => {
    expect(parseUnifiedDiff("")).toEqual([]);
    expect(parseUnifiedDiff(null)).toEqual([]);
  });
});

describe("highlight", () => {
  it("knows the language from the extension", () => {
    expect(langOf("src/a.tsx")).toBe("ts");
    expect(langOf("x/y.py")).toBe("py");
    expect(langOf("README.md")).toBe("md");
    expect(langOf("Dockerfile")).toBe("sh");
    expect(langOf("LICENSE")).toBe("");
  });

  it("colours keywords, strings, comments and numbers, keeping every character", () => {
    const line = 'return "open"; // new 42';
    const spans = highlight(line, "ts");
    expect(spans.map((s) => s.v).join("")).toBe(line);
    expect(spans.find((s) => s.t === "kw")?.v).toBe("return");
    expect(spans.find((s) => s.t === "str")?.v).toBe('"open"');
    expect(spans.find((s) => s.t === "com")?.v).toBe("// new 42");
    const py = highlight("def x(): return None  # gone", "py");
    expect(py.filter((s) => s.t === "kw").map((s) => s.v)).toEqual(["def", "return", "None"]);
    expect(py[py.length - 1]).toEqual({ t: "com", v: "# gone" });
    expect(highlight("x = 12", "py").find((s) => s.t === "num")?.v).toBe("12");
  });

  it("colours html tags and attributes, json keys, markdown headings", () => {
    const html = highlight('<h1 class="t">Hi</h1>', "html");
    expect(html.map((s) => s.v).join("")).toBe('<h1 class="t">Hi</h1>');
    expect(html.filter((s) => s.t === "tag").map((s) => s.v)).toContain("<h1");
    expect(html.find((s) => s.t === "attr")?.v).toBe(" class=");
    expect(highlight('"name": "x"', "json")[0]).toEqual({ t: "attr", v: '"name"' });
    expect(highlight("# Title", "md")).toEqual([{ t: "kw", v: "# Title" }]);
    expect(highlight("plain words", "")).toEqual([{ t: "plain", v: "plain words" }]);
  });
});

describe("commentsByLine", () => {
  it("keys inline review comments by path and new line", () => {
    const m = commentsByLine([{ path: "index.html", line: 1, body: "Add a title." }, { body: "general" }]);
    expect(m.get("index.html:1")?.[0].body).toBe("Add a title.");
    expect(m.size).toBe(1);
  });
});
