import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { stepQuestion } from "../app/delivery-workflows-ui";
import { itemSubject } from "../app/boss-agent-home";
import {
  SUBJECT_KINDS, dockParams, parseSubjectRef, subjectRef, type DockSubject,
} from "../lib/dock-subject";
import { codeOnly } from "./_helpers/source";

/**
 * Every "Ask the <role>" / "Ask BOSS about this" door hands over WHAT it is about, typed.
 *
 * boss-1 (2026-10-02): a Preview step failed with "ImportError: attempted relative import with
 * no known parent package … lead_triage_core/approvals.py". "Ask the Qa" opened the dock with a
 * sentence built from the step — the error cut at 400 characters, mid-word, and "Check the
 * app's code and the host before answering." stapled on — scoped to the BUILD, because a run id
 * was the only thing a door could hand over. The workflow run, the step, the preview and its
 * log never travelled. The turn ran 276 seconds and answered that the app "has never been
 * deployed". The cause was in the question the whole time.
 *
 * Eleven doors each built their own URL and their own prose. Now one function opens the dock,
 * and these tests hold every door to it.
 */
const ROOTS = ["app", "components"];

function sources(dir: string): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...sources(path));
    else if (/\.(tsx|ts)$/.test(name)) out.push([path, readFileSync(path, "utf8")]);
  }
  return out;
}
const ALL = ROOTS.flatMap((r) => sources(join(process.cwd(), r)));

describe("one way into the dock", () => {
  it("no file outside the helper sets the dock's URL params itself", () => {
    const offenders = ALL
      .filter(([, src]) => /searchParams\.set\("(dock_ask|agent|dock_role)"/.test(src))
      .map(([path]) => path.replace(process.cwd() + "/", ""));
    expect(offenders, "open the dock with openDockAbout (lib/dock-subject.ts)").toEqual([]);
  });

  it("every door that offers to ask about something goes through openDockAbout", () => {
    // A button labelled "Ask the <role>" / "Ask BOSS about this …" / "Open BOSS here" in a file
    // that does not call the helper is a door built some other way.
    const doors = ALL.filter(([, src]) =>
      /(`Ask the \$\{|Ask BOSS about this|Open BOSS here|Design one with BOSS)/
        .test(src));
    const offenders = doors
      .filter(([path, src]) => !src.includes("openDockAbout(")
        // The dock itself and the chat it hosts are not doors into it.
        && !/ConsoleShell|agent-ui|AgentChat/.test(path))
      .map(([path]) => path.replace(process.cwd() + "/", ""));
    expect(offenders).toEqual([]);
    expect(doors.length).toBeGreaterThanOrEqual(5);
  });

  it("every subject kind a door names is one the engine resolves", () => {
    // The engine side reads SUBJECT_KINDS from lib/dock-subject.ts and checks each has a
    // resolver (engine/tests/test_an_ask_about_this_is_answered_from_its_record.py); this
    // side checks no door invents a kind outside the list.
    const kinds = new Set<string>(SUBJECT_KINDS);
    const used = ALL.flatMap(([, src]) =>
      [...src.matchAll(/kind:\s*"([a-z_]+)",\s*id:/g)].map((m) => m[1]));
    expect(used.length).toBeGreaterThanOrEqual(5);
    expect(used.filter((k) => !kinds.has(k))).toEqual([]);
  });
});

describe("the doors that are about a record pass that record", () => {
  const DOORS: Array<[string, string]> = [
    // The incident's own door.
    ["app/delivery-workflows-ui.tsx",
     '{ kind: "workflow_step", id: workflowRunId, step: step.step_id }'],
    ["app/boss-agent-home.tsx", "subject: itemSubject(item)"],
    ["app/monitoring-ui.tsx", '{ kind: "deployment", id: t.target_id }'],
  ];
  it.each(DOORS)("%s", (file, subject) => {
    expect(codeOnly(file)).toContain(subject);
  });

  it("the briefing's stopped step names its workflow run and step", () => {
    expect(itemSubject({ kind: "workflow_step", id: "wfs_1", run_id: "wfr_9c78",
                         step: "preview" } as never))
      .toEqual({ kind: "workflow_step", id: "wfr_9c78", step: "preview" });
  });
});

describe("the question is the operator's, not a prompt", () => {
  it("the incident's step asks the literal question and nothing else", () => {
    const q = stepQuestion({ step_id: "preview", status: "failed" }, "Preview");
    expect(q).toBe("Why did the Preview step fail, and what is the fix?");
  });

  it("no door staples on an instruction to go exploring", () => {
    for (const [path, src] of ALL) {
      expect(codeOnly(path), path).not.toContain("Check the app's code and");
      void src;
    }
  });

  it("and no door pastes a truncated error into it", () => {
    const dw = codeOnly("app/delivery-workflows-ui.tsx");
    expect(dw).not.toContain("askNote");
  });
});

describe("the subject travels as the thread's scope", () => {
  const step: DockSubject = { kind: "workflow_step", id: "wfr_9c78", step: "preview" };

  it("a step is keyed by its run and step", () => {
    expect(subjectRef(step)).toBe("workflow_step:wfr_9c78:preview");
    expect(parseSubjectRef("workflow_step:wfr_9c78:preview")).toEqual(step);
  });

  it("a build keeps the bare run id every build conversation was keyed on", () => {
    expect(subjectRef({ kind: "code_task", id: "crun_1" })).toBe("crun_1");
    expect(parseSubjectRef("crun_1")).toEqual({ kind: "code_task", id: "crun_1" });
  });

  it("a role thread is not a subject", () => {
    expect(parseSubjectRef("role:Qa")).toBeNull();
  });

  it("opening the dock sets the subject, the question and the role together", () => {
    expect(dockParams({ subject: step, question: "Why did it fail?", role: "Qa" })).toEqual({
      agent: "workflow_step:wfr_9c78:preview", dock_ask: "Why did it fail?", dock_role: "Qa",
      // Cleared, never inherited: a door that names no agent key or work must not keep the last.
      dock_role_key: null, dock_act: null,
      dock_mode: null, dock_fresh: null,
    });
  });

  it("no subject means BOSS's general dock, and a null role is CLEARED, not inherited", () => {
    const p = dockParams({ subject: null, question: "", role: null });
    expect(p.agent).toBe("general");
    expect(p.dock_role).toBeNull();
    expect(p.dock_ask).toBeNull();
  });
});
