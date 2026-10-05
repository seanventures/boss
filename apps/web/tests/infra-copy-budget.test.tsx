// @vitest-environment happy-dom
/**
 * Infra's copy budget (spec §5.1), on what every tab RENDERS from the full fixtures, with each
 * drawer and sheet open: no visible run over six words outside a tooltip or data
 * (`data-copy="data"`), no <p>, none of the banned words, and never micros or `$0`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { MAX_WORDS } from "./_helpers/copy-budget";
import { click, mountInfra, unmountInfra } from "./_helpers/infra-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

const SKIP = "title, [data-tip], [role='tooltip'], [title], textarea, script, style, option, [hidden], [aria-hidden='true'], .ag-sr, [data-user-text], [data-copy='data'], code, pre";
const BANNED = /\b(ProviderOps|Resource Plane|ChangeSet|micros|governed|fail-closed|elastic fabric|entitlements?|SecretRef|idempotent|ETA|estimated)\b|\$0(?![.\d])|\$0\.00|docs\/api/i;

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
  const roots = [...document.querySelectorAll(".inf, .inf-drawer, .inf-sheet")];
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

afterEach(() => { unmountInfra(); vi.unstubAllGlobals(); });

const CASES: { name: string; url: string; press?: string[] }[] = [
  { name: "Fleet", url: "view=fleet" },
  { name: "Fleet + setting-up host drawer", url: "view=fleet&host=h_setup" },
  { name: "Fleet + ready host drawer + retire", url: "view=fleet&host=h_ready", press: ["inf-host-retire"] },
  { name: "This computer", url: "view=fleet&host=this" },
  { name: "Target drawer", url: "view=fleet&target=t_live" },
  { name: "Cloud target drawer", url: "view=fleet&target=t_cloud" },
  { name: "+ Add › Host", url: "view=fleet", press: ["infra-add", "infra-add-host"] },
  { name: "+ Add › App target", url: "view=fleet", press: ["infra-add", "infra-add-target"] },
  { name: "+ Add › Secret", url: "view=fleet", press: ["infra-add", "infra-add-secret"] },
  { name: "+ Add › Enroll", url: "view=fleet", press: ["infra-add", "infra-add-enroll"] },
  { name: "+ Add › Hardware account", url: "view=fleet", press: ["infra-add", "infra-add-hwaccount"] },
  { name: "Ask BOSS", url: "view=fleet", press: ["infra-ask"] },
  { name: "Deploys", url: "view=deploys" },
  { name: "Deploy drawer (failed)", url: "view=deploys&deploy=stack%3Asd_failed" },
  { name: "Deploy drawer (registry)", url: "view=deploys&deploy=registry%3Adr_1" },
  { name: "+ Deploy", url: "view=deploys", press: ["inf-deploy-new"] },
  { name: "Rules", url: "view=deploys", press: ["inf-deploys-more", "inf-deploys-rules"] },
  { name: "CI config", url: "view=deploys", press: ["inf-deploys-more", "inf-deploys-ci"] },
  { name: "Previews", url: "view=previews" },
  { name: "Cloud accounts + drawer", url: "view=cloud&cloud=accounts&account=acct_1" },
  { name: "Cloud + Account sheet", url: "view=cloud&cloud=accounts", press: ["inf-account-new"] },
  { name: "Cloud resources + drawer", url: "view=cloud&cloud=resources&resource=res_1" },
  { name: "Cloud capacity + pool", url: "view=cloud&cloud=capacity&pool=pool_1" },
  { name: "Cloud spend + provider", url: "view=cloud&cloud=spend&account=aws" },
  { name: "Cloud changes + change", url: "view=cloud&cloud=changes&changeset=cs_1" },
];

describe("the Infra copy budget", () => {
  for (const c of CASES) {
    it(c.name, async () => {
      await mountInfra(c.url);
      for (const p of c.press ?? []) await click(p);
      const a = audit();
      expect(a.paragraphs).toEqual([]);
      expect(a.banned).toEqual([]);
      expect(a.over).toEqual([]);
      expect(a.longTips).toEqual([]);
    });
  }
});
