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

/**
 * One agent's identity, re-resolved when identities load. The way a component names or draws ONE
 * agent: calling identityOf/agentIdentity at render without subscribing keeps the answer from
 * before the load (the bundled cast: no callsign for most roles). A component that resolves many
 * (a list, a memo) calls useIdentityVersion() and puts its value in the memo's deps instead.
 * tests/identity-readers-subscribe.test.ts holds every component to one or the other.
 */
export function useIdentity(keyOrTitle: string, name?: string): AgentIdentity {
  const v = useIdentityVersion();
  return useMemo(() => {
    const found = agentIdentity(keyOrTitle);
    return name && !found.name ? { ...found, name } : found;
  }, [keyOrTitle, name, v]);
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
