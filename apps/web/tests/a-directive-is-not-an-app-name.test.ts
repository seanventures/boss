import { describe, expect, it } from "vitest";

import { appIdentity, identityKey } from "../app/app-identity";
import { codeOnly } from "./_helpers/source";

/**
 * BOSS, on the Deploy step:
 *
 *   "'Lead-Triage Inbox' has no 'staging' deployment target of its own... The 5 'staging'
 *    targets on repo 'boss-sandbox' belong to other apps (Build a lead triage inbox.Who it is
 *    for: a small team drowning in in…, Change Calendar, …)"
 *
 * The target for that app EXISTED. It is the first one in its own list. It had been created
 * with the whole directive as its `app_name`:
 *
 *   deptgt_68bc4259…  "Build a lead triage inbox.Who it is for: a small team drowning in in…"
 *                     staging · active · 2.29.2.202:3014 · created_by executive_console
 *
 * `executive_console` is the Set-target form on that very step, whose app-name field defaulted
 * to the run's raw title. One click, and the app had a target under a name that target
 * resolution — which matches on app identity — could never find. So the deploy refused, and
 * listed the app's own target among the ones belonging to somebody else.
 */
const RUN = {
  title: "Build a lead triage inbox.\nWho it is for: a small team drowning in inbound leads",
};

describe("the name the form sends", () => {
  it("is the app, not the brief", () => {
    const name = appIdentity(RUN).name;
    expect(name).not.toContain("Who it is for");
    expect(identityKey(name)).toBe(identityKey("Lead-Triage Inbox"));
  });

  it("the form uses the console's own namer", () => {
    const UI = codeOnly("app/delivery-workflows-ui.tsx");
    expect(UI).toContain("const appName = appIdentity(runInput as AppRun).name;");
    expect(UI).not.toContain('const appName = typeof runInput.title === "string" ? runInput.title : ""');
  });

  it("a name that is already a name is left alone", () => {
    expect(appIdentity({ title: "Change Calendar" }).name).toBe("Change Calendar");
    expect(appIdentity({ title: "Meeting cost meter" }).name).toBe("Meeting cost meter");
  });
});

describe("and the app can find its own target again", () => {
  it("the damaged name and the real one are the same app once named", () => {
    // This is what lets pressing Set target REPAIR the row instead of creating a second target
    // on a second port and deploying the app twice.
    expect(identityKey(appIdentity({ title: RUN.title }).name))
      .toBe(identityKey("Lead-Triage Inbox"));
  });

  it("but two genuinely different apps still are not", () => {
    expect(identityKey(appIdentity({ title: "Change Calendar" }).name))
      .not.toBe(identityKey("Lead-Triage Inbox"));
  });
});
