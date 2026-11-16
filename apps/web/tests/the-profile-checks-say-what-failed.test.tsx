/**
 * The profile checks say what failed, beside Passed or Pending, not only on the title.
 * Blank or whitespace issues say nothing. Errors come first, then warnings. Each one is
 * trimmed, then joined with "; ". More than six words is a paragraph; six or fewer is a span.
 * The sentence is validationIssueSentence(validation.errors, validation.warnings).
 * The skills list and the last-test summary stay off the row.
 *
 * Breaks if the issues stay title-only, padding survives, a blank issue still renders,
 * warnings precede errors, a long sentence is a span, Passed or Pending is replaced,
 * the skills list or the last-test summary is shown, or the row stops calling
 * validationIssueSentence(validation.errors, validation.warnings).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import * as profileUi from "../app/agent-profile-ui";
import type { AgentStudioFeed, StudioAgent } from "../lib/agent-studio";
import { codeOnly } from "./_helpers/source";

const { ProfileRow } = profileUi;
const validationIssueSentence = profileUi.validationIssueSentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SRC = codeOnly(join(__dirname, "../app/agent-profile-ui.tsx"));

const SHORT = "schema gap";
const SIX = "one two three four five six";
const SEVEN = "one two three four five six seven";
const JOINED = "schema gap; stale prompt";
const SKILL = "vault-reader";
const SUMMARY = "auth suite exploded overnight";

const feed: AgentStudioFeed = {
  projectId: "p1", apiReachable: true, agents: [], panels: [],
  agentRuns: [], panelRuns: [],
  tests: [{
    id: "t1", status: "failed", agent_name: "QA", created_at: "2026-11-16T00:00:00Z",
    result: { summary: SUMMARY },
  }],
};

function agent(
  key: string,
  validation: { valid?: boolean; errors?: string[]; warnings?: string[] },
  skills: string[] = [],
): StudioAgent {
  return {
    id: `def_${key}`, agent_key: key, name: "QA", owner: "ceo", status: "active",
    active_run_count: 0,
    active_version: {
      id: `ver_${key}`, version: 1, role_key: "qa", model_policy: {},
      requested_skills: skills, scopes: {}, validation,
    },
  };
}

let root: Root | null = null;
let host: HTMLElement | null = null;

function unmountRow(): void {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
}

afterEach(() => {
  unmountRow();
  document.body.innerHTML = "";
});

async function show(rows: StudioAgent[]): Promise<HTMLElement> {
  unmountRow();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <>
        {rows.map((row) => <ProfileRow key={row.agent_key} agent={row} feed={feed} actor="ceo" />)}
      </>,
    );
  });
  return host;
}

function profile(hostEl: HTMLElement, key: string): HTMLElement {
  const found = hostEl.querySelector<HTMLElement>(`[data-testid="agent-profile-${key}"]`);
  expect(found, key).toBeTruthy();
  return found as HTMLElement;
}

function checksDd(scope: ParentNode): HTMLElement {
  const cell = [...scope.querySelectorAll("div")].find((div) => div.querySelector("dt")?.textContent === "Checks");
  const dd = cell?.querySelector("dd");
  expect(dd).toBeTruthy();
  return dd as HTMLElement;
}

function whyEl(scope: ParentNode, key: string): HTMLElement | null {
  return scope.querySelector<HTMLElement>(`[data-testid="agent-profile-checks-${key}"]`);
}

describe("validationIssueSentence", () => {
  it("joins trimmed errors, then warnings, and is nothing when they are blank or whitespace", () => {
    expect(validationIssueSentence(["  schema gap  "], ["  stale prompt  "])).toBe(JOINED);
    expect(validationIssueSentence([`  ${SHORT}  `], [])).toBe(SHORT);
    expect(validationIssueSentence(["  one two three  ", "  four five six  "], [])).toBe("one two three; four five six");
    expect(validationIssueSentence([`  ${SIX}  `], [])).toBe(SIX);
    expect(validationIssueSentence([SEVEN], ["  extra  "])).toBe(`${SEVEN}; extra`);
    expect(validationIssueSentence(["  alpha  ", " ", "beta"], ["  ", "gamma"])).toBe("alpha; beta; gamma");
    expect(validationIssueSentence(["  a  b  "], undefined)).toBe("a  b");
    expect(validationIssueSentence(undefined, ["  warn only  "])).toBe("warn only");
    expect(validationIssueSentence(null, null)).toBeNull();
    expect(validationIssueSentence(undefined, undefined)).toBeNull();
    expect(validationIssueSentence([], [])).toBeNull();
    expect(validationIssueSentence([""], [""])).toBeNull();
    expect(validationIssueSentence(["  ", "\n\t"], [" \n "])).toBeNull();
  });
});

describe("the profile checks say what failed", () => {
  it("shows the issues beside Passed or Pending, as a paragraph when longer than six words", async () => {
    const shown = await show([
      agent("six", { valid: true, warnings: [`  ${SIX}  `] }),
      agent("seven", { valid: false, errors: [`  ${SEVEN}  `] }),
      agent("join", { valid: false, errors: ["  schema gap  ", " "], warnings: ["  stale prompt  "] }, [SKILL]),
    ]);

    const six = profile(shown, "six");
    const sixDd = checksDd(six);
    const sixWhy = whyEl(six, "six");
    expect(sixDd.textContent).toBe("Passed");
    expect(sixDd.getAttribute("title")).toBe(`  ${SIX}  `);
    expect(sixWhy?.tagName).toBe("SPAN");
    expect(sixWhy?.textContent).toBe(SIX);
    expect(sixWhy!.textContent!.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(sixWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(sixDd.contains(sixWhy)).toBe(false);
    expect(sixWhy?.parentElement).toBe(sixDd.parentElement);
    expect(sixDd.compareDocumentPosition(sixWhy!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const seven = profile(shown, "seven");
    const sevenDd = checksDd(seven);
    const sevenWhy = whyEl(seven, "seven");
    expect(sevenDd.textContent).toBe("Pending");
    expect(sevenDd.getAttribute("title")).toBe(`  ${SEVEN}  `);
    expect(sevenWhy?.tagName).toBe("P");
    expect(sevenWhy?.textContent).toBe(SEVEN);
    expect(sevenWhy!.textContent!.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
    expect(sevenWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(sevenDd.contains(sevenWhy)).toBe(false);
    expect(sevenWhy?.parentElement).toBe(sevenDd.parentElement);

    const join = profile(shown, "join");
    const joinDd = checksDd(join);
    const joinWhy = whyEl(join, "join");
    expect(joinDd.textContent).toBe("Pending");
    expect(joinDd.getAttribute("title")).toBe("  schema gap       stale prompt  ");
    expect(joinWhy?.tagName).toBe("SPAN");
    expect(joinWhy?.textContent).toBe(JOINED);
    expect(joinWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(join.textContent).not.toContain("  schema gap  ");
    expect(join.textContent).not.toContain(SKILL);
    expect(join.textContent).not.toContain(SUMMARY);
    const skills = join.querySelector("dd[title='vault-reader']");
    expect(skills?.textContent).toBe("1");
    const last = [...join.querySelectorAll("dd")].find((dd) => dd.getAttribute("title") === SUMMARY);
    expect(last?.textContent).toBe("failed");
  });

  it("says nothing when the issues are blank and keeps Passed, Pending, and the title", async () => {
    const shown = await show([
      agent("blank", { valid: false, errors: ["  ", "\n\t"], warnings: [" \n "] }),
      agent("passed", { valid: true }),
      agent("pending", { valid: false, errors: [], warnings: [] }),
    ]);

    const blank = profile(shown, "blank");
    const blankDd = checksDd(blank);
    expect(whyEl(blank, "blank")).toBeNull();
    expect(blankDd.textContent).toBe("Pending");
    expect(blankDd.getAttribute("title")).toBe("   \n\t  \n ");
    expect(blank.textContent).not.toContain(SKILL);
    expect(blank.textContent).not.toContain(SUMMARY);

    const passed = profile(shown, "passed");
    const passedDd = checksDd(passed);
    expect(whyEl(passed, "passed")).toBeNull();
    expect(passedDd.textContent).toBe("Passed");
    expect(passedDd.hasAttribute("title")).toBe(false);

    const pending = profile(shown, "pending");
    const pendingDd = checksDd(pending);
    expect(whyEl(pending, "pending")).toBeNull();
    expect(pendingDd.textContent).toBe("Pending");
    expect(pendingDd.hasAttribute("title")).toBe(false);
    const last = [...pending.querySelectorAll("dd")].find((dd) => dd.getAttribute("title") === SUMMARY);
    expect(last?.textContent).toBe("failed");
    expect(pending.textContent).not.toContain(SUMMARY);
  });

  it("calls validationIssueSentence with the check errors and warnings", () => {
    const raw = readFileSync(join(__dirname, "../app/agent-profile-ui.tsx"), "utf8");
    const start = raw.indexOf("function ProfileRow");
    const end = raw.indexOf("function PanelsView", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const row = raw.slice(start, end);
    expect(row).toContain("validationIssueSentence(validation.errors, validation.warnings)");
    expect(row).toContain("agent-profile-checks-${agent.agent_key}");
    expect(row).toContain('validation.valid ? "Passed" : "Pending"');
    expect(row).toContain('[...(validation.errors ?? []), ...(validation.warnings ?? [])].join(" ")');
    const code = SRC.slice(SRC.indexOf("function ProfileRow"), SRC.indexOf("function PanelsView"));
    expect(code).toContain("validationIssueSentence(validation.errors, validation.warnings)");
    expect(code).not.toContain("validationIssueSentence(version.requested_skills");
    expect(code).not.toContain("validationIssueSentence(lastTest");
    expect(code).toContain("(version.requested_skills ?? []).length");
    expect(code).toContain("title={lastTest.result?.summary}");
  });
});
