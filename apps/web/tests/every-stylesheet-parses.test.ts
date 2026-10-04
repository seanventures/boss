/**
 * EVERY STYLESHEET PARSES. A merge on 2026-10-04 joined two branches that both ended a block on a
 * shared `}`, leaving agent-world.css with an unclosed @media — no test noticed, and the
 * production build on boss-1 refused it ("Unclosed block"). Parse them here, where it is cheap.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..");

function cssFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    if (name === "node_modules" || name.startsWith(".")) return [];
    const full = join(dir, name);
    return statSync(full).isDirectory() ? cssFiles(full) : name.endsWith(".css") ? [full] : [];
  });
}

/** Where the first unbalanced brace is, skipping comments and strings; null when balanced. */
function unbalanced(css: string): string | null {
  let depth = 0;
  let line = 1;
  const opened: number[] = [];
  for (let i = 0; i < css.length; i += 1) {
    const c = css[i];
    if (c === "\n") line += 1;
    else if (c === "/" && css[i + 1] === "*") {
      const end = css.indexOf("*/", i + 2);
      if (end < 0) return `comment opened on line ${line} never closes`;
      line += css.slice(i, end).split("\n").length - 1;
      i = end + 1;
    } else if (c === '"' || c === "'") {
      const end = css.indexOf(c, i + 1);
      if (end < 0) return `string opened on line ${line} never closes`;
      i = end;
    } else if (c === "{") { depth += 1; opened.push(line); }
    else if (c === "}") {
      depth -= 1;
      opened.pop();
      if (depth < 0) return `extra } on line ${line}`;
    }
  }
  return depth ? `block opened on line ${opened[opened.length - 1]} never closes` : null;
}

describe("every console stylesheet parses", () => {
  const files = [...cssFiles(join(ROOT, "app")), ...cssFiles(join(ROOT, "components")).filter(Boolean)];
  it("finds the stylesheets", () => expect(files.length).toBeGreaterThan(5));
  it.each(files.map((f) => [f.slice(ROOT.length + 1), f]))("%s", (_name, file) => {
    expect(unbalanced(readFileSync(file, "utf8"))).toBeNull();
  });
});
