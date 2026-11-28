// @vitest-environment happy-dom
/**
 * Recent sends still draws the first 15 rows. When the returned array is longer,
 * the page says how many of those rows are on it and how many are not. It does
 * not raise the slice and it does not count rows the response did not include.
 * The capped sentence stays. A failed or dead row shows its error word as text.
 * The status chip stays. A visible sentence longer than six words is a paragraph.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Notification } from "../lib/monitoring";
import { $, click, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const box = vi.hoisted(() => ({ rows: Object.assign([] as Notification[], { capped: false }) }));

vi.mock("../lib/monitoring", async () => {
  const actual = await vi.importActual<typeof import("../lib/monitoring")>("../lib/monitoring");
  const polled = <T,>(data: T) => ({ data, error: null, loading: false, refresh: () => {} });
  return {
    ...actual,
    useContactPoints: () => polled([]),
    usePolicies: () => polled([]),
    useSilences: () => polled({ silences: [], windows: [], inhibit: [] }),
    useNotifications: () => polled(box.rows),
  };
});

const { NotifyTab } = await import("../app/observability/notify");

function send(i: number, over: Partial<Notification> = {}): Notification {
  return {
    id: `n${i}`, kind: "firing", status: "sent", contactPoint: "Console", channel: null,
    subject: `Subject ${i}`, body: null, attempts: 1, errorWord: null,
    sentAt: "2026-10-04T11:50:00.000Z", createdAt: "2026-10-04T11:50:00.000Z", sev: null,
    ...over,
  };
}

function rows(count: number, over: Partial<Notification> = {}, capped = false): Notification[] & { capped: boolean } {
  return Object.assign(Array.from({ length: count }, (_, i) => send(i, over)), { capped });
}

async function open(list: Notification[] & { capped: boolean }): Promise<void> {
  box.rows = list;
  await mount(<NotifyTab {...({ projectId: "p1", actor: "ceo" } as unknown as Parameters<typeof NotifyTab>[0])} />);
  await click($("[data-testid='obs-sends-toggle']"));
}

function note(): HTMLElement | null {
  return document.querySelector("[data-testid='obs-sends-more']");
}

afterEach(() => { unmount(); });

describe("recent sends say how many returned rows are not on the page", () => {
  it("says fifteen are on the page and how many of the returned rows are not", async () => {
    await open(rows(20));
    expect(document.querySelectorAll("[data-testid='obs-send']")).toHaveLength(15);
    const el = note();
    expect(el?.tagName).toBe("P");
    expect(el?.textContent).toBe("15 sends are on this page. 5 more are not.");
    expect(document.body.textContent).not.toContain("This list is the newest matches");
  });

  it("says one returned row is not on the page when sixteen came back", async () => {
    await open(rows(16));
    expect(document.querySelectorAll("[data-testid='obs-send']")).toHaveLength(15);
    expect(note()?.textContent).toBe("15 sends are on this page. 1 more is not.");
  });

  it("stays quiet when every returned row is on the page", async () => {
    for (const count of [1, 15]) {
      unmount();
      await open(rows(count));
      expect(note(), String(count)).toBeNull();
      expect(document.body.textContent, String(count)).not.toContain("are on this page");
      expect(document.querySelectorAll("[data-testid='obs-send']"), String(count)).toHaveLength(count);
    }
  });

  it("keeps the capped sentence and does not count rows the response left out", async () => {
    await open(rows(8, {}, true));
    expect(note()).toBeNull();
    expect($("[data-testid='obs-sends-capped']")?.textContent).toBe(
      "This list is the newest matches. Older matches are not on this page.",
    );
    expect(document.querySelectorAll("[data-testid='obs-send']")).toHaveLength(8);

    unmount();
    await open(rows(20, {}, true));
    expect(note()?.textContent).toBe("15 sends are on this page. 5 more are not.");
    expect($("[data-testid='obs-sends-capped']")?.textContent).toBe(
      "This list is the newest matches. Older matches are not on this page.",
    );
    expect(document.body.textContent).not.toContain("20 more");
  });
});

describe("a failed or dead send shows its error word", () => {
  it("shows the word as text and keeps the status chip", async () => {
    await open(rows(1, { status: "failed", errorWord: "Mailbox refused" }));
    const row = $("[data-testid='obs-send']");
    const chip = row?.querySelector(".obs-chip");
    const word = row?.querySelector("[data-testid='obs-send-error']");
    expect(chip?.textContent).toBe("failed");
    expect(word?.textContent).toBe("Mailbox refused");
    expect(word?.tagName).toBe("SPAN");
    expect(chip?.contains(word ?? null)).toBe(false);

    unmount();
    await open(rows(1, { status: "dead", errorWord: "Timed out" }));
    expect($("[data-testid='obs-send'] .obs-chip")?.textContent).toBe("dead");
    expect($("[data-testid='obs-send-error']")?.textContent).toBe("Timed out");
  });

  it("puts an error sentence longer than six words in a paragraph", async () => {
    const errorWord = "The contact point webhook is not in the secret store";
    await open(rows(1, { status: "dead", errorWord }));
    const word = $("[data-testid='obs-send-error']");
    expect(word?.tagName).toBe("P");
    expect(word?.textContent).toBe(errorWord);
    expect(errorWord.trim().split(/\s+/).length).toBeGreaterThan(6);
    expect($("[data-testid='obs-send'] .obs-chip")?.textContent).toBe("dead");
  });

  it("does not invent a word, and does not show one for a send that is not failed or dead", async () => {
    await open(rows(1, { status: "failed", errorWord: null }));
    expect($("[data-testid='obs-send-error']")).toBeNull();
    expect($("[data-testid='obs-send'] .obs-chip")?.textContent).toBe("failed");

    unmount();
    await open(rows(1, { status: "failed", errorWord: "   " }));
    expect($("[data-testid='obs-send-error']")).toBeNull();

    unmount();
    await open(rows(1, { status: "sent", errorWord: "Mailbox refused" }));
    expect($("[data-testid='obs-send-error']")).toBeNull();
    expect(document.body.textContent).not.toContain("Mailbox refused");
    expect($("[data-testid='obs-send'] .obs-chip")?.textContent).toBe("sent");
  });
});
