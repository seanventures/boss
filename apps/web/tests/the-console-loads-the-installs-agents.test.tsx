// @vitest-environment happy-dom
/**
 * THE CONSOLE LOADS THE INSTALL'S AGENTS. Seen live on boss-1 after the roster learned to hear
 * the identity store: the cards still said "Program Manager" where the install says Rally,
 * because nothing in the console ever called loadIdentities — the store stayed empty and every
 * name came from the bundled cast. The shell now loads them once for the active project.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CAST_BY_KEY } from "../lib/avatar-kit/cast";
import { resetIdentities } from "../lib/agent-identity";
import { useIdentity, useLoadIdentities } from "../lib/use-agent-identities";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function Page({ projectId }: { projectId: string }) {
  useLoadIdentities(projectId);
  return <Name roleKey="program_manager" />;
}
function Name({ roleKey }: { roleKey: string }) {
  const who = useIdentity(roleKey);
  return <span data-testid="name">{who.callsign || who.name}</span>;
}

let root: Root | null = null;
afterEach(() => {
  act(() => root?.unmount());
  root = null;
  resetIdentities();
  vi.unstubAllGlobals();
});

describe("the console loads the install's agents", () => {
  it("fetches the project's identities and every subscribed name repaints with them", async () => {
    expect((CAST_BY_KEY as Record<string, { callsign?: string }>).program_manager?.callsign).toBeFalsy();
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      project_id: "p1", aliases: {},
      items: [{ role_key: "program_manager", agent_id: "agt_x", display_name: "Program Manager",
                callsign: "Rally", tagline: "Which date slips?", avatar: { kit: "boss-v1", seed: "program_manager" },
                lifecycle: "active" }],
    }), { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetcher);
    const host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root!.render(<Page projectId="p1" />); });
    await act(async () => { await Promise.resolve(); await new Promise((r) => setTimeout(r, 0)); });
    expect(fetcher).toHaveBeenCalledWith("/v1/agents/identities?project_id=p1", expect.anything());
    expect(host.querySelector("[data-testid=name]")?.textContent).toBe("Rally");
  });

  it("is what the console shell does for the active project", () => {
    const shell = readFileSync(join(__dirname, "..", "app", "ConsoleShell.tsx"), "utf8");
    expect(shell).toMatch(/useLoadIdentities\(scope\.projectId \?\? scope\.project\)/);
  });
});
