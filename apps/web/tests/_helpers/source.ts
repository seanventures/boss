/**
 * Read a source file with its comments removed.
 *
 * A test that greps source will otherwise match the comment EXPLAINING the thing it forbids.
 * That has happened repeatedly here: an assertion that a component contains no
 * `revalidatePath` failed against a comment describing why revalidatePath was removed.
 *
 * The Python side has `code_only()` for the same reason. This is its counterpart.
 */
import { readdirSync, readFileSync } from "node:fs";

export function codeOnly(path: string): string {
  const raw = readFileSync(path, "utf8");
  return raw
    // Block comments, including JSDoc.
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    // Line comments — but not the // inside a URL like https://example.
    .replace(/(^|[^:"'`])\/\/[^\n]*/g, "$1")
    // JSX comments are block comments already handled above, but {/* ... */} leaves braces.
    .replace(/\{\s*\}/g, " ");
}


/**
 * CSS with its comments removed.
 *
 * Same trap as codeOnly, and it caught me twice: a check for `font-family` in a rule was
 * satisfied by the COMMENT saying "buttons do not inherit font-family", so deleting the
 * declaration still passed. A test that greps source must never see prose.
 */
export function cssOnly(path: string): string {
  return readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, " ");
}

/** Every .tsx under a directory, relative to apps/web. For checks that must scan components as
 *  a set rather than name each file -- a rule that only covers the files someone remembered to
 *  list goes stale the moment a screen is added. */
export function tsxFiles(dir: string): string[] {
  // Paths are relative to apps/web, matching how every other helper here is called.
  return readdirSync(dir)
    .filter((f) => f.endsWith(".tsx"))
    .map((f) => `${dir}/${f}`);
}
