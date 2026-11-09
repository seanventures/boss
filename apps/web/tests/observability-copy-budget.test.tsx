// @vitest-environment happy-dom
/**
 * Observability's copy budget (spec §5.1), on what the tabs RENDER from the full fixtures, with
 * every drawer and sheet open: no visible run over six words outside a tooltip or data
 * (`data-copy="data"`), no <p>, none of the banned words, and never micros or `$0`.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MAX_WORDS } from "./_helpers/copy-budget";
import { mockObsFetch } from "./_helpers/obs-fixtures";
import { fakeImproveApi, newImprove } from "./_helpers/improve-fixtures";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { ObsHome } = await import("../app/observability/obs-home");

// An SVG <title> is a tooltip, like [title].
// [data-wireframe-copy]: a line the approved wireframe itself spells out (screen 15's subtitle).
const SKIP = "title, [data-wireframe-copy], [data-tip], [role='tooltip'], [title], textarea, script, style, option, [hidden], [aria-hidden='true'], .ag-sr, [data-user-text], [data-copy='data']";
const BANNED = /\b(ProviderOps|Resource Plane|ChangeSet|micros|governed|fail-closed|elastic fabric|entitlements?|SecretRef|idempotent|ETA|estimated)\b|\$0(?![.\d])|\$0\.00/i;

function texts(root: Element): string[] {
  const out: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest(SKIP)) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("").trim();
    if (own) out.push(own);
  }
  return out;
}
function audit() {
  const roots = [...document.querySelectorAll(".obs, .inf-drawer, .inf-sheet")];
  const over = roots.flatMap((r) => texts(r).filter((t) => t.replace(/[·▸•|]/g, " ").trim().split(/\s+/).filter(Boolean).length > MAX_WORDS));
  return {
    over: [...new Set(over)],
    paragraphs: roots.flatMap((r) => [...r.querySelectorAll("p")].map((p) => p.textContent ?? "")),
    banned: [...new Set(roots.flatMap(texts).filter((t) => BANNED.test(t)))],
    // Tooltips ≤ 14 words (§5.1). A title on data (an error, a URL, a log line) is exempt.
    longTips: [...new Set(roots.flatMap((r) => [...r.querySelectorAll("[title]")])
      .filter((el) => !el.closest("[data-copy='data'], [data-user-text], code, pre"))
      .map((el) => el.getAttribute("title") ?? "")
      .filter((t) => t.trim().split(/\s+/).filter(Boolean).length > 14))],
  };
}

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const CASES: { name: string; url: string; press?: string[] }[] = [
  { name: "Apps + app drawer + Ask BOSS", url: "?view=apps&app=t_down", press: ["obs-incident-show-error"] },
  { name: "Apps + Ask BOSS menu", url: "?view=apps", press: ["obs-ask"] },
  { name: "Models + group drawer", url: "?view=models&model=model:claude-sonnet-4-5" },
  { name: "Models by agent", url: "?view=models&group=role" },
  { name: "System", url: "?view=system" },
  { name: "Sources + Check config", url: "?view=sources", press: ["obs-check-config"] },
  { name: "Sources + Add tools", url: "?view=sources", press: ["obs-add-tools", "obs-tools-recommend"] },
  { name: "Scorecards by role", url: "?view=scorecards" },
  { name: "Scorecards by harness, 90d", url: "?view=scorecards&by=harness&swin=90d" },
  { name: "Experiments + new", url: "?view=scorecards&sc=experiments", press: ["sc-exp-add"] },
  { name: "Installs (collector mode)", url: "?view=installs" },
  // App alerts (BOSSAPP-598): the drawer's Alerts tab with a fired alert open, its sheets, and
  // Observability › Alerts; the drawer without a Grafana that answers.
  { name: "App drawer › Alerts + an alert's detail", url: "?view=apps&app=t_down&atab=alerts", press: ["obs-alert-open", "obs-alert-runbook"] },
  { name: "App drawer › Alerts + edit rule", url: "?view=apps&app=t_down&atab=alerts", press: ["obs-rule-edit"] },
  { name: "App drawer › Alerts + add rule", url: "?view=apps&app=t_down&atab=alerts", press: ["obs-rule-add"] },
  { name: "App drawer › Alerts + silence", url: "?view=apps&app=t_down&atab=alerts", press: ["obs-rule-silence"] },
  { name: "Alerts (all apps)", url: "?view=alerts", press: ["obs-alert-open"] },
];

describe("the Observability copy budget", () => {
  for (const c of CASES) {
    it(c.name, async () => {
      const { fn } = mockObsFetch();
      const improve = newImprove();
      improve.sharing.switches.collector.on = true;
      vi.stubGlobal("fetch", fakeImproveApi(improve, fn as never));
      window.history.replaceState(null, "", `/?area=observability&${c.url.replace(/^\?/, "")}`);
      await mount(<ObsHome projectId="p1" actor="ceo" />);
      for (const p of c.press ?? []) await click($(`[data-testid="${p}"]`));
      await settle();
      const a = audit();
      expect(a.paragraphs).toEqual([]);
      expect(a.banned).toEqual([]);
      expect(a.over).toEqual([]);
      expect(a.longTips).toEqual([]);
    });
  }
});
