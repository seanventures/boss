// @vitest-environment happy-dom
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The problem note already says the headline, and puts the message only on the title.
 * That message has to be readable when it is not the headline again. Blank is nothing.
 * More than six words is a paragraph.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const { ProblemNote, problemMessageSentence } = await import("../app/agents/converse-parts");

const NEEDS = "Needs a model";
const BUDGET = "Budget refused this";
const BUSY = "Still answering";
const UNSENT = "Not sent";
const LONG = "The control plane refused because the model key is missing";
const PADDED = `  ${LONG}  `;
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "No model key";
const SETUP = "/?area=setup&focus=models";

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

function mount(code: string | undefined, message: string, onClose?: () => void): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const Note = ProblemNote!;
  act(() => {
    root!.render(<Note problem={{ code, message }} onClose={onClose} />);
  });
  return host;
}

function noteEl(card: HTMLElement): HTMLElement {
  const note = card.querySelector<HTMLElement>("[data-testid='ag-problem']");
  expect(note).not.toBeNull();
  return note as HTMLElement;
}

function headEl(note: HTMLElement): HTMLElement {
  const head = note.querySelector<HTMLElement>(".ag-fail-head");
  expect(head).not.toBeNull();
  return head as HTMLElement;
}

function sentenceEl(note: HTMLElement): HTMLElement | null {
  return note.querySelector<HTMLElement>("[data-testid='ag-problem-why']");
}

describe("problemMessageSentence", () => {
  it("is the trimmed message when it differs from the headline, else nothing", () => {
    expect(problemMessageSentence(NEEDS, PADDED)).toBe(LONG);
    expect(problemMessageSentence(NEEDS, SHORT)).toBe(SHORT);
    expect(problemMessageSentence(BUDGET, SIX)).toBe(SIX);
    expect(problemMessageSentence(BUSY, SEVEN)).toBe(SEVEN);
    expect(problemMessageSentence(UNSENT, `  ${SHORT}  `)).toBe(SHORT);
    expect(problemMessageSentence(NEEDS, "")).toBeNull();
    expect(problemMessageSentence(NEEDS, " \n\t ")).toBeNull();
    expect(problemMessageSentence(NEEDS, NEEDS)).toBeNull();
    expect(problemMessageSentence(`  ${SHORT}  `, `  ${SHORT}  `)).toBeNull();
    expect(problemMessageSentence(`  ${BUDGET}  `, BUDGET)).toBeNull();
    expect(problemMessageSentence(NEEDS, `  ${NEEDS}  `)).toBeNull();
    expect(problemMessageSentence(NEEDS, UNSENT)).toBe(UNSENT);
  });
});

describe("the problem note says the message", () => {
  it("keeps the Needs a model headline and the Setup link, and shows a long message as a paragraph", () => {
    const note = noteEl(mount("needs_model", PADDED));
    expect(note.getAttribute("data-code")).toBe("needs_model");
    const head = headEl(note);
    expect(head.textContent).toBe(NEEDS);
    expect(head.getAttribute("title")).toBe(PADDED);
    const sentence = sentenceEl(note);
    expect(sentence).not.toBeNull();
    expect(sentence!.textContent).toBe(LONG);
    expect(sentence!.tagName).toBe("P");
    expect(sentence!.hasAttribute("data-user-text")).toBe(true);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBe(10);
    expect(head.compareDocumentPosition(sentence!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const setup = note.querySelector<HTMLAnchorElement>("a.ag-link");
    expect(setup).not.toBeNull();
    expect(setup!.textContent).toBe("Setup ↗");
    expect(setup!.getAttribute("href")).toBe(SETUP);
    expect(setup!.getAttribute("title")).toBe("Needs a model. Connect one in Setup.");
    expect(sentence!.compareDocumentPosition(setup!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows six words as a span and seven as a paragraph, and keeps each headline", () => {
    const six = noteEl(mount("budget", SIX));
    expect(six.getAttribute("data-code")).toBe("budget");
    expect(headEl(six).textContent).toBe(BUDGET);
    expect(headEl(six).getAttribute("title")).toBe(SIX);
    const sixSentence = sentenceEl(six);
    expect(sixSentence!.textContent).toBe(SIX);
    expect(sixSentence!.tagName).toBe("SPAN");
    expect(sixSentence!.hasAttribute("data-user-text")).toBe(true);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);
    const budget = six.querySelector<HTMLAnchorElement>("a.ag-link");
    expect(budget!.textContent).toBe("Budget ↗");
    expect(budget!.getAttribute("href")).toBe("/?area=budget");
    expect(sixSentence!.compareDocumentPosition(budget!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    clearHost();

    const seven = noteEl(mount("busy", `  ${SEVEN}  `, () => {}));
    expect(seven.getAttribute("data-code")).toBe("busy");
    expect(headEl(seven).textContent).toBe(BUSY);
    expect(headEl(seven).getAttribute("title")).toBe(`  ${SEVEN}  `);
    const sevenSentence = sentenceEl(seven);
    expect(sevenSentence!.textContent).toBe(SEVEN);
    expect(sevenSentence!.tagName).toBe("P");
    expect(sevenSentence!.hasAttribute("data-user-text")).toBe(true);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);
    expect(seven.querySelector("a")).toBeNull();
    expect(seven.querySelector("[aria-label='Dismiss']")!.textContent).toBe("×");
    expect(headEl(seven).compareDocumentPosition(sevenSentence!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    clearHost();

    const short = noteEl(mount(undefined, `  ${SHORT}  `));
    expect(short.getAttribute("data-code")).toBe("");
    expect(headEl(short).textContent).toBe(UNSENT);
    expect(headEl(short).getAttribute("title")).toBe(`  ${SHORT}  `);
    expect(sentenceEl(short)!.tagName).toBe("SPAN");
    expect(sentenceEl(short)!.textContent).toBe(SHORT);
    expect(sentenceEl(short)!.hasAttribute("data-user-text")).toBe(true);
    expect(short.querySelector("a")).toBeNull();
  });

  it("shows no sentence when the message is blank or already the headline", () => {
    const blank = noteEl(mount("needs_model", " \n\t "));
    expect(headEl(blank).textContent).toBe(NEEDS);
    expect(headEl(blank).getAttribute("title")).toBe(" \n\t ");
    expect(sentenceEl(blank)).toBeNull();
    expect(blank.querySelector("a.ag-link")!.textContent).toBe("Setup ↗");
    expect(blank.querySelector("a.ag-link")!.getAttribute("title")).toBe("Needs a model. Connect one in Setup.");

    clearHost();

    const empty = noteEl(mount("budget", ""));
    expect(headEl(empty).textContent).toBe(BUDGET);
    expect(headEl(empty).getAttribute("title")).toBe("");
    expect(sentenceEl(empty)).toBeNull();
    expect(empty.querySelector("a.ag-link")!.textContent).toBe("Budget ↗");

    clearHost();

    const same = noteEl(mount("busy", `  ${BUSY}  `));
    expect(headEl(same).textContent).toBe(BUSY);
    expect(headEl(same).getAttribute("title")).toBe(`  ${BUSY}  `);
    expect(sentenceEl(same)).toBeNull();

    clearHost();

    const unsent = noteEl(mount("nope", UNSENT));
    expect(headEl(unsent).textContent).toBe(UNSENT);
    expect(headEl(unsent).getAttribute("title")).toBe(UNSENT);
    expect(sentenceEl(unsent)).toBeNull();
    expect(unsent.getAttribute("data-code")).toBe("nope");
  });

  it("calls problemMessageSentence with the headline and problem.message", () => {
    const raw = readFileSync(join(__dirname, "../app/agents/converse-parts.tsx"), "utf8");
    const start = raw.indexOf("function ProblemNote");
    const end = raw.indexOf("function runHref", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const note = raw.slice(start, end);
    expect(note).toContain("problemMessageSentence(headline, problem.message)");
    expect(note).toContain("title={problem.message}");
    expect(note).toContain("{headline}");
    expect(note).toContain('"Needs a model"');
    expect(note).toContain('"Budget refused this"');
    expect(note).toContain('"Still answering"');
    expect(note).toContain('"Not sent"');
    expect(note).not.toContain("failureActionSentence");
    const srcStart = SRC.indexOf("function ProblemNote");
    const srcEnd = SRC.indexOf("function runHref", srcStart);
    expect(SRC.slice(srcStart, srcEnd)).toContain("problemMessageSentence(headline, problem.message)");
  });
});
