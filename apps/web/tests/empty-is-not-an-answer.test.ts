import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * An empty list is not an answer until someone has asked.
 *
 * The operator reported "there's no host to Set target". There WAS a verified host —
 * `hetzner-ci-01`, status `verified`, returned by the API the whole time. The component
 * initialised `hosts` to `[]` and computed `ready = hosts.length > 0`, so between first paint
 * and the effect resolving it stated "No verified host yet": a definitive claim of absence,
 * made before it had looked. On a console page this size, hydration is slow enough that the
 * false claim is what gets read.
 *
 * The second half was worse. `if (r.ok)` had no else, so a FAILED lookup left the list empty
 * forever and the UI blamed a missing host for an infrastructure failure — silently, and it
 * sends the operator off to onboard a host they already own.
 *
 * Same shape as a preview reporting `running` because a port answered: a state asserting more
 * than was verified. "Not loaded", "load failed" and "genuinely none" are three different
 * facts and must render as three different things.
 */

const SOURCE = readFileSync(resolve(__dirname, "../app/factory/legacy-build-parts.tsx"), "utf8");

describe("a fetch-backed empty state", () => {
  it("does not claim absence before the fetch resolves", () => {
    expect(SOURCE).toContain('const [loadState, setLoadState]');
    expect(SOURCE).toMatch(/ready = loadState === "loaded" && hosts\.length > 0/);
  });

  it("has a distinct render for not-yet-asked", () => {
    expect(SOURCE).toMatch(/loadState === "loading" \?/);
    expect(SOURCE).toContain("Checking for a host");
  });

  it("surfaces a failed lookup instead of blaming a missing host", () => {
    expect(SOURCE).toMatch(/loadState === "error"/);
    expect(SOURCE).toContain("Could not read the host list");
  });

  it("still says 'no verified host' when there genuinely is none", () => {
    expect(SOURCE).toContain("No verified host yet");
  });

  it("does not swallow a failed action result", () => {
    // The original was `if (r.ok) setHosts(...)` with no else — the whole defect in one line.
    const effect = SOURCE.slice(
      SOURCE.indexOf("listDeploymentHostsAction"),
      SOURCE.indexOf("const ready = loadState"),
    );
    expect(effect).toMatch(/if \(!r\.ok\)/);
    expect(effect).toContain("setLoadState(\"error\")");
  });

  it("ignores a resolution that arrives after the component is gone", () => {
    const effect = SOURCE.slice(
      SOURCE.indexOf("listDeploymentHostsAction"),
      SOURCE.indexOf("const ready = loadState"),
    );
    expect(effect).toContain("if (!live) return;");
  });

  it("catches a thrown action, not only a falsy ok", () => {
    const effect = SOURCE.slice(
      SOURCE.indexOf("listDeploymentHostsAction"),
      SOURCE.indexOf("const ready = loadState"),
    );
    expect(effect).toMatch(/\.catch\(/);
  });
});
