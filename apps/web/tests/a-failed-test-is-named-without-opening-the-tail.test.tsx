/**
 * The Tests row prints the suite command, the exit code, and a folded output tail. A tail that
 * already names one failed test shows that name closed; opening the tail is not required.
 * The name is a string the tail already contains. A rerun command the tail did not print is not.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { parseCockpit } from "../lib/cockpit";
import { cockpit } from "./_helpers/factory-fixtures";

vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span data-testid="deploy-line" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));

const { Outputs } = await import("../app/factory/outputs");

type W = Record<string, unknown>;

/** Pytest's short summary, as last_verify_output keeps it (build page fixture). */
const PYTEST_ONE = [
  "=================================== FAILURES ===================================",
  "___________________________ test_google_login_redirect _________________________",
  "AssertionError: assert 500 == 302",
  "=========================== short test summary info ============================",
  "FAILED tests/test_auth.py::test_google_login_redirect - AssertionError: assert 500 == 302",
  "========================= 1 failed, 4 passed in 3.21s ==========================",
].join("\n");

function withTail(outputTail: string, command = "pytest -q") {
  const base = cockpit();
  return parseCockpit(cockpit({
    outputs: {
      ...(base.outputs as W),
      tests: {
        word: "1 of 1 command failed", role_key: "qa", output_tail: outputTail,
        commands: [{ command, exit_code: 1, ok: false, timed_out: false }],
      },
    },
  }));
}

function testsHtml(outputTail: string, command = "pytest -q"): string {
  const c = withTail(outputTail, command);
  return renderToStaticMarkup(
    <Outputs projectId="p1" actor="ceo" cockpit={c} open="tests" onOpen={() => {}} onLogs={() => {}} onRefresh={() => {}} />,
  );
}

describe("Tests row", () => {
  it("shows the one failed test the tail already names, with the tail still folded", () => {
    const html = testsHtml(PYTEST_ONE);
    const name = "tests/test_auth.py::test_google_login_redirect";
    expect(html).toContain(`data-testid="fx-tests-failed"`);
    expect(html).toContain(name);
    expect(PYTEST_ONE.includes(name)).toBe(true);
    expect(html).not.toContain("<pre");
    expect(html).toContain("pytest -q");
    expect(html).toContain("exit 1");
    expect(html).not.toContain(`pytest ${name}`);
    expect(html).toContain('data-testid="fx-tests-output"');
  });

  it("stays quiet when the tail names no single test", () => {
    const html = testsHtml("# fail 6\n", "npm test");
    expect(html).not.toContain('data-testid="fx-tests-failed"');
    expect(html).not.toContain("<pre");
    expect(html).toContain("npm test");
    expect(html).toContain("exit 1");
  });
});
