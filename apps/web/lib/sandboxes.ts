import { type SandboxSummary, type SandboxHost } from "@boss/api-client";

import { client } from "./client";

export type { SandboxSummary, SandboxHost } from "@boss/api-client";

export type SandboxFeed = {
  apiReachable: boolean;
  items: SandboxSummary[];
  unavailable?: string;
  host?: SandboxHost;
};

export async function fetchSandboxFeed(): Promise<SandboxFeed> {
  try {
    const res = await client.getSandboxes();
    return { apiReachable: true, items: res.sandboxes ?? [], unavailable: res.unavailable, host: res.host };
  } catch {
    return { apiReachable: false, items: [] };
  }
}
