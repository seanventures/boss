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

  it("goes to the area that owns the form", () => {
    expect(new URLSearchParams(searchForNewProject("?area=monitoring")).get("area"))
      .toBe("code_tasks");
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
