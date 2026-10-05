import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";
import { repositoryHint } from "../app/delivery-workflows-ui";

/**
 * The New Project form never promises a repository that does not exist.
 *
 * With nothing registered it said "the platform default is used". The API pre-selects that
 * default whenever one exists, so the branch only renders when there is none — and Community
 * has none at all. The build then deadlettered with "No repository registered for project …".
 */
// The Runs composer lives in its own module since the Workflows area became Runs + Library.
const UI = codeOnly("app/workflows/new-project-composer.tsx");

describe("the repository step", () => {
  it("says a build needs a connected repository when there is none", () => {
    const hint = repositoryHint(null, 0);
    expect(hint.needsConnection).toBe(true);
    expect(hint.text).toContain("no repository connected yet");
    expect(hint.text).toContain("build needs one");
    expect(hint.text).not.toContain("platform default");
  });

  it("names the pre-selected repository when the project resolves to one", () => {
    const primary = repositoryHint({ name: "acme/app", source: "registry_primary" }, 1);
    expect(primary).toEqual({
      text: " Pre-selected: acme/app — this project's primary.", needsConnection: false });
    const legacy = repositoryHint({ name: "acme/legacy", source: "legacy_settings" }, 0);
    expect(legacy.text).toBe(" Pre-selected: acme/legacy.");
  });

  it("asks for a choice when several repositories exist and none is primary", () => {
    const hint = repositoryHint(null, 2);
    expect(hint.needsConnection).toBe(false);
    expect(hint.text).toContain("choose one below");
  });

  it("links to the existing GitHub connection screen, not an environment variable", () => {
    expect(UI).not.toContain("so the platform default is used");
    expect(UI).toContain('{ area: "setup", focus: "repository" }');
    expect(UI).toContain('data-testid="dw-new-repo-connect"');
    expect(UI).not.toContain("BOSS_TARGET_GITHUB_OWNER");
  });
});
