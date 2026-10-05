import { describe, expect, it } from "vitest";

import { monitoringAsks } from "../app/observability/obs-util";
import { codeOnly } from "./_helpers/source";

/**
 * "I want you to expose the BOSS Agent also in Monitoring section, I want to propose few
 * changes I want to see if BOSS Agent can help."
 *
 * Monitoring was the one operating screen with no way to ask. It is also the screen where the
 * question is least likely to be "what happened" and most likely to be "what should we be
 * watching instead" — so the starters cover both, and they are built from the fleet ON SCREEN
 * rather than from a fixed list: naming the app that is actually down beats "ask about an app",
 * and the first question then carries facts BOSS would otherwise spend three lookups
 * rediscovering.
 *
 * The Monitoring screen (monitoring-ui.tsx) retired into Observability; its starters moved to
 * app/observability/obs-util.ts (`monitoringAsks`, built from the Pulse rows on screen) and its
 * door is Observability's header `Ask BOSS` (pulse.tsx ObsAskBoss).
 */
const target = (over: Record<string, unknown> = {}) => ({
  target_id: "deptgt_1", app_name: "Change Calendar", environment: "staging", status: "up", ...over,
});
const questions = (targets: ReturnType<typeof target>[]) => monitoringAsks(targets).map((a) => a.question);

describe("the questions it offers", () => {
  it("names the app that is actually broken, and asks ABOUT its target", () => {
    // The error and the address used to be pasted into the question. They travel as the
    // target itself now — the server resolves it into its whole record (boss-1: a pasted
    // paraphrase was answered about something else).
    const [first] = monitoringAsks([
      target(), target({ target_id: "deptgt_9", app_name: "Lead-Triage Inbox", status: "down" }),
    ]);
    expect(first.question).toContain("Lead-Triage Inbox");
    expect(first.question).toContain("down");
    expect(first.subject).toEqual({ kind: "deployment", id: "deptgt_9" });
  });

  it("lists them when several are unwell", () => {
    const [first] = questions([
      target({ app_name: "A", status: "down" }),
      target({ app_name: "B", status: "degraded" }),
    ]);
    expect(first).toContain("A, B");
  });

  it("distrusts an all-green board rather than congratulating it", () => {
    // Three apps read "up · 100% uptime · 0ms" for two days with nothing ever probed. A green
    // light not wired to anything is the same reassurance a working system gives.
    const [first] = questions([target(), target({ app_name: "Other" })]);
    expect(first).toContain("Check the probes are really running");
  });

  it("separates 'nothing deployed' from 'nothing watching'", () => {
    const qs = questions([target({ status: "unknown", app_name: "Quiet" })]);
    expect(qs.some((q) => q.includes("Quiet") && q.includes("no health signal"))).toBe(true);
  });

  it("always offers the one an operator arrives with an idea for", () => {
    for (const ts of [[], [target()]]) {
      expect(questions(ts).some((q) => q.includes("change what we monitor"))).toBe(true);
    }
  });

  it("says something even with no fleet at all", () => {
    expect(questions([]).length).toBeGreaterThan(0);
  });
});

describe("how it opens", () => {
  const PULSE = codeOnly("app/observability/pulse.tsx");
  const HOME = codeOnly("app/observability/obs-home.tsx");
  const askBoss = PULSE.slice(PULSE.indexOf("export function ObsAskBoss"));

  it("through the console's own dock, not a fourth chat surface", () => {
    expect(askBoss).toContain("openDockAbout({ subject: a.subject, question: a.question, role: null })");
  });

  it("it is present whether or not anything is set up", () => {
    // "Nothing is set up" is exactly when an operator wants to ask what setting it up involves.
    // It sits in the area's header, beside Find — not inside any tab's empty or loaded state.
    expect(HOME.split("<ObsAskBoss").length - 1).toBe(1);
    const head = HOME.slice(HOME.indexOf("<AreaHead"), HOME.indexOf("</AreaHead>"));
    expect(head).toContain("<ObsAskBoss {...props} />");
  });

  it("and a starter only asks: nothing is reconfigured until you act in the dock", () => {
    // The old card said so in a sentence; the menu now holds to it in code — a starter
    // prefills the dock and writes nothing.
    expect(askBoss).not.toMatch(/obsWrite|fetch\(|Action\(/);
  });
});
