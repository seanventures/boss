// @vitest-environment happy-dom
/**
 * Where apps install, now the Fleet (infra-observability spec §4.3-§4.4; parity P-I-03..P-I-07).
 * The old "Where apps install" screen (HardwareManager) is three things in the Fleet:
 *   - This computer is the first rack, and `host=this` shows its facts and `Correct record`;
 *   - `+ Add › Enroll machine` shows the enroll command the API returns;
 *   - `+ Add › Hardware account` registers a provider by secret REFERENCE and refuses a pasted
 *     secret before anything is sent.
 * The `#hardware-manager` anchor still lands on This computer (infra-home.tsx).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { hostFixture, type Route } from "./_helpers/infra-fixtures";
import { $$, byTestId, click, mountFleet, submit, type, unmountInfra } from "./_helpers/infra-render";

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(window.location.search), useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));

afterEach(() => unmountInfra());

describe("This computer and added hardware", () => {
  it("draws This computer first, selected, ahead of every other machine", async () => {
    await mountFleet("view=fleet");
    const racks = $$('[data-testid="inf-rack-this"], [data-testid="inf-rack"]');
    expect(racks[0].dataset.testid).toBe("inf-rack-this");
    expect(racks[0].textContent).toContain("This computer");
    expect(racks[0].textContent).toContain("boss-1");
  });

  it("shows its facts and the agent state, and says Not ready when the agent is absent", async () => {
    await mountFleet("view=fleet&host=this");
    const facts = byTestId("inf-host-facts")!;
    expect(facts.textContent).toContain("boss-1");
    expect(facts.textContent).toContain("arm64");
    expect(facts.textContent).toContain("Installed");
    unmountInfra();
    const absent: Route = { match: /^\/v1\/infra\/hosts\/this$/, etag: true, reply: () => {
      const h = hostFixture("this")!;
      return { ...h, facts: { ...(h.facts as Record<string, unknown>), agent_state: "absent" } };
    } };
    await mountFleet("view=fleet&host=this", { routes: [absent] });
    expect(byTestId("inf-host-facts")?.textContent).toContain("Not ready");
  });

  it("Correct record saves hostname, arch and OS for this computer", async () => {
    const mock = await mountFleet("view=fleet&host=this");
    await click("inf-host-correct");
    const form = byTestId("inf-host-correct-form")!;
    await type(form.querySelector('input[name="hostname"]'), "boss-2");
    await submit(form);
    const posted = mock.writes();
    expect(posted.map((w) => w.path)).toEqual(["/api/hardware/places/local"]);
    expect(posted[0].body).toEqual({ project_id: "project_1", actor: "ceo", hostname: "boss-2", arch: "arm64", os: "linux" });
  });

  it("Hardware account sends a secret reference and refuses a pasted raw secret", async () => {
    const mock = await mountFleet("view=fleet");
    await click("infra-add");
    await click("infra-add-hwaccount");
    await type("inf-hwaccount-name", "Production");
    await type("inf-hwaccount-secret", "AKIAABCDEFGHIJKLMNOP");
    await submit("inf-hwaccount-name");
    expect(byTestId("inf-hwaccount-error")?.textContent).toMatch(/secret reference|access key/i);
    expect(mock.writes()).toHaveLength(0);

    await type("inf-hwaccount-secret", "env:BOSS_AWS_SECRET");
    await submit("inf-hwaccount-name");
    const posted = mock.writes();
    expect(posted.map((w) => w.path)).toEqual(["/api/hardware/places/providers"]);
    expect(posted[0].body).toEqual({ project_id: "project_1", actor: "ceo", name: "Production", provider: "aws",
                                     secret_ref: "env:BOSS_AWS_SECRET" });
  });

  it("Enroll machine shows the enroll command the API returns", async () => {
    const mock = await mountFleet("view=fleet");
    await click("infra-add");
    await click("infra-add-enroll");
    await type("inf-enroll-name", "build-vm");
    await submit("inf-enroll-name");
    expect(byTestId("inf-enroll-command")?.textContent).toContain("curl -fsSL https://boss.example/enroll | sh -s -- tok_123");
    expect(mock.writes()[0].body).toEqual({ project_id: "project_1", actor: "ceo", name: "build-vm" });
  });

  it("the old #hardware-manager anchor still opens This computer", () => {
    const home = readFileSync(resolve(__dirname, "../app/infra/infra-home.tsx"), "utf8");
    expect(home).toContain('window.location.hash === "#hardware-manager"');
    expect(home).toContain('host: "this"');
  });
});
