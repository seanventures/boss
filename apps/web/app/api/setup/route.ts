import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { apiErrorText } from "../../../lib/api-error";

/**
 * Repo and models' reads and writes, off the Server Action path (a Server Action waits on a
 * full re-render). Every op is named here — the browser cannot reach an arbitrary API path with
 * the console's credentials — and a pasted token or key travels in the body, never a URL.
 *
 * GET  ?project_id=                        -> GET /v1/setup (the whole view)
 * GET  ?project_id=&repos=<connection_id>  -> what the stored token can see (+ Add repo)
 * GET  ?project_id=&catalogue=<provider>   -> the provider's model list (stored key)
 * POST {op, ...}                           -> one of OPS below
 */
export const dynamic = "force-dynamic";

type Op = { method: "POST" | "PUT" | "DELETE"; path: (b: Record<string, unknown>) => string;
            query?: string[] };

const enc = (v: unknown) => encodeURIComponent(String(v ?? ""));

const OPS: Record<string, Op> = {
  checks: { method: "POST", path: () => "/v1/setup/checks" },
  repo_token: { method: "POST", path: () => "/v1/setup/repository/token" },
  repo_select: { method: "POST", path: () => "/v1/setup/repository/select" },
  repo_app: { method: "POST", path: () => "/v1/setup/repository/app" },
  repo_default: { method: "POST", path: (b) => `/v1/setup/repository/${enc(b.binding_id)}/default`,
                  query: ["project_id"] },
  repo_remove: { method: "DELETE", path: (b) => `/v1/setup/repository/${enc(b.binding_id)}`,
                 query: ["project_id"] },
  models: { method: "POST", path: () => "/v1/setup/models" },
  model_catalogue: { method: "POST", path: (b) => `/v1/setup/models/${enc(b.provider)}/catalogue` },
  source_model: { method: "POST", path: (b) => `/v1/setup/models/${enc(b.source_id)}/model` },
  model_remove: { method: "DELETE", path: (b) => `/v1/setup/models/${enc(b.source_id)}`,
                  query: ["project_id", "confirm"] },
  job: { method: "PUT", path: (b) => `/v1/setup/jobs/${enc(b.job)}` },
  budget: { method: "PUT", path: () => "/v1/setup/budget" },
  try: { method: "POST", path: () => "/v1/setup/try" },
  decision_key: { method: "POST", path: () => "/v1/setup/decisions/key" },
  routing: { method: "PUT", path: () => "/v1/setup/routing" },
  routing_catalogue: { method: "POST", path: () => "/v1/setup/routing/catalogue" },
};

async function forward(r: Response) {
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    // A refusal is a pill's worth — {state, headline, fix} — kept whole for the console.
    const detail = (data as { detail?: unknown })?.detail;
    return NextResponse.json(
      typeof detail === "object" && detail !== null
        ? { error: apiErrorText(data, r.status), refusal: detail }
        : { error: apiErrorText(data, r.status) },
      { status: r.status });
  }
  return NextResponse.json(data);
}

export async function GET(req: Request) {
  const url = new URL(req.url);
  const projectId = (url.searchParams.get("project_id") ?? "").trim();
  const repos = url.searchParams.get("repos");
  const catalogue = url.searchParams.get("catalogue");
  const path = repos
    ? `/v1/setup/repository/repos?project_id=${enc(projectId)}&connection_id=${enc(repos)}`
    : catalogue
    ? `/v1/setup/models/${enc(catalogue)}/catalogue?project_id=${enc(projectId)}`
      + `&source_id=${enc(url.searchParams.get("source_id"))}`
    : `/v1/setup?project_id=${enc(projectId)}`;
  try {
    const r = await fetch(`${API_URL}${path}`, {
      headers: authHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
      // A read is answered from what the API recorded; it never needs long. A model list
      // (catalogue=) asks the provider, so it gets more room.
      signal: AbortSignal.timeout(catalogue || repos ? 30_000 : 10_000),
    });
    return await forward(r);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const op = OPS[String(body.op ?? "")];
  if (!op) {
    return NextResponse.json({ error: `op must be one of: ${Object.keys(OPS).join(", ")}` },
                             { status: 400 });
  }
  const { op: _op, ...payload } = body;
  // The press's key, for the routes that de-duplicate a repeated press themselves.
  const pressKey = req.headers.get("Idempotency-Key");
  if (pressKey && payload.idempotency_key === undefined) payload.idempotency_key = pressKey;
  const query = (op.query ?? [])
    .filter((k) => payload[k] !== undefined && payload[k] !== null)
    .map((k) => `${k}=${enc(payload[k])}`).join("&");
  const path = op.path(payload) + (query ? `?${query}` : "");
  try {
    const r = await apiWrite(`${API_URL}${path}`, {
      method: op.method,
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: op.method === "DELETE" ? undefined : JSON.stringify(payload),
      cache: "no-store",
    });
    return await forward(r);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
