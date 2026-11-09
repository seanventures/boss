/**
 * WAITING FOR GITHUB IS NEVER "FAILED" (BOSSAPP-922), AND A LOCAL PROJECT'S MACHINE CHECKS ARE
 * DOTS (BOSSAPP-924).
 *
 * A build that finished while GitHub was down ends `waiting_for_github`: its commits are safe on
 * this machine and BOSS pushes them when GitHub is back. Every surface (the build page, Project ›
 * Work, Agents at work, Needs you) draws it with ONE helper (lib/github-wait.ts) and ONE
 * component (components/github-wait.tsx, reached through WhyLine), calm blue, with the next try.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { GitHubWait } from "../components/github-wait";
import { WhyLine } from "../components/stuck-work";
import { GITHUB_WAIT_WORDS, isWaitingForGitHub, waitingWords } from "../lib/github-wait";
import { fixedWords, parseMachine } from "../lib/machine-checks";
import { shelfState } from "../lib/command-center-apps";
import { parseWaitReason } from "../lib/stuck-work";
import { parseOnboarding, parseOverview } from "../lib/project-experience";
import { STILL_STATES } from "../app/factory/build-row";
import { sortBuilds } from "../app/factory/floor";
import { MachineDots } from "../app/projects/onboarding";

const ROOT = join(__dirname, "..");
const NOW = Date.parse("2026-11-08T14:00:00Z");

describe("the helper", () => {
  it("knows the status, the build state and the wait code", () => {
    for (const s of ["waiting_for_github", "WAITING_FOR_GITHUB", "github_wait", "github"]) expect(isWaitingForGitHub(s)).toBe(true);
    for (const s of ["failed", "running", "", null, undefined, "blocked"]) expect(isWaitingForGitHub(s)).toBe(false);
  });

  it("says the operator's words, then when BOSS tries next", () => {
    expect(GITHUB_WAIT_WORDS).toBe("Waiting for GitHub · your work is saved here and will push when GitHub is back");
    expect(waitingWords(null, NOW).next).toBe("");
    expect(waitingWords(null, NOW).words).toBe(GITHUB_WAIT_WORDS);
    expect(waitingWords("2026-11-08T14:03:00Z", NOW).next).toBe("next try in 3 min");
    expect(waitingWords("2026-11-08T13:59:00Z", NOW).next).toBe("trying now");
    const later = waitingWords("2026-11-08T16:32:00Z", NOW).next;
    expect(later).toMatch(/^next try \d\d:\d\d$/);
    expect(waitingWords("2026-11-08T14:03:00Z", NOW).words).toMatch(/· next try in 3 min$/);
  });

  it("never says failed, never paints red", () => {
    const html = renderToStaticMarkup(<GitHubWait nextAt="2026-11-08T14:05:00Z" now={NOW} />);
    expect(html).toContain('data-testid="github-wait"');
    expect(html).toContain("Waiting for GitHub");
    expect(html).toContain("your work is saved here and will push when GitHub is back");
    expect(html).toContain("next try in 5 min");
    expect(html.toLowerCase()).not.toContain("fail");
    const css = readFileSync(join(ROOT, "components/github-wait.css"), "utf8");
    expect(css).not.toMatch(/--red/);
    expect(css).not.toMatch(/font-size:\s*\d/);
  });
});

describe("every surface draws it through WhyLine", () => {
  const why = parseWaitReason({ code: "github", who: "boss", words: "Waiting for GitHub · your work is saved here and will push when GitHub is back",
                                detail: "2 push tries so far", since: null, eta_minutes: 3, next_at: "2026-11-08T14:03:00Z" });

  it("parses the next try", () => {
    expect(why?.nextAt).toBe("2026-11-08T14:03:00Z");
    expect(why?.code).toBe("github");
  });

  it("WhyLine draws the one component (testid github-wait)", () => {
    const html = renderToStaticMarkup(<WhyLine why={why} testId="px-work-why-1" compact />);
    expect(html).toContain('data-testid="px-work-why-1"');
    expect(html).toContain('data-testid="github-wait"');
    expect(html).not.toMatch(/data-who="stuck"/);
  });

  it("Project › Work, Agents at work, the build page and Needs you all use WhyLine or GitHubWait", () => {
    for (const f of ["app/projects/tab-work.tsx", "app/projects/drawers.tsx", "app/command-center/agents-at-work.tsx",
                     "app/agent-ops/agent-ops-home.tsx", "app/factory/cockpit.tsx"]) {
      expect(readFileSync(join(ROOT, f), "utf8"), f).toMatch(/<WhyLine /);
    }
    expect(readFileSync(join(ROOT, "app/projects/tab-overview.tsx"), "utf8")).toMatch(/<GitHubWait /);
  });
});

describe("the state maps", () => {
  it("the Floor keeps a waiting build on the line, still, and never Failed", () => {
    expect(STILL_STATES.has("github_wait")).toBe(true);
    const b = (runId: string, state: string) => ({ runId, state } as never);
    const sorted = sortBuilds([b("q", "queued"), b("g", "github_wait"), b("p", "needs_plan")]);
    expect((sorted as { runId: string }[]).map((x) => x.runId)).toEqual(["p", "g", "q"]);
    expect(readFileSync(join(ROOT, "app/factory/factory.css"), "utf8")).toMatch(/\.fx-word-github_wait \{ color: var\(--blue\)/);
  });

  it("an app card waiting for GitHub is building, never failed", () => {
    const app = { state: "github_wait", word: "Waiting for GitHub", prNumber: null, prState: null, attemptStates: {} } as never;
    expect(shelfState(app, null)).toBe("building");
  });

  it("Needs you lists it as an FYI, never as a card it counts", () => {
    const ov = parseOverview({ project: { id: "p", name: "P" }, needs_you: [],
                               github_waits: [{ id: "wfr_1", label: "Login page", href: "?x", words: GITHUB_WAIT_WORDS, next_at: "2026-11-08T14:03:00Z" }] });
    expect(ov.needs_you).toHaveLength(0);
    expect(ov.github_waits?.[0]).toMatchObject({ id: "wfr_1", label: "Login page", next_at: "2026-11-08T14:03:00Z" });
  });
});

describe("the machine checks (BOSSAPP-924)", () => {
  const raw = {
    dots: [{ key: "git", label: "git", ok: "true", words: "git version 2.44" },
           { key: "local_repo", label: "repo", ok: "true", words: "local_p/notes on this machine" },
           { key: "disk", label: "disk", ok: "true", words: "120 GB free" },
           { key: "network", label: "network", ok: "true", words: "Offline: GitHub is not reachable." },
           { key: "model", label: "model", ok: "false", words: "offline and remote" }],
    fixed: ["made the local repository", "made the first commit"], online: false,
    cant: [{ key: "model", line: "Offline: the model is remote — connect a local engine in Repo and models", href: "?project_id=p&area=setup&focus=models", link: "Repo and models" }],
  };

  it("parses five dots in order, an unknown one grey", () => {
    const m = parseMachine({ ...raw, dots: raw.dots.filter((d) => d.key !== "disk") })!;
    expect(m.dots.map((d) => d.key)).toEqual(["git", "local_repo", "disk", "network", "model"]);
    expect(m.dots.find((d) => d.key === "disk")?.ok).toBe("unknown");
    expect(parseMachine(null)).toBeNull();
    expect(fixedWords(m)).toBe("Fixed: made the local repository, made the first commit");
  });

  it("the onboarding item carries it", () => {
    const ob = parseOnboarding({ state: "needs_you", checklist: [{ key: "repo_access", label: "Repo access", state: "failed", owner: "you", machine: raw }] });
    expect(ob.checklist[0].machine?.cant[0].line).toMatch(/^Offline: the model is remote/);
  });

  it("draws dots, what was fixed, and one line with its fix link", () => {
    const html = renderToStaticMarkup(<MachineDots m={parseMachine(raw)!} />);
    expect(html).toContain('data-testid="ob-machine"');
    for (const k of ["git", "local_repo", "disk", "network", "model"]) expect(html).toContain(`data-testid="ob-dot-${k}"`);
    expect(html).toMatch(/data-ok="false"[^>]*data-testid="ob-dot-model"|data-testid="ob-dot-model"[^>]*data-ok="false"/);
    expect(html).toContain('data-testid="ob-machine-fixed"');
    expect(html).toContain('data-testid="ob-machine-cant"');
    expect(html).toContain("Repo and models ›");
    const css = readFileSync(join(ROOT, "app/projects/onboarding.css"), "utf8");
    const mine = css.slice(css.indexOf("BOSSAPP-924"));
    expect(mine).not.toMatch(/font-size:\s*\d/);
  });
});
