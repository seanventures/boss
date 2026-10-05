import { describe, expect, it } from "vitest";

import { inWords } from "../app/infra/infra-util";
import { parseChange } from "../app/infra/cloud-rows";
import { parseHostDetail } from "../lib/infra-view";

describe("Infra says errors in words", () => {
  it("drops ids, source files and exception class names, keeps the sentence", () => {
    const raw = "ChangeSet chg_49e71b78b1634e569ee5 is a host deployment: approve its decision "
      + "dec_0a1b2c3d4e5f6a7b in Approvals and BOSS's worker applies it "
      + "(services/governed_host_deploy.py). It is never approved or applied through the "
      + "ChangeSet routes.";
    const said = inWords(raw);
    expect(said).toContain("This change is a host deployment");
    expect(said).not.toMatch(/chg_|dec_|\.py|services\//);
    expect(inWords("RuntimeError: transfer failed")).toBe("transfer failed");
  });
});

describe("a deploy's change and its host say what they are", () => {
  it("a deploy's ChangeSet carries the decision it is approved through", () => {
    const c = parseChange({ id: "chg_1", title: "Deploy x", can: { approve: false, approve_deploy: true },
      decision_id: "dec_1" });
    expect(c.decisionId).toBe("dec_1");
    expect(c.can.approve).toBe(false);
    expect(c.can.approve_deploy).toBe(true);
  });

  it("a host's key state is read as words", () => {
    const h = parseHostDetail({ id: "dh_1", host_key: { pinned: true, fingerprint: "SHA256:abc",
      words: "Key pinned when it was verified." } });
    expect(h.hostKey).toEqual({ pinned: true, fingerprint: "SHA256:abc",
      words: "Key pinned when it was verified." });
    expect(parseHostDetail({ id: "this" }).hostKey).toBeNull();
  });
});
