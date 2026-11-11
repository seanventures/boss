// @vitest-environment happy-dom
/**
 * EVERY CARD NAMES ITS CHANGE (BOSSAPP-994).
 *
 * A run/work/build card that produced a change links it as "Change #N" (local change set) or
 * "PR #N" (GitHub) — through ONE component, `ChangeLink`, whose words and destination come from
 * `lib/repo-links.ts`. Before this, the Floor's build rows had no link at all, the cockpit's
 * attempts menu and v2 run rows said "PR #N" to projects with no GitHub anywhere, and the
 * overview's work rows showed the words without the link. The scan below keeps any new card from
 * spelling "PR #" by hand again: the host decides the words, never the card.
 */
import React from "react";
import { describe, expect, it } from "vitest";

import { ChangeLink } from "../app/change-link";
import { BuildRow } from "../app/factory/build-row";
import { parseBuildRow } from "../lib/factory-floor";
import { codeOnly } from "./_helpers/source";
import { mountAs, unmount } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LOCAL = "http://boss-local/local_p1/helpdesk/pull/4";
const GITHUB = "https://github.com/acme/web/pull/9";

/** The card components that say what a run shipped. The words "PR #" may only come from
 *  `changeRef`/`ChangeLink`: a literal here would call a local change set a pull request. */
const CARD_FILES = [
  "app/change-link.tsx",
  "app/projects/tab-work.tsx",
  "app/projects/tab-overview.tsx",
  "app/projects/drawers.tsx",
  "app/projects/drawers-more.tsx",
  "app/factory/build-row.tsx",
  "app/factory/floor.tsx",
  "app/factory/cockpit.tsx",
  "app/factory/apps-tab.tsx",
  "app/factory/outputs.tsx",
  "app/run-report-ui.tsx",
  "components/v2/WorkspaceScreen.tsx",
];

describe("no card spells PR # by hand", () => {
  for (const file of CARD_FILES) {
    it(`${file} gets its change words from repo-links`, () => {
      expect(codeOnly(file)).not.toContain("PR #");
    });
  }
});

describe("ChangeLink", () => {
  it("names a local change set Change #N and opens BOSS's change page", async () => {
    await mountAs(<ChangeLink url={LOCAL} number={4} testid="t-local" />);
    const a = document.querySelector("[data-testid='t-local']") as HTMLAnchorElement;
    expect(a.textContent).toContain("Change #4");
    expect(a.textContent).not.toContain("PR");
    expect(a.getAttribute("href")).toBe("/changes/p1/4");
    expect(a.getAttribute("target")).toBeNull(); // stays in BOSS
    unmount();
  });

  it("names a GitHub pull request PR #N and opens its project's change page", async () => {
    await mountAs(<ChangeLink url={GITHUB} number={9} testid="t-gh" />);
    const a = document.querySelector("[data-testid='t-gh']") as HTMLAnchorElement;
    expect(a.textContent).toContain("PR #9");
    expect(a.getAttribute("href")).toBe("/changes/github/acme/web/9");
    unmount();
  });

  it("a number without a recorded URL is words, never a guessed link", async () => {
    await mountAs(<ChangeLink url={null} number={7} testid="t-bare" />);
    expect(document.querySelector("a[data-testid='t-bare']")).toBeNull();
    expect(document.querySelector("[data-testid='t-bare']")!.textContent).toContain("PR #7");
    unmount();
  });

  it("renders nothing without a number", async () => {
    await mountAs(<ChangeLink url={LOCAL} number={null} testid="t-none" />);
    expect(document.querySelector("[data-testid='t-none']")).toBeNull();
    unmount();
  });
});

describe("a Floor build row links the change it produced", () => {
  const row = (pr: unknown) => parseBuildRow({
    run_id: "crun_1", app: "HelpDesk", title: "Ship it", attempt: 1, state: "pr_open",
    word: "PR open", verb: "Shipped", station: null, stations: [], k: 3, n: 3, rounds: 0,
    cost_usd: 0, metered: false, pr,
  });

  it("shows Change #N for a local change set", async () => {
    await mountAs(<ul><BuildRow build={row({ number: 4, url: LOCAL })} now={null} onOpen={() => undefined} /></ul>);
    const a = document.querySelector("[data-testid='fx-build-pr-crun_1']") as HTMLAnchorElement;
    expect(a.textContent).toContain("Change #4");
    expect(a.getAttribute("href")).toBe("/changes/p1/4");
    unmount();
  });

  it("shows nothing before a change exists", async () => {
    await mountAs(<ul><BuildRow build={row(null)} now={null} onOpen={() => undefined} /></ul>);
    expect(document.querySelector("[data-testid='fx-build-pr-crun_1']")).toBeNull();
    unmount();
  });

  it("parseBuildRow keeps the server's pr and drops a number-less one", () => {
    expect(row({ number: 9, url: GITHUB }).pr).toEqual({ number: 9, url: GITHUB });
    expect(row({ url: GITHUB }).pr).toBeNull();
    expect(row(null).pr).toBeNull();
  });
});
