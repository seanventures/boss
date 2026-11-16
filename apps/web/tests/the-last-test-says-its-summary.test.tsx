/**
 * The last test says its summary, beside the status word, not only on the title.
 * Blank or whitespace is nothing. The visible sentence is trimmed. More than six
 * words is a paragraph; six or fewer is a span. The sentence is
 * lastTestSummarySentence(lastTest.result?.summary). The status word and the title
 * stay. The checks sentence, the skills count, and the description stay as they are.
 *
 * Breaks if the summary stays title-only, padding survives, a blank summary still
 * renders, a long sentence is a span, the status word or the title is replaced,
 * or the row stops calling lastTestSummarySentence(lastTest.result?.summary).
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
const lastTestSummarySentence = profileUi.lastTestSummarySentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SRC = codeOnly(join(__dirname, "../app/agent-profile-ui.tsx"));

const SHORT = "Reviews pull requests";
const SIX = "one two three four five six";
const SEVEN = "one two three four five six seven";
const PADDED = `  ${SEVEN}  `;
const BLANK = " \n\t ";

type Row = {
  key: string;
  name?: string;
  description?: string;
  skills?: string[];
  valid?: boolean;
  test?: { status: string; summary?: string } | null;
};

function agent(row: Row): StudioAgent {
  return {
    id: `def_${row.key}`,
    agent_key: row.key,
    name: row.name ?? row.key,
    owner: "ceo",
    status: "active",
    description: row.description,
    active_run_count: 0,
    active_version: {
      id: `ver_${row.key}`,
      version: 1,
      role_key: "qa",
      model_policy: {},
      requested_skills: row.skills ?? [],
      scopes: {},
      validation: { valid: row.valid ?? true },
    },
  };
}

function feedFor(rows: Row[]): AgentStudioFeed {
  return {
    projectId: "p1",
    apiReachable: true,
    agents: [],
    panels: [],
    agentRuns: [],
    panelRuns: [],
    tests: rows.flatMap((row) => {
      if (!row.test) return [];
      return [{
        id: `t_${row.key}`,
        status: row.test.status,
        agent_name: row.name ?? row.key,
        created_at: "2026-11-16T00:00:00Z",
        result: row.test.summary === undefined ? {} : { summary: row.test.summary },
      }];
    }),
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

async function show(rows: Row[]): Promise<HTMLElement> {
  unmountRow();
  const feed = feedFor(rows);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <>
        {rows.map((row) => <ProfileRow key={row.key} agent={agent(row)} feed={feed} actor="ceo" />)}
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

function lastDd(scope: ParentNode): HTMLElement {
  const cell = [...scope.querySelectorAll("div")].find((div) => div.querySelector("dt")?.textContent === "Last test");
  const dd = cell?.querySelector("dd");
  expect(dd).toBeTruthy();
  return dd as HTMLElement;
}

function saidEl(scope: ParentNode, key: string): HTMLElement | null {
  return scope.querySelector<HTMLElement>(`[data-testid="agent-profile-last-test-${key}"]`);
}

describe("lastTestSummarySentence", () => {
  it("is the trimmed summary, or nothing when it is blank or whitespace", () => {
    expect(lastTestSummarySentence(PADDED)).toBe(SEVEN);
    expect(lastTestSummarySentence(`  ${SHORT}  `)).toBe(SHORT);
    expect(lastTestSummarySentence(SIX)).toBe(SIX);
    expect(lastTestSummarySentence(`  ${SIX}  `)).toBe(SIX);
    expect(lastTestSummarySentence(null)).toBeNull();
    expect(lastTestSummarySentence(undefined)).toBeNull();
    expect(lastTestSummarySentence("")).toBeNull();
    expect(lastTestSummarySentence(BLANK)).toBeNull();
    expect(lastTestSummarySentence(" \n\t ")).toBeNull();
  });
});

describe("the last test says its summary", () => {
  it("shows the summary beside the status word, as a paragraph when it is longer than six words", async () => {
    const shown = await show([
      { key: "six", test: { status: "passed", summary: SIX }, skills: ["vault-reader"] },
      { key: "seven", description: SHORT, skills: ["vault-reader", "lint"], valid: false, test: { status: "not_run", summary: PADDED } },
      { key: "short", test: { status: "failed", summary: `  ${SHORT}  ` } },
    ]);

    const six = profile(shown, "six");
    const sixDd = lastDd(six);
    const sixSaid = saidEl(six, "six");
    expect(sixDd.textContent).toBe("passed");
    expect(sixDd.getAttribute("title")).toBe(SIX);
    expect(sixSaid?.tagName).toBe("SPAN");
    expect(sixSaid?.textContent).toBe(SIX);
    expect(sixSaid!.textContent!.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(sixSaid?.hasAttribute("data-user-text")).toBe(true);
    expect(sixDd.contains(sixSaid)).toBe(false);
    expect(sixSaid?.parentElement).toBe(sixDd.parentElement);
    expect(sixDd.compareDocumentPosition(sixSaid!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const skills = six.querySelector("dd[title='vault-reader']");
    expect(skills?.textContent).toBe("1");

    const seven = profile(shown, "seven");
    const sevenDd = lastDd(seven);
    const sevenSaid = saidEl(seven, "seven");
    expect(sevenDd.textContent).toBe("not run");
    expect(sevenDd.getAttribute("title")).toBe(PADDED);
    expect(sevenSaid?.tagName).toBe("P");
    expect(sevenSaid?.textContent).toBe(SEVEN);
    expect(sevenSaid!.textContent!.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
    expect(sevenSaid?.hasAttribute("data-user-text")).toBe(true);
    expect(sevenDd.contains(sevenSaid)).toBe(false);
    expect(sevenSaid?.parentElement).toBe(sevenDd.parentElement);
    expect(seven.textContent).not.toContain(PADDED);
    expect(seven.querySelector("[data-testid='agent-profile-desc-seven']")?.tagName).toBe("SPAN");
    expect(seven.querySelector("[data-testid='agent-profile-desc-seven']")?.textContent).toBe(SHORT);
    expect(seven.querySelector("[data-testid='agent-profile-checks-seven']")).toBeNull();
    const checks = [...seven.querySelectorAll("div")].find((div) => div.querySelector("dt")?.textContent === "Checks");
    expect(checks?.querySelector("dd")?.textContent).toBe("Pending");
    expect(seven.querySelector("dd[title='vault-reader, lint']")?.textContent).toBe("2");

    const short = profile(shown, "short");
    const shortDd = lastDd(short);
    const shortSaid = saidEl(short, "short");
    expect(shortDd.textContent).toBe("failed");
    expect(shortDd.getAttribute("title")).toBe(`  ${SHORT}  `);
    expect(shortSaid?.tagName).toBe("SPAN");
    expect(shortSaid?.textContent).toBe(SHORT);
    expect(shortSaid?.hasAttribute("data-user-text")).toBe(true);
    expect(short.textContent).not.toContain(`  ${SHORT}  `);
  });

  it("says nothing when the summary is blank and keeps the status word and the title", async () => {
    const shown = await show([
      { key: "blank", test: { status: "failed", summary: BLANK } },
      { key: "empty", test: { status: "passed", summary: "" } },
      { key: "missing", test: { status: "error" } },
      { key: "none", name: "Other", test: null },
    ]);

    const blank = profile(shown, "blank");
    const blankDd = lastDd(blank);
    expect(saidEl(blank, "blank")).toBeNull();
    expect(blankDd.textContent).toBe("failed");
    expect(blankDd.getAttribute("title")).toBe(BLANK);
    expect(blank.textContent).not.toContain(SEVEN);

    const empty = profile(shown, "empty");
    const emptyDd = lastDd(empty);
    expect(saidEl(empty, "empty")).toBeNull();
    expect(emptyDd.textContent).toBe("passed");
    expect(emptyDd.getAttribute("title")).toBe("");

    const missing = profile(shown, "missing");
    const missingDd = lastDd(missing);
    expect(saidEl(missing, "missing")).toBeNull();
    expect(missingDd.textContent).toBe("error");
    expect(missingDd.hasAttribute("title")).toBe(false);

    const none = profile(shown, "none");
    expect(saidEl(none, "none")).toBeNull();
    expect([...none.querySelectorAll("dt")].some((dt) => dt.textContent === "Last test")).toBe(false);
  });

  it("calls lastTestSummarySentence with the last test summary", () => {
    const raw = readFileSync(join(__dirname, "../app/agent-profile-ui.tsx"), "utf8");
    const start = raw.indexOf("function ProfileRow");
    const end = raw.indexOf("function PanelsView", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const row = raw.slice(start, end);
    expect(row).toContain("lastTestSummarySentence(lastTest.result?.summary)");
    expect(row).toContain("agent-profile-last-test-${agent.agent_key}");
    expect(row).toContain("title={lastTest.result?.summary}");
    expect(row).toContain("{words(lastTest.status)}");
    expect(row).toContain("agentDescriptionSentence(agent.description)");
    expect(row).toContain("validationIssueSentence(validation.errors, validation.warnings)");
    expect(row).toContain("(version.requested_skills ?? []).length");
    const code = SRC.slice(SRC.indexOf("function ProfileRow"), SRC.indexOf("function PanelsView"));
    expect(code).toContain("lastTestSummarySentence(lastTest.result?.summary)");
    expect(code).not.toContain("lastTestSummarySentence(agent.description)");
    expect(code).not.toContain("agentDescriptionSentence(lastTest");
    expect(code).not.toContain("validationIssueSentence(lastTest");
  });
});
