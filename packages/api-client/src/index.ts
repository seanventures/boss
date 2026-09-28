import { createBossApiClient as createCommunityBossApiClient, type BossApiClientConfig } from "./community";
import { createPaidBossApiMethods } from "./paid";

export * from "./community";
export * from "./paid";

/** Private monorepo compatibility entrypoint. The public release uses community.ts. */
export function createBossApiClient(config: BossApiClientConfig) {
  return {
    ...createCommunityBossApiClient(config),
    ...createPaidBossApiMethods(config),
  };
}
