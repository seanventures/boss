import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

const read = (p: string) => fs.readFileSync(path.join(process.cwd(), p), "utf8");
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

describe("v2 design tokens match the artifact", () => {
  // Read from docs/product/console-v2-reference/, not eyeballed. If a token drifts, v2 stops
  // looking like the approved reference, and nobody notices until it ships.
  const tokens = read("components/v2/tokens.css");

  it.each([
    ["--v2-canvas", "#090D17"],
    ["--v2-panel", "#0E1422"],
    ["--v2-border", "#1E2940"],
    ["--v2-mint", "#4FE3C1"],
    ["--v2-green", "#58D68A"],
    ["--v2-amber", "#F5B14C"],
    ["--v2-red", "#F26D6D"],
    ["--v2-purple", "#A78BFA"],
    ["--v2-ink", "#E6EDF5"],
    ["--v2-muted", "#8D9AB5"],
    ["--v2-dim", "#5A6785"],
    ["--v2-idle", "#3E4D6B"],
  ])("%s is %s", (token, hex) => {
    expect(tokens).toContain(`${token}: ${hex}`);
  });

  it("uses the artifact's two typefaces", () => {
    expect(tokens).toContain("Space Grotesk");
    expect(tokens).toContain("IBM Plex Mono");
  });

  it("has exactly two animations, both meaning something", () => {
    const keyframes = [...tokens.matchAll(/@keyframes\s+([\w-]+)/g)].map((m) => m[1]);
    expect(keyframes.sort()).toEqual(["v2-blink", "v2-pulse"]);
  });

  it("suppresses motion for readers who asked for less", () => {
    expect(tokens).toContain("prefers-reduced-motion");
  });
});

describe("status is never colour alone", () => {
  // A colourblind operator, a greyscale screenshot and a printed page all have to keep the
  // meaning. Every status carries a dot AND the word.
  it("renders the status word beside the dot", () => {
    const projects = read("components/v2/ProjectsScreen.tsx");
    const i = projects.indexOf('className="v2-status');
    expect(i).toBeGreaterThan(-1);
    const block = projects.slice(i, i + 320);
    expect(block).toContain("v2-dot");
    expect(block).toContain("{p.status}");
  });
});

describe("the console toggle", () => {
  const toggle = stripComments(read("components/v2/ConsoleToggle.tsx"));

  it("offers both consoles", () => {
    expect(toggle).toContain('go("v1")');
    expect(toggle).toContain('go("v2")');
  });

  it("remembers the choice", () => {
    expect(toggle).toContain("localStorage");
  });

  it("does a full navigation, not a client transition", () => {
    // The two consoles are different React trees; a soft transition leaves v1 mounted.
    expect(toggle).toContain("window.location.assign");
  });

  it("is reachable from v1 as well as v2", () => {
    const v1 = read("app/ConsoleShell.tsx");
    expect(v1).toContain("<ConsoleToggle current=\"v1\"");
    const v2 = read("components/v2/Shell.tsx");
    expect(v2).toContain('current="v2"');
  });
});

describe("the v2 shell", () => {
  const shell = read("components/v2/Shell.tsx");

  it("has the artifact's nine nav items in order", () => {
    const ids = [...shell.matchAll(/\{ id: "([a-z]+)"/g)].map((m) => m[1]);
    expect(ids).toEqual([
      "missions", "hive", "factory", "org", "panels",
      "boss", "timeline", "infra", "settings",
    ]);
  });

  it("labels them as v1 labels the same screens", () => {
    // REWRITTEN: this pinned the artifact's labels ("Workforce", "Org Chart", "Decisions",
    // "Workflows"), which named the same screens differently from v1 (console walk on boss-1,
    // 2026-10-02). Labels now match v1 unless docs/product/console_v2_parity.yaml says why —
    // see console-v2-surface.test.ts.
    for (const label of [
      // v1 renamed its own: "Agent work" is Agents and "Workflow" is Workflows (Workflow
      // Studio), and /v2/factory, /v2/org and /v2/boss open those v1 areas.
      "Projects", "Hive", "Agents", "Team structure", "Approvals",
      "Workflows", "Receipts", "Infra & Models", "Settings",
    ]) {
      expect(shell).toContain(`label: "${label}"`);
    }
  });

  it("shows burn, gates and autonomy in the chrome", () => {
    expect(shell).toContain("v2-burn");
    expect(shell).toContain("v2-gates");
    // The autonomy block was extracted into its own component when the switch became real, so
    // the shell MOUNTS it rather than containing the testid. Asserting on the string was a
    // test about where code lives, not about what the chrome shows.
    expect(shell).toContain("<AutonomyControl");
    expect(shell).toMatch(/initial=\{overview\.autonomy\}/);
  });

  it("passes an actor to the autonomy control", () => {
    // The one control that reduces human oversight must never be able to act anonymously.
    expect(shell).toMatch(/actor="[a-z_]+"/);
  });

  it("only colours the gate count when a gate is waiting", () => {
    // A badge that is always amber stops being read as a signal.
    expect(shell).toContain("data-active={gates > 0}");
  });
});

describe("the overview feed", () => {
  const lib = stripComments(read("lib/v2/overview.ts"));

  it("is one call, not many", () => {
    const fetches = [...lib.matchAll(/await fetch\(/g)].length;
    expect(fetches).toBe(1);
  });

  it("never invents numbers when the API is unreachable", () => {
    // Zeros that look like facts are how an outage reads as an empty company.
    expect(lib).toContain("apiReachable: false");
    expect(lib).toContain("blockedReason");
  });
});

describe("empty states say which emptiness this is", () => {
  const projects = read("components/v2/ProjectsScreen.tsx");
  it.each([
    ["v2-projects-unreachable", "API down"],
    ["v2-projects-none", "company has no projects"],
    ["v2-projects-filtered-out", "filter matches nothing"],
  ])("distinguishes %s (%s)", (testid) => {
    expect(projects).toContain(testid);
  });
});
