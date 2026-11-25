// @vitest-environment happy-dom
/**
 * The people-by-access-role row names the role. The description is on the title today
 * and has to be readable in the row. Blank is nothing. More than six words is a paragraph.
 * The sentence is roleDescriptionSentence(role.description).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { RoleDescription, roleDescriptionSentence } from "../app/identity/overview";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SRC = codeOnly(join(__dirname, "../app/identity/overview.tsx"));

const SHORT = "Reads every record";
const SIX = "one two three four five six";
const LONG = "one two three four five six seven";
const ROLE_ID = "arl_owner";

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(description: string | null | undefined): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<RoleDescription id={ROLE_ID} sentence={roleDescriptionSentence(description)} />);
  });
  return host;
}

describe("roleDescriptionSentence", () => {
  it("is the trimmed description, or nothing", () => {
    expect(roleDescriptionSentence(`  ${LONG}  `)).toBe(LONG);
    expect(roleDescriptionSentence(SHORT)).toBe(SHORT);
    expect(roleDescriptionSentence(SIX)).toBe(SIX);
    expect(roleDescriptionSentence(null)).toBeNull();
    expect(roleDescriptionSentence(undefined)).toBeNull();
    expect(roleDescriptionSentence("")).toBeNull();
    expect(roleDescriptionSentence(" \n\t ")).toBeNull();
  });
});

describe("the overview names the role", () => {
  it("shows a description longer than six words as a paragraph", async () => {
    const hostEl = await show(`  ${LONG}  `);
    const said = hostEl.querySelector(`[data-testid="overview-role-desc-${ROLE_ID}"]`);
    expect(said?.tagName).toBe("P");
    expect(said?.textContent).toBe(LONG);
    expect(said?.hasAttribute("data-user-text")).toBe(true);
  });

  it("shows six words or fewer as a span", async () => {
    const six = await show(SIX);
    const sixSaid = six.querySelector(`[data-testid="overview-role-desc-${ROLE_ID}"]`);
    expect(sixSaid?.tagName).toBe("SPAN");
    expect(sixSaid?.textContent).toBe(SIX);
    expect(sixSaid?.hasAttribute("data-user-text")).toBe(true);

    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;

    const short = await show(SHORT);
    const shortSaid = short.querySelector(`[data-testid="overview-role-desc-${ROLE_ID}"]`);
    expect(shortSaid?.tagName).toBe("SPAN");
    expect(shortSaid?.textContent).toBe(SHORT);
  });

  it("says nothing when the description is blank", async () => {
    const blank = await show(" \n\t ");
    expect(blank.querySelector(`[data-testid="overview-role-desc-${ROLE_ID}"]`)).toBeNull();
    expect(blank.querySelector("p, span")).toBeNull();

    act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;

    const missing = await show(null);
    expect(missing.querySelector(`[data-testid="overview-role-desc-${ROLE_ID}"]`)).toBeNull();
    expect(missing.textContent).toBe("");
  });

  it("calls roleDescriptionSentence with the role description", () => {
    expect(SRC).toContain("roleDescriptionSentence(role.description)");
    expect(SRC).toContain('title={role.description}');
    expect(SRC).toContain('className="idn-rrow"');
  });
});
