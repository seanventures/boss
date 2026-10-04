/**
 * Agent names (tester K19). When the identities request was aborted, every screen fell back to the
 * bundled cast — which still called the frontend engineer "Pixel", renamed Prism in its role file
 * — and stayed there until a reload. The bundled callsigns are the role files' own, and a failed
 * load is retried on its own.
 */
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CAST } from "../lib/avatar-kit/cast";
import { agentIdentity, loadIdentities, resetIdentities } from "../lib/agent-identity";

const ROLES = resolve(__dirname, "../../../packages/specpack/roles");

afterEach(() => { vi.useRealTimers(); resetIdentities(); });

describe("the bundled cast says what the role files say", () => {
  it("every hand-designed callsign matches its role's callsign", () => {
    const files = new Map(readdirSync(ROLES).filter((f) => f.endsWith(".yaml"))
      .map((f) => [f.replace(/\.yaml$/, ""), readFileSync(resolve(ROLES, f), "utf8")]));
    const checked: string[] = [];
    for (const member of CAST) {
      const yaml = files.get(member.roleKey);
      const said = yaml?.match(/^\s*callsign:\s*["']?([A-Za-z][\w-]*)/m)?.[1];
      if (!member.callsign || !said) continue;
      expect(member.callsign, member.roleKey).toBe(said);
      checked.push(member.roleKey);
    }
    expect(checked).toContain("frontend_engineer");
    expect(agentIdentity("frontend_engineer").callsign).toBe("Prism");
  });
});

describe("a failed identities load retries by itself", () => {
  it("an aborted request is followed by another, and the install's names land", async () => {
    vi.useFakeTimers();
    resetIdentities();
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new DOMException("aborted", "AbortError"))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ role_key: "pm", callsign: "Jun" }], aliases: {} })));
    await loadIdentities("p-retry", fetcher as unknown as typeof fetch);
    expect(agentIdentity("pm").callsign).toBe("Juno");
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(agentIdentity("pm").callsign).toBe("Jun");
  });
});
