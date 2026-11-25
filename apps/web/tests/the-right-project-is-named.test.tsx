// @vitest-environment happy-dom
/**
 * The console names the project you are on, and a link to a place carries no stale instruction
 * (stranger test r2, 2026-11-17):
 *   · the new-app sheet's "Local repo" dot named the OPEN project's repo (local_hello-world/…);
 *   · after creating a project the URL kept project_id=hello-world, and Setup's
 *     `focus=repository&open=add` rode along to Approvals and Projects;
 *   · Setup › Repos said "Hello World keeps its code here" with the to-do app selected;
 *   · the breadcrumb said "Default project", which the Projects list never shows.
 */
import { describe, expect, it } from "vitest";

import { moveToArea } from "../app/url-intent";
import { projectPageHref } from "../lib/start-project";
import { newAppDots, type Dot } from "../lib/add-project";
import { crumbProjectShown } from "../app/shell-crumbs";
import { localCodeWords } from "../app/setup/repository-row";

describe("the right project is named", () => {
  it("a new app on this machine gets its own repository, never the open project's", () => {
    const dots: Dot[] = [
      { key: "model", label: "Model", ok: false, detail: "no model", href: "/?area=setup" },
      { key: "repository", label: "Local repo", ok: true, detail: "keeps its code locally in local_hello-world/hello-world", href: null },
    ];
    const local = newAppDots(dots, "local").find((d) => d.key === "repository")!;
    expect(local.detail).not.toContain("hello-world");
    expect(local.detail).toContain("its own repository on this machine");
    expect(newAppDots(dots, "github_new").find((d) => d.key === "repository")!.label).toBe("GitHub");
    expect(newAppDots(dots, "local")[0]).toBe(dots[0]);
  });

  it("a new project's page names the new project and drops the last page's instructions", () => {
    window.history.replaceState(null, "", "/?org=o1&project_id=hello-world&area=setup&focus=repository&open=add");
    const href = new URLSearchParams(projectPageHref("todo", "boss-dogfood").replace(/^\/\?/, ""));
    expect(href.get("project_id")).toBe("todo");
    expect(href.get("area")).toBe("project");
    expect(href.get("org")).toBe("o1");
    expect(href.has("focus")).toBe(false);
    expect(href.has("open")).toBe(false);
  });

  it("moving to another area drops focus and open; staying keeps them", () => {
    const away = moveToArea(new URLSearchParams("area=setup&focus=repository&open=add&org=o1"), "decisions");
    expect(Object.fromEntries(away)).toEqual({ area: "decisions", org: "o1" });
    const stay = moveToArea(new URLSearchParams("area=setup&focus=repository&open=add"), "setup");
    expect(stay.get("focus")).toBe("repository");
  });

  it("the breadcrumb leaves out the Default project while the list does", () => {
    expect(crumbProjectShown("boss-dogfood", "boss-dogfood", [{ id: "hello-world" }])).toBe(false);
    expect(crumbProjectShown("boss-dogfood", "boss-dogfood", [{ id: "boss-dogfood" }])).toBe(true);
    expect(crumbProjectShown("boss-dogfood", "boss-dogfood", null)).toBe(true);
    expect(crumbProjectShown("hello-world", "boss-dogfood", [])).toBe(true);
  });

  it("Setup › Repos names only the project it shows", () => {
    const hw = { project_id: "hello-world", project_name: "Hello World" };
    expect(localCodeWords(hw, "hello-world")).toMatch(/^Hello World keeps its code here\./);
    expect(localCodeWords(hw, "todo")).not.toContain("Hello World");
  });
});
