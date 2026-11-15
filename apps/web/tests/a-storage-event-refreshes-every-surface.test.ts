/**
 * A `storage.*` or `infra.hold.*` event on the console's stream (BOSSAPP-1156) refreshes every
 * surface that shows the incident: the strip is on every area ("*"), the card on the Command
 * Center, Storage under Govern, the paused runs on the work screens; and it restarts the client
 * polls (usePolled) through the console's change signal, so the strip moves within a second of
 * the agent, not at its next 10 s tick.
 */
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

import { EVENT_INVALIDATES, createEventSubscription, invalidatedKeys, isStorageEvent } from "../lib/console-refresh";
import { codeOnly } from "./_helpers/source";

describe("a storage event", () => {
  it("invalidates every area, the Command Center, Govern and the work screens", () => {
    for (const type of ["storage.pressure", "storage.incident.opened", "storage.incident.resolved", "storage.action.done", "storage.rule.proposed"]) {
      const keys = invalidatedKeys(type);
      expect(keys).toContain("*");
      expect(keys).toContain("overview");
      expect(keys).toContain("budget");
      expect(keys).toContain("activity");
    }
    expect(invalidatedKeys("infra.hold.set")).toContain("delivery_workflows");
    expect(invalidatedKeys("infra.hold.released")).toContain("*");
    expect(invalidatedKeys("decision.created")).not.toContain("*");
    expect(EVENT_INVALIDATES.some(([p]) => p === "storage.")).toBe(true);
  });

  it("is recognised, and the shell watches '*' and sends the change signal for it", () => {
    expect(isStorageEvent("storage.incident.opened")).toBe(true);
    expect(isStorageEvent("infra.hold.set")).toBe(true);
    expect(isStorageEvent("workflow.step.done")).toBe(false);
    const shell = codeOnly(join(__dirname, "../app/use-console-events.ts"));
    expect(shell).toMatch(/watching: \(\) => \[area\.current, "\*"\]/);
    expect(shell).toMatch(/isStorageEvent\(reason\.event\.event_type\)\) notifyChanged\(/);
  });

  it("reaches a viewer on any area through the subscription", () => {
    let onmessage: ((ev: { data: string }) => void) | null = null;
    const invalidate = vi.fn();
    const timers: Array<() => void> = [];
    createEventSubscription({
      open: () => ({ set onmessage(fn) { onmessage = fn; }, get onmessage() { return onmessage; }, onerror: null, addEventListener: () => {}, close: () => {} }) as never,
      watching: () => ["agents", "*"],
      invalidate,
      setTimer: (fn) => { timers.push(fn); return 1; },
      clearTimer: () => {},
    });
    onmessage!({ data: JSON.stringify({ id: "1", event_type: "storage.incident.opened" }) });
    timers.forEach((t) => t());
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(invalidate.mock.calls[0][0]).toMatchObject({ kind: "event", event: { event_type: "storage.incident.opened" } });
  });
});
