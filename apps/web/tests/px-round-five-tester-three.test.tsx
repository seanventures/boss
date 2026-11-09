// @vitest-environment happy-dom
/**
 * TESTER ROUND 3 (feat/px-r5): each finding fixed at its cause, pinned here.
 *
 * 00b-13/PX-15 the guide follows the project being onboarded, never a deployed one; 03-16 Access is
 * never ✓ beside "Connect GitHub first"; 04-02 a journey day is the viewer's; 07-18 Open work
 * opens that item; 00a-26 the sample checks the model before it creates anything; X-14 one
 * needs-you number across projects; 00a-34 first run replaces Command Center's setup rows;
 * 12-09 an incident line always has its clock; 10-08 the strip's wires follow done and now;
 * 00a-06 Helm is the largest face of the orbit; X-11 / 04 at 390 nothing runs off a phone.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { guideSteps, pickGuided } from "../app/projects/first-run";
import { repoChecks } from "../app/start-project/import-repo";
import { acceptedWorkId } from "../app/projects/tab-roadmap";
import { dayTime } from "../app/projects/tab-health";
import { isSetupRow } from "../app/command-center-ui";
import { installWideRefusal } from "../lib/start-project";
import { nodeWords, orgNeedsOf, type ProjectCard, type ProjectsSummary } from "../lib/project-experience";

const card = (id: string, over: Partial<ProjectCard>): ProjectCard => ({
  id, name: id, initials: "P", repo: `acme/${id}`, stage: "onboarding", journey: [], needs_you: 0, running: 0, health: "unknown",
  live_url: "", captain: null, updated_at: "2026-11-01T00:00:00Z", href: "", cost_week: null, series: [], status_label: "",
  onboarding_active: false, start_onboarding: null, archived: false, uptime_pct: null, onboarding_progress: null, lines: [],
  line_tone: "", crew: [], live_agents: [], merged: 0, building: 0, week_cap_usd: null, ...over,
} as ProjectCard);

describe("the first-run guide (00b-13, PX-15)", () => {
  it("follows the project being onboarded, never a deployed one", () => {
    const deployed = card("live", { stage: "staging", updated_at: "2026-11-06T00:00:00Z" });
    const fresh = card("new", { stage: "onboarding", updated_at: "2026-11-05T00:00:00Z" });
    expect(pickGuided([deployed, fresh], "live")?.id).toBe("new");
    expect(pickGuided([deployed, fresh, card("reading", { onboarding_active: true })], null)?.id).toBe("reading");
    expect(pickGuided([deployed], null)?.id).toBe("live");
    expect(pickGuided([], null)).toBeNull();
  });
  it("reads a project that already worked as onboarded, never 'not started yet'", () => {
    const steps = guideSteps({ name: "x", repo: "acme/x", journey: [{ key: "working", state: "done" }] },
      { state: "not_started", stages: [] } as never);
    expect(steps.every((s) => s.state === "done")).toBe(true);
    expect(steps.map((s) => s.sub).join(" ")).not.toContain("not started");
  });
});

describe("Add a project (03-16)", () => {
  it("never ticks Access beside Connect GitHub first", () => {
    const repo = { id: "r", full_name: "acme/a", checks: {}, new_project: { ok: false, message: "Connect GitHub first." } } as never;
    const access = repoChecks(repo, { facts: { language: "TS" }, mapping: {} } as never)[0];
    expect(access.state).toBe("failed");
    expect(access.sub).toBe("connect GitHub first");
  });
});

describe("dates are the viewer's (04-02, 12-09)", () => {
  it("a dated journey node says its day from the moment, in the viewer's zone", () => {
    const n = { key: "onboarding", label: "Onboarded", state: "done" as const, sub: "", href: "", detail: "Onboarded {day} — Express", at: "2026-11-02T06:30:00Z" };
    const words = nodeWords(n, () => "Nov 1");
    expect(words).toEqual({ sub: "Nov 1", detail: "Onboarded Nov 1 — Express" });
  });
  it("an incident on another day still says its clock time", () => {
    const now = new Date(2026, 10, 6, 18, 0);
    expect(dayTime(new Date(2026, 10, 4, 9, 12).toISOString(), now)).toBe("Wed 09:12");
    expect(dayTime(new Date(2026, 9, 2, 9, 12).toISOString(), now)).toMatch(/09:12$/);
  });
});

describe("Roadmap › Accepted (07-18)", () => {
  it("Open work opens the run it became, else its planned row, else the row of its name", () => {
    const items = [{ id: "wfr_1", label: "Email intake" }, { id: "wi_2", label: "Weekend SLA fix" }];
    expect(acceptedWorkId(items, { id: "wi_9", title: "x", work_id: "wfr_1" })).toBe("wfr_1");
    expect(acceptedWorkId(items, { id: "wi_2", title: "Weekend SLA fix" })).toBe("wi_2");
    expect(acceptedWorkId(items, { id: "wi_7", title: "Email intake" })).toBe("wfr_1");
    expect(acceptedWorkId(items, { id: "wi_7", title: "Nothing like it" })).toBeNull();
  });
});

describe("the sample (00a-26)", () => {
  it("is refused for a missing model BEFORE anything is created", async () => {
    const asked: string[] = [];
    const got = await installWideRefusal("home", "minor_feature", async <T,>(p: string) => {
      asked.push(p);
      return { ready: false, checked: [{ key: "model", ok: false }, { key: "repository", ok: false }], missing: [{ key: "model" }, { key: "repository" }] } as T;
    });
    expect(asked[0]).toBe("/v1/delivery-workflows/minor_feature/readiness?project_id=home");
    expect(got?.missing?.map((m) => m.key)).toEqual(["model"]);
    expect(await installWideRefusal("home", "minor_feature", async <T,>() => ({ missing: [{ key: "repository" }], checked: [] }) as T)).toBeNull();
    expect(await installWideRefusal("home", "minor_feature", async () => { throw new Error("404"); })).toBeNull();
    // "Start anyway" on the model goes on to create it.
    expect(await installWideRefusal("home", "minor_feature", async <T,>() => ({ missing: [{ key: "model" }], checked: [] }) as T, ["model"])).toBeNull();
  });
  it("the sheet asks before it creates the project", () => {
    const src = readFileSync("app/start-project/start-sheet.tsx", "utf8");
    expect(src.indexOf("installWideRefusal(")).toBeGreaterThan(0);
    expect(src.indexOf("installWideRefusal(")).toBeLessThan(src.indexOf("createFresh(SAMPLE.name)"));
  });
});

describe("one needs-you number (X-14) and first run (00a-34)", () => {
  it("sums every open project's own count", () => {
    const s = { items: [card("a", { needs_you: 2 }), card("b", { needs_you: 1 }), card("c", { needs_you: 4, archived: true })] } as ProjectsSummary;
    expect(orgNeedsOf(s)).toBe(3);
    expect(orgNeedsOf(null)).toBeNull();
  });
  it("the bell, the rail badge and the tab title read it", () => {
    const shell = readFileSync("app/ConsoleShell.tsx", "utf8");
    expect(shell).toContain("useTitleCount(orgNeeds ??");
    expect(shell).toContain("const n = orgNeeds ??");
    expect(shell).toContain('variant="bell" total={orgNeeds}');
  });
  it("Command Center drops its setup rows while there is no project, and shows first run", () => {
    expect(isSetupRow({ kind: "readiness" } as never)).toBe(true);
    expect(isSetupRow({ kind: "approval" } as never)).toBe(false);
    const cc = readFileSync("app/command-center-ui.tsx", "utf8");
    expect(cc).toContain('data-testid="cc-first-run"');
  });
});

describe("drawn as the wireframe (00a-06, 10-08) and nothing off a phone (X-11, 04)", () => {
  it("Helm is the largest face of the orbit, on the left", () => {
    const src = readFileSync("app/projects/first-run.tsx", "utf8");
    const sizes = [...src.matchAll(/name: "(\w+)", role_key: "\w+", x: (\d+), y: \d+, size: (\d+)/g)].map((m) => ({ n: m[1], x: +m[2], s: +m[3] }));
    const helm = sizes.find((f) => f.n === "Helm")!;
    expect(sizes).toHaveLength(5);
    expect(sizes.every((f) => f.n === "Helm" || f.s < helm.s)).toBe(true);
    expect(sizes.every((f) => f.x >= helm.x)).toBe(true);
  });
  it("a stage wire is green only from a done stage into a done or current one", () => {
    const css = readFileSync("app/projects/px.css", "utf8");
    expect(css).toContain('.px-ob-s[data-w="on"]::before { background: var(--green); }');
    expect(css).not.toContain('.px-ob-s[data-s="done"]::before, .px-ob-s[data-s="now"]::before');
  });
  it("on a phone the top bar is solid, card headers wrap and a mini flow gets its own line", () => {
    const shell = readFileSync("app/shell.css", "utf8");
    expect(shell).toMatch(/\.bx-shell \.bx-topbar \{ height: 56px; gap: 10px; padding: 0 20px; background: var\(--base[^}]*backdrop-filter: none; \}/);
    const css = readFileSync("app/projects/px.css", "utf8");
    expect(css).toContain(".px-ov-wk > .px-ov-wk-st[data-flow] { grid-column: 2 / -1; grid-row: 2;");
    expect(css).toContain(".px-card-h { flex-wrap: wrap; row-gap: 6px; }");
  });
});
