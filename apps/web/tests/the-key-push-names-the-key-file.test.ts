// D1 (2026-10-09): the "Add BOSS's key to the server" sheet showed `ssh root@host "…"`, which
// only works when the laptop's default key opens the server. A key file puts `-i` in it.
import { describe, expect, it } from "vitest";

import { withKeyFile } from "../lib/ssh-key-file";

const CMD = `ssh -o StrictHostKeyChecking=accept-new -p 22 root@203.0.113.10 "mkdir -p ~/.ssh"`;

describe("withKeyFile", () => {
  it("leaves the command alone without a key file", () => {
    expect(withKeyFile(CMD, "  ")).toBe(CMD);
  });

  it("inserts -i right after ssh", () => {
    expect(withKeyFile(CMD, "~/.ssh/hetzner")).toBe(
      `ssh -i ~/.ssh/hetzner -o StrictHostKeyChecking=accept-new -p 22 root@203.0.113.10 "mkdir -p ~/.ssh"`);
  });

  it("replaces an -i already there and quotes a path with spaces", () => {
    const had = withKeyFile(CMD, "/old/key");
    expect(withKeyFile(had, "/Users/me/My Keys/id")).toBe(
      `ssh -i '/Users/me/My Keys/id' -o StrictHostKeyChecking=accept-new -p 22 root@203.0.113.10 "mkdir -p ~/.ssh"`);
  });

  it("never touches a command that is not ssh (a provider console line, cloud-init)", () => {
    expect(withKeyFile("mkdir -p ~/.ssh && echo ok", "~/.ssh/k")).toBe("mkdir -p ~/.ssh && echo ok");
  });
});
