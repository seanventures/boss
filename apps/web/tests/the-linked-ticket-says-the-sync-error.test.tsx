// @vitest-environment happy-dom
/**
 * The linked ticket says the sync error beside the status tag, not only in
 * the title. A blank or whitespace error is nothing. More than six words is
 * a paragraph. Six or fewer is a span.
 *
 * Breaks if the error stays padded, a blank error still renders, a long
 * error is a span, the status words or title change, or the screen stops
 * calling syncErrorSentence(tl.sync_error).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { syncErrorSentence, LinkedSyncStatus } = await import("../app/projects/linked-ticket");

const SHORT = "jira said no";
const SIX = "one two three four five six";
const LONG = "one two three four five six seven";

describe("syncErrorSentence", () => {
  it("returns the trimmed error, and nothing when the error is blank", () => {
    expect(syncErrorSentence(`  ${SHORT}  `)).toBe(SHORT);
    expect(syncErrorSentence(SHORT)).toBe(SHORT);
    expect(syncErrorSentence(SIX)).toBe(SIX);
    expect(syncErrorSentence(LONG)).toBe(LONG);
    expect(syncErrorSentence(null)).toBeNull();
    expect(syncErrorSentence(undefined)).toBeNull();
    expect(syncErrorSentence("")).toBeNull();
    expect(syncErrorSentence(" \n\t ")).toBeNull();
  });
});

let root: Root | null = null;
let host: HTMLElement | null = null;

function removeHost(): void {
  act(() => { root?.unmount(); });
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
}

afterEach(() => {
  removeHost();
});

async function show(props: { tone: string; title?: string; status: string; error: string | null | undefined }): Promise<HTMLElement> {
  removeHost();
  const next = document.createElement("div");
  document.body.appendChild(next);
  host = next;
  root = createRoot(next);
  await act(async () => {
    root!.render(
      <LinkedSyncStatus
        tone={props.tone}
        title={props.title}
        status={props.status}
        sentence={syncErrorSentence(props.error)}
      />,
    );
  });
  return next;
}

function statusOf(el: HTMLElement): Element | null {
  return el.querySelector("[data-testid='tk-linked-status']");
}

function errorOf(el: HTMLElement): Element | null {
  return el.querySelector("[data-testid='tk-linked-sync-error']");
}

describe("the linked ticket says the sync error", () => {
  it("shows a short error as a span beside Write-back failed, and keeps the title", async () => {
    const raw = `  ${SHORT}  `;
    const el = await show({ tone: "red", title: raw, status: "Write-back failed", error: raw });
    const status = statusOf(el);
    const note = errorOf(el);
    expect(status?.textContent).toBe("Write-back failed");
    expect(status?.getAttribute("title")).toBe(raw);
    expect(status?.getAttribute("data-tone")).toBe("red");
    expect(status?.className).toBe("tk-tag");
    expect(note?.tagName).toBe("SPAN");
    expect(note?.textContent).toBe(SHORT);
    expect(note?.hasAttribute("data-user-text")).toBe(true);
    expect(note?.parentElement).toBe(status?.parentElement);
    expect(note?.parentElement).not.toBe(status);
  });

  it("shows six words as a span beside Sync paused", async () => {
    const el = await show({ tone: "amber", title: SIX, status: "Sync paused", error: SIX });
    const status = statusOf(el);
    const note = errorOf(el);
    expect(status?.textContent).toBe("Sync paused");
    expect(status?.getAttribute("title")).toBe(SIX);
    expect(status?.getAttribute("data-tone")).toBe("amber");
    expect(note?.tagName).toBe("SPAN");
    expect(note?.textContent).toBe(SIX);
    expect(note?.hasAttribute("data-user-text")).toBe(true);
  });

  it("shows more than six words as a paragraph beside the provider status", async () => {
    const raw = `  ${LONG}  `;
    const el = await show({ tone: "teal", title: raw, status: "Jira: In Progress", error: raw });
    const status = statusOf(el);
    const note = errorOf(el);
    expect(status?.textContent).toBe("Jira: In Progress");
    expect(status?.getAttribute("title")).toBe(raw);
    expect(status?.getAttribute("data-tone")).toBe("teal");
    expect(note?.tagName).toBe("P");
    expect(note?.textContent).toBe(LONG);
    expect(note?.hasAttribute("data-user-text")).toBe(true);
    expect(note?.parentElement).not.toBe(status);
  });

  it("says nothing visible when the error is blank or whitespace, and keeps the title", async () => {
    const blank = await show({ tone: "teal", title: "Last written 14:02", status: "Jira: In Progress", error: "" });
    expect(statusOf(blank)?.textContent).toBe("Jira: In Progress");
    expect(statusOf(blank)?.getAttribute("title")).toBe("Last written 14:02");
    expect(errorOf(blank)).toBeNull();

    const spaces = " \n\t ";
    const white = await show({ tone: "amber", title: spaces, status: "Sync paused", error: spaces });
    expect(statusOf(white)?.textContent).toBe("Sync paused");
    expect(statusOf(white)?.getAttribute("title")).toBe(spaces);
    expect(errorOf(white)).toBeNull();

    const missing = await show({ tone: "red", title: undefined, status: "Write-back failed", error: null });
    expect(statusOf(missing)?.textContent).toBe("Write-back failed");
    expect(statusOf(missing)?.hasAttribute("title")).toBe(false);
    expect(errorOf(missing)).toBeNull();
  });

  it("calls syncErrorSentence with tl.sync_error beside the status words", () => {
    const src = codeOnly(join(__dirname, "../app/projects/linked-ticket.tsx"));
    const start = src.indexOf("tk-run-head");
    const end = src.indexOf("tk-linked-steps", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const head = src.slice(start, end);
    expect(head).toContain("syncErrorSentence(tl.sync_error)");
    expect(head).toContain("<LinkedSyncStatus");
    expect(head).toContain("Write-back failed");
    expect(head).toContain("Sync paused");
    expect(head).toContain("tl.sync_error || (tl.last_synced_at ?");
    expect(head).toContain("`${label}${status ? `: ${status}` : \"\"}`");
  });
});
