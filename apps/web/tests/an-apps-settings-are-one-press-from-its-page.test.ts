/** An app's settings are one press from its own page, beside "＋ Add a feature".
 *
 *  They were only behind Factory › Apps › ⋯ › Setup. The operator, told to set two deploy
 *  settings, landed on Repo and models, then on Repositories, and asked for the steps twice. */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const COCKPIT = codeOnly("app/factory/cockpit.tsx");

describe("the build page header offers the app's Settings", () => {
  it("has a Settings button next to Add a feature", () => {
    const add = COCKPIT.indexOf('data-testid="fx-add-feature"');
    const settings = COCKPIT.indexOf('data-testid="fx-app-settings"');
    expect(add).toBeGreaterThan(-1);
    expect(settings).toBeGreaterThan(add);
    expect(settings - add).toBeLessThan(900);
  });

  it("opens the same Setup sheet the Apps tab opens, for this build's app", () => {
    expect(COCKPIT).toMatch(/import \{ AppSetupSheet \} from "\.\.\/\.\.\/components\/v2\/templates\/setup-sheet"/);
    expect(COCKPIT).toMatch(/<AppSetupSheet projectId=\{projectId\} runId=\{c\.run\.id\} appName=\{c\.app\}/);
  });
});
