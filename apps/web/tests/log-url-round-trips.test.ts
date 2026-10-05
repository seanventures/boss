/**
 * The Log's URL is its state, and every link into it is built ONE way (activity-govern §4.1).
 *
 * The bug this closes, as a class: Agent Studio's river linked with `workflow_run_id=`, a name
 * the Log never read, so "Open in Activity log" landed on the whole project. Every token now
 * round-trips through the URL; the legacy spellings are read and rewritten; and no file in the
 * console may build an Activity-log link by hand — they all call `logHref()`.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

import {
  CANT_EXCLUDE, VIEWS, apiParams, logHref, parseToken, readLogParams, removeToken, toggleToken, viewOf,
  writeLogParams, type LogState,
} from "../lib/log-url";

const ROOT = process.cwd();

describe("every token survives the URL", () => {
  it("writes and reads back the same state", () => {
    const state: LogState = {
      tokens: [{ key: "level", value: "error" }, { key: "level", value: "verbose", negate: true },
               { key: "src", value: "llm_call" }, { key: "agent", value: "software_engineer" },
               { key: "wfr", value: "wfr_1" }, { key: "step", value: "plan" }, { key: "kind", value: "budget.refused" },
               { key: "svc", value: "worker" }, { key: "subject", value: "dpt_1" }],
      q: "npm ERR", range: "7d", from: null, to: null, row: "evt_9", group: "run", showVerbose: true,
      names: { step: "Plan it", run: "Lead Triage" },
    };
    const search = writeLogParams(state, "?org=org_boss&area=activity");
    const params = new URLSearchParams(search);
    expect(params.get("org")).toBe("org_boss");
    expect(params.get("level")).toBe("error,-verbose");
    expect(params.get("row")).toBe("evt_9");
    expect(readLogParams(search)).toEqual(state);
  });

  it("a custom range is from/to; a relative one is range (24h is the default and not written)", () => {
    expect(readLogParams("?area=activity").range).toBe("24h");
    const custom = readLogParams("?from=2026-10-04T10:00:00Z&to=2026-10-04T11:00:00Z");
    expect(custom.range).toBeNull();
    expect(custom.from).toBe("2026-10-04T10:00:00Z");
    expect(new URLSearchParams(writeLogParams({ ...custom, range: "24h", from: null, to: null }, "")).has("range")).toBe(false);
  });

  it("reads the legacy and sibling-contract spellings and rewrites them", () => {
    const legacy = readLogParams("?area=activity&workflow_run_id=wfr_9&run_id=crun_3&step_id=build");
    expect(legacy.tokens).toEqual(expect.arrayContaining([{ key: "run", value: "crun_3" },
      { key: "wfr", value: "wfr_9" }, { key: "step", value: "build" }]));
    expect(legacy.tokens).toHaveLength(3);
    const rewritten = new URLSearchParams(writeLogParams(legacy, "?area=activity&workflow_run_id=wfr_9&run_id=crun_3"));
    expect(rewritten.get("wfr")).toBe("wfr_9");
    expect(rewritten.has("workflow_run_id")).toBe(false);
    expect(rewritten.has("run_id")).toBe(false);
    // Infra & Observability link here as ?area=activity&source=<s>&subject=<id>&since=<iso>.
    const infra = readLogParams("?area=activity&source=deployment&subject=dpt_7&since=2026-10-01T00:00:00Z");
    expect(infra.tokens).toEqual(expect.arrayContaining([{ key: "src", value: "deployment" },
                                                          { key: "subject", value: "dpt_7" }]));
    expect(infra.from).toBe("2026-10-01T00:00:00Z");
    expect(apiParams(infra)).toMatchObject({ source: "deployment", subject: "dpt_7", since: "2026-10-01T00:00:00Z" });
  });

  it("a view link expands into its tokens", () => {
    expect(viewOf(readLogParams("?area=activity&view=refusals").tokens)?.key).toBe("refusals");
    expect(viewOf(readLogParams("?view=govern-changes").tokens)?.label).toBe("Govern changes");
    expect(VIEWS.map((v) => v.label)).toEqual(["All", "Errors", "Model calls", "Deploys", "Govern changes", "Refusals"]);
  });
});

describe("the API is asked exactly what the tokens say", () => {
  it("maps tokens to the API's filters, verbose hidden unless asked", () => {
    const s = readLogParams("?level=error,-verbose&src=llm_call&agent=-qa_engineer&wfr=wfr_1&step=plan&svc=api&q=boom");
    expect(apiParams(s)).toEqual({
      level: "error", exclude_level: "verbose", source: "llm_call", exclude_agent: "qa_engineer",
      workflow_run_id: "wfr_1", step_id: "plan", service: "api", q: "boom", range: "24h",
    });
    expect(apiParams(readLogParams("")).hide_verbose).toBe("1");
    expect(apiParams(readLogParams("?verbose=1")).hide_verbose).toBeUndefined();
    // A step means nothing without its run.
    expect(apiParams(readLogParams("?step=plan")).step_id).toBeUndefined();
  });

  it("parses typed tokens, refuses an exclusion the server cannot do, turns a pasted id into its scope", () => {
    expect(parseToken("level:error")).toEqual({ token: { key: "level", value: "error" } });
    expect(parseToken("-level:verbose")).toEqual({ token: { key: "level", value: "verbose", negate: true } });
    expect(parseToken("-actor:ceo")).toEqual({ error: CANT_EXCLUDE });
    expect(parseToken('run:"crun_x"')).toEqual({ token: { key: "run", value: "crun_x" } });
    expect(parseToken("crun_8f2a")).toEqual({ token: { key: "run", value: "crun_8f2a" } });
    expect(parseToken("wfr_12")).toEqual({ token: { key: "wfr", value: "wfr_12" } });
    expect(parseToken("act_01")).toEqual({ token: { key: "act", value: "act_01" } });
    expect(parseToken("just words")).toBeNull();
  });

  it("removing a workflow run takes its step with it; a single-value key replaces", () => {
    const tokens = readLogParams("?wfr=wfr_1&step=plan&level=error").tokens;
    expect(removeToken(tokens, { key: "wfr", value: "wfr_1" }).map((t) => t.key)).toEqual(["level"]);
    expect(toggleToken([{ key: "run", value: "crun_1" }], { key: "run", value: "crun_2" })).toEqual([{ key: "run", value: "crun_2" }]);
    expect(toggleToken([{ key: "level", value: "error" }], { key: "level", value: "warn" }).length).toBe(2);
  });
});

describe("logHref is the only way into the Log", () => {
  it("builds scoped links with the Log's own names, keeping the org", () => {
    const href = logHref({ wfr: "wfr_1", step: "plan", step_name: "Plan it", run_name: "Lead" }, "?org=o1&area=x&project=p");
    const q = new URLSearchParams(href.split("?")[1]);
    expect(q.get("area")).toBe("activity");
    expect(q.get("org")).toBe("o1");
    expect(q.get("wfr")).toBe("wfr_1");
    expect(q.get("step")).toBe("plan");
    expect(q.get("step_name")).toBe("Plan it");
    expect(q.has("workflow_run_id")).toBe(false);
    expect(new URLSearchParams(logHref({ act: "act_9" }, "").split("?")[1]).get("act")).toBe("act_9");
    // A step without its run is not a scope.
    expect(new URLSearchParams(logHref({ step: "plan" }, "").split("?")[1]).has("step")).toBe(false);
  });

  it("no console file builds an Activity-log link by hand", () => {
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (name === "node_modules" || name === "tests") continue;
        if (statSync(path).isDirectory()) { walk(path); continue; }
        if (!/\.(tsx?|ts)$/.test(name) || path.endsWith("log-url.ts")) continue;
        const text = readFileSync(path, "utf8");
        if (/area:\s*"activity"/.test(text) || /[?&]area=activity&/.test(text.replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, ""))) {
          offenders.push(relative(ROOT, path));
        }
      }
    };
    walk(join(ROOT, "app"));
    walk(join(ROOT, "lib"));
    walk(join(ROOT, "components"));
    expect(offenders).toEqual([]);
  });
});
