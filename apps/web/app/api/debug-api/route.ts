import { API_URL, authHeaders } from "../../../lib/client";

// Temporary diagnostic: shows exactly how the web container's server-side call to boss-api fares
// (reachability vs auth). Remove after debugging the web→api link.
export const dynamic = "force-dynamic";

export async function GET() {
  const url = `${API_URL}/v1/status?project_id=boss-dogfood`;
  const started = Date.now();
  try {
    const r = await fetch(url, { cache: "no-store", headers: authHeaders() });
    const body = await r.text();
    return Response.json({
      api_url: API_URL,
      target: url,
      ok: r.ok,
      status: r.status,
      ms: Date.now() - started,
      body_preview: body.slice(0, 240),
    });
  } catch (e: unknown) {
    const err = e as { message?: string; code?: string; cause?: { code?: string } };
    return Response.json({
      api_url: API_URL,
      target: url,
      error: err?.message ?? String(e),
      code: err?.code ?? err?.cause?.code ?? null,
      ms: Date.now() - started,
    });
  }
}
