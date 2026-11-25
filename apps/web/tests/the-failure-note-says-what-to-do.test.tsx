// @vitest-environment happy-dom
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The failure note already says the headline, and puts the next step only on the title.
 * That step has to be readable when it is not the headline again. Blank is nothing.
 * More than six words is a paragraph.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const { FailureNote, failureActionSentence } = await import("../app/agents/converse-parts");

const HEAD = "The model stopped";
const LONG = "Open the model setup and pick a model that can answer";
const PADDED = `  ${LONG}  `;
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "Pick a model";
const HREF = "/?area=setup&focus=models";

const SRC = codeOnly(join(__dirname, "../app/agents/converse-parts.tsx"));

let root: Root | null = null;
let host: HTMLElement | null = null;

function clearHost() {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null;
  host = null;
}

afterEach(() => {
  clearHost();
});

function failure(headline: string, action: string) {
  return { kind: "needs_model", headline, action, href: HREF, retry: false };
}

function mount(headline: string, action: string): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(<FailureNote failure={failure(headline, action)} />));
  return host;
}

function noteEl(card: HTMLElement): HTMLElement {
  const note = card.querySelector<HTMLElement>("[data-testid='ag-fail']");
  expect(note).not.toBeNull();
  return note as HTMLElement;
}

function headEl(note: HTMLElement): HTMLElement {
  const head = note.querySelector<HTMLElement>(".ag-fail-head");
  expect(head).not.toBeNull();
  return head as HTMLElement;
}

function sentenceEl(note: HTMLElement): HTMLElement | null {
  return note.querySelector<HTMLElement>("[data-testid='ag-fail-action']");
}

function fixEl(note: HTMLElement): HTMLElement {
  const link = note.querySelector<HTMLElement>("a.ag-link");
  expect(link).not.toBeNull();
  return link as HTMLElement;
}

describe("failureActionSentence", () => {
  it("is the trimmed action when it differs from the headline, else nothing", () => {
    expect(failureActionSentence(HEAD, PADDED)).toBe(LONG);
    expect(failureActionSentence(HEAD, SHORT)).toBe(SHORT);
    expect(failureActionSentence(HEAD, SIX)).toBe(SIX);
    expect(failureActionSentence(HEAD, SEVEN)).toBe(SEVEN);
    expect(failureActionSentence(HEAD, `  ${SHORT}  `)).toBe(SHORT);
    expect(failureActionSentence(HEAD, "")).toBeNull();
    expect(failureActionSentence(HEAD, " \n\t ")).toBeNull();
    expect(failureActionSentence(HEAD, HEAD)).toBeNull();
    expect(failureActionSentence(`  ${SHORT}  `, `  ${SHORT}  `)).toBeNull();
    expect(failureActionSentence(`  ${HEAD}  `, HEAD)).toBeNull();
  });
});

describe("the failure note says what to do", () => {
  it("keeps the headline and the Fix it link, and shows a long action as a paragraph", () => {
    const note = noteEl(mount(HEAD, PADDED));
    const head = headEl(note);
    expect(head.textContent).toBe(HEAD);
    expect(head.getAttribute("title")).toBe(PADDED);
    const sentence = sentenceEl(note);
    expect(sentence).not.toBeNull();
    expect(sentence!.textContent).toBe(LONG);
    expect(sentence!.tagName).toBe("P");
    expect(sentence!.hasAttribute("data-user-text")).toBe(true);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
    expect(head.compareDocumentPosition(sentence!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const fix = fixEl(note);
    expect(fix.textContent).toBe("Fix it ↗");
    expect(fix.getAttribute("href")).toBe(HREF);
    expect(fix.getAttribute("title")).toBe(PADDED);
    expect(sentence!.compareDocumentPosition(fix) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows six words as a span and seven as a paragraph", () => {
    const six = noteEl(mount(HEAD, SIX));
    expect(headEl(six).textContent).toBe(HEAD);
    expect(headEl(six).getAttribute("title")).toBe(SIX);
    const sixSentence = sentenceEl(six);
    expect(sixSentence!.textContent).toBe(SIX);
    expect(sixSentence!.tagName).toBe("SPAN");
    expect(sixSentence!.hasAttribute("data-user-text")).toBe(true);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(fixEl(six).textContent).toBe("Fix it ↗");

    clearHost();

    const seven = noteEl(mount(HEAD, SEVEN));
    expect(headEl(seven).textContent).toBe(HEAD);
    const sevenSentence = sentenceEl(seven);
    expect(sevenSentence!.textContent).toBe(SEVEN);
    expect(sevenSentence!.tagName).toBe("P");
    expect(sevenSentence!.hasAttribute("data-user-text")).toBe(true);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);
    expect(fixEl(seven).textContent).toBe("Fix it ↗");

    clearHost();

    const short = noteEl(mount(HEAD, `  ${SHORT}  `));
    expect(headEl(short).textContent).toBe(HEAD);
    expect(headEl(short).getAttribute("title")).toBe(`  ${SHORT}  `);
    expect(sentenceEl(short)!.tagName).toBe("SPAN");
    expect(sentenceEl(short)!.textContent).toBe(SHORT);
    expect(sentenceEl(short)!.hasAttribute("data-user-text")).toBe(true);
    expect(fixEl(short).textContent).toBe("Fix it ↗");
  });

  it("shows no sentence when the action is blank or already the headline", () => {
    const blank = noteEl(mount(HEAD, " \n\t "));
    expect(headEl(blank).textContent).toBe(HEAD);
    expect(headEl(blank).getAttribute("title")).toBe(" \n\t ");
    expect(sentenceEl(blank)).toBeNull();
    expect(fixEl(blank).textContent).toBe("Fix it ↗");
    expect(fixEl(blank).getAttribute("title")).toBe(" \n\t ");

    clearHost();

    const empty = noteEl(mount(HEAD, ""));
    expect(headEl(empty).textContent).toBe(HEAD);
    expect(headEl(empty).getAttribute("title")).toBe("");
    expect(sentenceEl(empty)).toBeNull();
    expect(fixEl(empty).textContent).toBe("Fix it ↗");

    clearHost();

    const same = noteEl(mount(`  ${SHORT}  `, `  ${SHORT}  `));
    expect(headEl(same).textContent).toBe(`  ${SHORT}  `);
    expect(headEl(same).getAttribute("title")).toBe(`  ${SHORT}  `);
    expect(sentenceEl(same)).toBeNull();
    expect(fixEl(same).textContent).toBe("Fix it ↗");
  });

  it("calls failureActionSentence with the failure headline and action", () => {
    const raw = readFileSync(join(__dirname, "../app/agents/converse-parts.tsx"), "utf8");
    const start = raw.indexOf("function FailureNote");
    const end = raw.indexOf("function ProblemNote", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const note = raw.slice(start, end);
    expect(note).toContain("failureActionSentence(failure.headline, failure.action)");
    expect(note).toContain('title={failure.action}');
    expect(note).toContain("{failure.headline}");
    expect(note).toContain("Fix it ↗");
    expect(SRC.slice(SRC.indexOf("function FailureNote"), SRC.indexOf("function ProblemNote"))).toContain(
      "failureActionSentence(failure.headline, failure.action)",
    );
  });
});
