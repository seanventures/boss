import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * A press sent twice sends the SAME request twice — so the API can tell it is one press.
 *
 * Review of 6ca9119, 2026-10-02. On the server, each API call a server action makes is keyed
 * `<press intent>.<hash of method, path and body>`. Thirteen server actions put `Date.now()` or
 * `crypto.randomUUID()` into that body — an outbox dry run's dedupe_key, a decision digest's, a
 * work item's subject id, the mission-control writes — so the two copies of one double-submitted
 * SubmitButton press (the 2026-10-02 shape) hashed to two keys and both ran, and the API's own
 * dedupe_key was random too, so nothing downstream caught it. Such a value now comes from
 * `pressToken()`: fresh for each press, the same for its copies.
 */

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));

const SOURCES = /Date\.now\(\)|crypto\.randomUUID\(\)|Math\.random\(\)|newIntentKey\(\)|new Date\(\)/g;

function serverActionModules(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) {
        if (name !== "node_modules") walk(path);
      } else if (/\.(tsx?|jsx?)$/.test(name)
                 && /^\s*["']use server["']/.test(readFileSync(path, "utf8"))) {
        out.push(path);
      }
    }
  };
  ["app", "lib", "components"].forEach((root) => { try { walk(root); } catch { /* absent */ } });
  return out;
}

describe("no server action puts a fresh value of its own into what it sends", () => {
  it("finds the server-action modules (the enumeration is not empty)", () => {
    const found = serverActionModules();
    expect(found).toEqual(expect.arrayContaining([join("app", "actions.ts"),
      join("app", "work-orchestration-actions.ts"), join("app", "agent-studio-actions.ts")]));
  });

  it("every 'use server' module takes a per-press value from pressToken(), never the clock or a random id", () => {
    const offenders: string[] = [];
    for (const file of serverActionModules()) {
      const code = codeOnly(file);
      for (const m of code.matchAll(SOURCES)) {
        const line = code.slice(0, m.index).split("\n").length;
        offenders.push(`${file}:${line} ${m[0]}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe("the same press, submitted twice, sends one request twice", () => {
  afterEach(async () => {
    vi.unstubAllGlobals();
    const { installServerIntentResolver } = await import("../lib/api-write");
    installServerIntentResolver(async () => undefined);
  });

  async function twice(press: string, act: () => Promise<unknown>) {
    const sent: Array<{ key: string | null; body: string }> = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: unknown, init?: RequestInit) => {
      sent.push({ key: new Headers(init?.headers).get("Idempotency-Key"),
                  body: String(init?.body ?? "") });
      return Response.json({ id: "x", status: "ok" });
    }));
    const { installServerIntentResolver } = await import("../lib/api-write");
    installServerIntentResolver(async () => press);
    await act();
    await act();
    return sent;
  }

  it("a work item added with no subject id: one key, one body — the API makes one item", async () => {
    const { upsertRankedWorkAction } = await import("../app/work-orchestration-actions");
    const form = new FormData();
    form.set("project_id", "p");
    form.set("title", "Ship the inbox");
    const sent = await twice("0b8f6a52-3c1d-4e2f-9a7b-112233445566",
                             () => upsertRankedWorkAction(form));
    expect(sent).toHaveLength(2);
    expect(sent[0].key).toBeTruthy();
    expect(sent[1]).toEqual(sent[0]);
    // And a DIFFERENT press is a different item.
    const other = await twice("1c8f6a52-3c1d-4e2f-9a7b-112233445566",
                              () => upsertRankedWorkAction(form));
    expect(other[0].key).not.toBe(sent[0].key);
    expect(other[0].body).not.toBe(sent[0].body);
  });
});
