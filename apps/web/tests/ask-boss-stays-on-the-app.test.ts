import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * Ask BOSS threw the operator into a different app.
 *
 * Reported: open "[latency-probe] add a formatBytes helper" (PR #125), press Ask BOSS, and the
 * page becomes "Database Schema Drift Detector" (PRs #114/#129) — a different app, with the
 * question you were about to ask now attached to the wrong thing.
 *
 * The dock is opened by pushing `?agent=<run id>` and firing a SYNTHETIC popstate so the shell
 * notices. That event is a broadcast: every popstate listener wakes, including this file's own
 * `?run=` reader, which "only ever SETS" so as not to fight the back button. Selection is
 * component state, so clicking a card never wrote `?run=` — the one in the URL belonged to
 * whatever app had been opened before. Pressing Ask BOSS re-applied it.
 *
 * Not an app_id collision, and not the dock: a synthetic event fired for one purpose being
 * observed by a listener that assumed a real navigation.
 */
const SRC = codeOnly("app/code-tasks-ui.tsx");

const ASK = SRC.slice(SRC.indexOf("const askBossAbout"),
                      SRC.indexOf("const livePreviewFor"));

describe("opening the dock", () => {
  it("names the run the conversation is about", () => {
    expect(ASK).toContain('url.searchParams.set("agent", m.id)');
  });

  it("also puts the page on that app, so a stale ?run= cannot hijack it", () => {
    expect(ASK).toContain('url.searchParams.set("run", m.id)');
  });

  it("sets both before pushing, so one push describes one state", () => {
    // Two pushes, or a push with only half the state, leaves a window where the URL says the
    // dock is about one app and the page is on another — which is the bug.
    const agentAt = ASK.indexOf('searchParams.set("agent"');
    const runAt = ASK.indexOf('searchParams.set("run"');
    const pushAt = ASK.indexOf("history.pushState");
    expect(agentAt).toBeLessThan(pushAt);
    expect(runAt).toBeLessThan(pushAt);
    expect(ASK.split("history.pushState").length - 1).toBe(1);
  });

  it("still fires the synthetic popstate the shell listens for", () => {
    expect(ASK).toContain('new PopStateEvent("popstate")');
  });
});

describe("the ?run= reader it has to coexist with", () => {
  const READER = SRC.slice(SRC.indexOf("const [selected, setSelected]"),
                           SRC.indexOf("const [showNew, setShowNew]"));

  it("still refuses to clear on an absent param", () => {
    // That guard is correct and stays: clearing would snap the operator out of a run they
    // opened by clicking a card. The fix is to stop the URL disagreeing with the page, not to
    // weaken the reader.
    expect(READER).toContain("if (run) setSelected(run)");
  });

  it("reads on popstate, which is why a broadcast could reach it", () => {
    expect(READER).toContain('window.addEventListener("popstate", read)');
  });
});
