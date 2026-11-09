// @vitest-environment happy-dom
/**
 * Factory › Repositories: a non-default repository gets a "Make default" action (FR1.3),
 * backed by the existing Setup default route — no new backend route. Clicking it reloads the
 * table with the new repository marked "· default".
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { ProjectRepositories } from "../app/multirepo/project-repos";
import {
  $$, click, mountAs, projectReposFixture, stubFetch, tid, unmount, type Call,
} from "./_helpers/multirepo-fixtures";

afterEach(unmount);

describe("Factory › Repositories · Make default", () => {
  it("P1: a non-default repo's Make default action moves the default marker", async () => {
    const view = projectReposFixture();
    const madeDefault = {
      ...view,
      items: view.items.map((r) => ({ ...r, is_default: r.binding_id === "bnd_contracts" })),
    };
    const calls: Call[] = [];
    stubFetch((c) => {
      calls.push(c);
      if (c.method === "GET" && c.path === "/v1/projects/p1/repos") return { body: view };
      if (c.method === "POST" && c.path.includes("/default")) return { body: madeDefault };
      return undefined;
    });

    await mountAs(<ProjectRepositories projectId="p1" />);
    expect($$('[data-testid^="mr-prj-row-"]')).toHaveLength(4);

    await click(tid("mr-prj-default-bnd_contracts"));

    const defaultCall = calls.find((c) => c.method === "POST" && c.path.includes("/default"));
    expect(defaultCall?.path).toContain("bnd_contracts");
    expect(tid("mr-prj-row-bnd_contracts")?.textContent).toContain("default");
  });
});
