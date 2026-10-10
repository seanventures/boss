import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { labRunHref } from "../components/v2/templates/template-sheet";
import { labHref } from "../app/factory/lab-links";
import { ProjectAway } from "../app/project-away";
import { codeOnly } from "./_helpers/source";

/**
 * N2 (tester round 2): an insecure template is built in its own LAB project, and its run was in
 * no Workflows or Factory anyone could open — the console only ever showed the org's own project.
 */
(globalThis as Record<string, unknown>).React = React;

describe("a lab project's run can be opened", () => {
  it("an apply that made a lab opens the run in the lab project's Workflows", () => {
    const href = new URLSearchParams(labRunHref("proj_lab1", "wfr_1").slice(1));
    expect(href.get("project_id")).toBe("proj_lab1");
    expect(href.get("area")).toBe("delivery_workflows");
    expect(href.get("wfr")).toBe("wfr_1");
  });

  it("the sheet goes there instead of opening the run here", () => {
    const src = codeOnly("components/v2/templates/template-sheet.tsx");
    expect(src).toMatch(/started\?\.lab\?\.project_id[\s\S]{0,300}labRunHref\(/);
  });

  it("the source project's Factory links to each lab's own Factory", () => {
    const href = new URLSearchParams(labHref("proj_lab1").slice(1));
    expect(href.get("project_id")).toBe("proj_lab1");
    expect(href.get("area")).toBe("code_tasks");
    expect(codeOnly("app/factory/apps-tab.tsx")).toContain("<LabLinks projectId={projectId} />");
  });

  it("the console opens another project of the org by link, and only of this org", () => {
    const page = codeOnly("app/page.tsx");
    expect(page).toContain("p.id === askedProjectId && p.orgId === selectedOrg.org_id");
    expect(page).toContain("const activeProjectId = otherProject ? otherProject.id : selectedOrg.project_id;");
  });

  it("says which project is open and the way back", () => {
    const html = renderToStaticMarkup(<ProjectAway name="Bank lab (insecure by design)" homeName="BOSS" lab />);
    expect(html).toContain("Lab project: ");
    expect(html).toContain("Back to BOSS");
    expect(html).toContain("never deployed to a host");
  });
});
