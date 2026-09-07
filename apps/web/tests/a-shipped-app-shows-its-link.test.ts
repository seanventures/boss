import { describe, expect, it } from "vitest";

import { appIdentity, identityKey } from "../app/app-identity";
import { codeOnly } from "./_helpers/source";

/**
 * "The app is deployed but I don't see the link in the Deploy option in Workflow, it does appear
 * for some app like Change Calendar but other apps like Lead triage box it is not displayed."
 *
 * The step said everything it needed to:
 *
 *   status=succeeded · healthy=true   APP NAME Lead-Triage Inbox
 *   TARGET ID deptgt_33af7f727c424ede8da1   DEPLOYMENT RUN ID deprun_3335d79ba0fe4a9b9c01
 *
 * and the target holds `domains: ["2.29.2.202:3014"]`. Both lookups behind the link compared
 * against the run's raw TITLE — the operator's whole directive:
 *
 *   targets.find((t) => t.app_name === runInput.title)
 *   fetch(`/api/run-deployments?...&app_names=${runInput.title}`)
 *
 * So the one app whose title happens to BE its name — "Change Calendar" — got its link, and
 * every app whose title is a brief got nothing. (The stored name also carries a U+2010 hyphen,
 * "Lead‐Triage Inbox", which string equality would have missed even against the right name.)
 */
const RUN = {
  title: "Build a lead triage inbox.\nWho it is for: a small team drowning in inbound leads",
};

describe("finding the app's target", () => {
  it("the stored name and the run are the same app, by identity", () => {
    // U+2010, as actually stored on deptgt_33af7f.
    expect(identityKey("Lead‐Triage Inbox"))
      .toBe(identityKey(appIdentity(RUN).name));
  });

  it("which exact string comparison would have missed", () => {
    expect("Lead‐Triage Inbox").not.toBe(RUN.title);
    expect("Lead‐Triage Inbox").not.toBe(appIdentity(RUN).name);
  });

  it("an app whose title IS its name keeps working", () => {
    // The case that masked this: it was never that Change Calendar was special, only that its
    // title needed no interpretation.
    expect(identityKey("Change Calendar"))
      .toBe(identityKey(appIdentity({ title: "Change Calendar" }).name));
  });

  it("and two different apps still do not match", () => {
    expect(identityKey(appIdentity(RUN).name)).not.toBe(identityKey("Change Calendar"));
  });
});

describe("what the view now asks", () => {
  const UI = codeOnly("app/delivery-workflows-ui.tsx");

  it("the step's own target id first — it records what this deploy actually used", () => {
    expect(UI).toContain("const byStep = targets.find((t) => t.id === fromStep);");
  });

  it("then the run's, then the app by identity", () => {
    expect(UI).toContain("identityKey(String(t.app_name ?? \"\")) === wanted");
  });

  it("and deployments are looked up by the app's name, not the brief", () => {
    expect(UI).toContain("const name = appIdentity(runInput as AppRun).name;");
  });

  it("no lookup compares against the raw title any more", () => {
    expect(UI).not.toContain("targets.find((t) => t.app_name === name)");
  });
});
