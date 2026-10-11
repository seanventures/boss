// @vitest-environment happy-dom

import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { PluginsTab } from "../app/infra/plugins";
import { INFRA_TABS, parsePluginList, permissionWords, readInfraUrl } from "../lib/infra-view";
import { mockInfraFetch, type Route } from "./_helpers/infra-fixtures";
import { byTestId, mount, press, settle, TabHarness, type Mounted } from "./_helpers/infra-tab-render";

/**
 * Infra › Plugins: one row per plugin with its support level, source, capabilities and On/Off.
 * Turning a community plugin on first lists what it may touch; a built-in turns off in one press.
 */

const PLUGINS = {
  plugins: [
    { id: "official.postgres", version: "0.1.0", name: "PostgreSQL", source: "builtin", supportLevel: "certified",
      claimedSupportLevel: "certified", capabilities: ["database.relational"], features: ["backup"], engines: ["postgres"],
      permissions: { secrets: ["write-scoped"], network: ["declared-services"], filesystem: [], docker: true, subprocess: false, cloud: [] },
      enabled: true, usable: true, problems: [], checksum: "sha256:a" },
    { id: "community.example", version: "0.2.0", name: "Example cache", source: "community", supportLevel: "community",
      claimedSupportLevel: "certified", capabilities: ["cache"], features: [], engines: ["valkey"],
      permissions: { secrets: ["write-scoped"], network: [], filesystem: [], docker: true, subprocess: false, cloud: [] },
      enabled: false, usable: false, problems: [], checksum: "sha256:b" },
    { id: "community.broken", version: "0.0.1", name: "Broken", source: "community", supportLevel: "community",
      capabilities: ["queue"], permissions: {}, enabled: false, usable: false,
      problems: ["its files changed after it was enabled"], checksum: "sha256:c" },
  ],
};

let m: Mounted | null = null;
let restore: (() => void) | null = null;
afterEach(async () => { await m?.unmount(); m = null; restore?.(); restore = null; });

async function open() {
  const routes: Route[] = [{ match: /^\/v1\/infra\/plugins$/, reply: () => PLUGINS }];
  const f = mockInfraFetch({ routes });
  restore = f.install();
  m = await mount(<TabHarness Tab={PluginsTab} />, "?area=infra&view=plugins");
  await settle(() => !!byTestId("inf-plugins-table"), "plugins table");
  return f;
}

describe("Infra › Plugins", () => {
  it("is a tab and parses the registry", () => {
    expect(INFRA_TABS.map((t) => t.key)).toContain("plugins");
    expect(readInfraUrl("?view=plugins").view).toBe("plugins");
    const rows = parsePluginList(PLUGINS).rows;
    expect(rows).toHaveLength(3);
    expect(rows[1]).toMatchObject({ source: "community", supportLevel: "community", claimedSupportLevel: "certified", enabled: false });
    expect(rows[2].permissions.docker).toBe(false);
    expect(permissionWords(rows[0])).toEqual(["start and remove containers", "store its own secrets", "reach the services it declares"]);
    expect(parsePluginList({}).rows).toEqual([]);
  });

  it("lists support level, source and capabilities with tooltips", async () => {
    await open();
    const pg = byTestId("inf-plugin-row-official.postgres")!;
    expect(pg.textContent).toContain("PostgreSQL");
    expect(pg.textContent).toContain("Certified");
    expect(pg.textContent).toContain("Built-in");
    expect(pg.textContent).toContain("database.relational");
    expect(byTestId("inf-plugin-level", pg)!.getAttribute("title")).toMatch(/tested in CI/);
    const ex = byTestId("inf-plugin-row-community.example")!;
    expect(byTestId("inf-plugin-level", ex)!.getAttribute("title")).toMatch(/claims Certified/);
    const broken = byTestId("inf-plugin-row-community.broken")!;
    expect(byTestId("inf-plugin-problem", broken)!.getAttribute("title")).toMatch(/files changed/);
    expect((byTestId("inf-plugin-on", broken) as HTMLButtonElement).disabled).toBe(true);
  });

  it("asks before turning a community plugin on, listing its permissions, then posts enable", async () => {
    const f = await open();
    await press(byTestId("inf-plugin-on", byTestId("inf-plugin-row-community.example")!), "On");
    expect(f.writes()).toHaveLength(0);
    expect(byTestId("inf-plugin-perms")!.textContent).toContain("start and remove containers");
    await press(byTestId("inf-plugin-confirm-on"), "Turn on");
    await settle(() => f.writes().length > 0, "enable post");
    expect(f.writes()[0].path).toBe("/v1/infra/plugins/community.example/enable");
  });

  it("turns a built-in off in one press", async () => {
    const f = await open();
    await press(byTestId("inf-plugin-off", byTestId("inf-plugin-row-official.postgres")!), "Off");
    await settle(() => f.writes().length > 0, "disable post");
    expect(f.writes()[0].path).toBe("/v1/infra/plugins/official.postgres/disable");
  });
});
