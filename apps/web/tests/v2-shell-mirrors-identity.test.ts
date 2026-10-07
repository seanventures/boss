/**
 * THE v2 SCREENS MIRROR WHO IS SIGNED IN (round 3 role walk). Without an IdentityProvider every
 * gated control asks the unmirrored default, which allows everything: the v2 autonomy footer's
 * "Switch to Full auto" (approval_rule:manage) was live for a Viewer. The Shell loads the
 * person's permissions and provides them, like ConsoleShell.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

describe("the v2 Shell", () => {
  it("wraps every v2 screen in the identity mirror, loaded for its project", () => {
    const src = readFileSync(join(__dirname, "..", "components", "v2", "Shell.tsx"), "utf8");
    expect(src).toMatch(/export async function Shell\(/);
    expect(src).toMatch(/await loadIdentity\(projectId\)/);
    expect(src).toMatch(/<IdentityProvider me=\{identity\.me\} org=\{identity\.org\} project=\{identity\.project\} projectId=\{projectId\}/);
  });
});
