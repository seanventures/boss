// @vitest-environment happy-dom
/** Govern › Journeys (BOSSAPP-1284/1285): today's golden journeys and the freeze's green days. */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JourneysCard } from "../app/govern/journeys-card";
import { counterWords, todayShort, type JourneysView } from "../lib/golden-journeys";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const VIEW: JourneysView = {
  today: "2026-11-25", hosts: ["laptop", "boss-1"], green_days: 3, needed: 5, freeze_lifted: false,
  journeys: [
    { id: "J1", title: "Ticket to pull request", ticket: "BOSSAPP-1284", modes: ["scripted", "real"], pending: null,
      today: { laptop: { status: "green", why: "", recorded_by: "sa_journeys", source: "run" },
              "boss-1": { status: "red", why: "AC-2 missing: status is failed", recorded_by: "usr_x", source: "posted" } } },
    { id: "J2", title: "Enterprise Server repo, in Go", ticket: "BOSSAPP-1284", modes: ["real"],
      pending: { waits_for: "PR #330", why: "GHES routing" },
      today: { laptop: { status: "pending" }, "boss-1": { status: "pending" } } },
  ],
  pending: [{ id: "J2", waits_for: "PR #330", why: "GHES routing" }],
  history: Array.from({ length: 14 }, (_, k) => ({ day: `2026-11-${String(12 + k).padStart(2, "0")}`, green: k > 9, short: k > 9 ? [] : ["J2 pending"] })),
  schedule: { at_time: "06:00", timezone: "Europe/London", next_wake_at: null },
  running: false,
  fixture_project: "journeys",
};

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("the golden journeys card", () => {
  it("shows the green-day counter, the schedule, the last 14 days and a chip per host", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(VIEW)));
    await mount(<JourneysCard projectId="p1" paused={false} />);
    await settle();
    expect($('[data-testid="gv-jr-counter"]')?.textContent).toBe("3 of 5 green days");
    expect($('[data-testid="gv-jr-schedule"]')?.textContent).toBe("Daily 06:00 Europe/London");
    expect($$('[data-testid="gv-jr-days"] li')).toHaveLength(14);
    const j1 = $$('[data-testid="gv-jr-J1"] [data-testid="gv-jr-chip"]');
    expect(j1.map((c) => c.getAttribute("data-status"))).toEqual(["green", "red"]);
    expect(j1[1].getAttribute("title")).toContain("AC-2 missing");
    expect(j1[1].textContent).toContain("boss-1");
    expect(j1[0].getAttribute("title")).toContain("run by sa_journeys");
    expect(j1[1].getAttribute("title")).toContain("posted by usr_x, not counted");
    expect(j1[1].hasAttribute("data-posted")).toBe(true);
  });

  it("says a pending journey waits for its PR", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(VIEW)));
    await mount(<JourneysCard projectId="p1" paused={false} />);
    await settle();
    const chip = $('[data-testid="gv-jr-J2"] [data-testid="gv-jr-chip"]');
    expect(chip?.getAttribute("data-status")).toBe("pending");
    expect(chip?.getAttribute("title")).toContain("PR #330");
  });

  it("Run now posts one run, naming no project (the fixture project is the operator's)", async () => {
    const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) =>
      String(url).includes("/run") ? json({ ok: true, started: ["J1", "J2"] }, 202) : json(VIEW));
    vi.stubGlobal("fetch", fetchMock);
    await mount(<JourneysCard projectId="p1" paused={false} />);
    await settle();
    const button = $('[data-testid="gv-jr-run"]') as HTMLButtonElement;
    expect(button.textContent).toContain("Run now");
    expect(button.querySelector("svg")).not.toBeNull();
    button.click();
    await settle();
    const posts = fetchMock.mock.calls.filter(([u, i]) => String(u).includes("/v1/golden-journeys/run")
      && (i as RequestInit | undefined)?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String((posts[0][1] as RequestInit).body))).toEqual({});
  });

  it("says why the read failed", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ message: "no access" }, 403)));
    await mount(<JourneysCard projectId="p1" paused={false} />);
    await settle();
    expect($('[data-testid="gv-jr-error"]')?.textContent).toBe("no access");
  });
});

describe("the counter's words", () => {
  it("counts days, then says the freeze lifted", () => {
    expect(counterWords({ green_days: 0, needed: 5, freeze_lifted: false })).toBe("0 of 5 green days");
    expect(counterWords({ green_days: 5, needed: 5, freeze_lifted: true })).toBe("Freeze lifted");
  });

  it("names what keeps today from green, pending first", () => {
    expect(todayShort(VIEW)).toBe("1 pending · 1 red");
    expect(todayShort({ ...VIEW, journeys: [VIEW.journeys[0]].map((j) => ({ ...j, today: { laptop: { status: "green" as const } } })) }))
      .toBe("Every journey green today");
  });
});
