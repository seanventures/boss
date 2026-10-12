import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Markdown } from "../app/markdown";

// Harbor told an operator to set "BOSSAGENTHOSTSHELL": the renderer read the underscores inside
// BOSS_AGENT_HOST_SHELL as emphasis and dropped them. Underscores inside a word are not emphasis.
const html = (md: string) => renderToStaticMarkup(<Markdown text={md} />);

describe("an identifier with underscores survives rendering", () => {
  it("keeps env var, tool and path names whole in prose", () => {
    const out = html("Set BOSS_AGENT_HOST_SHELL, or use run_on_host on /opt/boss_apps.");
    expect(out).toContain("BOSS_AGENT_HOST_SHELL");
    expect(out).toContain("run_on_host");
    expect(out).toContain("/opt/boss_apps");
    expect(out).not.toContain("<em>");
  });

  it("still renders real underscore emphasis and bold", () => {
    expect(html("this is _really_ it")).toContain("<em>really</em>");
    expect(html("this is __bold__ text")).toContain("<strong>bold</strong>");
  });

  it("leaves code spans alone", () => {
    expect(html("run `BOSS_AGENT_HOST_SHELL=1`")).toContain("<code>BOSS_AGENT_HOST_SHELL=1</code>");
  });
});
