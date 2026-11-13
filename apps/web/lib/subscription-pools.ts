/**
 * MULTI-ACCOUNT POOLS (BOSSAPP-886; API routers/subscription_pools.py): several Claude or Codex
 * subscriptions used as one — each run takes the account with the most room and keeps it for its
 * task. A login goes in once and never comes back. Parsing is pure; writes go through obsWrite.
 */
import { aaRead, type ReadResult } from "./ambient";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

export type PoolMember = { id: string; label: string; status: "active" | "disabled" | "needs_reauth"; position: number;
  room: { state: string; left: number | null; resets_at: string | null } | null };
export type Pool = { id: string; engine: "claude_code" | "codex"; name: string; status: "active" | "disabled";
  members: PoolMember[] };

const obj = (v: unknown): Record<string, unknown> =>
  (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown) => (typeof v === "string" ? v : "");

export function parsePools(w: unknown): Pool[] {
  return arr(obj(w).pools).map(obj).map((p) => ({
    id: str(p.id), engine: p.engine === "codex" ? "codex" : "claude_code", name: str(p.name),
    status: p.status === "disabled" ? "disabled" : "active",
    members: arr(p.members).map(obj).map((m) => {
      const h = m.headroom ? obj(m.headroom) : null;
      const status = m.status === "disabled" || m.status === "needs_reauth" ? m.status : "active";
      return { id: str(m.id), label: str(m.label), status, position: Number(m.position) || 0,
               room: h ? { state: str(h.state) || "plenty", left: typeof h.left === "number" ? h.left : null,
                           resets_at: str(h.resets_at) || null } : null } as PoolMember;
    }),
  }));
}

export const ENGINE_WORDS: Record<Pool["engine"], string> = { claude_code: "Claude", codex: "Codex" };
export const ROOM_WORDS: Record<string, string> = { plenty: "Plenty", tight: "Tight", near_limit: "Near limit", exhausted: "At limit" };

const q = (projectId: string) => `project_id=${encodeURIComponent(projectId)}`;
const press = (scope: string, label: string) => ({ scope: `spool:${scope}`, label });

export const readPools = (projectId: string): Promise<ReadResult<Pool[]>> =>
  aaRead(`/v1/subscription-pools?${q(projectId)}`, parsePools);
export const createPool = (projectId: string, engine: Pool["engine"]): Promise<WriteResult> =>
  obsWrite("/v1/subscription-pools", { project_id: projectId, engine }, press(`new:${engine}`, "Start a pool"));
export const addPoolMember = (projectId: string, poolId: string, label: string, credential: string): Promise<WriteResult> =>
  obsWrite(`/v1/subscription-pools/${encodeURIComponent(poolId)}/members`, { project_id: projectId, label, credential },
           press(`${poolId}:add`, "Add account"));
export const updatePoolMember = (projectId: string, memberId: string, fields: Record<string, unknown>, label = "Save"): Promise<WriteResult> =>
  obsWrite(`/v1/subscription-pools/members/${encodeURIComponent(memberId)}`, { project_id: projectId, ...fields },
           press(memberId, label), "PATCH");
export const removePoolMember = (projectId: string, memberId: string): Promise<WriteResult> =>
  obsWrite(`/v1/subscription-pools/members/${encodeURIComponent(memberId)}?${q(projectId)}`, {}, press(memberId, "Remove"), "DELETE");
