/**
 * The agent profile says its description, under the name, not only on the title.
 * A blank description says nothing. A sentence over six words is a paragraph.
 * The sentence is agentDescriptionSentence(agent.description).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { ProfileRow, agentDescriptionSentence } from "../app/agent-profile-ui";
import type { AgentStudioFeed, StudioAgent } from "../lib/agent-studio";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SRC = codeOnly(join(__dirname, "../app/agent-profile-ui.tsx"));

const SHORT = "Reviews pull requests";
const SIX = "one two three four five six";
const LONG = "one two three four five six seven";
const PADDED = `  ${LONG}  `;

const feed: AgentStudioFeed = {
  projectId: "p1", apiReachable: true, agents: [], panels: [],
  agentRuns: [], panelRuns: [], tests: [],
};

function agent(key: string, description?: string): StudioAgent {
  return {
    id: `def_${key}`, agent_key: key, name: "QA", owner: "ceo", status: "active",
    active_run_count: 0, description,
    active_version: {
      id: `ver_${key}`, version: 1, role_key: "qa", model_policy: {}, requested_skills: [],
      scopes: {}, validation: { valid: true },
    },
  };
}

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(rows: StudioAgent[]): Promise<HTMLElement> {
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

function sentenceEl(scope: ParentNode, sentence: string): Element | undefined {
  return [...scope.querySelectorAll("p, span")].find((el) => el.textContent === sentence);
}

describe("agentDescriptionSentence", () => {
  it("is the trimmed description, or nothing when it is blank", () => {
    expect(agentDescriptionSentence(PADDED)).toBe(LONG);
    expect(agentDescriptionSentence(SHORT)).toBe(SHORT);
    expect(agentDescriptionSentence(SIX)).toBe(SIX);
    expect(agentDescriptionSentence(`  ${SHORT}  `)).toBe(SHORT);
    expect(agentDescriptionSentence(null)).toBeNull();
    expect(agentDescriptionSentence(undefined)).toBeNull();
    expect(agentDescriptionSentence("")).toBeNull();
    expect(agentDescriptionSentence(" \n\t ")).toBeNull();
  });
});

describe("the agent profile says its description", () => {
  it("shows the description under the name, as a paragraph when it is longer than six words", async () => {
    const shown = await show([
      agent("short", SHORT),
      agent("six", SIX),
      agent("long", LONG),
      agent("pad", PADDED),
      agent("blank", "   "),
      agent("none"),
    ]);

    const short = profile(shown, "short");
    const shortName = short.querySelector("strong");
    const shortDesc = short.querySelector("[data-testid='agent-profile-desc-short']");
    expect(shortName?.textContent).toBe("QA");
    expect(shortName?.getAttribute("title")).toBe(SHORT);
    expect(shortDesc?.textContent).toBe(SHORT);
    expect(shortDesc?.tagName).toBe("SPAN");
    expect(shortDesc?.hasAttribute("data-user-text")).toBe(true);
    expect(sentenceEl(short, SHORT)?.tagName).toBe("SPAN");
    expect(shortName!.compareDocumentPosition(shortDesc!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const six = profile(shown, "six");
    expect(six.querySelector("strong")?.getAttribute("title")).toBe(SIX);
    expect(six.querySelector("[data-testid='agent-profile-desc-six']")?.tagName).toBe("SPAN");
    expect(six.querySelector("[data-testid='agent-profile-desc-six']")?.textContent).toBe(SIX);
    expect(six.querySelector("[data-testid='agent-profile-desc-six']")?.hasAttribute("data-user-text")).toBe(true);

    const long = profile(shown, "long");
    expect(long.querySelector("strong")?.getAttribute("title")).toBe(LONG);
    expect(long.querySelector("[data-testid='agent-profile-desc-long']")?.tagName).toBe("P");
    expect(long.querySelector("[data-testid='agent-profile-desc-long']")?.textContent).toBe(LONG);
    expect(sentenceEl(long, LONG)?.tagName).toBe("P");
    expect(long.querySelector("[data-testid='agent-profile-desc-long']")?.hasAttribute("data-user-text")).toBe(true);

    const pad = profile(shown, "pad");
    expect(pad.querySelector("strong")?.getAttribute("title")).toBe(PADDED);
    expect(pad.querySelector("[data-testid='agent-profile-desc-pad']")?.tagName).toBe("P");
    expect(pad.querySelector("[data-testid='agent-profile-desc-pad']")?.textContent).toBe(LONG);
    expect(pad.textContent).not.toContain(PADDED);

    const blank = profile(shown, "blank");
    expect(blank.querySelector("strong")?.getAttribute("title")).toBe("   ");
    expect(blank.querySelector("[data-testid='agent-profile-desc-blank']")).toBeNull();
    expect(blank.querySelector("strong")?.textContent).toBe("QA");

    const none = profile(shown, "none");
    expect(none.querySelector("strong")?.hasAttribute("title")).toBe(false);
    expect(none.querySelector("[data-testid='agent-profile-desc-none']")).toBeNull();
  });

  it("calls agentDescriptionSentence with the agent's description", () => {
    const raw = readFileSync(join(__dirname, "../app/agent-profile-ui.tsx"), "utf8");
    const start = raw.indexOf("function ProfileRow");
    const end = raw.indexOf("function PanelsView", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const row = raw.slice(start, end);
    expect(row).toContain("agentDescriptionSentence(agent.description)");
    expect(SRC.slice(SRC.indexOf("function ProfileRow"), SRC.indexOf("function PanelsView"))).toContain(
      "agentDescriptionSentence(agent.description)",
    );
  });
});
