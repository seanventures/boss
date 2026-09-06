import { describe, expect, it } from "vitest";

import { consoleHref } from "../app/url-intent";
import { codeOnly } from "./_helpers/source";

/**
 * "When I click on Open research (from Workflow) of Build a lead triage inbox, it is not going
 * to specific project's research but only to the home page of the Research."
 *
 * Everything about the link was right. The step carries
 * `discovery_id = drun_57163caa92a34955b968`, the href is
 * `?area=discovery&run=drun_…`, the page parses `run`, passes it as `initialDiscoveryId`, and
 * the dossier is the FIRST item in the feed.
 *
 * The console renders all fifty areas at once and shows one. So DiscoveriesHome had already
 * mounted — with no dossier in the URL — long before the click, and
 * `useState(initialDiscoveryId ?? null)` reads its argument exactly once. The prop changed and
 * the state did not. The plumbing was right and nothing consumed it.
 */
describe("the link itself", () => {
  it("names the dossier and keeps who you are", () => {
    const href = consoleHref("?org=org_boss&area=code_tasks&run=crun_33a0", {
      area: "discovery", run: "drun_57163caa92a34955b968",
    });
    const q = new URLSearchParams(href.slice(1));
    expect(q.get("area")).toBe("discovery");
    expect(q.get("run")).toBe("drun_57163caa92a34955b968");
    expect(q.get("org")).toBe("org_boss");
  });

  it("the workflow step's previous selection does not survive the switch", () => {
    const href = consoleHref("?org=org_boss&area=code_tasks&run=crun_33a0", {
      area: "discovery", run: "drun_1",
    });
    expect(href).not.toContain("crun_33a0");
  });
});

describe("the page consumes it on every navigation", () => {
  const UI = codeOnly("app/discoveries-ui.tsx");

  it("the selection follows the prop, not only the first mount", () => {
    expect(UI).toContain("setSelected(initialDiscoveryId ?? null);");
    expect(UI).toContain("}, [initialDiscoveryId]);");
  });

  it("and going back still clears it", () => {
    // The effect keys on the prop, so Back — which changes state and not the URL — is not
    // immediately undone by it.
    expect(UI).toContain("onBack={() => setSelected(null)}");
  });
});
