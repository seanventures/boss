"use client";

/**
 * The identity store's React hooks. Kept apart from lib/agent-identity.ts, which has no React,
 * so a server route (the share card, the Credits image) can resolve a face without importing
 * hooks it cannot run.
 */
import { useEffect, useMemo, useSyncExternalStore } from "react";

import type { AgentIdentity } from "./agent-world-types";
import { CAST } from "./avatar-kit/cast";
import {
  agentIdentity, identityVersion, loadIdentities, loadedIdentities, subscribeIdentities,
} from "./agent-identity";

/** Re-render when identities load. Every face subscribes, so a callsign or custom face lands everywhere at once. */
export function useIdentityVersion(): number {
  return useSyncExternalStore(subscribeIdentities, identityVersion, () => 0);
}

/** The identities for a project, loading them on first use. */
export function useAgentIdentities(projectId: string): Map<string, AgentIdentity> {
  const v = useIdentityVersion();
  useEffect(() => { void loadIdentities(projectId); }, [projectId]);
  return useMemo(() => {
    const out = new Map<string, AgentIdentity>();
    for (const c of CAST) out.set(c.roleKey, agentIdentity(c.roleKey));
    for (const [k, i] of loadedIdentities()) out.set(k, i);
    return out;
  }, [v]); // eslint-disable-line react-hooks/exhaustive-deps -- the store `v` versions
}
