import { type SandboxSummary } from "@boss/api-client";

import { client } from "./client";

export type { SandboxSummary } from "@boss/api-client";

export type SandboxFeed = {
  apiReachable: boolean;
  items: SandboxSummary[];
  unavailable?: string;
};

export async function fetchSandboxFeed(): Promise<SandboxFeed> {
  try {
    const res = await client.getSandboxes();
    return { apiReachable: true, items: res.sandboxes ?? [], unavailable: res.unavailable };
  } catch {
    return { apiReachable: false, items: [] };
  }
}
