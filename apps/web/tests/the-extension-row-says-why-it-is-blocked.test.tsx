// @vitest-environment happy-dom
/**
 * The extension row says why it is blocked. The problems are on the title today
 * and have to be readable beside Blocked. Blank is nothing. More than six words
 * is a paragraph. The sentence is extensionProblemSentence(e.trust.problems).
 * The checksum stays off this sentence.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { ExtensionProblems, extensionProblemSentence } from "../app/infra/extensions";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SRC = codeOnly(join(__dirname, "../app/infra/extensions.tsx"));

const SHORT = "files changed";
const SIX = "one two three four five six";
const LONG = "one two three four five six seven";

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(problems: readonly string[] | null | undefined): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<ExtensionProblems sentence={extensionProblemSentence(problems)} />);
  });
  return host;
}

describe("extensionProblemSentence", () => {
  it("joins trimmed problems and drops blanks", () => {
    expect(extensionProblemSentence([`  ${LONG}  `])).toBe(LONG);
    expect(extensionProblemSentence([SHORT])).toBe(SHORT);
    expect(extensionProblemSentence([SIX])).toBe(SIX);
    expect(extensionProblemSentence(["  files changed  ", "  manifest invalid  "])).toBe("files changed; manifest invalid");
    expect(extensionProblemSentence(["files changed", "   ", "manifest invalid"])).toBe("files changed; manifest invalid");
    expect(extensionProblemSentence(null)).toBeNull();
    expect(extensionProblemSentence(undefined)).toBeNull();
    expect(extensionProblemSentence([])).toBeNull();
    expect(extensionProblemSentence([""])).toBeNull();
    expect(extensionProblemSentence([" \n\t "])).toBeNull();
    expect(extensionProblemSentence(["  ", ""])).toBeNull();
  });
});

describe("the extension row says why it is blocked", () => {
  it("shows a reason longer than six words as a paragraph", async () => {
    const hostEl = await show([`  ${LONG}  `]);
    const said = hostEl.querySelector('[data-testid="inf-ext-problems"]');
    expect(said?.tagName).toBe("P");
    expect(said?.textContent).toBe(LONG);
    expect(said?.hasAttribute("data-user-text")).toBe(true);
  });

  it("shows six words or fewer as a span", async () => {
    const six = await show([SIX]);
    const sixSaid = six.querySelector('[data-testid="inf-ext-problems"]');
    expect(sixSaid?.tagName).toBe("SPAN");
    expect(sixSaid?.textContent).toBe(SIX);
    expect(sixSaid?.hasAttribute("data-user-text")).toBe(true);

    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;

    const short = await show([SHORT]);
    const shortSaid = short.querySelector('[data-testid="inf-ext-problems"]');
    expect(shortSaid?.tagName).toBe("SPAN");
    expect(shortSaid?.textContent).toBe(SHORT);
  });

  it("says nothing when every problem is blank", async () => {
    const blank = await show([" \n\t "]);
    expect(blank.querySelector('[data-testid="inf-ext-problems"]')).toBeNull();
    expect(blank.querySelector("p, span")).toBeNull();

    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;

    const missing = await show([]);
    expect(missing.querySelector('[data-testid="inf-ext-problems"]')).toBeNull();
    expect(missing.textContent).toBe("");
  });

  it("calls extensionProblemSentence with the trust problems and does not show the checksum", () => {
    const blocked = SRC.slice(SRC.indexOf('data-testid="inf-ext-blocked"'), SRC.indexOf('data-testid="inf-ext-trusted"'));
    expect(blocked).toContain("extensionProblemSentence(e.trust.problems)");
    expect(blocked).toContain('title={e.trust.problems.join("\\n")}');
    expect(blocked).toContain(">Blocked</span>");
    expect(blocked).not.toContain("approvedChecksum");
    expect(blocked).not.toContain("bundleChecksum");
    expect(blocked).not.toContain("computedChecksum");
    expect(blocked).not.toContain("sha256");
  });
});
