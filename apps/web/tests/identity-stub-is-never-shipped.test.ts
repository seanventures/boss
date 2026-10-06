/**
 * THE IDENTITY STUB NEVER STANDS IN FOR REAL AUTHENTICATION. It answers only when
 * BOSS_IDENTITY_STUB is the exact deliberate value, and nothing that builds or installs BOSS sets
 * it: not a Dockerfile, not the compose file, not install.sh, not an .env example.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { identityStubEnabled, STUB_ENABLE_VALUE } from "../lib/identity/stub/enabled";

const REPO = join(process.cwd(), "..", "..");
afterEach(() => vi.unstubAllEnvs());

describe("the identity stub", () => {
  it.each(["Dockerfile.community", "scripts/install/community-compose.yml", "install.sh", ".env.example",
    "apps/web/.env.example", "apps/api/.env.example", "apps/worker/.env.example"])("%s does not set it", (file) => {
    const path = join(REPO, file);
    expect(existsSync(path), `${file} moved: update this list`).toBe(true);
    expect(readFileSync(path, "utf8")).not.toMatch(/BOSS_IDENTITY_STUB/);
  });

  it("is off unless the exact value is set", () => {
    for (const v of ["", "1", "true", "yes", "fixtures", STUB_ENABLE_VALUE.toUpperCase(), ` ${STUB_ENABLE_VALUE}`]) {
      vi.stubEnv("BOSS_IDENTITY_STUB", v);
      expect(identityStubEnabled(), JSON.stringify(v)).toBe(false);
    }
    vi.stubEnv("BOSS_IDENTITY_STUB", STUB_ENABLE_VALUE);
    expect(identityStubEnabled()).toBe(true);
  });

  it("cannot be turned on in a production build, even with the exact value", () => {
    vi.stubEnv("BOSS_IDENTITY_STUB", STUB_ENABLE_VALUE);
    vi.stubEnv("NODE_ENV", "production");
    expect(identityStubEnabled()).toBe(false);
  });
});
