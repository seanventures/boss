/**
 * A verify tail names a failed test only in the lines the runner already printed there
 * (pytest's FAILED node id, node:test's `not ok`, jest/vitest's mark or `file > name`).
 * A count, a unittest summary, or two different names is not one name. The result is a
 * substring of the tail, never a rerun command the tail did not print.
 */
import { describe, expect, it } from "vitest";

import { failedTestName } from "../lib/failed-test-name";

/** Pytest's short summary, as last_verify_output keeps it (build page fixture). */
const PYTEST_ONE = [
  "=================================== FAILURES ===================================",
  "___________________________ test_google_login_redirect _________________________",
  "AssertionError: assert 500 == 302",
  "=========================== short test summary info ============================",
  "FAILED tests/test_auth.py::test_google_login_redirect - AssertionError: assert 500 == 302",
  "========================= 1 failed, 4 passed in 3.21s ==========================",
].join("\n");

const PYTEST_TWO = [
  "FAILED tests/test_auth.py::test_google_login_redirect - AssertionError: assert 500 == 302",
  "FAILED tests/test_auth.py::test_github_login_redirect - AssertionError",
].join("\n");

/** node:test, as a stored last_verify_output names a single failure. */
const TAP_ONE = "not ok 1 tests/sla_lib.test.mjs\n# tests 1\n# pass 0\n# fail 1\n";
const TAP_NAMED = "not ok 3 - rejects a bad token\n# tests 5\n# pass 4\n# fail 1\n";

describe("failedTestName", () => {
  it("reads the one pytest node id the stored tail already printed", () => {
    const name = failedTestName(PYTEST_ONE);
    expect(name).toBe("tests/test_auth.py::test_google_login_redirect");
    expect(PYTEST_ONE.includes(name!)).toBe(true);
    expect(name).not.toMatch(/^pytest\b/);
  });

  it("reads one node:test name, with or without the dash", () => {
    expect(failedTestName(TAP_ONE)).toBe("tests/sla_lib.test.mjs");
    expect(TAP_ONE.includes(failedTestName(TAP_ONE)!)).toBe(true);
    expect(failedTestName(TAP_NAMED)).toBe("rejects a bad token");
  });

  it("reads one jest or vitest name the runner printed, without its duration", () => {
    const jest = "FAIL src/a.test.ts\n  ✕ logs in with Google (12 ms)\nTests:       1 failed, 3 passed, 4 total\n";
    expect(failedTestName(jest)).toBe("logs in with Google");
    expect(jest.includes("logs in with Google")).toBe(true);
    const vitest = " FAIL  tests/a.test.ts > login > redirects\n";
    expect(failedTestName(vitest)).toBe("tests/a.test.ts > login > redirects");
  });

  it("does not invent a name from a count, a summary, or two different failures", () => {
    expect(failedTestName("1..148\n# tests 148\n# pass 142\n# fail 6\n")).toBeNull();
    expect(failedTestName("FAILED (failures=2)\n")).toBeNull();
    expect(failedTestName("ok\nok")).toBeNull();
    expect(failedTestName(PYTEST_TWO)).toBeNull();
    expect(failedTestName("")).toBeNull();
    expect(failedTestName(undefined)).toBeNull();
  });

  it("does not turn a node id into a rerun command the tail never printed", () => {
    const name = failedTestName(PYTEST_ONE);
    expect(name).toBe("tests/test_auth.py::test_google_login_redirect");
    expect(PYTEST_ONE.includes(`pytest ${name}`)).toBe(false);
  });

  it("keeps one name when the same node id is printed twice", () => {
    const tail = "tests/test_cart.py::test_discount_applies FAILED\n"
      + "FAILED tests/test_cart.py::test_discount_applies - AssertionError: assert 100 == 90\n";
    expect(failedTestName(tail)).toBe("tests/test_cart.py::test_discount_applies");
    expect(tail.includes("tests/test_cart.py::test_discount_applies")).toBe(true);
  });
});
