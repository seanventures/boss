/**
 * The unbound history span says "last deploy: {status}" and keeps the time.
 * Its title is the raw failure reason. The reason is also readable beside
 * that span. Blank or whitespace is nothing. A trimmed reason is the
 * sentence. More than six words is a paragraph. Six or fewer is a span.
 * Deploy button titles stay.
 *
 * Breaks if a padded reason stays padded, a blank reason still renders, a
 * seven-word reason is a span, the status words or the time disappear, the
 * title stops being the raw reason, or the line stops calling
 * deployHistoryReasonSentence(latest.failure_reason).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type DeployHistoryLineProps = {
  status: string;
  reason?: string | null;
  sentence: string | null;
  time?: React.ReactNode;
};

const parts = await import("../app/factory/legacy-build-parts") as {
  deployHistoryReasonSentence?: (reason: string | null | undefined) => string | null;
  DeployHistoryLine?: (props: DeployHistoryLineProps) => React.ReactNode;
};
const deployHistoryReasonSentence = parts.deployHistoryReasonSentence!;
const DeployHistoryLine = parts.DeployHistoryLine!;

const SHORT = "port held";
const SIX = "one two three four five six";
const SEVEN = "one two three four five six seven";
const PADDED = `  ${SEVEN}  `;
const ISO = "2026-09-28T00:00:00Z";

let root: Root | null = null;
let host: HTMLElement | null = null;

function dropLine(): void {
  act(() => { root?.unmount(); });
  host?.remove();
  root = null;
  host = null;
}

afterEach(() => {
  dropLine();
  document.body.innerHTML = "";
});

async function show(status: string, reason: string | null | undefined, finishedAt?: string | null): Promise<HTMLElement> {
  dropLine();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const sentence = deployHistoryReasonSentence(reason);
  const Line = DeployHistoryLine;
  const time = finishedAt ? <>, <time dateTime={finishedAt} /></> : null;
  await act(async () => {
    root!.render(<Line status={status} reason={reason} sentence={sentence} time={time} />);
  });
  return host;
}

function history(el: HTMLElement): HTMLElement {
  const found = el.querySelector<HTMLElement>(".ct-deploy-history");
  expect(found).not.toBeNull();
  return found as HTMLElement;
}

function why(el: HTMLElement): HTMLElement | null {
  return el.querySelector<HTMLElement>("[data-testid=app-deploy-history-why]");
}

describe("deployHistoryReasonSentence", () => {
  it("is nothing when the reason is blank", () => {
    expect(deployHistoryReasonSentence(null)).toBeNull();
    expect(deployHistoryReasonSentence(undefined)).toBeNull();
    expect(deployHistoryReasonSentence("")).toBeNull();
  });

  it("is nothing when the reason is whitespace", () => {
    expect(deployHistoryReasonSentence("   ")).toBeNull();
    expect(deployHistoryReasonSentence(" \n\t ")).toBeNull();
  });

  it("is the trimmed reason", () => {
    expect(deployHistoryReasonSentence(`  ${SHORT}  `)).toBe("port held");
    expect(deployHistoryReasonSentence(SIX)).toBe("one two three four five six");
    expect(deployHistoryReasonSentence(PADDED)).toBe("one two three four five six seven");
    expect(deployHistoryReasonSentence("  a  b  ")).toBe("a  b");
  });
});

describe("the old deploy line says why it failed", () => {
  it("shows the trimmed reason beside the status and the time, as a paragraph past six words", async () => {
    const shown = await show("failed", PADDED, ISO);
    const span = history(shown);
    const said = why(shown);
    expect(span.textContent?.startsWith("last deploy: failed")).toBe(true);
    expect(span.textContent).toContain(",");
    expect(span.querySelector("time")?.getAttribute("dateTime")).toBe(ISO);
    expect(span.getAttribute("title")).toBe(PADDED);
    expect(said?.tagName).toBe("P");
    expect(said?.textContent).toBe(SEVEN);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);
    expect(said?.hasAttribute("data-user-text")).toBe(true);
    expect(span.contains(said)).toBe(false);
    expect(said?.parentElement).toBe(span.parentElement);
    expect(span.compareDocumentPosition(said!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(span.textContent).not.toContain(SEVEN);

    const six = await show("rolled_back", `  ${SIX}  `, ISO);
    const sixSpan = history(six);
    const sixSaid = why(six);
    expect(sixSpan.textContent?.startsWith("last deploy: rolled_back")).toBe(true);
    expect(sixSpan.querySelector("time")?.getAttribute("dateTime")).toBe(ISO);
    expect(sixSpan.getAttribute("title")).toBe(`  ${SIX}  `);
    expect(sixSaid?.tagName).toBe("SPAN");
    expect(sixSaid?.textContent).toBe(SIX);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(sixSaid?.hasAttribute("data-user-text")).toBe(true);
    expect(sixSpan.contains(sixSaid)).toBe(false);

    const short = await show("execute_failed", `  ${SHORT}  `);
    const shortSpan = history(short);
    const shortSaid = why(short);
    expect(shortSpan.textContent).toBe("last deploy: execute_failed");
    expect(shortSpan.querySelector("time")).toBeNull();
    expect(shortSpan.getAttribute("title")).toBe(`  ${SHORT}  `);
    expect(shortSaid?.tagName).toBe("SPAN");
    expect(shortSaid?.textContent).toBe(SHORT);
    expect(shortSaid?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when the reason is blank and keeps the status, the time, and the title", async () => {
    const blank = await show("failed", " \n\t ", ISO);
    const blankSpan = history(blank);
    expect(why(blank)).toBeNull();
    expect(blankSpan.textContent?.startsWith("last deploy: failed")).toBe(true);
    expect(blankSpan.querySelector("time")?.getAttribute("dateTime")).toBe(ISO);
    expect(blankSpan.getAttribute("title")).toBe(" \n\t ");
    expect(blank.textContent).not.toContain("app-deploy-history-why");

    const empty = await show("blocked", null);
    const emptySpan = history(empty);
    expect(why(empty)).toBeNull();
    expect(emptySpan.textContent).toBe("last deploy: blocked");
    expect(emptySpan.querySelector("time")).toBeNull();
    expect(emptySpan.hasAttribute("title")).toBe(false);
  });

  it("calls deployHistoryReasonSentence with the failure reason and keeps the deploy button titles", () => {
    const src = codeOnly(join(__dirname, "../app/factory/legacy-build-parts.tsx"));
    const start = src.indexOf("function DeploymentLine");
    const end = src.indexOf("function RebuildWithSpec", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const line = src.slice(start, end);
    expect(line).toContain("deployHistoryReasonSentence(latest.failure_reason)");
    expect(line).toContain("<DeployHistoryLine");
    expect(line).toContain("status={latest.status}");
    expect(line).toContain("reason={latest.failure_reason}");
    expect(line).toContain("<ClientRelativeTime iso={latest.finished_at} />");
    expect(line).toContain('title="Create a new ChangeSet for the same commit and target"');
    expect(line).toContain('title="Unbind this app from its host and port. Deployment history is kept."');
    expect(line).toContain("This deployment ChangeSet is awaiting review");
    expect(line).toContain("Create a ChangeSet to review before deploying this app");
    const componentStart = src.indexOf("function DeployHistoryLine");
    expect(componentStart).toBeGreaterThan(-1);
    const componentEnd = src.indexOf("function DeploymentLine", componentStart);
    expect(componentEnd).toBeGreaterThan(componentStart);
    const component = src.slice(componentStart, componentEnd);
    expect(component).toContain("last deploy: {status}");
    expect(component).toContain("title={reason ?? undefined}");
    expect(component).toContain("{time}");
    expect(component).toContain('data-testid="app-deploy-history-why"');
    expect(component).toContain("data-user-text");
    expect(line).toContain('className="detail ct-deploy-reason"');
    expect(line).toContain("String(latest.failure_reason).slice(0, 60)");
  });
});
