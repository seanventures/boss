// @vitest-environment happy-dom
/**
 * NO ACTION SHOWS A RAW "step_up_required". The App target sheet's Create showed it in red: Infra's
 * writes go through apiWrite, which had no step-up handling. Now:
 *  - apiWrite (every browser write: tests/every-write-goes-through-api-write.test.ts) opens the
 *    step-up sheet on 401 step_up_required and sends the write once more with a fresh key;
 *  - every server action is called through callAction WITH its permission (or ActionForm, which
 *    handles it), so its step_up_required result opens the sheet too.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiWrite } from "../lib/api-write";
import { installMirror } from "../lib/identity/mirror";

const WEB = join(__dirname, "..");
afterEach(() => { installMirror(null); vi.unstubAllGlobals(); });

describe("step-up on every write", () => {
  it("apiWrite opens the sheet and re-sends with a fresh key", async () => {
    const keys: string[] = [];
    let n = 0;
    vi.stubGlobal("fetch", vi.fn(async (_u: string, init: RequestInit) => {
      keys.push(new Headers(init.headers).get("Idempotency-Key") ?? "");
      n += 1;
      return n === 1
        ? new Response(JSON.stringify({ detail: { error: "step_up_required" } }), { status: 401 })
        : new Response(JSON.stringify({ id: "t1" }), { status: 200 });
    }));
    const stepUp = vi.fn(async () => true);
    installMirror({ check: () => ({ allowed: true, reason: "", needsStepUp: false }) as never, stepUp, refresh: async () => {} });
    const r = await apiWrite("/v1/deployments/stack/targets", { method: "POST", body: "{}" }, { press: { scope: "t", label: "Create" } });
    expect(r.status).toBe(200);
    expect(stepUp).toHaveBeenCalledTimes(1);
    expect(keys).toHaveLength(2);
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("cancelled: the refusal comes back, sent once", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ error: "step_up_required" }), { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);
    installMirror({ check: () => ({ allowed: true, reason: "", needsStepUp: false }) as never, stepUp: async () => false, refresh: async () => {} });
    const r = await apiWrite("/v1/x", { method: "POST", body: "{}" });
    expect(r.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("every server action call names its permission (so a step-up result opens the sheet)", () => {
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const full = join(dir, name);
        const rel = relative(WEB, full);
        if (/(^|\/)(node_modules|\.next|tests)(\/|$)/.test(rel)) continue;
        if (statSync(full).isDirectory()) { walk(full); continue; }
        if (!/\.(ts|tsx)$/.test(name) || rel === "lib/call-action.ts") continue;
        const text = readFileSync(full, "utf8");
        for (const m of text.matchAll(/callAction\(([^()]*(?:\([^()]*\))?[^()]*)\)/g)) {
          const args = m[1].split(",").map((a) => a.trim()).filter(Boolean);
          // ActionForm (app/client-forms.tsx) handles step_up_required on its own result.
          if (args.length < 3 && rel !== "app/client-forms.tsx") offenders.push(`${rel}: callAction(${m[1].slice(0, 60)})`);
        }
      }
    };
    for (const top of ["app", "components", "lib"]) walk(join(WEB, top));
    expect(offenders).toEqual([]);
  });
});
