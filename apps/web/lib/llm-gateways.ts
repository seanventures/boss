/**
 * LLM GATEWAYS FOR THE CODING CLIS (BOSSAPP-887; API routers/llm_gateways.py): an operator-named
 * OpenAI-compatible or Anthropic gateway the harnesses are routed through, via BOSS's model proxy.
 * A key goes in once and never comes back: the API answers only `secret_set`. Parsing is pure;
 * writes go through obsWrite.
 */
import { aaRead, type ReadResult } from "./ambient";
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

export type GatewayTest = { ok: boolean; status: number | null; detail: string; at: string | null };
export type Gateway = {
  id: string; scope: "project" | "org"; name: string; protocol: "openai" | "anthropic"; base_url: string;
  routes: string[]; status: "active" | "disabled"; secret_set: boolean; last_test: GatewayTest | null;
};

const obj = (v: unknown): Record<string, unknown> =>
  (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown) => (typeof v === "string" ? v : "");

export function parseGateway(w: unknown): Gateway {
  const o = obj(w);
  const t = o.last_test ? obj(o.last_test) : null;
  return {
    id: str(o.id), scope: o.scope === "org" ? "org" : "project", name: str(o.name),
    protocol: o.protocol === "anthropic" ? "anthropic" : "openai", base_url: str(o.base_url),
    routes: arr(o.routes).map(str).filter(Boolean), status: o.status === "disabled" ? "disabled" : "active",
    secret_set: o.secret_set === true,
    last_test: t ? { ok: t.ok === true, status: typeof t.status === "number" ? t.status : null,
                     detail: str(t.detail), at: str(t.at) || null } : null,
  };
}

export const parseGateways = (w: unknown): Gateway[] => arr(obj(w).gateways).map(parseGateway);

/** "claude_code" → "Claude Code"; the harnesses a gateway routes, in words. */
export const HARNESS_WORDS: Record<string, string> = { claude_code: "Claude Code", codex: "Codex" };

/**
 * Why the Add gateway form cannot be saved yet, in words, or null. Mirrors the API's rule
 * (services/llm_gateway.normalise_url) so a bad URL is said before the press, never a Save that
 * does nothing (BOSSAPP-1042 d).
 */
export function gatewayFormError(f: { name: string; url: string; secret: string }): string | null {
  if (!f.name.trim()) return "Name the gateway.";
  const url = f.url.trim();
  if (!url) return "Enter the gateway's URL, e.g. https://llm-gateway.example.com";
  let parts: URL;
  try { parts = new URL(url); } catch { return "That is not a URL. Use one like https://llm-gateway.example.com"; }
  if (parts.protocol !== "https:" && parts.protocol !== "http:") return "The URL must start with https:// or http://";
  if (!parts.hostname) return "The URL needs a host, e.g. https://llm-gateway.example.com";
  if (parts.username || parts.password) return "Put the key in the Key field, never in the URL.";
  if (parts.search || parts.hash) return "The URL takes no ?query or #fragment.";
  if (!f.secret) return "Enter the gateway's key.";
  return null;
}

const q = (projectId: string) => `project_id=${encodeURIComponent(projectId)}`;
const one = (id: string) => `/v1/llm-gateways/${encodeURIComponent(id)}`;
const press = (scope: string, label: string) => ({ scope: `llmgw:${scope}`, label });

export const readGateways = (projectId: string): Promise<ReadResult<Gateway[]>> =>
  aaRead(`/v1/llm-gateways?${q(projectId)}`, parseGateways);
export const createGateway = (projectId: string, body: Record<string, unknown>): Promise<WriteResult> =>
  obsWrite("/v1/llm-gateways", { project_id: projectId, ...body }, press("new", "Add gateway"));
export const updateGateway = (projectId: string, id: string, fields: Record<string, unknown>, label = "Save"): Promise<WriteResult> =>
  obsWrite(one(id), { project_id: projectId, ...fields }, press(id, label), "PATCH");
export const deleteGateway = (projectId: string, id: string): Promise<WriteResult> =>
  obsWrite(`${one(id)}?${q(projectId)}`, {}, press(id, "Remove"), "DELETE");
export const testGateway = (projectId: string, id: string): Promise<WriteResult> =>
  obsWrite(`${one(id)}/test`, { project_id: projectId }, press(`${id}:test`, "Test"));
