/**
 * A recent Studio run says why it stopped, in the row, not only on the title.
 * A blank reason says nothing. A sentence over six words is a paragraph.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { AgentProfile, runStoppedSentence } from "../app/agent-profile-ui";
import type { AgentStudioFeed, StudioAgent, StudioRun } from "../lib/agent-studio";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SRC = codeOnly(join(__dirname, "../app/agent-profile-ui.tsx"));

const agent: StudioAgent = {
  id: "def_qa", agent_key: "qa", name: "QA", owner: "ceo", status: "active", active_run_count: 0,
  active_version: {
    id: "ver_qa", version: 1, role_key: "qa", model_policy: {}, requested_skills: [],
    scopes: {}, validation: { valid: true },
  },
};

function run(id: string, reason: string | null | undefined, summary = ""): StudioRun {
  return {
    id, status: "blocked", agent_key: "qa", created_at: "2026-11-16T00:00:00Z",
    completed_at: "2026-11-16T01:00:00Z",
    blocked_reason: reason ?? undefined,
    output: summary ? { summary } : {},
  };
}

const SHORT = "no credit";
const SIX = "one two three four five";
const LONG = "the model ran out of credit overnight";

function feed(runs: StudioRun[]): AgentStudioFeed {
  return {
    projectId: "p1", apiReachable: true, agents: [agent], panels: [],
    agentRuns: runs, panelRuns: [], tests: [],
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

async function show(runs: StudioRun[]): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<AgentProfile feed={feed(runs)} roleKey="qa" actor="ceo" bare />);
  });
  return host;
}

function row(hostEl: HTMLElement, title: string): HTMLLIElement {
  const found = [...hostEl.querySelectorAll("li")].find((li) => li.getAttribute("title") === title);
  expect(found, title).toBeTruthy();
  return found as HTMLLIElement;
}

function sentenceEl(li: HTMLElement, sentence: string): Element | undefined {
  return [...li.querySelectorAll("p, span")].find((el) => el.textContent === sentence);
}

describe("runStoppedSentence", () => {
  it("is Stopped and the trimmed reason, or nothing", () => {
    expect(runStoppedSentence("  no credit  ")).toBe("Stopped: no credit");
    expect(runStoppedSentence("budget")).toBe("Stopped: budget");
    expect(runStoppedSentence(null)).toBeNull();
    expect(runStoppedSentence(undefined)).toBeNull();
    expect(runStoppedSentence("")).toBeNull();
    expect(runStoppedSentence(" \n\t ")).toBeNull();
  });
});

describe("a recent run says why it stopped", () => {
  it("shows the sentence in the run, as a paragraph when it is longer than six words", async () => {
    const shown = await show([
      run("r-short", SHORT),
      run("r-six", SIX),
      run("r-long", LONG),
      run("r-blank", "   ", "Shipped the brief"),
      run("r-ok", null, "Shipped the brief"),
    ]);
    const short = row(shown, "Stopped: no credit");
    const six = row(shown, `Stopped: ${SIX}`);
    const long = row(shown, `Stopped: ${LONG}`);
    expect(short.textContent).toContain("Stopped: no credit");
    expect(sentenceEl(short, "Stopped: no credit")?.tagName).toBe("SPAN");
    expect(sentenceEl(six, `Stopped: ${SIX}`)?.tagName).toBe("SPAN");
    expect(long.textContent).toContain(`Stopped: ${LONG}`);
    expect(sentenceEl(long, `Stopped: ${LONG}`)?.tagName).toBe("P");
    const blank = row(shown, "Stopped:    ");
    expect(blank.textContent ?? "").not.toContain("Stopped");
    const ok = row(shown, "Shipped the brief");
    expect(ok.textContent ?? "").not.toContain("Stopped");
    expect(ok.getAttribute("title")).toBe("Shipped the brief");
  });

  it("calls runStoppedSentence with the run's blocked reason", () => {
    expect(readFileSync(join(__dirname, "../app/agent-profile-ui.tsx"), "utf8")).toContain("blocked_reason");
    expect(SRC).toContain("runStoppedSentence(run.blocked_reason)");
  });
});
