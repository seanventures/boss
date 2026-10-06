/**
 * THE IDENTITY SCREENS ARE DRAWN FROM TOKENS ONLY (the same guard Infra, Observability and the
 * Factory have). The only literal colours are the :root token blocks at the top of identity.css;
 * every other rule reads a var(), and no component writes an inline style.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = process.cwd();
function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(join(ROOT, dir))) {
    const path = join(dir, name);
    if (statSync(join(ROOT, path)).isDirectory()) out.push(...walk(path));
    else out.push(path);
  }
  return out;
}
const HEX = /#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?(?:[0-9a-fA-F]{2})?\b(?![\w-])/;
const CSS = walk("app/identity").filter((f) => f.endsWith(".css"));
const TSX = ["app/identity", "app/(auth)", "app/account", "components/identity"].flatMap(walk).filter((f) => /\.tsx?$/.test(f));

/** CSS with its :root token blocks (and comments) removed. */
function rules(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/:root(\[data-theme="[a-z]+"\])?\s*\{[^}]*\}/g, "");
}

describe("identity is tokens only", () => {
  it("finds the files", () => {
    expect(CSS.length).toBeGreaterThan(0);
    expect(TSX.length).toBeGreaterThan(15);
  });

  it.each(CSS)("%s: no colour outside the token blocks", (f) => {
    const body = rules(readFileSync(join(ROOT, f), "utf8"));
    const line = body.split("\n").find((l) => HEX.test(l) || /\brgba?\(\s*\d/.test(l));
    expect(line, f).toBeUndefined();
  });

  it.each(TSX)("%s: no inline style, no hex colour", (f) => {
    const src = readFileSync(join(ROOT, f), "utf8");
    expect(src, "inline style").not.toMatch(/style=\{\{/);
    expect(src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, ""), "hex colour").not.toMatch(HEX);
  });
});
