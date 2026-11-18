import { NextResponse } from "next/server";

import { describeApiError } from "../../../../lib/api-error";
import { apiWrite } from "../../../../lib/api-write";
import { API_URL, authHeaders } from "../../../../lib/client";

/**
 * Easy Mode's specs, files and images (BOSSAPP-1273): /v1/factory/attachments. The multipart
 * body is passed through untouched with its own Content-Type (the boundary lives there).
 */
export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const type = req.headers.get("content-type") ?? "";
  if (!type.toLowerCase().startsWith("multipart/form-data")) {
    return NextResponse.json({ error: "attachments are sent as a form upload" }, { status: 415 });
  }
  try {
    const body = await req.arrayBuffer();
    const r = await apiWrite(`${API_URL}/v1/factory/attachments`, {
      method: "POST", headers: authHeaders({ "Content-Type": type }), body, cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    // The API answers {error: code, message: sentence}: keep the sentence for the person.
    if (!r.ok) {
      const sentence = (data as { message?: unknown }).message;
      return NextResponse.json({ ...(data as object),
        error: typeof sentence === "string" && sentence ? sentence : describeApiError(data, r.status) },
        { status: r.status });
    }
    return NextResponse.json(data, { status: r.status });
  } catch (e) {
    return NextResponse.json({ error: `Upload did not reach BOSS: ${(e as Error).message}` }, { status: 502 });
  }
}
