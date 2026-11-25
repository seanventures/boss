// @vitest-environment happy-dom
/**
 * A project explains itself (operator, 2026-11-19): "in the Overview page of the project we need to
 * display the PRD overview and the link to the PRD … per work details should display what was
 * discussed and what was implemented by the Agent — we don't need to display full chat but an
 * explanation about what was implemented."
 *
 * Fixtures are the engine's answers for the Park bench map project (park-bench-map-e86c6d9e):
 * GET …/overview `about` and GET …/builds/crun_1caf52e519c04ce4b168/explained (engine
 * services/project_experience/explained.py, apps/api/tests/test_a_project_explains_itself.py).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { criteriaWords, explainedPath, parseAbout, parseExplained } from "../lib/explained";
import { parseOverview, parseWorkStory } from "../lib/project-experience";
import { $, $$, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AboutCard } = await import("../app/projects/about-app");
const { WorkExplained } = await import("../app/projects/work-explained");

afterEach(() => { unmount(); vi.restoreAllMocks(); });

const AC = [
  ["Opening the app shows a MapLibre map rendered from the bundled style document with at least 8 bench points plotted around the invented park, with no tile account or API key used.", "partial"],
  ["Tapping any bench point opens a panel showing that bench's name and its note (starting as placeholder text if no note exists yet).", "met"],
  ["Typing a note and pressing Save shows a \"Saved\" confirmation, and reloading the page still shows that same note for that bench.", "met"],
  ["Disabling the network and reloading the page still shows all bench points on the map, unchanged.", "partial"],
  ["Selecting a bench from the plain HTML list (not the map) opens the same note panel for that bench.", "met"],
  ["Attempting to save a note under a failure condition shows an explicit inline error rather than failing silently.", "met"],
];
export const ABOUT = {
  source: "prd", title: "Park Bench Map",
  summary: "Park Bench Map is a small single-service web app that shows at least eight named bench locations around one invented park on a MapLibre GL map, rendered entirely from assets bundled in the repo (no tile account, no API key, no network calls). Tapping a bench opens a panel with its name and a note the visitor can read and edit. Notes persist in a sqlite file on disk, keyed by the bench's GeoJSON feature id, and survive a page reload.",
  criteria: AC.map(([text, status], i) => ({ n: i + 1, text, status, evidence: "public/app.js:init creates the MapLibre map and markers." })),
  counts: { met: 4, partial: 2, not_met: 0, unchecked: 0 },
  checked_at: "2026-11-19T07:57:09+00:00",
  approved: { words: "Approved by your Easy Mode start", at: "2026-11-19T07:39:21+00:00" },
  spec_href: "/?project_id=park-bench-map-e86c6d9e&area=meetings&meeting=mrun_c41ee0ef307d4cf0b373",
  note: null,
  later: [{ title: "Bench Name Label on Map Bubble", approved_at: "2026-11-19T13:45:48+00:00",
            href: "/?project_id=park-bench-map-e86c6d9e&area=meetings&meeting=mrun_c3927a97702b4f37ab75" }],
  later_count: 1,
};
export const EXPLAINED = {
  discussed: {
    version: 1, seats: 5,
    decisions: [
      "Stack line conflict (Juno said Node/Vite, Forge said Node/Express): one service. Stack: park-bench-map=Node/Express — Express serves the API (sqlite CRUD on notes) and…",
      "node:sqlite vs fallback: try node:sqlite first; if Node version <22.5, fall back to sql.js. This check happens before writing persistence code, not discovered mid-build.",
      "\"Offline\" scope: strict zero-network-calls, matching Juno's and Forge's stricter read. No dissent.",
      "Service worker for offline: not building one. Bundling assets locally already satisfies the offline requirement and the stated test.",
    ],
    open_questions: ["Repo state unknown: Forge has not yet read the existing park-bench-map repo."],
    dissent: null,
    href: "/?project_id=park-bench-map-e86c6d9e&area=meetings&meeting=mrun_c41ee0ef307d4cf0b373",
  },
  built: {
    version: 1, build_id: "crun_1caf52e519c04ce4b168", status: "completed", title: "Park bench map", number: 1,
    bullets: [
      "Fixture & style: data/benches.geojson (9 named benches around invented \"Willowmere Park\"), public/style.json (background-only MapLibre style, no…",
      "Changed 17 files: server code 3, styles 2, data 1, screens 1, +3 more areas",
      "Tests: 4 test files added or updated, all 15 tests pass",
      "Review: 2 review rounds, 8 findings raised, 4 of 6 acceptance criteria met, 2 partly",
      "Left as follow-ups: benches_viewed_total{result=\"success\"} increments when HTML is requested, before the GeoJSON…; +2 more",
      "Merged as change #1",
    ],
    groups: [
      { purpose: "Server code", files: ["index.js", "metrics.js", "server.js"], count: 3 },
      { purpose: "Styles", files: ["public/app.css", "public/style.json"], count: 2 },
      { purpose: "Data", files: ["data/benches.geojson"], count: 1 },
      { purpose: "Screens", files: ["public/app.js"], count: 1 },
      { purpose: "Tests", files: ["tests/deploy-manifest.test.js", "tests/metrics.test.js", "tests/notes-api.test.js", "tests/offline-bundle.test.js"], count: 4 },
      { purpose: "Deploy setup", files: ["Dockerfile", "boss.deploy.yaml", "boss.preview.command", "docker-compose.yml"], count: 4 },
      { purpose: "Dependencies", files: ["package-lock.json", "package.json"], count: 2 },
    ],
    files_changed: 17, follow_ups: ["benches_viewed_total…", "Must-have 1…", "Must-have 4…"],
  },
  later: [{ build_id: "crun_ed46708f50594e3ab559", title: "Follow-up: deferred review findings — Park bench map", number: 2,
            line: "The implementation already correctly addresses all deferred findings: app.js sets note via textarea.value property…" }],
};

describe("About this app", () => {
  it("parses the engine's card and words the checklist", () => {
    const about = parseAbout(ABOUT)!;
    expect(about.source).toBe("prd");
    expect(about.criteria.map((c) => c.status)).toEqual(["partial", "met", "met", "partial", "met", "met"]);
    expect(criteriaWords(about)).toBe("4 of 6 met · 2 partly");
    expect(criteriaWords({ criteria: about.criteria.map((c) => ({ ...c, status: "unchecked" as const })), counts: { met: 0, partial: 0, not_met: 0, unchecked: 6 } })).toBe("Not checked yet");
    expect(parseAbout(null)).toBeNull();
    // The Overview carries it (and a card that failed carries none).
    expect(parseOverview({ about: ABOUT }).about?.title).toBe("Park Bench Map");
    expect(parseOverview({}).about).toBeNull();
  });

  it("shows the summary, each criterion's verdict, who approved it and the link to the full spec", async () => {
    await mount(<AboutCard about={parseAbout(ABOUT)!} />);
    expect($('[data-testid="px-ov-about-title"]')?.textContent).toBe("Park Bench Map");
    expect($('[data-testid="px-ov-about-summary"]')?.textContent).toMatch(/^Park Bench Map is a small single-service web app/);
    expect($('[data-testid="px-ov-about-approved"]')?.textContent).toMatch(/^Approved by your Easy Mode start · Nov 19, 2026/);
    expect($('[data-testid="px-ov-about-verdict"]')?.textContent).toBe("4 of 6 met · 2 partly");
    const rows = $$('[data-testid^="px-ov-about-ac-"]');
    expect(rows).toHaveLength(6);
    expect(rows[0].getAttribute("data-status")).toBe("partial");
    expect(rows[0].textContent).toMatch(/Partly$/);
    expect(rows[1].textContent).toMatch(/Met$/);
    const spec = $('[data-testid="px-ov-about-spec"]') as HTMLAnchorElement;
    expect(spec.textContent).toMatch(/Read the full spec/);
    expect(spec.getAttribute("href")).toMatch(/area=meetings.*meeting=mrun_c41ee0ef307d4cf0b373/);
    expect($('[data-testid="px-ov-about-later"]')?.textContent).toMatch(/Changed since:.*Bench Name Label on Map Bubble/);
  });

  it("a project with no PRD shows its brief and says so", async () => {
    await mount(<AboutCard about={parseAbout({ source: "brief", title: "Park bench map", summary: "A visitor taps a bench and reads its note.",
                                              criteria: [], approved: null, spec_href: null, note: "No spec yet — this is the project brief.", later: [] })!} />);
    expect($('[data-testid="px-ov-about"]')?.getAttribute("data-source")).toBe("brief");
    expect($('[data-testid="px-ov-about-summary"]')?.textContent).toBe("A visitor taps a bench and reads its note.");
    expect($('[data-testid="px-ov-about-note"]')?.textContent).toBe("No spec yet — this is the project brief.");
    expect($('[data-testid="px-ov-about-spec"]')).toBeNull();
    expect($('[data-testid="px-ov-about-criteria"]')).toBeNull();
  });
});

describe("What was discussed / What was built", () => {
  it("parses decisions and bullets, and an empty answer is nothing to show", () => {
    const got = parseExplained(EXPLAINED)!;
    expect(got.discussed?.decisions).toHaveLength(4);
    expect(got.built?.bullets).toHaveLength(6);
    expect(got.built?.groups.map((g) => g.purpose)).toContain("Deploy setup");
    expect(parseExplained({ discussed: { decisions: [] }, built: { bullets: [] } })).toBeNull();
    expect(parseExplained({ built: EXPLAINED.built })?.discussed).toBeNull();
    expect(parseWorkStory({ explained: EXPLAINED }).explained?.built?.buildId).toBe("crun_1caf52e519c04ce4b168");
    expect(explainedPath("park-bench-map-e86c6d9e", "crun_1")).toBe("/v1/projects/park-bench-map-e86c6d9e/builds/crun_1/explained");
  });

  it("shows the panel's decisions (never the chat) and the build's bullets with files by purpose", async () => {
    await mount(<WorkExplained explained={parseExplained(EXPLAINED)} testid="px-drawer-work-explained" />);
    const said = $('[data-testid="px-drawer-work-explained-discussed"]')!;
    expect(said.textContent).toMatch(/What was discussed/);
    expect(said.querySelectorAll("li")).toHaveLength(4);
    expect(said.textContent).toMatch(/Left for you: Repo state unknown/);
    expect(said.textContent).toMatch(/panel of 5 agents/);
    expect(($('[data-testid="px-drawer-work-explained-meeting"]') as HTMLAnchorElement).getAttribute("href")).toMatch(/meeting=mrun_c41ee0ef307d4cf0b373/);
    const built = $('[data-testid="px-drawer-work-explained-built"]')!;
    const items = [...built.querySelectorAll("li")].map((li) => li.textContent);
    expect(items.length).toBeGreaterThanOrEqual(3);
    expect(items.length).toBeLessThanOrEqual(6);
    expect(items).toContain("Tests: 4 test files added or updated, all 15 tests pass");
    expect(items[items.length - 1]).toBe("Merged as change #1");
    const chips = [...$$('[data-testid="px-drawer-work-explained-groups"] .wx-group')];
    expect(chips.map((c) => c.textContent)).toEqual(["Server code 3", "Styles 2", "Data 1", "Screens 1", "Tests 4", "Deploy setup 4", "Dependencies 2"]);
    expect(chips[0].getAttribute("title")).toBe("index.js\nmetrics.js\nserver.js");
    expect($('[data-testid="px-drawer-work-explained-later"]')?.textContent).toMatch(/^Then change #2: The implementation already/);
  });

  it("renders nothing when there is nothing to explain", async () => {
    await mount(<WorkExplained explained={null} />);
    expect($('[data-testid="px-explained"]')).toBeNull();
  });
});
