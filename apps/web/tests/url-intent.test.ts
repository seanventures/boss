import { describe, expect, it } from "vitest";

import {
  readNewProjectIntent,
  readRunParam,
  searchForNewProject,
  searchForProject,
} from "../app/url-intent";

/**
 * The reported bug, as a sequence of URLs.
 *
 * Press "New project" → `?new=1`. Then click any project in the top strip: the strip copies the
 * current URL, adds `?run=<id>`, and dispatches popstate. `new=1` is still there, so the form
 * reader fires AGAIN — opening the form and clearing the selection. Every project click landed
 * on an empty new-project dialog, and the Factory list was the only page you could reach.
 *
 * Two source-grep tests passed throughout, including one of mine that asserted the reader must
 * never close the form. That test encoded the bug. These follow the actual sequence instead.
 */
describe("clicking a project after pressing New project", () => {
  it("opens the project, not the form", () => {
    // 1. Press "New project".
    let search = searchForNewProject("?area=monitoring");
    expect(readNewProjectIntent(search).open).toBe(true);

    // 2. The area consumes the command, as the reader now does.
    search = readNewProjectIntent(search).nextSearch;

    // 3. Click a project in the strip.
    search = searchForProject(search, "crun_abc");

    // 4. The form must NOT reopen, and the project must be the one selected.
    expect(readNewProjectIntent(search).open).toBe(false);
    expect(readRunParam(search)).toBe("crun_abc");
  });

  it("survives a project click even if the command was never consumed", () => {
    // Belt and braces: navigating TO a project drops any pending command, so the bug cannot
    // come back through a reader that forgets to consume.
    const search = searchForProject("?area=code_tasks&new=1", "crun_xyz");
    expect(readNewProjectIntent(search).open).toBe(false);
    expect(readRunParam(search)).toBe("crun_xyz");
  });

  it("does not re-deliver the command on any later navigation", () => {
    let search = searchForNewProject("");
    search = readNewProjectIntent(search).nextSearch;
    // Three unrelated navigations, each copying the query string as the console does.
    for (const area of ["monitoring", "decisions", "code_tasks"]) {
      const params = new URLSearchParams(search);
      params.set("area", area);
      search = `?${params.toString()}`;
      expect(readNewProjectIntent(search).open).toBe(false);
    }
  });
});

describe("reading the one-shot command", () => {
  it("opens the form when asked", () => {
    expect(readNewProjectIntent("?area=code_tasks&new=1").open).toBe(true);
  });

  it("consumes only the command, keeping where you are", () => {
    const { nextSearch } = readNewProjectIntent("?area=code_tasks&new=1");
    expect(nextSearch).toBe("?area=code_tasks");
  });

  it("leaves the URL untouched when there is nothing to consume", () => {
    // So the caller can skip the history write entirely rather than pushing a no-op entry on
    // every navigation.
    const search = "?area=code_tasks&run=crun_1";
    expect(readNewProjectIntent(search).nextSearch).toBe(search);
  });

  it("handles the command being the only parameter", () => {
    expect(readNewProjectIntent("?new=1")).toEqual({ open: true, nextSearch: "" });
  });

  it("ignores any value that is not the command", () => {
    // `?new=0` or a stray `?new=` is not a request to open anything.
    for (const q of ["?new=0", "?new=", "?news=1", ""]) {
      expect(readNewProjectIntent(q).open).toBe(false);
    }
  });
});

describe("starting a new project", () => {
  it("clears what you were looking at", () => {
    // A stale `?run=` survived the navigation and re-opened that project's detail on top of the
    // form, so "New project" landed on somebody else's app.
    const search = searchForNewProject("?area=code_tasks&run=crun_old&agent=crun_old");
    expect(readRunParam(search)).toBeNull();
    expect(new URLSearchParams(search).get("agent")).toBeNull();
    expect(readNewProjectIntent(search).open).toBe(true);
  });

  it("goes to the area that WATCHES the work", () => {
    // This opened the Factory form, because Factory was where projects were listed. The press
    // now starts a workflow RUN — panel, spec, approval, build, preview, deploy — so it lands
    // on the page that shows those steps.
    expect(new URLSearchParams(searchForNewProject("?area=monitoring")).get("area"))
      .toBe("delivery_workflows");
  });
});

describe("opening a project", () => {
  it("goes to the area that shows it", () => {
    expect(new URLSearchParams(searchForProject("", "crun_1")).get("area")).toBe("code_tasks");
  });

  it("clears the run when a project has none to show", () => {
    // A project whose newest run is unknown must not inherit the previously-open one.
    expect(readRunParam(searchForProject("?run=crun_old", null))).toBeNull();
  });

  it("keeps unrelated parameters", () => {
    const search = searchForProject("?tab=runs&filter=live", "crun_2");
    expect(new URLSearchParams(search).get("tab")).toBe("runs");
    expect(new URLSearchParams(search).get("filter")).toBe("live");
  });
});

/**
 * Starting a project must LAND you where the work is.
 *
 * Creating a project left the operator on the form that started it, with no sign anything had
 * happened; the run they wanted to watch was in another area they had to know to open. And the
 * Workflow page only polled while something was already live — so anyone holding it open when
 * the sole run was blocked saw a frozen list and no sign of the project they had just started.
 */
describe("where you land after starting a project", () => {
  it("goes to the Workflow area", async () => {
    const { workflowHref } = await import("../app/actions-href");
    expect(new URLSearchParams(workflowHref("wfr_1").split("?")[1]).get("area"))
      .toBe("delivery_workflows");
  });

  it("names the run so the page can open it", async () => {
    const { workflowHref } = await import("../app/actions-href");
    expect(new URLSearchParams(workflowHref("wfr_1").split("?")[1]).get("wfr")).toBe("wfr_1");
  });

  it("still goes there when the run id is not known yet", async () => {
    // Approving a PRD starts the build on a background thread, so there is no run id to carry.
    // The page polls, and a run that appears seconds later sorts to the top because it is live.
    const { workflowHref } = await import("../app/actions-href");
    const href = workflowHref(null);
    expect(new URLSearchParams(href.split("?")[1]).get("area")).toBe("delivery_workflows");
    expect(new URLSearchParams(href.split("?")[1]).get("wfr")).toBeNull();
  });
});

/**
 * "Select your organization", mid-run.
 *
 * The console gates on `?org=`. Every href built from scratch — `?area=code_tasks&run=…` —
 * dropped it, so the workflow's own "open the project" and "activity log" links, and the
 * redirect after creating a project, threw the operator onto the organisation picker. It reads
 * as being logged out, and it happened repeatedly because several links did it.
 */
describe("a link inside the console keeps who you are", () => {
  it("carries the org", async () => {
    const { consoleHref } = await import("../app/url-intent");
    const href = consoleHref("?org=org_boss&area=delivery_workflows",
                             { area: "code_tasks", run: "crun_1" });
    const p = new URLSearchParams(href.slice(1));
    expect(p.get("org")).toBe("org_boss");
    expect(p.get("area")).toBe("code_tasks");
    expect(p.get("run")).toBe("crun_1");
  });

  it("accepts the other spelling links have shipped with", async () => {
    const { consoleHref } = await import("../app/url-intent");
    const href = consoleHref("?org_id=org_boss", { area: "meetings" });
    expect(new URLSearchParams(href.slice(1)).get("org_id")).toBe("org_boss");
  });

  it("does not carry a one-shot command along with a link to a place", async () => {
    const { consoleHref } = await import("../app/url-intent");
    const href = consoleHref("?org=org_boss&new=1", { area: "code_tasks" });
    expect(new URLSearchParams(href.slice(1)).get("new")).toBeNull();
  });

  it("drops what the destination replaces", async () => {
    // Opening the panel should not carry the previous screen's run id.
    const { consoleHref } = await import("../app/url-intent");
    const href = consoleHref("?org=org_boss&run=crun_old", { area: "meetings" });
    expect(new URLSearchParams(href.slice(1)).get("run")).toBeNull();
  });
});

describe("the redirect after starting a project keeps who you are", () => {
  it("puts the org in the destination", async () => {
    const { workflowHref } = await import("../app/actions-href");
    const p = new URLSearchParams(workflowHref("wfr_1", "org_boss").split("?")[1]);
    expect(p.get("org")).toBe("org_boss");
    expect(p.get("area")).toBe("delivery_workflows");
    expect(p.get("wfr")).toBe("wfr_1");
  });

  it("lifts the org from the page that submitted the form", async () => {
    // A server action has no URL of its own; the referer is the page the operator was on, and
    // it carries the org because the console cannot render without one.
    const { orgFromReferer } = await import("../app/actions-href");
    expect(orgFromReferer("https://boss.local.example.test/?org=org_boss&area=code_tasks"))
      .toBe("org_boss");
    expect(orgFromReferer("https://boss.local.example.test/?org_id=org_boss")).toBe("org_boss");
  });

  it("survives a missing or unparseable referer", async () => {
    // Still navigates — the org gate is recoverable; a crashed action is not.
    const { orgFromReferer, workflowHref } = await import("../app/actions-href");
    for (const bad of [null, undefined, "", "not a url"]) {
      expect(orgFromReferer(bad)).toBeNull();
    }
    expect(workflowHref("wfr_1", null)).toContain("area=delivery_workflows");
  });
});
