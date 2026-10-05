/**
 * What a row is for survives the width (independent walk, item 14).
 *
 * - At 390 Cloud › Changes hid every column from the fourth, so a change's State and Age — the
 *   two things a phone visit is for — were gone. They stay, stacked beside the title.
 * - At 1440 a deploy row's app · env was ellipsized ("Lead-Triage Inbox · sta…"); the env is the
 *   half that says where it went. It wraps instead.
 * Checked on the stylesheets: the layout rule is the contract (jsdom has no layout).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = (f: string) => readFileSync(join(process.cwd(), "app", "infra", f), "utf8");
const narrow = (src: string) => src.slice(src.indexOf("@media (max-width: 639px)"));

describe("Cloud › Changes at 390", () => {
  it("shows each change's State (column 5) and Age (column 6), stacked", () => {
    const rules = narrow(css("cloud.css"));
    const sec = '.cloud .cloud-sec[aria-labelledby="inf-cloud-changes"] .inf-table';
    expect(rules).toContain(`${sec} td:nth-child(5) { display: block; grid-area: s;`);
    expect(rules).toContain(`${sec} td:nth-child(6) { display: block; grid-area: a;`);
    expect(rules).toMatch(/grid-template-areas:\s*"c s"\s*"c a"/);
  });
});

describe("a deploy row at 1440", () => {
  it("wraps its app · env rather than cutting it off", () => {
    const rule = css("deploys.css").match(/\.inf-dep-name \{([^}]*)\}/)![1];
    expect(rule).not.toMatch(/text-overflow:\s*ellipsis/);
    expect(rule).not.toMatch(/white-space:\s*nowrap/);
    expect(rule).toMatch(/flex-wrap:\s*wrap/);
  });
});
