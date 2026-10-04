/**
 * Every component that names or draws an agent from the identity store re-renders when the
 * store loads.
 *
 * Seen live: 12 of 34 roster cards showed "Program Manager", "COO", "CTO" instead of Rally,
 * Cadence, Vector, although `/v1/agents/identities` had answered with the callsigns. The card
 * read `identityOf()` at render and never subscribed, so a card drawn before the load kept the
 * bundled cast's answer — which has callsigns only for the hand-drawn roles. The class is every
 * component that resolves an identity at render, directly or through a helper, without hearing
 * the store.
 *
 * The rule, held over the whole console (parsed and resolved through imports, not grepped, so a
 * comment or a same-named local never satisfies or trips it):
 *  - a READER is one of the store's accessors (lib/agent-identity.ts: agentIdentity, seatAvatar,
 *    resolveRoleKey, …) or any module-level function, anywhere, that calls a reader — found
 *    transitively, so identityOf, nameOf and a helper written tomorrow are all covered;
 *  - a component (a PascalCase function) that calls a reader at render must call a SUBSCRIBING
 *    hook (useIdentityVersion, useIdentity, useAgentIdentities, or any hook that calls one);
 *  - a hook that calls a reader must itself subscribe;
 *  - a useMemo whose callback calls a reader lists the subscribed version in its deps, or the
 *    memo hands back the stale answer even though the component re-rendered.
 * Calls inside an inline `on…={…}` handler run when the person acts, not at render, and are not
 * counted. A server route (no "use client", cannot hold a hook) renders once per request from
 * the store as it is, and is exempt.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, normalize } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const ROOTS = ["app", "lib", "components"];
/** The module that holds the store. Its exported functions are the seed readers. */
const STORE = "lib/agent-identity.ts";
/** Store functions that only load, prime or subscribe: they do not resolve a name or a face. */
const NOT_READERS = new Set([
  "primeIdentities", "resetIdentities", "subscribeIdentities", "identityVersion", "loadIdentities", "refreshIdentities",
]);
/** The store's subscribing hook: every subscriber reaches it. */
const SEED_HOOK = "lib/use-agent-identities.ts#useIdentityVersion";

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (name === "node_modules" || name.startsWith(".")) continue;
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) out.push(p);
  }
  return out;
}

type Fn = { key: string; name: string; file: string; node: ts.FunctionLikeDeclaration; topLevel: boolean };
type Mod = {
  file: string; src: ts.SourceFile; client: boolean;
  /** Module-level functions by name. */
  local: Map<string, string>;
  /** Imported local name → [module file, imported name]. */
  imports: Map<string, [string, string]>;
  /** Re-exports: exported name → [module file, its name there]; plus `export *` sources. */
  reexports: Map<string, [string, string]>;
  star: string[];
};

const isComponentName = (n: string) => /^[A-Z]/.test(n);
const isHookName = (n: string) => /^use[A-Z]/.test(n);
const isFn = (n: ts.Node): n is ts.FunctionLikeDeclaration =>
  ts.isFunctionDeclaration(n) || ts.isFunctionExpression(n) || ts.isArrowFunction(n) || ts.isMethodDeclaration(n);

/** The name a function goes by: its own, or the const it is assigned to (through memo/forwardRef). */
function fnName(node: ts.FunctionLikeDeclaration): string | null {
  if ((ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node)) && node.name) return node.name.text;
  let p: ts.Node = node.parent;
  while (p && ts.isCallExpression(p)) p = p.parent; // memo(() => …), forwardRef(function (…) {…})
  if (p && ts.isVariableDeclaration(p) && ts.isIdentifier(p.name)) return p.name.text;
  return null;
}

/** True when the function is declared at module scope (directly, or as a module-level const). */
function atModuleScope(node: ts.Node): boolean {
  let p: ts.Node = node.parent;
  while (p && (ts.isCallExpression(p) || ts.isVariableDeclaration(p) || ts.isVariableDeclarationList(p)
    || ts.isVariableStatement(p) || ts.isParenthesizedExpression(p))) p = p.parent;
  return !!p && ts.isSourceFile(p);
}

function resolveModule(from: string, spec: string): string | null {
  if (!spec.startsWith(".")) return null;
  const base = normalize(join(dirname(from), spec));
  for (const c of [base, `${base}.ts`, `${base}.tsx`, join(base, "index.ts"), join(base, "index.tsx")]) {
    if (/\.(ts|tsx)$/.test(c) && existsSync(c)) return c;
  }
  return null;
}

const MODS = new Map<string, Mod>();
const FNS: Fn[] = [];
for (const root of ROOTS) {
  for (const file of walk(root)) {
    const text = readFileSync(file, "utf8");
    const src = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true,
      file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    const mod: Mod = {
      file, src, client: /^\s*["']use client["']/m.test(text.split("\n").slice(0, 5).join("\n")),
      local: new Map(), imports: new Map(), reexports: new Map(), star: [],
    };
    for (const st of src.statements) {
      if (ts.isImportDeclaration(st) && ts.isStringLiteral(st.moduleSpecifier) && st.importClause) {
        const target = resolveModule(file, st.moduleSpecifier.text);
        const nb = st.importClause.namedBindings;
        if (target && nb && ts.isNamedImports(nb)) {
          for (const el of nb.elements) mod.imports.set(el.name.text, [target, (el.propertyName ?? el.name).text]);
        }
      }
      if (ts.isExportDeclaration(st) && st.moduleSpecifier && ts.isStringLiteral(st.moduleSpecifier)) {
        const target = resolveModule(file, st.moduleSpecifier.text);
        if (!target) continue;
        if (!st.exportClause) mod.star.push(target);
        else if (ts.isNamedExports(st.exportClause)) {
          for (const el of st.exportClause.elements) mod.reexports.set(el.name.text, [target, (el.propertyName ?? el.name).text]);
        }
      }
    }
    const visit = (n: ts.Node) => {
      if (isFn(n)) {
        const name = fnName(n);
        if (name) {
          const topLevel = atModuleScope(n);
          const key = `${file}#${name}`;
          if (topLevel) mod.local.set(name, key);
          FNS.push({ key, name, file, node: n, topLevel });
        }
      }
      ts.forEachChild(n, visit);
    };
    visit(src);
    MODS.set(file, mod);
  }
}

/** The module-level function an exported name of `file` is, following re-exports. */
function exported(file: string, name: string, seen = new Set<string>()): string | null {
  const id = `${file}#${name}`;
  if (seen.has(id)) return null;
  seen.add(id);
  const m = MODS.get(file);
  if (!m) return null;
  if (m.local.has(name)) return m.local.get(name)!;
  const re = m.reexports.get(name) ?? m.imports.get(name); // `import { x } …; export { x }`
  if (re) return exported(re[0], re[1], seen);
  for (const s of m.star) { const hit = exported(s, name, seen); if (hit) return hit; }
  return null;
}

/** What a call's callee is, as a module-level function key, or null. */
function callee(file: string, c: ts.CallExpression): string | null {
  if (!ts.isIdentifier(c.expression)) return null;
  const name = c.expression.text;
  const m = MODS.get(file)!;
  if (m.local.has(name)) return m.local.get(name)!;
  const imp = m.imports.get(name);
  return imp ? exported(imp[0], imp[1]) : null;
}

const isHandlerAttr = (n: ts.Node) => ts.isJsxAttribute(n) && /^on[A-Z]/.test(n.name.getText());

/** The calls a function makes, skipping nested components/hooks (their own entries) and, when asked, inline handlers. */
function callsIn(fn: Fn, skipHandlers: boolean): ts.CallExpression[] {
  const out: ts.CallExpression[] = [];
  const visit = (n: ts.Node) => {
    if (n !== fn.node && isFn(n)) {
      const nm = fnName(n);
      if (nm && (isComponentName(nm) || isHookName(nm))) return;
    }
    if (skipHandlers && isHandlerAttr(n)) return;
    if (ts.isCallExpression(n)) out.push(n);
    ts.forEachChild(n, visit);
  };
  if (fn.node.body) visit(fn.node.body);
  return out;
}

/** Readers and subscribing hooks, closed transitively over every module. */
const READERS = new Set<string>();
const isExported = (n: ts.Node) => ts.canHaveModifiers(n) && !!ts.getModifiers(n)?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
for (const f of FNS) {
  if (f.file === STORE && f.topLevel && isExported(f.node) && !NOT_READERS.has(f.name)) READERS.add(f.key);
}
const HOOKS = new Set<string>([SEED_HOOK]);
for (let changed = true; changed;) {
  changed = false;
  for (const f of FNS) {
    if (!f.topLevel || f.file === STORE || isComponentName(f.name)) continue;
    const called = callsIn(f, false).map((c) => callee(f.file, c));
    if (isHookName(f.name)) {
      if (!HOOKS.has(f.key) && called.some((c) => c && HOOKS.has(c))) { HOOKS.add(f.key); changed = true; }
    } else if (!READERS.has(f.key) && called.some((c) => c && READERS.has(c))) {
      READERS.add(f.key); changed = true;
    }
  }
}

const readsAt = (f: Fn, skipHandlers: boolean) => callsIn(f, skipHandlers).some((c) => READERS.has(callee(f.file, c) ?? ""));
const subscribes = (f: Fn) => callsIn(f, false).some((c) => HOOKS.has(callee(f.file, c) ?? ""));
const line = (n: ts.Node) => n.getSourceFile().getLineAndCharacterOfPosition(n.getStart()).line + 1;
const where = (f: Fn) => `${f.file}:${line(f.node)} ${f.name}`;
const renders = (f: Fn) => isComponentName(f.name) && !!f.node.body && MODS.get(f.file)!.client;

/** The names bound to a subscribing hook's result in this function (`const v = useIdentityVersion()`). */
function versionNames(fn: Fn): Set<string> {
  const out = new Set<string>();
  const visit = (n: ts.Node) => {
    if (ts.isVariableDeclaration(n) && n.initializer && ts.isCallExpression(n.initializer)
        && HOOKS.has(callee(fn.file, n.initializer) ?? "")) {
      if (ts.isIdentifier(n.name)) out.add(n.name.text);
    }
    ts.forEachChild(n, visit);
  };
  if (fn.node.body) visit(fn.node.body);
  return out;
}

function callsReader(file: string, node: ts.Node): boolean {
  let hit = false;
  const visit = (n: ts.Node) => {
    if (hit) return;
    if (ts.isCallExpression(n) && READERS.has(callee(file, n) ?? "")) { hit = true; return; }
    ts.forEachChild(n, visit);
  };
  visit(node);
  return hit;
}

describe("a component that resolves an agent's identity hears the identity store", () => {
  it("finds the readers and the subscribing hooks (the rule is not vacuous)", () => {
    for (const r of ["lib/agent-identity.ts#agentIdentity", "lib/agent-world.ts#identityOf", "app/agents/run-face.tsx#nameOf"]) {
      expect(READERS.has(r), r).toBe(true);
    }
    for (const n of ["primeIdentities", "loadIdentities"]) expect(READERS.has(`${STORE}#${n}`)).toBe(false);
    for (const h of ["useIdentityVersion", "useIdentity", "useAgentIdentities"]) {
      expect(HOOKS.has(`lib/use-agent-identities.ts#${h}`), h).toBe(true);
    }
    const comps = FNS.filter((f) => renders(f) && readsAt(f, true)).map((f) => f.name);
    expect(comps).toContain("ActCard"); // nameOf, through run-face.tsx
    expect(comps).toContain("AgentQuickCreate"); // identityOf and a local helper that calls it
    expect(comps.length).toBeGreaterThan(20);
    // The card the bug was seen on resolves through the subscribing hook.
    const card = FNS.find((f) => f.key === "app/agents/roster-zones.tsx#RosterCard")!;
    expect(subscribes(card)).toBe(true);
  });

  it("every component that calls a reader at render calls a subscribing hook", () => {
    const bad = FNS.filter((f) => renders(f) && readsAt(f, true) && !subscribes(f)).map(where);
    expect(bad, "resolves an identity at render without useIdentity/useIdentityVersion").toEqual([]);
  });

  it("every hook that calls a reader subscribes", () => {
    const bad = FNS.filter((f) => isHookName(f.name) && f.node.body && readsAt(f, false) && !subscribes(f)).map(where);
    expect(bad).toEqual([]);
  });

  it("a useMemo that calls a reader lists the identity version in its deps", () => {
    const bad: string[] = [];
    for (const f of FNS.filter((x) => (renders(x) || isHookName(x.name)) && x.node.body)) {
      const names = versionNames(f);
      for (const c of callsIn(f, false)) {
        if (!ts.isIdentifier(c.expression) || c.expression.text !== "useMemo") continue;
        if (!c.arguments[0] || !callsReader(f.file, c.arguments[0])) continue;
        const deps = c.arguments[1];
        const listed = deps && ts.isArrayLiteralExpression(deps)
          && deps.elements.some((e) => ts.isIdentifier(e) && names.has(e.text));
        if (!listed) bad.push(`${f.file}:${line(c)} useMemo in ${f.name}`);
      }
    }
    expect(bad, "memoises an identity without the store version in its deps").toEqual([]);
  });
});
