import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";

/**
 * Every place the console's code mentions a server action, and whether that mention carries a
 * press to the server.
 *
 * Shared by tests/every-write-goes-through-api-write.test.ts. A server action reaches the API on
 * the server, so the only way its writes get an Idempotency-Key is the press the browser sends
 * along: the SubmitButton's hidden field when the action is a form's, or lib/call-action.ts when
 * code invokes it. 63 code invocations had neither (review of 45e7693): Deploy, Redeploy, the
 * shepherd's sweep and the agent chat turn — the 2026-09-10 duplicate-chat path — went out
 * unkeyed while the fetch-only scan reported the console clean.
 */

/** Comments removed, newlines kept, so a reported line number is the real one. */
function codeWithLines(path: string): string {
  return readFileSync(path, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "))
    .replace(/(^|[^:"'`\\])\/\/[^\n]*/g, "$1");
}

function sourceFiles(root: string, dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(join(root, dir))) {
    const path = join(dir, name);
    if (statSync(join(root, path)).isDirectory()) out.push(...sourceFiles(root, path));
    else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) out.push(path);
  }
  return out;
}

export type ActionRef = { file: string; line: number; name: string; how: string; ok: boolean };

export function serverActionModules(root: string): Record<string, string[]> {
  const modules: Record<string, string[]> = {};
  for (const file of ["app", "lib", "components"].flatMap((d) => sourceFiles(root, d))) {
    const raw = readFileSync(join(root, file), "utf8");
    if (!/^\s*(?:\/\*[\s\S]*?\*\/\s*|\/\/[^\n]*\n\s*)*["']use server["']/.test(raw)) continue;
    modules[file] = [...codeWithLines(join(root, file))
      .matchAll(/export\s+async\s+function\s+([\w$]+)/g)].map((m) => m[1]);
  }
  return modules;
}

/** The `<form …>…</form>` around `at`, if any. */
function enclosingForm(src: string, at: number): string | null {
  const open = src.lastIndexOf("<form", at);
  if (open < 0) return null;
  const closeBefore = src.lastIndexOf("</form>", at);
  if (closeBefore > open) return null;
  const close = src.indexOf("</form>", at);
  return close < 0 ? null : src.slice(open, close);
}

/** The JSX tag an attribute at `at` belongs to: `form`, `SubmitButton`, `ActionForm`, … */
function ownerTag(src: string, at: number): string {
  const head = src.slice(Math.max(0, at - 2000), at);
  const tags = [...head.matchAll(/<([A-Za-z][\w.]*)(?=[\s>])/g)];
  return tags.length ? tags[tags.length - 1][1] : "";
}

/** Components defined in this file whose own JSX renders a SubmitButton — a form that renders
 *  one of them (`<ConveneActions />`) has a SubmitButton as surely as one that writes it out. */
function submitCarriers(src: string): Set<string> {
  const carriers = new Set<string>(["SubmitButton"]);
  const defs = [...src.matchAll(/\n(?:export\s+)?function\s+([A-Z][\w$]*)/g)];
  defs.forEach((def, i) => {
    const body = src.slice(def.index!, i + 1 < defs.length ? defs[i + 1].index : src.length);
    if (/<SubmitButton\b/.test(body)) carriers.add(def[1]);
  });
  return carriers;
}

/**
 * Components that take a server action as a prop and invoke it through callAction themselves.
 * Each entry is verified by the test (the named file's component must call `callAction(action)`),
 * so the list cannot claim a carrier that does not carry.
 */
export const PRESS_CARRYING_COMPONENTS: Record<string, string> = {
  ActionForm: "app/client-forms.tsx",
};

/** The file a relative import names, if it is one of `files`. */
function resolveImport(root: string, from: string, spec: string, files: Set<string>): string | null {
  if (!spec.startsWith(".")) return null;
  const base = relative(root, resolve(root, dirname(from), spec));
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`, `${base}/index.ts`, `${base}/index.tsx`]) {
    if (files.has(candidate)) return candidate;
  }
  return null;
}

/** A NON-relative import (`@/app/actions`, `app/actions`) that names a server-action module. */
function nonRelativeServerModule(spec: string, modules: Record<string, string[]>): string | null {
  if (spec.startsWith(".")) return null;
  const bare = spec.replace(/^[@~]\//, "");
  return Object.keys(modules).find((f) => {
    const stem = f.replace(/\.tsx?$/, "");
    return bare === stem || bare.endsWith(`/${stem}`);
  }) ?? null;
}

/**
 * Every module that runs in the browser: each `"use client"` file and, transitively, every file
 * it imports. A file with no directive is a SERVER component only when no client file imports
 * it; imported by one, it is bundled into the client and its direct call of an action has no
 * press. The scanner used to call every directive-less file server-rendered (review of 6ca9119).
 */
function clientModules(root: string, files: string[], modules: Record<string, string[]>): Set<string> {
  const all = new Set(files);
  const client = new Set<string>();
  const queue = files.filter((f) =>
    /^\s*["']use client["']/.test(readFileSync(join(root, f), "utf8")));
  while (queue.length) {
    const file = queue.pop()!;
    if (client.has(file) || file in modules) continue;
    client.add(file);
    const src = codeWithLines(join(root, file));
    for (const m of src.matchAll(/(?:import|export)\s[^;'"]*?from\s*["']([^"']+)["']|import\s*["']([^"']+)["']/g)) {
      const target = resolveImport(root, file, m[1] ?? m[2], all);
      if (target && !client.has(target)) queue.push(target);
    }
  }
  return client;
}

export function serverActionRefs(root: string): ActionRef[] {
  const modules = serverActionModules(root);
  const refs: ActionRef[] = [];
  const files = ["app", "lib", "components"].flatMap((d) => sourceFiles(root, d));
  const inBrowser = clientModules(root, files, modules);
  for (const file of files) {
    if (file in modules) continue;
    const src = codeWithLines(join(root, file));
    const carriers = submitCarriers(src);
    const serverRendered = !inBrowser.has(file);
    // SHAPES THE SCAN CANNOT FOLLOW ARE REFUSED, NOT SKIPPED. A namespace import
    // (`import * as actions`) is used as `actions.deploy(…)`, which no name below matches, and a
    // non-relative import was never parsed at all — either would let a direct call through.
    for (const m of src.matchAll(/import\s+\*\s+as\s+([\w$]+)\s+from\s*["']([^"']+)["']/g)) {
      const module = resolveImport(root, file, m[2], new Set(Object.keys(modules)))
        ?? nonRelativeServerModule(m[2], modules);
      if (module) {
        refs.push({ file, line: src.slice(0, m.index).split("\n").length, name: m[1],
                    how: `namespace import of ${module} — import its actions by name`, ok: false });
      }
    }
    for (const m of src.matchAll(/import\s+(?:type\s+)?[^;'"]*?from\s*["']([^"'.][^"']*)["']/g)) {
      const module = nonRelativeServerModule(m[1], modules);
      if (module && !/^import\s+type\b/.test(m[0]) && !/\*\s+as\s/.test(m[0])) {
        refs.push({ file, line: src.slice(0, m.index).split("\n").length, name: m[1],
                    how: `non-relative import of ${module} — import it relatively`, ok: false });
      }
    }
    const imports = /import\s+(type\s+)?\{([^}]*)\}\s*from\s*["']([^"']+)["']/g;
    for (let m = imports.exec(src); m; m = imports.exec(src)) {
      if (m[1] || !m[3].startsWith(".")) continue;
      const target = relative(root, resolve(root, dirname(file), m[3]));
      const module = Object.keys(modules).find((f) => f.replace(/\.tsx?$/, "") === target);
      if (!module) continue;
      const names = m[2].split(",").map((part) => part.trim())
        .filter((part) => part && !part.startsWith("type "))
        .map((part) => part.split(/\s+as\s+/).pop()!.trim())
        .filter((name) => modules[module].includes(name) || /\s+as\s+/.test(m![2]));
      for (const name of names) {
        const use = new RegExp(`(?<![\\w$.])${name.replace(/\$/g, "\\$")}(?![\\w$])`, "g");
        for (let r = use.exec(src); r; r = use.exec(src)) {
          if (r.index >= m.index && r.index < m.index + m[0].length) continue;
          const before = src.slice(Math.max(0, r.index - 160), r.index);
          const line = src.slice(0, r.index).split("\n").length;
          let how = "referenced";
          let ok = false;
          if (/\btypeof\s+$/.test(before)) { how = "typeof"; ok = true; }
          else if (/\bcallAction\(\s*$/.test(before)) { how = "callAction"; ok = true; }
          else if (/\b(?:form)?[aA]ction=\{\s*$/.test(before)) {
            const tag = ownerTag(src, r.index);
            if (tag in PRESS_CARRYING_COMPONENTS) {
              how = `prop of ${tag}, which invokes it through callAction`;
              ok = true;
            } else if (tag === "form" || carriers.has(tag)) {
              const form = enclosingForm(src, r.index) ?? "";
              ok = [...carriers].some((c) => new RegExp(`<${c}\\b`).test(form)) || carriers.has(tag);
              how = ok ? "form with SubmitButton" : "form WITHOUT SubmitButton";
            } else {
              how = `prop of <${tag}>, which nothing shows to carry a press`;
            }
          } else if (src[r.index + name.length] === "(") {
            // A server component rendering: there is no press, the call IS the render.
            how = serverRendered ? "called while rendering on the server" : "called directly";
            ok = serverRendered;
          }
          refs.push({ file, line, name, how, ok });
        }
      }
    }
  }
  return refs;
}
