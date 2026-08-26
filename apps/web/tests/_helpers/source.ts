/**
 * Read a source file with its comments removed.
 *
 * A test that greps source will otherwise match the comment EXPLAINING the thing it forbids.
 * That has happened repeatedly here: an assertion that a component contains no
 * `revalidatePath` failed against a comment describing why revalidatePath was removed.
 *
 * The Python side has `code_only()` for the same reason. This is its counterpart.
 */
import { readFileSync } from "node:fs";

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
