"use client";

/**
 * What the cockpit's buttons post. Same-origin `/v1/…` (app/v1/[...path]/route.ts adds the key on
 * the server), so the browser never holds a credential. Every call returns the API's own
 * sentence on refusal; nothing is retried behind the operator's back.
 *
 * Retry, the preview and the Change-it proposal reuse the server actions the old Factory posted
 * (app/actions.ts), so the engine refuses here exactly what it refused there.
 */
import { parseToolCalls, type ToolCalls } from "../../lib/agents-live-board";

export type Posted<T = Record<string, unknown>> = { ok: boolean; data?: T; error?: string; status?: number };

async function said(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as { detail?: unknown; error?: unknown };
    const d = body.detail ?? body.error;
    if (typeof d === "string" && d) return d;
    if (d && typeof d === "object") return JSON.stringify(d);
  } catch { /* no body */ }
  return `The API answered ${r.status}.`;
}

export async function postJson<T = Record<string, unknown>>(path: string, body: Record<string, unknown>): Promise<Posted<T>> {
  try {
    const r = await fetch(path, {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok) return { ok: false, error: await said(r), status: r.status };
    let data: T | undefined;
    try { data = (await r.json()) as T; } catch { data = undefined; }
    return { ok: true, data, status: r.status };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "The API did not answer." };
  }
}

const run = (runId: string) => `/v1/code-tasks/${encodeURIComponent(runId)}`;

/** Stop a build. Keeps the work so far; Retry resumes it. 409 when it already ended. */
export function stopBuild(runId: string, actor: string, reason?: string): Promise<Posted> {
  return postJson(`${run(runId)}/cancel`, { actor, ...(reason ? { reason } : {}) });
}

/** Guidance the agents read at the next station boundary. */
export function steerBuild(runId: string, actor: string, guidance: string): Promise<Posted> {
  return postJson(`${run(runId)}/steer`, { actor, guidance });
}

/** A new attempt in the same lineage. */
export function retryBuild(runId: string, actor: string): Promise<Posted<{ run_id?: string; attempt?: number }>> {
  return postJson(`${run(runId)}/retry`, { actor });
}

/** Resolve (addressed) or dismiss one review comment. */
export function resolveComment(runId: string, commentId: string, actor: string,
                               status: "addressed" | "dismissed", note = ""): Promise<Posted> {
  return postJson(`${run(runId)}/review-comments/${encodeURIComponent(commentId)}/resolve`, { actor, status, note });
}

export function stopPreview(previewId: string, actor: string): Promise<Posted> {
  return postJson(`/v1/previews/${encodeURIComponent(previewId)}/stop`, { actor });
}

/** The tool calls under one act: name, ok, latency — never their content. At most 50. */
export async function fetchTools(projectId: string, actId: string): Promise<ToolCalls | null> {
  try {
    const r = await fetch(`/v1/acts/${encodeURIComponent(actId)}/tools?project_id=${encodeURIComponent(projectId)}&limit=50`,
      { cache: "no-store" });
    if (!r.ok) return null;
    return parseToolCalls(await r.json());
  } catch {
    return null;
  }
}

export type LogLine = { id: string; at: string; title: string; agent: string; level: string; body: string };

/** This build's log, oldest first, from the activity log (the same rows Activity log shows). */
export async function fetchBuildLog(projectId: string, runId: string): Promise<LogLine[] | null> {
  try {
    const q = new URLSearchParams({ project_id: projectId, run_id: runId, order: "asc", limit: "200" });
    const r = await fetch(`/api/activity?${q.toString()}`, { cache: "no-store" });
    if (!r.ok) return null;
    const body = (await r.json()) as { items?: Record<string, unknown>[] };
    return (body.items ?? []).map((x, i) => ({
      id: String(x.id ?? i), at: String(x.created_at ?? ""), title: String(x.title ?? ""),
      agent: String(x.agent ?? ""), level: String(x.level ?? "info"), body: String(x.body ?? ""),
    }));
  } catch {
    return null;
  }
}
