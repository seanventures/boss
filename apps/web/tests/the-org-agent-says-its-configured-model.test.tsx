/**
 * The org agent says its configured model, beside the models it called, not only in the title.
 * A blank model says nothing. A sentence over six words is a paragraph.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { OrgAgentPanel, configuredModelSentence } from "../app/agents/org-agent-panel";
import { parseOrgBoard } from "../lib/agents-org-board";
import { orgWire } from "./_helpers/org-board-fixtures";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SRC = codeOnly(join(__dirname, "../app/agents/org-agent-panel.tsx"));

const SHORT = "gpt-4o";
const SIX = "one two three four five";
const LONG = "one two three four five six";
const MODELS = "claude-sonnet-4-6 40, haiku 3";

function board(model: string | null) {
  return parseOrgBoard(orgWire({
    agents: {
      qa: {
        spend: { metered_usd: 1, plan_calls: 2 },
        models: [{ model: "claude-sonnet-4-6", calls: 40 }, { model: "haiku", calls: 3 }],
        configured_model: model,
      },
    },
  }));
}

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
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(model: string | null): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <OrgAgentPanel board={board(model)} live={new Map()} roleKey="qa" days={7} actions={null} onClose={() => {}} />,
    );
  });
  const row = document.querySelector<HTMLElement>("[data-row='window']");
  expect(row, "window row").toBeTruthy();
  return row as HTMLElement;
}

function sentenceEl(row: HTMLElement, sentence: string): Element | undefined {
  return [...row.querySelectorAll("p, span")].find((el) => el.textContent === sentence);
}

describe("configuredModelSentence", () => {
  it("is Configured and the trimmed model, or nothing", () => {
    expect(configuredModelSentence("  gpt-4o  ")).toBe("Configured: gpt-4o.");
    expect(configuredModelSentence(SHORT)).toBe("Configured: gpt-4o.");
    expect(configuredModelSentence(SIX)).toBe(`Configured: ${SIX}.`);
    expect(configuredModelSentence(LONG)).toBe(`Configured: ${LONG}.`);
    expect(configuredModelSentence(null)).toBeNull();
    expect(configuredModelSentence(undefined)).toBeNull();
    expect(configuredModelSentence("")).toBeNull();
    expect(configuredModelSentence(" \n\t ")).toBeNull();
  });
});

describe("the org agent says its configured model", () => {
  it("shows the sentence beside the models it called, as a paragraph when it is longer than six words", async () => {
    const short = await show(SHORT);
    expect(short.textContent).toContain(MODELS);
    expect(short.textContent).toContain("Configured: gpt-4o.");
    expect(sentenceEl(short, "Configured: gpt-4o.")?.tagName).toBe("SPAN");
    const models = [...short.querySelectorAll("span")].find((el) => el.textContent === MODELS);
    expect(models?.getAttribute("title")).toContain("Configured: gpt-4o.");

    clearHost();

    const six = await show(SIX);
    expect(six.textContent).toContain(MODELS);
    expect(six.textContent).toContain(`Configured: ${SIX}.`);
    expect(sentenceEl(six, `Configured: ${SIX}.`)?.tagName).toBe("SPAN");

    clearHost();

    const long = await show(LONG);
    expect(long.textContent).toContain(MODELS);
    expect(long.textContent).toContain(`Configured: ${LONG}.`);
    expect(sentenceEl(long, `Configured: ${LONG}.`)?.tagName).toBe("P");

    clearHost();

    const blank = await show("   ");
    expect(blank.textContent).toContain(MODELS);
    expect(blank.textContent ?? "").not.toContain("Configured");

    clearHost();

    const missing = await show(null);
    expect(missing.textContent).toContain(MODELS);
    expect(missing.textContent ?? "").not.toContain("Configured");
  });

  it("calls configuredModelSentence with the agent's configured model", () => {
    expect(readFileSync(join(__dirname, "../app/agents/org-agent-panel.tsx"), "utf8")).toContain("configuredModel");
    expect(SRC).toContain("configuredModelSentence(agent.configuredModel)");
  });
});
