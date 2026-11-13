/**
 * Event-driven refresh (BOSSAPP-835): the console subscribes to GET /v1/events/stream and turns a
 * matching event into ONE coalesced refresh of what is on screen. Polling stays the fallback.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  consoleEventsUrl,
  createEventSubscription,
  invalidatedKeys,
  type EventSourceLike,
} from "../lib/console-refresh";

class FakeSource implements EventSourceLike {
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  closed = false;
  listeners = new Map<string, (ev: { data: string }) => void>();
  addEventListener(type: string, fn: (ev: { data: string }) => void) { this.listeners.set(type, fn); }
  close() { this.closed = true; }
  push(event: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(event) }); }
  resync() { this.listeners.get("resync")?.({ data: "{}" }); }
}

function fakeTimers() {
  const due: Array<{ fn: () => void; id: number }> = [];
  let seq = 0;
  return {
    setTimer: (fn: () => void) => { const id = ++seq; due.push({ fn, id }); return id; },
    clearTimer: (id: unknown) => { const i = due.findIndex((d) => d.id === id); if (i >= 0) due.splice(i, 1); },
    fire() { for (const d of due.splice(0)) d.fn(); },
    pending: () => due.length,
  };
}

function setup(area: string) {
  const source = new FakeSource();
  const timers = fakeTimers();
  const asked: Array<{ kind: string; keys: string[] }> = [];
  let watching = [area];
  const sub = createEventSubscription({
    open: () => source,
    watching: () => watching,
    invalidate: (r) => asked.push({ kind: r.kind, keys: r.keys }),
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
  });
  return { source, timers, asked, sub, look: (a: string) => { watching = [a]; } };
}

describe("the console's event subscription", () => {
  it("maps an event kind to the polled keys it invalidates", () => {
    expect(invalidatedKeys("decision.created")).toEqual(
      expect.arrayContaining(["decisions", "overview", "activity"]));
    expect(invalidatedKeys("code_task.failed")).toContain("code_tasks");
    expect(invalidatedKeys("something.new")).toEqual(["activity"]);
  });

  it("refreshes once for a burst of matching events on the area on screen", () => {
    const t = setup("decisions");
    t.source.push({ id: "e1", event_type: "decision.created" });
    t.source.push({ id: "e2", event_type: "decision.approved" });
    t.source.push({ id: "e3", event_type: "chat.decision_answered" });
    expect(t.asked).toEqual([]);              // coalesced until the debounce
    t.timers.fire();
    expect(t.asked).toHaveLength(1);
    expect(t.asked[0].keys).toContain("decisions");
    expect(t.sub.stats()).toEqual({ events: 3, invalidations: 1, resyncs: 0 });
  });

  it("ignores events for areas that are not on screen, and junk", () => {
    const t = setup("meetings");
    t.source.push({ id: "e1", event_type: "code_task.started" });
    t.source.onmessage?.({ data: "not json" });
    t.source.onmessage?.({ data: JSON.stringify({ id: "x" }) });
    t.timers.fire();
    expect(t.asked).toEqual([]);
    t.look("code_tasks");
    t.source.push({ id: "e2", event_type: "code_task.finished" });
    t.timers.fire();
    expect(t.asked).toHaveLength(1);
  });

  it("a resync from the server refreshes what is on screen", () => {
    const t = setup("delivery_workflows");
    t.source.resync();
    t.timers.fire();
    expect(t.asked).toEqual([{ kind: "resync", keys: ["delivery_workflows"] }]);
    expect(t.sub.stats().resyncs).toBe(1);
  });

  it("closing stops the stream and drops an owed refresh", () => {
    const t = setup("decisions");
    t.source.push({ id: "e1", event_type: "decision.created" });
    expect(t.timers.pending()).toBe(1);
    t.sub.close();
    expect(t.source.closed).toBe(true);
    expect(t.timers.pending()).toBe(0);
    t.source.push({ id: "e2", event_type: "decision.created" });
    t.timers.fire();
    expect(t.asked).toEqual([]);
  });

  it("subscribes through the same-origin route, scoped to the project", () => {
    expect(consoleEventsUrl("p-1")).toBe("/api/events/stream?project_id=p-1");
    expect(consoleEventsUrl(null)).toBe("/api/events/stream");
  });

  it("the shell keeps polling: the subscription only asks the coordinator for a refresh", () => {
    const shell = readFileSync(join(__dirname, "..", "app", "ConsoleShell.tsx"), "utf8");
    expect(shell).toMatch(/useConsoleEvents\(refreshCoordinator,/);
    const hook = readFileSync(join(__dirname, "..", "app", "use-console-events.ts"), "utf8");
    expect(hook).toMatch(/coordinator\.request\("poll"\)/);
    expect(hook).not.toMatch(/router\.refresh\(/);
  });
});
