import { describe, expect, it } from "vitest";

import { mapActivityLog } from "../lib/activity-log";
import { LOG_FORWARD } from "../lib/activity-proxy";
import { apiParams, readLogParams } from "../lib/log-url";
import { LEVELS, firstLine } from "../app/activity/log-model";
import { codeOnly } from "./_helpers/source";

/**
 * The Activity log, as a log viewer rather than a table of rows that happen to be logs.
 *
 * A build runs for forty to ninety minutes and its longest phase used to be one row. These pin
 * the parts of the viewer that make that phase legible, and the parts that stop the transcript
 * from burying everything else.
 */
const ROWS = codeOnly("app/activity/log-rows.tsx");
const DRAWER = codeOnly("app/activity/log-drawer.tsx");
const SHELL = codeOnly("app/ConsoleShell.tsx");
const EDITION = codeOnly("lib/edition.ts");

describe("it reaches every stream and every filter", () => {
  it("the route handler forwards every filter the API understands", () => {
    for (const p of ["level", "exclude_level", "hide_verbose", "source", "exclude_source", "run_id", "workflow_run_id",
                     "step_id", "agent", "exclude_agent", "service", "act", "subject", "q", "kind", "status", "actor",
                     "entity_type", "since", "until", "range", "cursor", "after", "summary", "total"]) {
      expect(LOG_FORWARD as readonly string[]).toContain(p);
    }
  });
});

describe("levels make the default view readable", () => {
  it("has the four levels, loudest first", () => {
    expect(LEVELS.map((l) => l.key)).toEqual(["error", "warn", "info", "verbose"]);
  });

  it("hides verbose by default, and shows it when asked or when a level is chosen", () => {
    expect(apiParams(readLogParams("")).hide_verbose).toBe("1");
    expect(apiParams(readLogParams("?verbose=1")).hide_verbose).toBeUndefined();
    expect(apiParams(readLogParams("?level=verbose")).hide_verbose).toBeUndefined();
  });

  it("shows the level as a WORD beside its glyph, never colour alone", () => {
    expect(ROWS).toContain("<span>{LEVEL_LABEL[item.level] ?? item.level}</span>");
  });

  it("defaults a level-less row to info rather than dropping it", () => {
    const log = mapActivityLog({ items: [{ id: "a", source: "event", created_at: "2026-10-04T10:00:00Z" }] });
    expect(log.items[0].level).toBe("info");
  });
});

describe("a turn can be read", () => {
  it("shows the first line of a body after the title", () => {
    expect(firstLine("\n  npm ERR! code 1\nmore")).toBe("  npm ERR! code 1");
    expect(firstLine("x".repeat(200)).length).toBe(141);
  });

  it("opens in a drawer with the body as output, and says when it was clipped", () => {
    expect(DRAWER).toContain('<pre className="lx-text" data-user-text data-testid="lx-body">');
    expect(DRAWER).toContain("Clipped when stored, ");
    expect(DRAWER).toContain('title="A clipped line is not a short one"');
  });
});

describe("scoping comes from the URL", () => {
  it("reads the run and the workflow run, and shows them as tokens", () => {
    const s = readLogParams("?run=crun_1&wfr=wfr_2");
    expect(apiParams(s)).toMatchObject({ run_id: "crun_1", workflow_run_id: "wfr_2" });
  });
});

describe("where it lives", () => {
  it("sits at the top level, beside Observability rather than under it", () => {
    const monitoring = SHELL.slice(SHELL.indexOf('id: "monitoring"'));
    expect(monitoring.slice(0, monitoring.indexOf("},"))).not.toContain('parent:');
    const activity = SHELL.slice(SHELL.indexOf('id: "activity"'));
    expect(activity.slice(0, activity.indexOf("},"))).not.toContain('parent:');
    expect(SHELL).toContain('id: "activity", label: "Activity log", sub: "Every line BOSS wrote, searchable"');
  });

  it("the section is a disclosure, not a destination, and never hides the page you are on", () => {
    const parent = SHELL.slice(SHELL.indexOf("bx-nav-parent"));
    expect(parent).toContain("aria-expanded={open}");
    expect(SHELL).toContain("open || holdsActive ?");
  });

  it("is visible to self-hosted operators", () => {
    const set = EDITION.slice(EDITION.indexOf("V1_COMMUNITY_PRIMARY_NAV = new Set("));
    expect(set.slice(0, set.indexOf("]);"))).toContain('"activity"');
  });
});
