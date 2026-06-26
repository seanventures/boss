import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

// Phase 2 skill-invocation framework (GET /v1/skills/invocations) — read directly.

export type SkillInvocation = {
  id: string;
  skillId: string;
  caller: string;
  mode: string;
  status: string;
  permissionDecision: string;
  attempts: number;
  reason: string | null;
  createdAt: string;
};

export async function fetchSkillInvocations(
  projectId = PROJECT_ID,
  limit = 15,
): Promise<SkillInvocation[]> {
  try {
    const r = await fetch(
      `${API_URL}/v1/skills/invocations?project_id=${encodeURIComponent(resolveProjectId(projectId))}&limit=${limit}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return [];
    const items = (await r.json()).items ?? [];
    return items.map((it: Record<string, unknown>) => ({
      id: String(it.id),
      skillId: String(it.skill_id),
      caller: String(it.caller),
      mode: String(it.mode),
      status: String(it.status),
      permissionDecision: String(it.permission_decision),
      attempts: Number(it.attempts ?? 0),
      reason: it.reason != null ? String(it.reason) : null,
      createdAt: String(it.created_at ?? ""),
    }));
  } catch {
    return [];
  }
}
