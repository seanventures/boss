// @vitest-environment node
/**
 * Recent sends already calls useNotifications. A labeled text field passes q
 * through that hook. An empty field omits q. The 15-row slice and the server
 * cap sentence stay as they are.
 *
 * happy-dom is not installed, and this worktree does not resolve the client
 * bundle, so the test does not mount the screen. It runs sendsQuery from the
 * source and checks that the recent-sends section wires the field to it.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const src = readFileSync(new URL("../app/observability/notify.tsx", import.meta.url), "utf8");

function loadSendsQuery(source: string): (typed: string) => { q: string } | undefined {
  const m = /export function sendsQuery\(typed: string\): \{ q: string \} \| undefined \{\n([\s\S]*?)\n\}/.exec(source);
  if (!m) throw new Error("sendsQuery is not in notify.tsx");
  return new Function("typed", m[1]) as (typed: string) => { q: string } | undefined;
}

describe("recent sends find", () => {
  it("passes q only when someone typed, through a labeled field", () => {
    const sendsQuery = loadSendsQuery(src);
    expect(sendsQuery("")).toBeUndefined();
    expect(sendsQuery("   ")).toBeUndefined();
    expect(sendsQuery("invoice")).toEqual({ q: "invoice" });
    expect(sendsQuery("  SEV1  ")).toEqual({ q: "SEV1" });

    const sends = src.slice(src.indexOf('data-testid="obs-sends-toggle"'), src.indexOf('data-testid="obs-sends-capped"'));
    const fieldAt = sends.indexOf('data-testid="obs-sends-q"');
    const listAt = sends.indexOf('data-testid="obs-sends"');
    expect(fieldAt).toBeGreaterThan(-1);
    expect(listAt).toBeGreaterThan(fieldAt);
    expect(sends).toMatch(/<label[\s\S]*Find in subject or body[\s\S]*data-testid="obs-sends-q"/);
    expect(sends).toMatch(/value=\{q\}/);
    expect(sends).toMatch(/setQ\(e\.target\.value\)/);
    expect(src).toContain('useNotifications(sends ? projectId : "", sendsQuery(q))');
    expect(src).toContain("outbox.data.slice(0, 15)");
    expect(src).toContain('data-testid="obs-sends-capped">This list is the newest matches. Older matches are not on this page.</p>');
  });
});
