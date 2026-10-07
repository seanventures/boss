/**
 * Every button that runs a server action says what happened — success or failure, and why.
 *
 * Ninety-four server actions returned `Promise<void>` and failed by throwing. In a production
 * build Next replaces the thrown message with "An error occurred in the Server Components
 * render", and the console has no error boundary, so a refused Approve, a 403 on Create agent
 * or a 409 on Retry crashed the page or did nothing visible. A dozen more never read the
 * response and reported success for a refusal; Approvals swallowed the error and said
 * "Decision recorded. Inbox revalidated."
 *
 * The fix is one shape (lib/action-result.ts) and one form (ActionForm in client-forms.tsx).
 * These checks ENUMERATE every server action and every form, so a new one cannot opt out.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it, vi } from "vitest";

import { codeOnly } from "./_helpers/source";
import {
  describeActionError,
  failureSentence,
  missingInput,
  whatToDo,
} from "../lib/action-result";

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else if (/\.(ts|tsx)$/.test(name)) out.push(path);
  }
  return out;
}

const FILES = walk("app");
const SERVER_FILES = FILES.filter((f) =>
  /^\s*["']use server["']/.test(readFileSync(f, "utf8")));

type Sig = { file: string; name: string; params: string; returns: string };

/** The declared return type: everything between `):` and the body's `{`, generics included. */
function returnType(src: string, from: number): string {
  let angle = 0;
  let brace = 0;
  for (let i = from; i < src.length; i += 1) {
    const c = src[i];
    if (c === "<") angle += 1;
    else if (c === ">" && src[i - 1] !== "=") angle -= 1;
    else if (c === "{" && angle === 0 && brace === 0) {
      return src.slice(from, i).replace(/^\s*:\s*/, "").trim();
    } else if (c === "{") brace += 1;
    else if (c === "}") brace -= 1;
  }
  return "";
}

const SERVER_ACTIONS: Sig[] = SERVER_FILES.flatMap((file) => {
  const src = readFileSync(file, "utf8");
  return [...src.matchAll(/export async function (\w+)\(([^)]*)\)/g)]
    .map((m) => ({ file, name: m[1], params: m[2],
                   returns: returnType(src, (m.index ?? 0) + m[0].length) }));
});
const ACTION_NAMES = new Set(SERVER_ACTIONS.map((a) => a.name));

describe("every server action returns a result instead of throwing", () => {
  it("finds the server actions it is checking (the scan works)", () => {
    expect(SERVER_FILES.length).toBeGreaterThanOrEqual(5);
    expect(SERVER_ACTIONS.length).toBeGreaterThan(150);
    expect(ACTION_NAMES.has("resolveDecisionAction")).toBe(true);
  });

  it("no server action is declared Promise<void>", () => {
    const voids = SERVER_ACTIONS.filter((a) => /^Promise<\s*void\s*>$/.test(a.returns))
      .map((a) => `${relative(".", a.file)}: ${a.name}`);
    expect(voids).toEqual([]);
  });

  it("every action a form submits returns a result type", () => {
    // A form action takes the FormData. Its return must be a result the form can render —
    // ActionResult, or one of the screen-specific *Result shapes that already carry `error`.
    const offenders = SERVER_ACTIONS
      .filter((a) => /formData\s*:\s*FormData/.test(a.params))
      .filter((a) => !/Result\b|error\??\s*:/.test(a.returns))
      .map((a) => `${relative(".", a.file)}: ${a.name} -> ${a.returns || "(undeclared)"}`);
    expect(offenders).toEqual([]);
  });

  it("every Promise<ActionResult> action in the shared files runs its body through act()", () => {
    // act() turns a throw into a sentence and re-throws only Next's own redirect/notFound.
    const offenders: string[] = [];
    for (const file of SERVER_FILES) {
      const src = codeOnly(file);
      for (const m of src.matchAll(
        /export async function (\w+)\(([^)]*)\)\s*:\s*Promise<ActionResult>\s*\{\s*([\s\S]{0,40})/g)) {
        const isFormAction = /^\s*formData\s*:\s*FormData\s*,?\s*$/.test(m[2]);
        if (isFormAction && !m[3].startsWith("return act(")) {
          offenders.push(`${relative(".", file)}: ${m[1]}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("no action awaits a bare fetch and ignores the answer", () => {
    // `await fetch(...)` as a statement throws nothing on a 403 — the refusal reads as success.
    const offenders = SERVER_FILES.flatMap((file) =>
      codeOnly(file).split("\n")
        .map((line, i) => ({ line, i }))
        .filter(({ line }) => /^\s*await fetch\(/.test(line))
        .map(({ i }) => `${relative(".", file)}:${i + 1}`));
    expect(offenders).toEqual([]);
  });
});

describe("every form that submits a server action goes through ActionForm", () => {
  it("no <form action={serverAction}> is left raw", () => {
    const offenders: string[] = [];
    for (const file of FILES.filter((f) => f.endsWith(".tsx"))) {
      // Raw source, not codeOnly: its comment stripper eats code after a "/*" in a string.
      const src = readFileSync(file, "utf8");
      for (const m of src.matchAll(/<form\b((?:[^<>]|=>)*?)>/g)) {
        const named = /\baction=\{\s*(\w+)\s*\}/.exec(m[1]);
        if (named && ACTION_NAMES.has(named[1])) {
          offenders.push(`${relative(".", file)}: <form action={${named[1]}}>`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("finds the forms it is checking (the scan works)", () => {
    const count = FILES.filter((f) => f.endsWith(".tsx"))
      .reduce((n, f) => n + (readFileSync(f, "utf8").match(/<ActionForm\b/g)?.length ?? 0), 0);
    // A floor that proves the scan finds forms, not a quota. 99 at integration; the Oct 4-5
    // redesigns retired the old Infra, System Configuration, Observability and Panels screens
    // (14d0f8a8, f923b20c) and their forms with them: 76 after. Lower it only for a removal.
    expect(count).toBeGreaterThan(70);
  });

  it("ActionForm renders the result: the failure sentence inline and as a toast", () => {
    const form = codeOnly("app/client-forms.tsx");
    expect(form).toContain('data-testid="action-form-error"');
    expect(form).toContain("showToast('error'");
    expect(form).toContain("isActionResult(returned)");
    // Pending comes from the form itself, so no form needs wiring for it.
    expect(form).toContain("useFormStatus()");
  });
});

describe("a failure says what happened and what to do", () => {
  it("each HTTP refusal names a next step", () => {
    for (const status of [400, 401, 403, 404, 409, 422, 429, 500, 503]) {
      expect(whatToDo(status).length).toBeGreaterThan(20);
    }
    expect(whatToDo(403)).toContain("ask an Admin");
  });

  it("the server's sentence comes first and is not repeated", () => {
    expect(failureSentence("Decision is already approved and cannot be rejected.", 409))
      .toBe("Decision is already approved and cannot be rejected. "
        + "Reload to see its current state before trying again.");
  });

  it("a status-carrying error keeps the server's explanation", () => {
    const err = Object.assign(new Error("principal 'ceo' may not act as 'vp_agents'"),
      { status: 403 });
    expect(describeActionError(err)).toContain("principal 'ceo' may not act as 'vp_agents'");
    expect(describeActionError(err)).toContain("not allowed");
  });

  it("an unreachable API is called that, not 'fetch failed'", () => {
    expect(describeActionError(new Error("fetch failed"))).toContain("could not be reached");
  });

  it("an empty required field is named, not silently skipped", () => {
    expect(missingInput("roleKey").error).toBe(
      "Role key is required — fill it in and try again.");
    expect(missingInput("title", "instructions").error).toContain("Title and instructions are");
  });
});

describe("act() turns the body's outcome into a result", () => {
  it("success, failure and Next's redirect", async () => {
    const { act, ApiRefusal } = await import("../lib/server-action");
    const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(await act(async () => undefined, "Saved.")).toEqual({ ok: true, message: "Saved." });
    const refused = await act(async () => { throw new ApiRefusal("nope", 403); });
    expect(refused.ok).toBe(false);
    expect(refused.error).toContain("nope");
    const redirect = Object.assign(new Error("NEXT_REDIRECT"),
      { digest: "NEXT_REDIRECT;replace;/?area=x;307;" });
    await expect(act(async () => { throw redirect; })).rejects.toBe(redirect);
    quiet.mockRestore();
  });
});
