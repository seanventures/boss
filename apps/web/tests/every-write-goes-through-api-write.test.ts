import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";
import {
  PRESS_CARRYING_COMPONENTS, serverActionModules, serverActionRefs,
} from "./_helpers/server-action-refs";

/**
 * Every console write carries its press: a fetch through lib/api-write.ts, a server action
 * through a SubmitButton form or lib/call-action.ts. No exceptions without a reason here.
 *
 * The same duplicate came back on a new button four times — 2026-08-22 a run started twice,
 * 2026-08-30 "Open preview happened twice", 2026-09-10 duplicate chat actions, 2026-10-02 "Stop
 * and run again" pressed twice started two workflows — because each fix guarded the one button
 * that broke. The guard now lives in one helper (a key per press, a double click joins the first
 * call), and this scan is what keeps it the ONLY way: it reads every .ts/.tsx under app/, lib/
 * and components/ and fails on any fetch that can write without it.
 *
 * "Can write" is deliberately broad. A literal POST/PUT/PATCH/DELETE is obvious; a computed
 * `method:` is a write some of the time; and an init that SPREADS a caller's options
 * (`fetch(url, { ...init })`) is a write whenever a caller passes one — which is exactly how a
 * local wrapper hides a POST from a naive grep.
 */

const ROOT = process.cwd();

/** The only places a raw fetch may write, and why. Each must still exist and still fetch. */
const ALLOWED: Record<string, string> = {
  "lib/api-write.ts": "the helper itself — the one place that sends the write",
  "app/v1/[...path]/route.ts":
    "the transparent same-origin proxy: it forwards the browser's own Idempotency-Key unchanged "
    + "(asserted below), so the key minted by apiWrite in the browser reaches the API as is",
};

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
    else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) out.push(path);
  }
  return out;
}

/** The text between a call's parentheses, skipping strings and template literals. */
function callArgs(src: string, open: number): string {
  let depth = 1;
  let i = open;
  const quotes: string[] = [];
  while (i < src.length) {
    const c = src[i];
    const top = quotes[quotes.length - 1];
    if (top === "'" || top === '"' || top === "`") {
      if (c === "\\") { i += 2; continue; }
      if (c === top) quotes.pop();
      else if (top === "`" && src.startsWith("${", i)) { quotes.push("{"); i += 2; continue; }
      i += 1;
      continue;
    }
    if (c === "'" || c === '"' || c === "`") quotes.push(c);
    else if (c === "{") quotes.push("{");
    else if (c === "}" && top === "{") quotes.pop();
    else if (c === "(") depth += 1;
    else if (c === ")" && --depth === 0) return src.slice(open, i);
    i += 1;
  }
  return src.slice(open);
}

/** Split off the init argument at the first top-level comma. */
function initOf(args: string): string {
  let depth = 0;
  let quote: string | null = null;
  for (let i = 0; i < args.length; i += 1) {
    const c = args[i];
    if (quote) {
      if (c === "\\") { i += 1; continue; }
      if (c === quote) quote = null;
      continue;
    }
    if (c === "'" || c === '"' || c === "`") quote = c;
    else if ("([{".includes(c)) depth += 1;
    else if (")]}".includes(c)) depth -= 1;
    else if (c === "," && depth === 0) return args.slice(i + 1).trim();
  }
  return "";
}

export function writeCapableFetches(path: string): string[] {
  const src = codeOnly(path);
  const found: string[] = [];
  const call = /(?<![\w$.])(?:(?:window|globalThis|self)\.)?fetch\(/g;
  for (let m = call.exec(src); m; m = call.exec(src)) {
    const init = initOf(callArgs(src, m.index + m[0].length));
    if (!init) continue; // fetch(url): a GET
    // `method: X`, or the shorthand `method,` — a variable, so sometimes a write.
    const named = /\bmethod\s*(?::\s*([^,}\n]+)|(?=\s*[,}\n]))/.exec(init);
    const method = named ? (named[1]?.trim() || "method") : undefined;
    const readLiteral = method && /^["'`](GET|HEAD)["'`]$/i.test(method);
    const writes =
      (method && !readLiteral)                // a literal write, or computed: sometimes a write
      || /\.\.\./.test(init)                  // spreads a caller's options: may carry a method
      || !init.startsWith("{");               // an opaque init variable: may carry a method
    if (writes) {
      const line = src.slice(0, m.index).split("\n").length;
      found.push(`${path}:${line}  fetch(…, ${init.replace(/\s+/g, " ").slice(0, 60)}…)`);
    }
  }
  return found;
}

const FILES = ["app", "lib", "components"]
  .flatMap((dir) => sourceFiles(join(ROOT, dir)))
  .map((path) => relative(ROOT, path));

describe("every console write goes through lib/api-write.ts", () => {
  it("finds the console's real source to scan", () => {
    expect(FILES.length).toBeGreaterThan(150);
    expect(FILES).toContain("app/delivery-workflows-ui.tsx");
  });

  it("has no fetch that can write outside the helper", () => {
    const offenders = FILES.filter((f) => !(f in ALLOWED)).flatMap(writeCapableFetches);
    expect(offenders, "use apiWrite (or writeAwareFetch for a pass-through wrapper)").toEqual([]);
  });

  it("allows nothing that does not exist or no longer needs allowing", () => {
    for (const [file, reason] of Object.entries(ALLOWED)) {
      expect(FILES, file).toContain(file);
      expect(writeCapableFetches(file).length, `${file} no longer writes: drop it`)
        .toBeGreaterThan(0);
      expect(reason.split(" ").length).toBeGreaterThan(6);
    }
  });

  it("actually uses the helper where the console writes", () => {
    const users = FILES.filter((f) => /\b(apiWrite|writeAwareFetch)\(/.test(codeOnly(f)));
    expect(users.length).toBeGreaterThan(40);
  });

  it("catches the shapes a grep for 'POST' would miss", () => {
    const probe = (code: string) => {
      const file = join(ROOT, "tests", "__probe.tmp.ts");
      require("node:fs").writeFileSync(file, code);
      try { return writeCapableFetches(file).length; } finally { require("node:fs").unlinkSync(file); }
    };
    expect(probe('fetch("/a", { method: "POST" })')).toBe(1);
    expect(probe('fetch("/a", { method: remove ? "DELETE" : "POST" })')).toBe(1);
    expect(probe("fetch(url, { ...init, cache: \"no-store\" })")).toBe(1);
    expect(probe("fetch(url, init)")).toBe(1);
    expect(probe('window.fetch("/a", { method: "PUT" })')).toBe(1);
    expect(probe("fetch(target, { method, headers })")).toBe(1);
    expect(probe('fetch("/a", { cache: "no-store" })')).toBe(0);
    expect(probe('fetch("/a", { method: "GET" })')).toBe(0);
    expect(probe('fetch("/a")')).toBe(0);
    expect(probe('apiWrite("/a", { method: "POST" })')).toBe(0);
  });
});

describe("the key travels the whole way to the API", () => {
  it("the same-origin proxy forwards Idempotency-Key rather than stripping it", () => {
    const proxy = codeOnly("app/v1/[...path]/route.ts");
    const stripped = /const STRIPPED = new Set\(\[([\s\S]*?)\]\)/.exec(proxy)?.[1] ?? "";
    expect(stripped).toContain("authorization");
    expect(stripped.toLowerCase()).not.toContain("idempotency");
  });

  it("the typed client sends its writes through the helper", () => {
    expect(codeOnly("lib/client.ts")).toMatch(/fetchImpl:\s*writeAwareFetch/);
  });

  it("a server-action press reaches the server: SubmitButton names it, middleware lifts it", () => {
    expect(codeOnly("app/submit-button.tsx")).toMatch(/name=\{INTENT_FIELD\}/);
    expect(codeOnly("middleware.ts")).toMatch(/intentFromActionBody/);
    expect(codeOnly("instrumentation.ts")).toMatch(/installServerIntentResolver/);
  });

  it("the proxy routes under app/api derive their keys from the incoming one", () => {
    // Every handler that writes reaches the API through apiWrite/writeAwareFetch or the typed
    // client; the scan above proves no raw write remains, and instrumentation.ts gives the
    // server side the incoming Idempotency-Key to derive from.
    const routes = FILES.filter((f) => f.startsWith("app/api/") && f.endsWith("route.ts"));
    expect(routes.length).toBeGreaterThan(30);
    for (const route of routes) expect(writeCapableFetches(route), route).toEqual([]);
  });
});

describe("every server action carries its press", () => {
  /**
   * The fetch scan above was the whole enforcement in 45e7693, and it could not see the other
   * door: a server action called from code. 63 such calls — Deploy, Redeploy, the shepherd's
   * sweep, the agent chat turn (the 2026-09-10 duplicate-chat path) — went out with no key while
   * this file reported the console clean. Every mention of a server action in the console's code
   * must now be one of: `callAction(X)` (lib/call-action.ts), the action of a `<form>` that
   * renders a SubmitButton, a prop of a component verified to invoke it through callAction, a
   * call made while rendering on the server (there is no press — the call is the render), or a
   * `typeof`.
   */
  const REFS = serverActionRefs(ROOT);
  /** The only direct calls allowed in client code, and why. */
  const DIRECT_CALLS_ALLOWED: Record<string, string> = {
    "lib/call-action.ts runPressedAction":
      "the relay itself: callAction sends every press through it with the key as its first argument",
  };

  it("finds the console's server actions and the code that uses them", () => {
    const modules = serverActionModules(ROOT);
    expect(Object.keys(modules)).toContain("app/actions.ts");
    expect(Object.keys(modules)).toContain("app/press-relay.ts");
    expect(Object.values(modules).flat().length).toBeGreaterThan(150);
    expect(REFS.length).toBeGreaterThan(150);
    // The calls the review named are seen, and seen to be carried.
    for (const [file, name] of [["app/deploy-ui.tsx", "deployTargetAction"],
                                ["app/deploy-ui.tsx", "redeployAction"],
                                ["app/shepherd-ui.tsx", "shepherdSweepAction"],
                                ["app/agent-ui.tsx", "askBossAction"],
                                ["app/agent-ui.tsx", "runAgentActionAction"]]) {
      const mine = REFS.filter((r) => r.file === file && r.name === name && r.how !== "typeof");
      expect(mine.length, `${file} ${name}`).toBeGreaterThan(0);
      expect(mine.every((r) => r.how === "callAction"), `${file} ${name}`).toBe(true);
    }
  });

  it("has no server action reached any other way", () => {
    const offenders = REFS
      .filter((r) => !r.ok && !(`${r.file} ${r.name}` in DIRECT_CALLS_ALLOWED))
      .map((r) => `${r.file}:${r.line}  ${r.name} — ${r.how}`);
    expect(offenders,
      "wrap it: callAction(action)(...args), or make the form's button a SubmitButton").toEqual([]);
  });

  it("allows nothing that does not exist or no longer needs allowing", () => {
    for (const [ref, reason] of Object.entries(DIRECT_CALLS_ALLOWED)) {
      const [file, name] = ref.split(" ");
      expect(REFS.some((r) => r.file === file && r.name === name && !r.ok), ref).toBe(true);
      expect(reason.split(" ").length).toBeGreaterThan(6);
    }
  });

  it("each component trusted to carry a press really invokes its action through callAction", () => {
    for (const [component, file] of Object.entries(PRESS_CARRYING_COMPONENTS)) {
      const src = codeOnly(file);
      const start = src.search(new RegExp(`function ${component}\\b`));
      expect(start, `${component} in ${file}`).toBeGreaterThan(-1);
      const body = src.slice(start, src.indexOf("\nexport function", start + 1));
      expect(body, component).toMatch(/callAction\(action\)\(/);
      expect(body, `${component} must not also call the action directly`)
        .not.toMatch(/(?<![\w.(])action\(/);
    }
  });

  it("catches the shapes the review found unguarded", () => {
    const probe = (code: string, client = true) => {
      const dir = join(ROOT, "app", "__probe_tmp");
      const fs = require("node:fs");
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(join(dir, "probe.tsx"),
        `${client ? '"use client";\n' : ""}import { redeployAction, deployTargetAction } from "../actions";\n${code}`);
      try {
        return serverActionRefs(ROOT).filter((r) => r.file.startsWith("app/__probe_tmp/"))
          .map((r) => r.ok);
      } finally { fs.rmSync(dir, { recursive: true, force: true }); }
    };
    expect(probe("async function f() { await redeployAction('d'); }")).toEqual([false]);
    expect(probe("const go = () => void deployTargetAction('t', 'main');")).toEqual([false]);
    expect(probe("const p = <Panel onSave={redeployAction} />;")).toEqual([false]);
    expect(probe("const f = <form action={redeployAction}><button type=\"submit\">Go</button></form>;"))
      .toEqual([false]);
    expect(probe("async function f() { await callAction(redeployAction)('d'); }")).toEqual([true]);
    expect(probe("const f = <form action={redeployAction}><SubmitButton>Go</SubmitButton></form>;"))
      .toEqual([true]);
    expect(probe("export default async function Page() { await redeployAction('d'); }", false))
      .toEqual([true]);
  });
});

void readFileSync;
