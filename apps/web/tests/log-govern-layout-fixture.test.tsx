// @vitest-environment happy-dom
/**
 * The Log and Govern as rendered, written as pages for a real browser to hit-test
 * (tests/e2e/selftest/test_every_log_and_govern_control_can_be_tapped.py, at 1440 and 390):
 * every button, link and field must be the thing a tap at its centre lands on. At 390,
 * "Lines / By run" shrank to 2px inside the views scroll strip and could not be tapped.
 * Runs with LAYOUT_OUT set; without it, it only checks the screens render their controls.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { fakeGovernApi, newGovern } from "./_helpers/govern-fixtures";
import { FACETS, fakeLogApi, newLog, rows } from "./_helpers/log-fixtures";
import { $, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { LogExplorer } = await import("../app/activity/log-explorer");
const { GovernHome } = await import("../app/govern/govern-home");
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

function page(body: string, fixedHeight = false): string {
  const web = resolve(__dirname, "..");
  const css = ["app/globals.css", "app/activity/log.css", "app/govern/govern.css"]
    .map((f) => `<link rel="stylesheet" href="file://${join(web, f)}">`).join("\n");
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
${css}<style>html,body{height:auto!important;overflow:auto!important} body{margin:0;padding:16px;background:var(--bg)} .frame{box-sizing:border-box;width:var(--frame)} .frame[data-fixed]{height:900px;display:flex;flex-direction:column}
@media (min-width:1024px){:root{--frame:calc(100vw - 280px)}} @media (max-width:1023px){:root{--frame:100%}}</style></head>
<body><div class="frame"${fixedHeight ? " data-fixed" : ""}>${body}</div></body></html>`;
}

describe("the pages the browser hit-test taps", () => {
  it("render the Log's and Govern's controls", async () => {
    vi.stubGlobal("fetch", fakeLogApi(newLog({ page: { items: rows(6) }, facets: FACETS })));
    setConsoleLive(true);
    window.history.replaceState({}, "", "/?area=activity");
    await mount(<LogExplorer projectId="p1" actor="ceo" />);
    expect($("[data-testid=lx-by-run]")).not.toBeNull();
    const log = document.querySelector(".lx")!.outerHTML;
    unmount();
    vi.stubGlobal("fetch", fakeGovernApi(newGovern()));
    window.history.replaceState({}, "", "/?area=budget");
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect($("[data-testid=gv-board]")).not.toBeNull();
    const govern = document.querySelector(".gv")!.outerHTML;
    const out = process.env.LAYOUT_OUT;
    if (!out) return;
    mkdirSync(out, { recursive: true });
    writeFileSync(join(out, "log.html"), page(log, true)); // the Log fills a fixed-height pane
    writeFileSync(join(out, "govern.html"), page(govern));
  });
});
