/**
 * DELETE PROJECT's dialog rules (app/project-delete.tsx): the Delete button stays off with its
 * reason until the name matches and, with a live production app, until the tick; after the
 * delete the console lands on Projects in the same org with no project named.
 */
import { describe, expect, it } from "vitest";

import { deleteBlocked, projectsHref } from "../app/project-delete";
import type { DeletePreview } from "../app/project-delete-actions";

const preview = (over: Partial<DeletePreview> = {}): DeletePreview => ({
  id: "hd-1", name: "HelpDesk AI", slug: "hd-1", home: false, refused: null, counts: [], total: 0,
  staging_apps: [], production_apps: [], previews_running: 0, graph: { nodes: 0, edges: 0 },
  keeps: [], export: { available: false, note: "There is no project export yet." }, ...over,
});

describe("Delete project", () => {
  it("keeps the button off with its reason until the name matches", () => {
    expect(deleteBlocked(null, "", false)).toMatch(/Reading/);
    expect(deleteBlocked(preview(), "", false)).toBe("Type HelpDesk AI to delete it.");
    expect(deleteBlocked(preview(), "HelpDesk", false)).toBe("The name doesn't match. Type HelpDesk AI exactly.");
    expect(deleteBlocked(preview(), "HelpDesk AI", false)).toBe("");
    expect(deleteBlocked(preview(), "  HelpDesk AI ", false)).toBe("");
  });

  it("asks for the production tick, and says why the home project can't go", () => {
    const prod = preview({ production_apps: [{ target_id: "t", app_name: "hd", environment: "production", host: "h", production: true }] });
    expect(deleteBlocked(prod, "HelpDesk AI", false)).toMatch(/^Retire its production first/);
    expect(deleteBlocked(prod, "HelpDesk AI", true)).toBe("");
    expect(deleteBlocked(preview({ refused: "It is the home project." }), "HelpDesk AI", false)).toBe("It is the home project.");
  });

  it("lands on Projects in the same org, naming no project", () => {
    expect(projectsHref("?org=org_boss&project_id=hd-1&area=project&tab=settings")).toBe("/?org=org_boss&area=projects");
    expect(projectsHref("")).toBe("/?area=projects");
  });
});
