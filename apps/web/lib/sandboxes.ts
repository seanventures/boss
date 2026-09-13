import { type SandboxSummary, type SandboxHost } from "@boss/api-client";

import { client } from "./client";

export type { SandboxSummary, SandboxHost } from "@boss/api-client";

export type SandboxFeed = {
  apiReachable: boolean;
  items: SandboxSummary[];
  unavailable?: string;
  host?: SandboxHost;
};

export async function fetchSandboxFeed(active = true): Promise<SandboxFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return { apiReachable: false, items: [] };
  try {
    const res = await client.getSandboxes();
    return { apiReachable: true, items: res.sandboxes ?? [], unavailable: res.unavailable, host: res.host };
  } catch {
    return { apiReachable: false, items: [] };
  }
}
